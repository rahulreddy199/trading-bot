"""
Position manager v2: three-phase exit management.

Phases per position:
  1. INITIAL: Keep original stop. Wait for 1R.
  2. BREAKEVEN: At 1R, move stop to entry + 0.1*ATR. Wait for 2R.
  3. TRAILING: At 2R, replace stop with 2.5*ATR trailing stop on full position.

Early invalidation: If close < 50 SMA within 3 bars of entry, exit at market.

No partial exits — full position trails after 2R for maximum trend capture.

Failure handling:
- State saved after each step. Recovery on next run if interrupted.
- Broker reconciliation before placing/replacing orders.
- All failures logged and alerted via Slack.
"""
import json
import hashlib
import time

from common import (
    ACTIVE_ORDER_STATUSES,
    STATE_DIR,
    alpaca_get,
    alpaca_post,
    cancel_order,
    cancel_order_and_verify,
    enforce_live_guardrails,
    get_positions,
    load_strategy,
    now_iso,
    save_json,
    send_alert,
    today_str,
    write_heartbeat,
)



def get_open_orders_fresh():
    """Fetch current open orders from broker."""
    return alpaca_get("/v2/orders", params={"status": "open", "limit": 100})


def get_order_by_id(order_id):
    return alpaca_get(f"/v2/orders/{order_id}")


def get_order_by_client_id(client_order_id):
    """Retrieve order by client_order_id — Alpaca supports this as a first-class lookup."""
    return alpaca_get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})



def has_existing_trailing_stop(symbol, open_orders):
    for order in open_orders:
        if (order.get("symbol") == symbol
                and order.get("side") == "sell"
                and order.get("type") == "trailing_stop"
                and order.get("status") in ACTIVE_ORDER_STATUSES):
            return True, order.get("id")
    return False, None


def get_stop_orders_for_symbol(symbol, open_orders):
    """Get existing stop sell orders for a symbol."""
    return [o for o in open_orders
            if o.get("symbol") == symbol
            and o.get("side") == "sell"
            and o.get("type") == "stop"
            and o.get("status") in ACTIVE_ORDER_STATUSES]


def _deterministic_client_id(prefix, symbol, phase, extra=""):
    """Generate a deterministic client_order_id for idempotent order submission.
    Same inputs on the same day produce the same ID, so retries and reruns
    reconcile against the broker instead of creating duplicates."""
    date_str = today_str().replace("-", "")
    key = f"{prefix}_{symbol}_{phase}_{date_str}_{extra}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"bot_{prefix}_{symbol}_{date_str}_{h}"


def submit_stop_order(symbol, qty, stop_price, phase="initial"):
    client_id = _deterministic_client_id("stop", symbol, phase)
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": round(stop_price, 2),
        "time_in_force": "gtc",
        "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def submit_trailing_stop(symbol, qty, trail_price):
    client_id = _deterministic_client_id("trail", symbol, "trailing")
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "trailing_stop",
        "trail_price": str(round(trail_price, 2)),
        "time_in_force": "gtc",
        "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def submit_market_sell(symbol, qty, reason="exit"):
    client_id = _deterministic_client_id("exit", symbol, reason)
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp.setdefault("client_order_id", client_id)
    return resp


def load_tracking():
    tracking_path = STATE_DIR / "position_tracking.json"
    if tracking_path.exists():
        return json.loads(tracking_path.read_text())
    return {}


def save_tracking(tracking):
    save_json(STATE_DIR / "position_tracking.json", tracking)


