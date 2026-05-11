"""
Growth Bot V1 — Position manager (orchestrator).

Phases:
  1. INITIAL: Hold original stop. Wait for 1.5R.
  2. PROTECTED: At 1.5R, move stop to near entry (entry - 0.1*ATR).
  3. TRAILING: At 2.5R or 5 bars in profit, trailing stop (3*ATR).

Time stop: Exit after 10 bars if no meaningful progress (< 0.5R).
"""
import json
import time
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    CONFIG_DIR,
    ACTIVE_ORDER_STATUSES,
    alpaca_get,
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
from growth.decisions import decide_phase_action, compute_current_r, compute_best_r
from growth.broker_exec import (
    get_open_orders_fresh,
    get_stop_orders_for_symbol,
    has_trailing_stop,
    submit_stop_order,
    submit_trailing_stop,
    submit_market_sell,
    cancel_all_stops_verified,
    validate_stop_before_submit,
    execute_cancel_and_replace,
)
from growth.recovery import try_reconstruct_metadata


# ── State I/O ──

def load_growth_strategy():
    return json.loads((CONFIG_DIR / "strategy_growth.json").read_text())


def load_tracking():
    path = resolve_state("growth", "position_tracking.json")
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_tracking(tracking):
    save_json(state_path("growth", "position_tracking.json"), tracking)
    save_json(STATE_DIR / "position_tracking_growth.json", tracking)


# ── Orchestrator ──

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

    tracking = load_tracking()
    actions = []

    for pos in positions:
        symbol = pos["symbol"]
        qty = int(float(pos["qty"]))
        avg_entry = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])

        # Ensure tracking entry exists
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

        # ── EXIT PENDING: check if exit order resolved ──
        if track.get("phase") == "exit_pending":
            actions.append(_handle_exit_pending(symbol, track, tracking))
            continue

        # ── PENDING → INITIAL: fill detected ──
        if track.get("phase") == "pending":
            _handle_pending_to_initial(track, avg_entry, qty)
            save_tracking(tracking)

        # ── RECONCILIATION: sync tracking phase with broker orders ──
        recon_action = _reconcile_broker_state(symbol, track, qty, tracking, dry_run)
        if recon_action:
            actions.append(recon_action)

        # ── UPDATE BARS (idempotent per day) ──
        _update_bars(track, current_price, avg_entry)
        track["best_price"] = max(track.get("best_price", 0), current_price)

        # ── METADATA RECONSTRUCTION ──
        if track.get("r_per_share") is None or track.get("atr14_at_entry") is None:
            reconstructed = try_reconstruct_metadata(symbol, track)
            if reconstructed:
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "metadata_reconstructed",
                                "r_per_share": track.get("r_per_share"), "atr": track.get("atr14_at_entry")})

        if track.get("r_per_share") is None or track.get("atr14_at_entry") is None:
            actions.append({"symbol": symbol, "action": "skip", "reason": "missing_r_or_atr",
                            "MANUAL_REVIEW": True})
            send_alert(f"⚠️ Growth {symbol}: missing R/ATR data, position unmanaged", level="warning")
            continue

        # Update R tracking
        r_per_share = track["r_per_share"]
        current_r = compute_current_r(track, current_price, avg_entry)
        track["best_gain_r"] = round(compute_best_r(track, avg_entry), 2)

        # ── PURE DECISION ──
        decision = decide_phase_action(track, current_price, avg_entry, qty, exit_cfg)
        action_type = decision["action"]

        # ── EXECUTE DECISION ──
        if action_type == "time_stop":
            action = _execute_time_stop(symbol, track, qty, tracking, dry_run)
            actions.append(action)
            continue

        elif action_type == "move_to_protected":
            action = _execute_move_to_protected(
                symbol, track, qty, avg_entry, decision["stop_price"], tracking, dry_run)
            actions.append(action)
            continue

        elif action_type == "move_to_trailing":
            action = _execute_move_to_trailing(
                symbol, track, qty, avg_entry, decision["trail_amount"],
                decision.get("tight", False), exit_cfg, tracking, dry_run)
            actions.append(action)
            continue

        elif action_type == "trail_upgrade":
            action = _execute_trail_upgrade(
                symbol, track, qty, decision["new_trail"],
                decision["threshold"], exit_cfg, tracking, dry_run)
            actions.append(action)

        elif action_type == "hold":
            actions.append({
                "symbol": symbol,
                "action": f"hold_{decision['phase']}",
                "price": current_price,
                "r": decision["current_r"],
                "bars": decision.get("bars_held", 0),
                "bars_in_profit": decision.get("bars_in_profit", 0),
                "current_stop": track.get("current_stop"),
                "exit_order_id": track.get("exit_order_id"),
            })

        # ── RECOVERY: check if protective order is missing ──
        if track["phase"] in ("protected", "trailing") and action_type == "hold":
            recovery_action = _check_and_recover_missing_stop(
                symbol, track, qty, current_price, avg_entry, exit_cfg, tracking, dry_run)
            if recovery_action:
                actions.append(recovery_action)
                continue

    save_tracking(tracking)

    # ── CLEANUP: remove closed positions from tracking ──
    _cleanup_closed(tracking, positions, actions)

    # ── WRITE LOG + HEARTBEAT + RECEIPT ──
    log = {"bot_name": "growth", "timestamp": now_iso(), "actions": actions}
    save_json(STATE_DIR / "manage_log_growth.json", log)
    save_json(state_path("growth", "manage_log.json"), log)

    manual_review_count = sum(1 for a in actions if a.get("MANUAL_REVIEW"))
    recoveries = sum(1 for a in actions if "recover" in a.get("action", ""))
    write_heartbeat("manage_growth", "ok", {
        "bot_name": "growth",
        "positions": len(positions),
        "actions": len(actions),
        "recoveries": recoveries,
        "manual_review_count": manual_review_count,
    })

    lock.write_receipt(
        status="completed",
        orders_submitted=0,
        errors=[a.get("error", "") for a in actions if "failed" in a.get("action", "")],
        warnings=[a.get("symbol", "") for a in actions if a.get("MANUAL_REVIEW")],
    )
    log_event("growth", "manage", "job_end", reason_code="JOB_END",
              extra={"actions": len(actions), "positions": len(positions)})

    print(f"Growth Manage: {len(actions)} actions on {len(positions)} positions")


