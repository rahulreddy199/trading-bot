"""
Growth Bot V1 — Position manager.

Phases:
  1. INITIAL: Hold original stop. Wait for 1.5R.
  2. PROTECTED: At 1.5R, move stop to near entry (entry - 0.1*ATR).
  3. TRAILING: At 2.5R or 5 bars in profit, trailing stop (3*ATR).

Time stop: Exit after 10 bars if no meaningful progress (< 0.5R).
"""
import json
import time
import hashlib
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    CONFIG_DIR,
    ACTIVE_ORDER_STATUSES,
    alpaca_get,
    alpaca_post,
    cancel_order_and_verify,
    enforce_live_guardrails,
    get_positions,
    JobLock,
    log_event,
    now_iso,
    resolve_state,
    save_json,
    send_alert,
    state_path,
    today_str,
    write_heartbeat,
)


def load_growth_strategy():
    return json.loads((CONFIG_DIR / "strategy_growth.json").read_text())


def get_open_orders_fresh():
    return alpaca_get("/v2/orders", params={"status": "open", "limit": 100})


def get_stop_orders_for_symbol(symbol, open_orders):
    return [o for o in open_orders
            if o.get("symbol") == symbol and o.get("side") == "sell"
            and o.get("type") == "stop" and o.get("status") in ACTIVE_ORDER_STATUSES]


def has_trailing_stop(symbol, open_orders):
    for o in open_orders:
        if (o.get("symbol") == symbol and o.get("side") == "sell"
                and o.get("type") == "trailing_stop" and o.get("status") in ACTIVE_ORDER_STATUSES):
            return True, o.get("id")
    return False, None


def gen_client_id(symbol, phase):
    date_str = datetime.now(MARKET_TZ).strftime("%Y%m%d")
    key = f"{date_str}_growth_{phase}_{symbol}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"growth_{phase}_{symbol}_{date_str}_{h}"