def _record_closed_trade(symbol, track_data, exit_price=None, exit_reason=None):
    """Record a closed trade in trade_history.json for performance tracking.
    Returns a string status: 'recorded', 'deferred', 'no_exit_price', or 'error'."""
    history_path = STATE_DIR / "trade_history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text())
    else:
        history = {"trades": []}

    entry_price = track_data.get("entry_price") or track_data.get("planned_entry")
    r_per_share = track_data.get("r_per_share")
    sell_order_id = track_data.get("exit_order_id")
    sell_client_order_id = track_data.get("exit_client_order_id")

    # Try to fetch exit price from broker if not provided
    filled_at = None
    _defer_recording = False
    if exit_price is None:
        try:
            # 1. Exact lookup by order ID (most reliable)
            if sell_order_id:
                order = get_order_by_id(sell_order_id)
                if order and order.get("symbol") == symbol:  # Symbol must match
                    if order.get("filled_avg_price") and order.get("status") == "filled":
                        exit_price = float(order["filled_avg_price"])
                        filled_at = order.get("filled_at")
                        if exit_reason is None:
                            exit_reason = order.get("type", "unknown")
                    elif order.get("status") in ACTIVE_ORDER_STATUSES:
                        # Order exists but not yet filled — defer, don't fall through to symbol scan
                        _defer_recording = True

            # 2. Exact lookup by client_order_id
            if exit_price is None and not _defer_recording and sell_client_order_id:
                order = get_order_by_client_id(sell_client_order_id)
                if order and order.get("symbol") == symbol:  # Symbol must match
                    if order.get("filled_avg_price") and order.get("status") == "filled":
                        exit_price = float(order["filled_avg_price"])
                        filled_at = order.get("filled_at")
                        if sell_order_id is None:
                            sell_order_id = order.get("id")
                        if exit_reason is None:
                            exit_reason = order.get("type", "unknown")
                    elif order.get("status") in ACTIVE_ORDER_STATUSES:
                        _defer_recording = True

            # 3. Strict fallback: require symbol + side + qty match to prevent wrong attribution
            if exit_price is None and not _defer_recording:
                from common import get_orders
                recent_orders = get_orders(status="filled", limit=50)
                target_qty = str(track_data.get("qty", ""))
                for order in recent_orders:
                    if (order.get("symbol") == symbol
                            and order.get("side") == "sell"
                            and target_qty and str(order.get("qty", "")) == target_qty
                            and order.get("filled_avg_price")):
                        exit_price = float(order["filled_avg_price"])
                        filled_at = order.get("filled_at")
                        sell_order_id = sell_order_id or order.get("id")
                        sell_client_order_id = sell_client_order_id or order.get("client_order_id")
                        if exit_reason is None:
                            exit_reason = order.get("type", "unknown")
                        break
        except Exception:
            pass

    # If exit order exists but isn't filled yet, skip recording — next run will catch it
    if _defer_recording and exit_price is None:
        return "deferred"

    # If no exit price found at all, record without P&L but flag it
    if exit_price is None:
        pass  # Still record the trade entry — P&L will be None

    # Calculate P&L and R-multiple
    pnl = None
    r_multiple = None
    if exit_price and entry_price:
        qty = track_data.get("qty", 1)
        pnl = round((exit_price - entry_price) * qty, 2)
        if r_per_share and r_per_share > 0:
            r_multiple = round((exit_price - entry_price) / r_per_share, 2)

    trade = {
        "symbol": symbol,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "r_per_share": r_per_share,
        "r_multiple": r_multiple,
        "pnl": pnl,
        "exit_reason": exit_reason or track_data.get("phase"),
        "phase_at_exit": track_data.get("phase"),
        "bars_held": track_data.get("bars_held", 0),
        "entry_date": track_data.get("entry_date"),
        "closed_at": filled_at or now_iso(),
        "sell_order_id": sell_order_id,
        "client_order_id": sell_client_order_id,
        "exit_order_type": track_data.get("exit_order_type"),
        "entry_order_id": track_data.get("order_id"),
        "entry_client_order_id": track_data.get("client_order_id"),
        "exit_price_source": "exact_id" if (exit_price and filled_at) else ("fallback_qty_match" if exit_price else "missing"),
    }
    history["trades"].append(trade)
    save_json(history_path, history)
    return "recorded" if exit_price else "no_exit_price"