# ── Phase Handlers (thin wrappers that delegate to broker_exec) ──

def _handle_exit_pending(symbol, track, tracking):
    """Check if an exit_pending order has resolved."""
    exit_oid = track.get("exit_order_id")
    if not exit_oid:
        return {"symbol": symbol, "action": "exit_pending", "note": "no_exit_order_id"}
    try:
        exit_order = alpaca_get(f"/v2/orders/{exit_oid}")
        if exit_order.get("status") == "filled":
            return {"symbol": symbol, "action": "exit_confirmed_filled"}
        elif exit_order.get("status") in ("canceled", "expired", "rejected"):
            track["phase"] = "initial"
            track.pop("exit_reason", None)
            save_tracking(tracking)
            send_alert(f"⚠️ Growth {symbol}: exit order {exit_order.get('status')}, reverting to initial", level="warning")
            return {"symbol": symbol, "action": "exit_pending_recovered",
                    "reason": f"exit_order_{exit_order.get('status')}"}
        else:
            return {"symbol": symbol, "action": "exit_pending", "exit_status": exit_order.get("status")}
    except Exception:
        return {"symbol": symbol, "action": "exit_pending", "note": "could_not_check_order"}


def _handle_pending_to_initial(track, avg_entry, qty):
    """Transition from pending to initial on fill."""
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