def submit_stop_order(symbol, qty, stop_price, phase="stop", dry_run=False):
    if dry_run:
        print(f"  [DRY RUN] Would submit stop: {symbol} qty={qty} stop=${stop_price:.2f} phase={phase}")
        return {"id": "dry_run", "client_order_id": gen_client_id(symbol, phase)}
    client_id = gen_client_id(symbol, phase)
    payload = {
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "stop", "stop_price": round(stop_price, 2),
        "time_in_force": "gtc", "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def submit_trailing_stop(symbol, qty, trail_price, dry_run=False):
    if dry_run:
        print(f"  [DRY RUN] Would submit trailing: {symbol} qty={qty} trail=${trail_price:.2f}")
        return {"id": "dry_run", "client_order_id": gen_client_id(symbol, "trail")}
    client_id = gen_client_id(symbol, "trail")
    payload = {
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "trailing_stop", "trail_price": str(round(trail_price, 2)),
        "time_in_force": "gtc", "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def submit_market_sell(symbol, qty, dry_run=False):
    if dry_run:
        print(f"  [DRY RUN] Would submit market sell: {symbol} qty={qty}")
        return {"id": "dry_run", "client_order_id": gen_client_id(symbol, "exit")}
    client_id = gen_client_id(symbol, "exit")
    payload = {
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "market", "time_in_force": "day", "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def load_tracking():
    path = resolve_state("growth", "position_tracking.json")
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_tracking(tracking):
    save_json(state_path("growth", "position_tracking.json"), tracking)
    # Legacy compat
    save_json(STATE_DIR / "position_tracking_growth.json", tracking)


def cancel_all_stops_verified(symbol, fresh_orders):
    """Cancel all stop/trailing orders for symbol with verification. Returns True if all confirmed cancelled."""
    stops = get_stop_orders_for_symbol(symbol, fresh_orders)
    trailing = [o for o in fresh_orders
                if o.get("symbol") == symbol and o.get("side") == "sell"
                and o.get("type") == "trailing_stop" and o.get("status") in ACTIVE_ORDER_STATUSES]
    all_cancelled = True
    for order in stops + trailing:
        if not cancel_order_and_verify(order["id"]):
            all_cancelled = False
    return all_cancelled


def validate_stop_before_submit(symbol, stop_price, current_price, qty):
    """Sanity-check stop parameters before placing a recovery/protective stop.
    Returns (ok, reason) tuple."""
    if stop_price is None or stop_price <= 0:
        return False, "stop_price_invalid"
    if stop_price >= current_price:
        return False, "stop_above_current_price"
    if qty is None or qty <= 0:
        return False, "qty_invalid"
    return True, "ok"


def try_reconstruct_metadata(symbol, track):
    """Try to reconstruct missing r_per_share/atr from multiple sources.
    Priority: tracking → last_orders.json → order_plan.json → candidates.json → ATR fallback.
    """
    reconstructed = False
    source = None

    # 1. last_orders_growth.json (closest to actual executed trade)
    last_orders_path = STATE_DIR / "last_orders_growth.json"
    if last_orders_path.exists():
        try:
            orders = json.loads(last_orders_path.read_text())
            for o in orders:
                if o.get("symbol") == symbol:
                    if track.get("atr14_at_entry") is None and "atr14" in o:
                        track["atr14_at_entry"] = float(o["atr14"])
                    if track.get("r_per_share") is None and o.get("r_per_share"):
                        track["r_per_share"] = float(o["r_per_share"])
                        source = "last_orders"
                    break
        except Exception:
            pass

    # 2. order_plan_growth.json
    if track.get("r_per_share") is None:
        order_plan_path = STATE_DIR / "order_plan_growth.json"
        if order_plan_path.exists():
            try:
                plan = json.loads(order_plan_path.read_text())
                for o in plan.get("orders", []):
                    if o.get("symbol") == symbol:
                        if track.get("r_per_share") is None and o.get("r_per_share"):
                            track["r_per_share"] = float(o["r_per_share"])
                            source = "order_plan"
                        break
            except Exception:
                pass

    # 3. candidates_growth.json
    if track.get("r_per_share") is None or track.get("atr14_at_entry") is None:
        candidates_path = STATE_DIR / "candidates_growth.json"
        if candidates_path.exists():
            try:
                data = json.loads(candidates_path.read_text())
                for c in data.get("candidates", []) + data.get("rejected", []):
                    if c.get("symbol") == symbol:
                        if track.get("atr14_at_entry") is None and "atr14" in c:
                            track["atr14_at_entry"] = float(c["atr14"])
                        if track.get("r_per_share") is None and "r_per_share" in c:
                            track["r_per_share"] = float(c["r_per_share"])
                            source = "candidates"
                        break
            except Exception:
                pass

    # 4. ATR fallback estimate (last resort)
    if track.get("r_per_share") is None and track.get("atr14_at_entry"):
        track["r_per_share"] = round(2.5 * track["atr14_at_entry"], 2)
        source = "atr_fallback_estimate"
        track["r_per_share_estimated"] = True
        track["MANUAL_REVIEW"] = True

    if source:
        track["r_per_share_source"] = source
        if source == "atr_fallback_estimate":
            print(f"  ⚠️ {symbol}: r_per_share estimated from ATR fallback (MANUAL_REVIEW)")
        else:
            print(f"  ℹ️ {symbol}: r_per_share reconstructed from {source}")
        reconstructed = True

    return reconstructed


def main(dry_run=False):
    enforce_live_guardrails()

    with JobLock("growth", "manage", timeout_minutes=15) as lock:
        if not lock.acquired:
            print("Manage skipped: another instance running")
            return
        log_event("growth", "manage", "job_start", reason_code="JOB_START")
        _run_manage_logic(dry_run, lock)


def _run_manage_logic(dry_run, lock):
    strategy = load_growth_strategy()
    positions = get_positions()
    exit_cfg = strategy["exit"]

    protected_r = exit_cfg["phase_protected_r"]        # 1.5R
    trailing_r = exit_cfg["phase_trailing_r"]           # 2.5R
    trailing_bars_threshold = exit_cfg["phase_trailing_bars_in_profit"]  # 5
    trailing_mult = exit_cfg["trailing_atr_multiplier"]  # 3.0
    trailing_tight_mult = exit_cfg.get("trailing_tight_atr_multiplier", 2.0)  # tighter trail at high R
    trailing_tight_threshold_r = exit_cfg.get("trailing_tight_threshold_r", 3.0)  # tighten at 3R+
    protected_buffer = exit_cfg["protected_stop_buffer_atr"]
    time_stop_bars = exit_cfg["time_stop_bars"]          # 10
    time_stop_enabled = exit_cfg["time_stop_enabled"]

    tracking = load_tracking()
    actions = []

    for pos in positions:
        symbol = pos["symbol"]
        qty = int(float(pos["qty"]))
        avg_entry = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])

        if symbol not in tracking:
            tracking[symbol] = {
                "planned_entry": avg_entry, "initial_stop": None,
                "current_stop": None, "r_per_share": None,
                "atr14_at_entry": None, "setup_type": "unknown",
                "phase": "initial", "bars_held": 0,
                "best_price": current_price, "best_gain_r": 0.0,
                "bars_in_profit": 0,
            }
            save_tracking(tracking)

        track = tracking[symbol]

        # Skip exit_pending — but reconcile if position is actually closed
        if track.get("phase") == "exit_pending":
            exit_oid = track.get("exit_order_id")
            if exit_oid:
                try:
                    exit_order = alpaca_get(f"/v2/orders/{exit_oid}")
                    if exit_order.get("status") == "filled":
                        # Position closed — will be cleaned up below
                        actions.append({"symbol": symbol, "action": "exit_confirmed_filled"})
                    elif exit_order.get("status") in ("canceled", "expired", "rejected"):
                        # Exit order disappeared but position still exists — recover
                        track["phase"] = "initial"
                        track.pop("exit_reason", None)
                        save_tracking(tracking)
                        actions.append({"symbol": symbol, "action": "exit_pending_recovered",
                                        "reason": f"exit_order_{exit_order.get('status')}"})
                        send_alert(f"⚠️ Growth {symbol}: exit order {exit_order.get('status')}, reverting to initial", level="warning")
                    else:
                        actions.append({"symbol": symbol, "action": "exit_pending", "exit_status": exit_order.get("status")})
                except Exception:
                    actions.append({"symbol": symbol, "action": "exit_pending", "note": "could_not_check_order"})
            else:
                actions.append({"symbol": symbol, "action": "exit_pending", "note": "no_exit_order_id"})
            continue

        # Pending → initial transition
        if track.get("phase") == "pending":
            track["phase"] = "initial"
            track["actual_entry"] = avg_entry
            track["qty"] = qty
            track["bars_held"] = 0
            track["bars_in_profit"] = 0
            if track.get("initial_stop"):
                track["r_per_share"] = round(avg_entry - track["initial_stop"], 2)
            # Slippage tracking
            planned_trigger = track.get("trigger_price", track.get("planned_entry"))
            planned_limit = track.get("limit_price")
            if planned_trigger:
                slippage = avg_entry - planned_trigger
                slippage_bps = (slippage / planned_trigger) * 10000 if planned_trigger else 0
                track["slippage"] = {
                    "planned_trigger": planned_trigger,
                    "planned_limit": planned_limit,
                    "actual_fill": avg_entry,
                    "slippage_dollars": round(slippage, 4),
                    "slippage_bps": round(slippage_bps, 1),
                }
            save_tracking(tracking)

        # ── BROKER-VS-TRACKING RECONCILIATION (item 16) ──
        fresh_orders_recon = get_open_orders_fresh()
        has_trail_recon, trail_id_recon = has_trailing_stop(symbol, fresh_orders_recon)
        has_stop_recon = bool(get_stop_orders_for_symbol(symbol, fresh_orders_recon))

        if track["phase"] == "protected" and has_trail_recon:
            # Broker has trailing but tracking says protected — sync up
            track["phase"] = "trailing"
            track["exit_order_id"] = trail_id_recon
            track["exit_order_type"] = "trailing_stop"
            save_tracking(tracking)
            actions.append({"symbol": symbol, "action": "reconciled_to_trailing"})
        elif track["phase"] == "trailing" and not has_trail_recon and has_stop_recon:
            # Broker only has regular stop but tracking says trailing — sync down
            track["phase"] = "protected"
            stop_orders = get_stop_orders_for_symbol(symbol, fresh_orders_recon)
            if stop_orders:
                track["exit_order_id"] = stop_orders[0].get("id")
                track["exit_order_type"] = "stop_protected"
            save_tracking(tracking)
            actions.append({"symbol": symbol, "action": "reconciled_to_protected"})
        elif track["phase"] in ("initial", "protected") and not has_stop_recon and not has_trail_recon:
            # NO protective order at broker — re-place the stop immediately
            stop_price = track.get("current_stop") or track.get("initial_stop")
            if stop_price:
                try:
                    resp = submit_stop_order(symbol, qty, stop_price, "recovery_missing")
                    track["exit_order_id"] = resp.get("id")
                    track["exit_order_type"] = f"stop_{track['phase']}"
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "stop_recovered",
                                    "phase": track["phase"], "stop": stop_price})
                    send_alert(f"🔧 Growth {symbol}: stop RECOVERED at ${stop_price:.2f} (was missing!)", level="warning")
                except Exception as e:
                    actions.append({"symbol": symbol, "action": "stop_recovery_failed",
                                    "error": str(e), "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: stop recovery FAILED — UNPROTECTED!", level="error")

        # Bars held (once per day, idempotent)
        today = today_str()
        if track.get("last_bar_date") != today:
            track["bars_held"] = track.get("bars_held", 0) + 1
            # Track actual bars where close > entry (not just "currently green")
            if current_price > avg_entry:
                track["bars_in_profit"] = track.get("bars_in_profit", 0) + 1
            track["last_bar_date"] = today

        # Update best price
        track["best_price"] = max(track.get("best_price", 0), current_price)

        r_per_share = track.get("r_per_share")
        atr = track.get("atr14_at_entry")

        # Try to reconstruct missing data before skipping
        if r_per_share is None or atr is None:
            reconstructed = try_reconstruct_metadata(symbol, track)
            r_per_share = track.get("r_per_share")
            atr = track.get("atr14_at_entry")
            if reconstructed:
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "metadata_reconstructed",
                                "r_per_share": r_per_share, "atr": atr})

        if r_per_share is None or atr is None:
            actions.append({"symbol": symbol, "action": "skip", "reason": "missing_r_or_atr",
                            "MANUAL_REVIEW": True})
            send_alert(f"⚠️ Growth {symbol}: missing R/ATR data, position unmanaged", level="warning")
            continue

        current_r = (current_price - avg_entry) / r_per_share if r_per_share > 0 else 0
        best_r = (track["best_price"] - avg_entry) / r_per_share if r_per_share > 0 else 0
        track["best_gain_r"] = round(best_r, 2)

        bars_in_profit = track.get("bars_in_profit", 0)

        # ── TIME STOP ──
        if (time_stop_enabled and track["phase"] == "initial"
                and track["bars_held"] >= time_stop_bars and current_r < 0.5):
            try:
                fresh_orders = get_open_orders_fresh()
                all_cancelled = cancel_all_stops_verified(symbol, fresh_orders)
                if not all_cancelled:
                    actions.append({"symbol": symbol, "action": "time_stop_cancel_failed",
                                    "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: time stop cancel failed, position may have overlapping exits", level="error")
                    continue
                time.sleep(0.3)
                resp = submit_market_sell(symbol, qty)
                track["phase"] = "exit_pending"
                track["exit_reason"] = "time_stop"
                track["exit_order_id"] = resp.get("id")
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "time_stop_exit",
                                "bars": track["bars_held"], "r": round(current_r, 2)})
                send_alert(f"⏰ GROWTH TIME STOP: {symbol} after {track['bars_held']} bars, R={current_r:.2f}", level="warning")
            except Exception as e:
                # Market sell failed after canceling stops — recreate protective stop
                try:
                    fallback_stop = track.get("current_stop") or track.get("initial_stop")
                    if fallback_stop:
                        submit_stop_order(symbol, qty, fallback_stop, "recovery")
                        actions.append({"symbol": symbol, "action": "time_stop_failed_stop_restored",
                                        "error": str(e), "restored_stop": fallback_stop})
                    else:
                        actions.append({"symbol": symbol, "action": "time_stop_failed_no_fallback",
                                        "error": str(e), "MANUAL_REVIEW": True})
                except Exception as e2:
                    actions.append({"symbol": symbol, "action": "time_stop_failed_recovery_failed",
                                    "error": str(e), "recovery_error": str(e2), "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: time stop AND recovery FAILED — NAKED POSITION", level="error")
            continue

        # ── INITIAL → PROTECTED (at 1.5R) ──
        if track["phase"] == "initial" and current_r >= protected_r:
            protected_stop = avg_entry - protected_buffer * atr
            fresh_orders = get_open_orders_fresh()

            # Cancel old stops with verification
            all_cancelled = cancel_all_stops_verified(symbol, fresh_orders)
            if not all_cancelled:
                actions.append({"symbol": symbol, "action": "protected_cancel_failed",
                                "MANUAL_REVIEW": True})
                send_alert(f"🚨 Growth {symbol}: old stop cancel failed at protected transition", level="error")
                continue

            time.sleep(0.5)

            try:
                resp = submit_stop_order(symbol, qty, protected_stop, "protected")
                track["phase"] = "protected"
                track["current_stop"] = round(protected_stop, 2)
                track["exit_order_id"] = resp.get("id")
                track["exit_order_type"] = "stop_protected"
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "moved_to_protected",
                                "stop": round(protected_stop, 2), "r": round(current_r, 2)})
                send_alert(f"🛡️ GROWTH PROTECTED: {symbol} at {current_r:.1f}R, stop=${protected_stop:.2f}", level="trade")
            except Exception as e:
                # CRITICAL: old stop cancelled but new one failed — recreate old stop
                try:
                    old_stop = track.get("current_stop") or track.get("initial_stop")
                    if old_stop:
                        resp = submit_stop_order(symbol, qty, old_stop, "recovery")
                        track["exit_order_id"] = resp.get("id")
                        save_tracking(tracking)
                        actions.append({"symbol": symbol, "action": "protected_failed_stop_restored",
                                        "error": str(e), "restored_stop": old_stop})
                    else:
                        actions.append({"symbol": symbol, "action": "protected_failed_no_fallback",
                                        "error": str(e), "MANUAL_REVIEW": True})
                        send_alert(f"🚨 Growth {symbol}: NAKED — protected stop failed, no fallback", level="error")
                except Exception as e2:
                    actions.append({"symbol": symbol, "action": "protected_failed_recovery_failed",
                                    "error": str(e), "recovery_error": str(e2), "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: NAKED POSITION — all stop attempts failed", level="error")
            continue

        # ── PROTECTED → TRAILING (at 2.5R or 5 bars in profit) ──
        should_trail = (current_r >= trailing_r) or (bars_in_profit >= trailing_bars_threshold and current_r > 0.5)
        if track["phase"] == "protected" and should_trail:
            fresh_orders = get_open_orders_fresh()

            already_trailing, trail_id = has_trailing_stop(symbol, fresh_orders)
            if already_trailing:
                track["phase"] = "trailing"
                track["exit_order_id"] = trail_id
                track["exit_order_type"] = "trailing_stop"
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "trailing_exists", "id": trail_id})
                continue

            # Cancel old stops with verification
            all_cancelled = cancel_all_stops_verified(symbol, fresh_orders)
            if not all_cancelled:
                actions.append({"symbol": symbol, "action": "trailing_cancel_failed",
                                "MANUAL_REVIEW": True})
                send_alert(f"🚨 Growth {symbol}: old stop cancel failed at trailing transition", level="error")
                continue

            time.sleep(0.5)

            trail_amount = trailing_mult * atr
            if current_r >= trailing_tight_threshold_r:
                trail_amount = trailing_tight_mult * atr
            try:
                resp = submit_trailing_stop(symbol, qty, trail_amount)
                track["phase"] = "trailing"
                track["exit_order_id"] = resp.get("id")
                track["exit_order_type"] = "trailing_stop"
                save_tracking(tracking)
                tight_label = " (TIGHT)" if current_r >= trailing_tight_threshold_r else ""
                actions.append({"symbol": symbol, "action": "trailing_activated",
                                "trail": round(trail_amount, 2), "r": round(current_r, 2), "tight": current_r >= trailing_tight_threshold_r})
                send_alert(f"🚀 GROWTH TRAILING{tight_label}: {symbol} at {current_r:.1f}R, trail=${trail_amount:.2f}", level="trade")
            except Exception as e:
                # CRITICAL: old stop cancelled but trailing failed — recreate protected stop
                try:
                    protected_stop = track.get("current_stop", avg_entry - protected_buffer * atr)
                    resp = submit_stop_order(symbol, qty, protected_stop, "recovery")
                    track["exit_order_id"] = resp.get("id")
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "trailing_failed_stop_restored",
                                    "error": str(e), "restored_stop": protected_stop})
                except Exception as e2:
                    actions.append({"symbol": symbol, "action": "trailing_failed_recovery_failed",
                                    "error": str(e), "recovery_error": str(e2), "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: NAKED POSITION — trailing and recovery failed", level="error")
            continue

        # ── RECOVERY: protected phase but no stop exists ──
        if track["phase"] == "protected":
            fresh_orders = get_open_orders_fresh()
            existing_stops = get_stop_orders_for_symbol(symbol, fresh_orders)
            if not existing_stops:
                protected_stop = track.get("current_stop", avg_entry - protected_buffer * atr)
                ok, reason = validate_stop_before_submit(symbol, protected_stop, current_price, qty)
                if not ok:
                    actions.append({"symbol": symbol, "action": "protected_recovery_blocked",
                                    "reason": reason, "stop": protected_stop, "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: recovery stop blocked ({reason})", level="error")
                    continue
                # Check no equivalent active stop already exists (re-check after validation)
                fresh_orders2 = get_open_orders_fresh()
                if get_stop_orders_for_symbol(symbol, fresh_orders2):
                    actions.append({"symbol": symbol, "action": "protected_stop_appeared"})
                    continue
                try:
                    resp = submit_stop_order(symbol, qty, protected_stop, "recovery")
                    track["exit_order_id"] = resp.get("id")
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "protected_stop_recovered",
                                    "stop": round(protected_stop, 2)})
                except Exception as e:
                    actions.append({"symbol": symbol, "action": "protected_recovery_failed",
                                    "error": str(e), "MANUAL_REVIEW": True})
                continue

        # ── RECOVERY: trailing phase but stop missing ──
        if track["phase"] == "trailing":
            fresh_orders = get_open_orders_fresh()
            has_trail, trail_id = has_trailing_stop(symbol, fresh_orders)
            if not has_trail:
                trail_amount = trailing_mult * atr
                if current_r >= trailing_tight_threshold_r:
                    trail_amount = trailing_tight_mult * atr
                try:
                    resp = submit_trailing_stop(symbol, qty, trail_amount)
                    track["exit_order_id"] = resp.get("id")
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "trailing_recovered"})
                    send_alert(f"🔧 Growth {symbol}: trailing stop recovered", level="warning")
                except Exception as e:
                    actions.append({"symbol": symbol, "action": "trailing_recovery_failed",
                                    "error": str(e), "MANUAL_REVIEW": True})
                    send_alert(f"🚨 Growth {symbol}: trailing recovery FAILED", level="error")
            else:
                # Coarse trail upgrade at 4R, 5R, 6R thresholds
                last_upgrade_r = track.get("last_trail_upgrade_r", trailing_tight_threshold_r)
                upgrade_thresholds = [4.0, 5.0, 6.0, 8.0]
                next_upgrade = None
                for t in upgrade_thresholds:
                    if current_r >= t and last_upgrade_r < t:
                        next_upgrade = t
                if next_upgrade:
                    # Tighten trail: 2.0 ATR at 3-4R, 1.75 at 5R, 1.5 at 6R+
                    if next_upgrade >= 6.0:
                        new_trail = 1.5 * atr
                    elif next_upgrade >= 5.0:
                        new_trail = 1.75 * atr
                    else:
                        new_trail = trailing_tight_mult * atr
                    # Cancel old and replace
                    if cancel_order_and_verify(trail_id):
                        time.sleep(0.3)
                        try:
                            resp = submit_trailing_stop(symbol, qty, new_trail)
                            track["exit_order_id"] = resp.get("id")
                            track["last_trail_upgrade_r"] = next_upgrade
                            save_tracking(tracking)
                            actions.append({"symbol": symbol, "action": "trail_upgraded",
                                            "r": round(current_r, 2), "threshold": next_upgrade,
                                            "new_trail": round(new_trail, 2)})
                            send_alert(f"🎯 GROWTH TRAIL UPGRADE: {symbol} at {current_r:.1f}R → trail=${new_trail:.2f}", level="trade")
                        except Exception as e:
                            # Recovery: put old trail back
                            try:
                                old_trail = trailing_tight_mult * atr
                                submit_trailing_stop(symbol, qty, old_trail)
                            except Exception:
                                pass
                            actions.append({"symbol": symbol, "action": "trail_upgrade_failed", "error": str(e)})
                    else:
                        actions.append({"symbol": symbol, "action": "trail_upgrade_cancel_failed"})
                else:
                    actions.append({
                        "symbol": symbol, "action": "trailing_active",
                        "price": current_price, "r": round(current_r, 2),
                        "gain_pct": round((current_price - avg_entry) / avg_entry * 100, 2),
                    })
        elif track["phase"] == "initial":
            actions.append({
                "symbol": symbol, "action": "hold_initial",
                "phase_before": "initial", "phase_after": "initial",
                "price": current_price, "r": round(current_r, 2),
                "bars": track["bars_held"], "bars_in_profit": bars_in_profit,
                "current_stop": track.get("current_stop"),
                "exit_order_id": track.get("exit_order_id"),
                "target_r": protected_r,
            })
        elif track["phase"] == "protected":
            actions.append({
                "symbol": symbol, "action": "hold_protected",
                "phase_before": "protected", "phase_after": "protected",
                "price": current_price, "r": round(current_r, 2),
                "bars_in_profit": bars_in_profit,
                "current_stop": track.get("current_stop"),
                "exit_order_id": track.get("exit_order_id"),
                "target_r": trailing_r,
            })

    save_tracking(tracking)

    # Cleanup closed positions
    open_symbols = {p["symbol"] for p in positions}
    fresh_orders = get_open_orders_fresh()
    pending_buy_symbols = {o["symbol"] for o in fresh_orders
                           if o.get("side") == "buy" and o.get("status") in ACTIVE_ORDER_STATUSES}
    closed = []
    for s in list(tracking.keys()):
        if s in open_symbols:
            continue
        if s in pending_buy_symbols and tracking[s].get("phase") == "pending":
            continue
        closed.append(s)
    for s in closed:
        del tracking[s]
    if closed:
        save_tracking(tracking)
        actions.append({"action": "cleaned", "symbols": closed})

    log = {"bot_name": "growth", "timestamp": now_iso(), "actions": actions}
    save_json(STATE_DIR / "manage_log_growth.json", log)
    save_json(state_path("growth", "manage_log.json"), log)

    # Richer heartbeat (item 20)
    manual_review_count = sum(1 for a in actions if a.get("MANUAL_REVIEW"))
    recoveries = sum(1 for a in actions if "recover" in a.get("action", ""))
    write_heartbeat("manage_growth", "ok", {
        "bot_name": "growth",
        "positions": len(positions),
        "actions": len(actions),
        "recoveries": recoveries,
        "manual_review_count": manual_review_count,
    })

    # Job receipt
    lock.write_receipt(
        status="completed",
        orders_submitted=0,
        errors=[a.get("error", "") for a in actions if "failed" in a.get("action", "")],
        warnings=[a.get("symbol", "") for a in actions if a.get("MANUAL_REVIEW")],
    )
    log_event("growth", "manage", "job_end", reason_code="JOB_END",
              extra={"actions": len(actions), "positions": len(positions)})

    print(f"Growth Manage: {len(actions)} actions on {len(positions)} positions")


if __name__ == "__main__":
    import sys as _sys
    DRY_RUN = "--dry-run" in _sys.argv
    main(dry_run=DRY_RUN)