def main():
    enforce_live_guardrails()
    strategy = load_strategy()
    positions = get_positions()

    trailing_atr_mult = strategy.get("trailing_atr_multiplier", 2.5)
    breakeven_buffer = strategy.get("breakeven_buffer_atr", 0.1)
    invalidation_bars = strategy.get("early_invalidation_bars", 3)

    tracking = load_tracking()
    actions = []

    for pos in positions:
        symbol = pos["symbol"]
        qty = int(float(pos["qty"]))
        avg_entry = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])

        # Initialize tracking for positions not placed by trade.py (manual or pre-existing)
        if symbol not in tracking:
            tracking[symbol] = {
                "planned_entry": avg_entry,
                "initial_stop": None,
                "r_per_share": None,
                "atr14": None,
                "sma50": None,
                "phase": "initial",
                "bars_held": 0,
            }
            save_tracking(tracking)

        track = tracking[symbol]

        # Skip positions with pending exit — cleanup handles them when position disappears
        if track.get("phase") == "exit_pending":
            actions.append({"symbol": symbol, "action": "exit_pending", "reason": track.get("exit_reason", "unknown")})
            continue

        # Transition from "pending" to "initial" once position exists (order filled)
        if track.get("phase") == "pending":
            track["phase"] = "initial"
            track["entry_price"] = avg_entry
            track["qty"] = qty
            track["filled_at"] = now_iso()
            track["bars_held"] = 0  # Reset — start counting from actual fill
            # Recalculate R using actual entry and stored initial stop
            if track.get("initial_stop"):
                track["r_per_share"] = round(avg_entry - track["initial_stop"], 2)
            save_tracking(tracking)

        # Increment bars_held only once per trading day (idempotent)
        today = today_str()
        if track.get("last_bar_date") != today:
            track["bars_held"] = track.get("bars_held", 0) + 1
            track["last_bar_date"] = today

        # Get R, ATR, SMA50 from tracking (persisted at order time)
        r_per_share = track.get("r_per_share")
        atr = track.get("atr14")
        sma50 = track.get("sma50")

        # Fallback: try candidates.json only if tracking has no data
        if r_per_share is None or atr is None:
            candidates_path = STATE_DIR / "candidates.json"
            if candidates_path.exists():
                cdata = json.loads(candidates_path.read_text())
                for c in cdata.get("candidates", []) + cdata.get("rejected", []):
                    if c.get("symbol") == symbol:
                        if atr is None and "atr14" in c:
                            atr = float(c["atr14"])
                            track["atr14"] = atr
                        if sma50 is None and "sma50" in c:
                            sma50 = float(c["sma50"])
                            track["sma50"] = sma50
                        break
            if r_per_share is None and atr is not None:
                # Only allow ATR-based fallback for positions NOT placed by this bot
                # (e.g. manual positions or pre-existing before v2 upgrade)
                if track.get("phase") == "initial" and track.get("entry_date"):
                    # This position was placed by our bot but lost its R data — flag it
                    actions.append({
                        "symbol": symbol,
                        "action": "WARNING_r_data_missing",
                        "MANUAL_REVIEW_REQUIRED": True,
                        "NOTE": "Position placed by bot but initial_stop/r_per_share lost. Using ATR fallback.",
                    })
                    send_alert(f"⚠️ {symbol}: R data missing for bot-originated position. Using ATR fallback.", level="warning")
                r_per_share = strategy["atr_stop_multiplier"] * atr
                track["r_per_share"] = round(r_per_share, 2)
            save_tracking(tracking)

        if r_per_share is None or atr is None:
            actions.append({"symbol": symbol, "action": "skip", "reason": "no_r_or_atr_data"})
            continue

        target_1r = avg_entry + r_per_share
        target_2r = avg_entry + strategy["reward_to_risk"] * r_per_share

        # --- EARLY INVALIDATION: close below 50 SMA within first N bars ---
        # manage.py is scheduled at 4:05 PM ET (after market close),
        # so current_price reflects the completed daily bar's close.
        if (track["phase"] == "initial"
                and track["bars_held"] <= invalidation_bars
                and sma50 is not None
                and current_price < sma50):
            try:
                # Cancel existing sell stops BEFORE submitting market sell
                fresh_orders = get_open_orders_fresh()
                existing_stops = get_stop_orders_for_symbol(symbol, fresh_orders)
                existing_trailing = [o for o in fresh_orders
                                     if o.get("symbol") == symbol and o.get("side") == "sell"
                                     and o.get("type") == "trailing_stop"
                                     and o.get("status") in ACTIVE_ORDER_STATUSES]
                all_cancel_verified = True
                for stop_order in existing_stops + existing_trailing:
                    if not cancel_order_and_verify(stop_order["id"]):
                        all_cancel_verified = False

                if not all_cancel_verified:
                    # Re-check broker state — if stops still active, abort to avoid overlapping exits
                    recheck = get_open_orders_fresh()
                    still_active = get_stop_orders_for_symbol(symbol, recheck)
                    still_trailing = [o for o in recheck
                                      if o.get("symbol") == symbol and o.get("side") == "sell"
                                      and o.get("type") == "trailing_stop"
                                      and o.get("status") in ACTIVE_ORDER_STATUSES]
                    if still_active or still_trailing:
                        actions.append({
                            "symbol": symbol,
                            "action": "early_exit_aborted",
                            "reason": "existing_stops_not_confirmed_cancelled",
                            "MANUAL_REVIEW_REQUIRED": True,
                        })
                        send_alert(f"🚨 {symbol}: Early exit ABORTED — existing stops still active after cancel attempt!", level="error")
                        continue

                resp = submit_market_sell(symbol, qty, reason="early_invalidation")
                send_alert(f"🚨 EARLY EXIT: {symbol} closed below 50 SMA within {track['bars_held']} bars. Selling at market.", level="warning")
                actions.append({
                    "symbol": symbol,
                    "action": "early_invalidation_exit",
                    "reason": "close_below_sma50",
                    "bars_held": track["bars_held"],
                    "order_id": resp.get("id"),
                })
                track["phase"] = "exit_pending"
                track["exit_reason"] = "early_invalidation"
                track["exit_order_id"] = resp.get("id")
                track["exit_client_order_id"] = resp.get("client_order_id")
                track["exit_order_type"] = "market_early_invalidation"
                save_tracking(tracking)
            except Exception as e:
                actions.append({"symbol": symbol, "action": "early_exit_failed", "error": str(e)})
                send_alert(f"🚨 {symbol}: Early exit FAILED: {e}", level="error")
            continue

        # --- PHASE: INITIAL → BREAKEVEN (at 1R) ---
        if track["phase"] == "initial" and current_price >= target_1r:
            breakeven_stop = avg_entry + breakeven_buffer * atr
            fresh_orders = get_open_orders_fresh()
            existing_stops = get_stop_orders_for_symbol(symbol, fresh_orders)

            # Check if an existing stop is already at or above breakeven — skip churn
            already_adequate = False
            for stop_order in existing_stops:
                existing_stop_price = float(stop_order.get("stop_price", 0))
                if existing_stop_price >= round(breakeven_stop, 2):
                    already_adequate = True
                    track["phase"] = "breakeven"
                    track["stop_order_id"] = stop_order.get("id")
                    track["exit_order_id"] = stop_order.get("id")
                    track["exit_client_order_id"] = stop_order.get("client_order_id")
                    track["exit_order_type"] = "stop_breakeven"
                    save_tracking(tracking)
                    actions.append({
                        "symbol": symbol,
                        "action": "moved_to_breakeven",
                        "new_stop": existing_stop_price,
                        "note": "existing_stop_already_adequate",
                        "order_id": stop_order.get("id"),
                    })
                    break

            if already_adequate:
                continue

            # Cancel existing stop orders for this symbol — verify each is confirmed cancelled
            all_cancelled = True
            for stop_order in existing_stops:
                if not cancel_order_and_verify(stop_order["id"]):
                    all_cancelled = False

            if not all_cancelled:
                # Re-check — if old stops still active, don't place a new one (avoid double protection)
                recheck = get_open_orders_fresh()
                still_open = get_stop_orders_for_symbol(symbol, recheck)
                if still_open:
                    actions.append({
                        "symbol": symbol,
                        "action": "breakeven_stop_aborted",
                        "reason": "old_stops_not_confirmed_cancelled",
                        "MANUAL_REVIEW_REQUIRED": True,
                    })
                    send_alert(f"🚨 {symbol}: Breakeven stop ABORTED — old stops still active!", level="error")
                    continue

            # Place new breakeven stop
            try:
                resp = submit_stop_order(symbol, qty, breakeven_stop, phase="breakeven")
                track["phase"] = "breakeven"
                track["stop_order_id"] = resp.get("id")
                track["exit_order_id"] = resp.get("id")
                track["exit_client_order_id"] = resp.get("client_order_id")
                track["exit_order_type"] = "stop_breakeven"
                save_tracking(tracking)
                actions.append({
                    "symbol": symbol,
                    "action": "moved_to_breakeven",
                    "new_stop": round(breakeven_stop, 2),
                    "order_id": resp.get("id"),
                })
                send_alert(f"📈 {symbol}: Hit 1R! Stop moved to breakeven ${breakeven_stop:.2f}", level="trade")
            except Exception as e:
                actions.append({"symbol": symbol, "action": "breakeven_stop_failed", "error": str(e)})
                send_alert(f"⚠️ {symbol}: Breakeven stop FAILED: {e}", level="warning")
            continue

        # --- PHASE: BREAKEVEN → TRAILING (at 2R) ---
        if track["phase"] == "breakeven" and current_price >= target_2r:
            fresh_orders = get_open_orders_fresh()

            # Check if trailing stop already exists
            already_exists, existing_id = has_existing_trailing_stop(symbol, fresh_orders)
            if already_exists:
                # Refresh canonical exit IDs from the existing trailing order
                existing_order = next((o for o in fresh_orders if o.get("id") == existing_id), {})
                track["phase"] = "trailing"
                track["trailing_order_id"] = existing_id
                track["exit_order_id"] = existing_id
                track["exit_client_order_id"] = existing_order.get("client_order_id")
                track["exit_order_type"] = "trailing_stop"
                save_tracking(tracking)
                actions.append({"symbol": symbol, "action": "trailing_already_exists", "order_id": existing_id})
                continue

            # Cancel breakeven stop — verify each is confirmed cancelled
            existing_stops = get_stop_orders_for_symbol(symbol, fresh_orders)
            for stop_order in existing_stops:
                cancel_order_and_verify(stop_order["id"])

            # Verify cancellation
            verify_orders = get_open_orders_fresh()
            still_open = get_stop_orders_for_symbol(symbol, verify_orders)
            if still_open:
                actions.append({
                    "symbol": symbol,
                    "action": "WARNING_old_stop_still_open",
                    "MANUAL_REVIEW_REQUIRED": True,
                })
                send_alert(f"🚨 {symbol}: Old stop still open after cancel — skipping trailing stop!", level="error")
                continue

            # Place trailing stop on FULL position
            trail_amount = trailing_atr_mult * atr
            try:
                resp = submit_trailing_stop(symbol, qty, trail_amount)
                track["phase"] = "trailing"
                track["trailing_order_id"] = resp.get("id")
                track["exit_order_id"] = resp.get("id")
                track["exit_client_order_id"] = resp.get("client_order_id")
                track["exit_order_type"] = "trailing_stop"
                save_tracking(tracking)
                actions.append({
                    "symbol": symbol,
                    "action": "trailing_stop_activated",
                    "qty": qty,
                    "trail_amount": round(trail_amount, 2),
                    "order_id": resp.get("id"),
                })
                send_alert(f"📈 {symbol}: Hit 2R! Trailing stop set at ${trail_amount:.2f} trail on {qty} shares.", level="trade")
            except Exception as e:
                save_tracking(tracking)
                actions.append({
                    "symbol": symbol,
                    "action": "trailing_stop_failed",
                    "error": str(e),
                    "MANUAL_REVIEW_REQUIRED": True,
                })
                send_alert(f"🚨 {symbol}: Trailing stop FAILED at 2R: {e}", level="error")
            continue

        # --- RECOVERY: phase is breakeven but no stop exists ---
        if track["phase"] == "breakeven":
            fresh_orders = get_open_orders_fresh()
            existing_stops = get_stop_orders_for_symbol(symbol, fresh_orders)
            if not existing_stops:
                # Re-place breakeven stop
                breakeven_stop = avg_entry + breakeven_buffer * atr
                try:
                    resp = submit_stop_order(symbol, qty, breakeven_stop, phase="breakeven_recovery")
                    track["stop_order_id"] = resp.get("id")
                    track["exit_order_id"] = resp.get("id")
                    track["exit_client_order_id"] = resp.get("client_order_id")
                    track["exit_order_type"] = "stop_breakeven"
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "breakeven_stop_recovered", "stop": round(breakeven_stop, 2)})
                except Exception as e:
                    actions.append({"symbol": symbol, "action": "breakeven_recovery_failed", "error": str(e)})
                continue

        # --- STATUS REPORT + TRAILING RECOVERY ---
        if track["phase"] == "trailing":
            # Verify trailing stop still exists at broker
            fresh_orders = get_open_orders_fresh()
            has_trailing, trailing_id = has_existing_trailing_stop(symbol, fresh_orders)
            if not has_trailing:
                # Trailing stop is gone — re-place it
                trail_amount = trailing_atr_mult * atr
                try:
                    resp = submit_trailing_stop(symbol, qty, trail_amount)
                    track["trailing_order_id"] = resp.get("id")
                    track["exit_order_id"] = resp.get("id")
                    track["exit_client_order_id"] = resp.get("client_order_id")
                    track["exit_order_type"] = "trailing_stop"
                    save_tracking(tracking)
                    actions.append({"symbol": symbol, "action": "trailing_stop_recovered", "trail": round(trail_amount, 2), "order_id": resp.get("id")})
                    send_alert(f"🔧 {symbol}: Trailing stop recovered (was missing). Trail: ${trail_amount:.2f}", level="warning")
                except Exception as e:
                    actions.append({"symbol": symbol, "action": "trailing_recovery_failed", "error": str(e), "MANUAL_REVIEW_REQUIRED": True})
                    send_alert(f"🚨 {symbol}: Trailing stop MISSING and recovery FAILED: {e}", level="error")
            else:
                actions.append({
                    "symbol": symbol,
                    "action": "trailing_active",
                    "current_price": current_price,
                    "entry": avg_entry,
                    "gain_pct": round((current_price - avg_entry) / avg_entry * 100, 2),
                    "trailing_order_id": trailing_id,
                })
        elif track["phase"] == "initial":
            # Reconcile exit_order_id: verify the OTO child stop still exists at broker
            expected_exit_id = track.get("exit_order_id")
            if expected_exit_id:
                try:
                    exit_order = get_order_by_id(expected_exit_id)
                    exit_status = exit_order.get("status", "") if exit_order else ""
                    if exit_status in ("canceled", "expired", "rejected", "replaced"):
                        # OTO child stop is gone — re-place initial stop
                        initial_stop = track.get("initial_stop")
                        if initial_stop and initial_stop > 0:
                            resp = submit_stop_order(symbol, qty, initial_stop, phase="initial_recovery")
                            track["stop_order_id"] = resp.get("id")
                            track["exit_order_id"] = resp.get("id")
                            track["exit_client_order_id"] = resp.get("client_order_id")
                            track["exit_order_type"] = "stop_initial_recovered"
                            save_tracking(tracking)
                            actions.append({
                                "symbol": symbol,
                                "action": "initial_stop_recovered",
                                "stop": round(initial_stop, 2),
                                "order_id": resp.get("id"),
                                "old_order_id": expected_exit_id,
                                "old_status": exit_status,
                            })
                            send_alert(f"🔧 {symbol}: Initial stop recovered (was {exit_status}). Stop: ${initial_stop:.2f}", level="warning")
                            continue
                except Exception:
                    pass
            actions.append({
                "symbol": symbol,
                "action": "hold_initial",
                "current_price": current_price,
                "target_1r": round(target_1r, 2),
                "pct_to_1r": round((target_1r - current_price) / current_price * 100, 2),
            })
        elif track["phase"] == "breakeven":
            actions.append({
                "symbol": symbol,
                "action": "hold_breakeven",
                "current_price": current_price,
                "target_2r": round(target_2r, 2),
                "pct_to_2r": round((target_2r - current_price) / current_price * 100, 2),
            })

    # Save bars_held updates
    save_tracking(tracking)

    # Clean up tracking for closed positions — but preserve "pending" entries with open buy orders
    open_symbols = {p["symbol"] for p in positions}
    fresh_orders = get_open_orders_fresh()
    pending_buy_symbols = {o["symbol"] for o in fresh_orders
                           if o.get("side") == "buy" and o.get("status") in ACTIVE_ORDER_STATUSES}

    closed = []
    deferred = []
    for s in list(tracking.keys()):
        if s in open_symbols:
            continue  # Position exists, keep tracking
        if s in pending_buy_symbols and tracking[s].get("phase") == "pending":
            continue  # Buy order still pending, preserve stored R/stop data
        # Position closed — record in trade history before deleting
        track_data = tracking[s]
        if track_data.get("phase") not in ("pending", None):
            record_status = _record_closed_trade(s, track_data, exit_reason=track_data.get("exit_reason"))
            if record_status == "deferred":
                # Exit order not yet filled — preserve tracking for next run
                deferred.append(s)
                actions.append({
                    "action": "trade_recording_deferred",
                    "symbol": s,
                    "exit_order_id": track_data.get("exit_order_id"),
                })
                continue
            if record_status == "no_exit_price":
                actions.append({
                    "action": "trade_recording_warning",
                    "symbol": s,
                    "status": record_status,
                    "exit_order_id": track_data.get("exit_order_id"),
                })
        closed.append(s)

    for s in closed:
        del tracking[s]
    if closed:
        save_tracking(tracking)
        actions.append({"action": "cleaned_tracking", "symbols": closed})

    # Save log
    log = {"timestamp": now_iso(), "actions": actions}
    save_json(STATE_DIR / "manage_log.json", log)
    write_heartbeat("manage", "ok", {"positions_managed": len(positions), "actions": len(actions)})
    print(f"Manage v2: {len(actions)} actions on {len(positions)} positions")


if __name__ == "__main__":
    main()