def _reconcile_broker_state(symbol, track, qty, tracking, dry_run):
    """Sync local phase with broker order state."""
    fresh_orders = get_open_orders_fresh()
    has_trail, trail_id = has_trailing_stop(symbol, fresh_orders)
    has_stop = bool(get_stop_orders_for_symbol(symbol, fresh_orders))

    # ── SYNC: update local stop price from broker's actual trailing/stop order ──
    for order in fresh_orders:
        if order.get("symbol") != symbol or order.get("side") != "sell":
            continue
        broker_stop = order.get("stop_price")
        broker_hwm = order.get("hwm")
        if broker_stop:
            track["current_stop"] = round(float(broker_stop), 2)
        if broker_hwm:
            track["highest_close"] = max(
                track.get("highest_close", 0), round(float(broker_hwm), 2)
            )
        break  # use the first matching sell order

    if track["phase"] == "protected" and has_trail:
        track["phase"] = "trailing"
        track["exit_order_id"] = trail_id
        track["exit_order_type"] = "trailing_stop"
        save_tracking(tracking)
        return {"symbol": symbol, "action": "reconciled_to_trailing"}

    elif track["phase"] == "trailing" and not has_trail and has_stop:
        track["phase"] = "protected"
        stop_orders = get_stop_orders_for_symbol(symbol, fresh_orders)
        if stop_orders:
            track["exit_order_id"] = stop_orders[0].get("id")
            track["exit_order_type"] = "stop_protected"
        save_tracking(tracking)
        return {"symbol": symbol, "action": "reconciled_to_protected"}

    elif track["phase"] in ("initial", "protected") and not has_stop and not has_trail:
        stop_price = track.get("current_stop") or track.get("initial_stop")
        if stop_price:
            try:
                resp = submit_stop_order(symbol, qty, stop_price, "recovery_missing", dry_run=dry_run)
                track["exit_order_id"] = resp.get("id")
                track["exit_order_type"] = f"stop_{track['phase']}"
                save_tracking(tracking)
                send_alert(f"🔧 Growth {symbol}: stop RECOVERED at ${stop_price:.2f} (was missing!)", level="warning")
                return {"symbol": symbol, "action": "stop_recovered", "phase": track["phase"], "stop": stop_price}
            except Exception as e:
                send_alert(f"🚨 Growth {symbol}: stop recovery FAILED — UNPROTECTED!", level="error")
                return {"symbol": symbol, "action": "stop_recovery_failed", "error": str(e), "MANUAL_REVIEW": True}

    return None


def _update_bars(track, current_price, avg_entry):
    """Update bars_held and bars_in_profit (once per day, idempotent)."""
    today = today_str()
    if track.get("last_bar_date") != today:
        track["bars_held"] = track.get("bars_held", 0) + 1
        if current_price > avg_entry:
            track["bars_in_profit"] = track.get("bars_in_profit", 0) + 1
        track["last_bar_date"] = today


def _execute_time_stop(symbol, track, qty, tracking, dry_run):
    """Execute time stop: cancel stops → market sell → fallback recovery."""
    try:
        fresh_orders = get_open_orders_fresh()
        all_cancelled = cancel_all_stops_verified(symbol, fresh_orders)
        if not all_cancelled:
            send_alert(f"🚨 Growth {symbol}: time stop cancel failed", level="error")
            return {"symbol": symbol, "action": "time_stop_cancel_failed", "MANUAL_REVIEW": True}
        time.sleep(0.3)
        resp = submit_market_sell(symbol, qty, dry_run=dry_run)
        track["phase"] = "exit_pending"
        track["exit_reason"] = "time_stop"
        track["exit_order_id"] = resp.get("id")
        save_tracking(tracking)
        send_alert(f"⏰ GROWTH TIME STOP: {symbol} after {track['bars_held']} bars", level="warning")
        return {"symbol": symbol, "action": "time_stop_exit", "bars": track["bars_held"]}
    except Exception as e:
        # Recovery: restore stop
        fallback_stop = track.get("current_stop") or track.get("initial_stop")
        if fallback_stop:
            try:
                submit_stop_order(symbol, qty, fallback_stop, "recovery", dry_run=dry_run)
                return {"symbol": symbol, "action": "time_stop_failed_stop_restored",
                        "error": str(e), "restored_stop": fallback_stop}
            except Exception as e2:
                send_alert(f"🚨 Growth {symbol}: time stop AND recovery FAILED — NAKED", level="error")
                return {"symbol": symbol, "action": "time_stop_failed_recovery_failed",
                        "error": str(e), "recovery_error": str(e2), "MANUAL_REVIEW": True}
        return {"symbol": symbol, "action": "time_stop_failed_no_fallback",
                "error": str(e), "MANUAL_REVIEW": True}


def _execute_move_to_protected(symbol, track, qty, avg_entry, stop_price, tracking, dry_run):
    """Execute initial→protected transition via cancel-and-replace."""
    fresh_orders = get_open_orders_fresh()
    fallback = track.get("current_stop") or track.get("initial_stop")

    success, result = execute_cancel_and_replace(
        symbol, qty, fresh_orders,
        lambda: submit_stop_order(symbol, qty, stop_price, "protected", dry_run=dry_run),
        fallback_stop=fallback, dry_run=dry_run,
    )

    if success:
        track["phase"] = "protected"
        track["current_stop"] = round(stop_price, 2)
        track["exit_order_id"] = result.get("order_id")
        track["exit_order_type"] = "stop_protected"
        save_tracking(tracking)
        r = compute_current_r(track, track.get("best_price", avg_entry), avg_entry)
        send_alert(f"🛡️ GROWTH PROTECTED: {symbol} stop=${stop_price:.2f}", level="trade")
        return {"symbol": symbol, "action": "moved_to_protected", "stop": round(stop_price, 2)}
    else:
        if result.get("recovered"):
            track["exit_order_id"] = result.get("recovery_order_id")
            save_tracking(tracking)
            return {"symbol": symbol, "action": "protected_failed_stop_restored",
                    "error": result.get("error"), "restored_stop": result.get("restored_stop")}
        if result.get("error") == "cancel_failed":
            send_alert(f"🚨 Growth {symbol}: old stop cancel failed at protected transition", level="error")
            return {"symbol": symbol, "action": "protected_cancel_failed", "MANUAL_REVIEW": True}
        send_alert(f"🚨 Growth {symbol}: NAKED — protected stop failed", level="error")
        return {"symbol": symbol, "action": "protected_failed_no_fallback",
                "error": result.get("error"), "MANUAL_REVIEW": True}


def _execute_move_to_trailing(symbol, track, qty, avg_entry, trail_amount, tight, exit_cfg, tracking, dry_run):
    """Execute protected→trailing transition via cancel-and-replace."""
    fresh_orders = get_open_orders_fresh()

    # Already trailing at broker?
    already_trailing, trail_id = has_trailing_stop(symbol, fresh_orders)
    if already_trailing:
        track["phase"] = "trailing"
        track["exit_order_id"] = trail_id
        track["exit_order_type"] = "trailing_stop"
        save_tracking(tracking)
        return {"symbol": symbol, "action": "trailing_exists", "id": trail_id}

    protected_buffer = exit_cfg["protected_stop_buffer_atr"]
    atr = track.get("atr14_at_entry", 0)
    fallback = track.get("current_stop", avg_entry - protected_buffer * atr)

    success, result = execute_cancel_and_replace(
        symbol, qty, fresh_orders,
        lambda: submit_trailing_stop(symbol, qty, trail_amount, dry_run=dry_run),
        fallback_stop=fallback, dry_run=dry_run,
    )

    if success:
        track["phase"] = "trailing"
        track["exit_order_id"] = result.get("order_id")
        track["exit_order_type"] = "trailing_stop"
        save_tracking(tracking)
        tight_label = " (TIGHT)" if tight else ""
        send_alert(f"🚀 GROWTH TRAILING{tight_label}: {symbol} trail=${trail_amount:.2f}", level="trade")
        return {"symbol": symbol, "action": "trailing_activated",
                "trail": round(trail_amount, 2), "tight": tight}
    else:
        if result.get("recovered"):
            track["exit_order_id"] = result.get("recovery_order_id")
            save_tracking(tracking)
            return {"symbol": symbol, "action": "trailing_failed_stop_restored",
                    "error": result.get("error"), "restored_stop": result.get("restored_stop")}
        if result.get("error") == "cancel_failed":
            send_alert(f"🚨 Growth {symbol}: old stop cancel failed at trailing transition", level="error")
            return {"symbol": symbol, "action": "trailing_cancel_failed", "MANUAL_REVIEW": True}
        send_alert(f"🚨 Growth {symbol}: NAKED — trailing and recovery failed", level="error")
        return {"symbol": symbol, "action": "trailing_failed_recovery_failed",
                "error": result.get("error"), "MANUAL_REVIEW": True}


def _execute_trail_upgrade(symbol, track, qty, new_trail, threshold, exit_cfg, tracking, dry_run):
    """Upgrade trailing stop at R milestones."""
    fresh_orders = get_open_orders_fresh()
    has_trail, trail_id = has_trailing_stop(symbol, fresh_orders)

    if not has_trail:
        # No trailing exists — recover it
        try:
            atr = track.get("atr14_at_entry", 0)
            resp = submit_trailing_stop(symbol, qty, new_trail, dry_run=dry_run)
            track["exit_order_id"] = resp.get("id")
            save_tracking(tracking)
            send_alert(f"🔧 Growth {symbol}: trailing stop recovered", level="warning")
            return {"symbol": symbol, "action": "trailing_recovered"}
        except Exception as e:
            send_alert(f"🚨 Growth {symbol}: trailing recovery FAILED", level="error")
            return {"symbol": symbol, "action": "trailing_recovery_failed",
                    "error": str(e), "MANUAL_REVIEW": True}

    # Cancel old trail and place tighter one
    from common import cancel_order_and_verify
    if cancel_order_and_verify(trail_id):
        time.sleep(0.3)
        try:
            resp = submit_trailing_stop(symbol, qty, new_trail, dry_run=dry_run)
            track["exit_order_id"] = resp.get("id")
            track["last_trail_upgrade_r"] = threshold
            save_tracking(tracking)
            send_alert(f"🎯 GROWTH TRAIL UPGRADE: {symbol} → trail=${new_trail:.2f}", level="trade")
            return {"symbol": symbol, "action": "trail_upgraded",
                    "threshold": threshold, "new_trail": round(new_trail, 2)}
        except Exception as e:
            # Recovery: put old-width trail back
            try:
                trailing_tight_mult = exit_cfg.get("trailing_tight_atr_multiplier", 2.0)
                atr = track.get("atr14_at_entry", 0)
                submit_trailing_stop(symbol, qty, trailing_tight_mult * atr, dry_run=dry_run)
            except Exception:
                pass
            return {"symbol": symbol, "action": "trail_upgrade_failed", "error": str(e)}
    else:
        return {"symbol": symbol, "action": "trail_upgrade_cancel_failed"}


def _check_and_recover_missing_stop(symbol, track, qty, current_price, avg_entry, exit_cfg, tracking, dry_run):
    """Check if a protective order is missing and recover it."""
    fresh_orders = get_open_orders_fresh()

    if track["phase"] == "protected":
        existing = get_stop_orders_for_symbol(symbol, fresh_orders)
        if existing:
            return None
        protected_buffer = exit_cfg["protected_stop_buffer_atr"]
        atr = track.get("atr14_at_entry", 0)
        stop_price = track.get("current_stop", avg_entry - protected_buffer * atr)
        ok, reason = validate_stop_before_submit(symbol, stop_price, current_price, qty)
        if not ok:
            send_alert(f"🚨 Growth {symbol}: recovery stop blocked ({reason})", level="error")
            return {"symbol": symbol, "action": "protected_recovery_blocked",
                    "reason": reason, "MANUAL_REVIEW": True}
        # Double-check
        fresh2 = get_open_orders_fresh()
        if get_stop_orders_for_symbol(symbol, fresh2):
            return {"symbol": symbol, "action": "protected_stop_appeared"}
        try:
            resp = submit_stop_order(symbol, qty, stop_price, "recovery", dry_run=dry_run)
            track["exit_order_id"] = resp.get("id")
            save_tracking(tracking)
            return {"symbol": symbol, "action": "protected_stop_recovered", "stop": round(stop_price, 2)}
        except Exception as e:
            return {"symbol": symbol, "action": "protected_recovery_failed",
                    "error": str(e), "MANUAL_REVIEW": True}

    elif track["phase"] == "trailing":
        has_trail, _ = has_trailing_stop(symbol, fresh_orders)
        if has_trail:
            return None
        atr = track.get("atr14_at_entry", 0)
        trailing_mult = exit_cfg["trailing_atr_multiplier"]
        trailing_tight_mult = exit_cfg.get("trailing_tight_atr_multiplier", 2.0)
        trailing_tight_threshold_r = exit_cfg.get("trailing_tight_threshold_r", 3.0)
        current_r = compute_current_r(track, current_price, avg_entry)
        trail_amount = trailing_mult * atr
        if current_r >= trailing_tight_threshold_r:
            trail_amount = trailing_tight_mult * atr
        try:
            resp = submit_trailing_stop(symbol, qty, trail_amount, dry_run=dry_run)
            track["exit_order_id"] = resp.get("id")
            save_tracking(tracking)
            send_alert(f"🔧 Growth {symbol}: trailing stop recovered", level="warning")
            return {"symbol": symbol, "action": "trailing_recovered"}
        except Exception as e:
            send_alert(f"🚨 Growth {symbol}: trailing recovery FAILED", level="error")
            return {"symbol": symbol, "action": "trailing_recovery_failed",
                    "error": str(e), "MANUAL_REVIEW": True}

    return None


def _cleanup_closed(tracking, positions, actions):
    """Remove closed positions from tracking."""
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


if __name__ == "__main__":
    import sys as _sys
    DRY_RUN = "--dry-run" in _sys.argv
    main(dry_run=DRY_RUN)

