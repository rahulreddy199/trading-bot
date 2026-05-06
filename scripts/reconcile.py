"""
Broker Reconciliation Module — Phase 0 Hardening

Compares local tracking state against broker truth (positions + orders).
Classifies mismatches, auto-heals where safe, flags MANUAL_REVIEW where not.

Used by both manage.py and manage_growth.py.
"""
import json
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    ACTIVE_ORDER_STATUSES,
    STATE_DIR,
    alpaca_get,
    get_positions,
    log_event,
    now_iso,
    resolve_state,
    save_json,
    send_alert,
)


def fetch_broker_state():
    """Fetch complete broker state: positions and open orders."""
    positions = get_positions()
    orders = alpaca_get("/v2/orders", params={"status": "open", "limit": 200})
    return {
        "positions": {p["symbol"]: p for p in positions},
        "orders": orders,
    }


def get_protective_orders(symbol, orders):
    """Get all sell-side protective orders (stop, trailing_stop) for a symbol."""
    stops = []
    trailing = []
    for o in orders:
        if o.get("symbol") != symbol or o.get("side") != "sell":
            continue
        if o.get("status") not in ACTIVE_ORDER_STATUSES:
            continue
        if o.get("type") == "stop":
            stops.append(o)
        elif o.get("type") == "trailing_stop":
            trailing.append(o)
    return stops, trailing


def reconcile(bot, tracking, broker_state=None):
    """
    Reconcile local tracking against broker truth.

    Returns:
        fixes: list of dicts describing each mismatch and action taken
        updated_tracking: the corrected tracking dict

    Mismatch categories:
        BROKER_HAS_POSITION_NO_LOCAL   — broker has position, no local tracking
        LOCAL_HAS_POSITION_NO_BROKER   — local tracks a position broker doesn't have
        NO_PROTECTIVE_ORDER            — position exists but no stop/trail at broker
        PHASE_MISMATCH                 — broker order type doesn't match local phase
        PARTIAL_FILL_QTY_MISMATCH      — broker qty differs from tracked qty
        ORDER_LIMBO                    — order stuck in pending_cancel/pending_replace
    """
    if broker_state is None:
        broker_state = fetch_broker_state()

    broker_positions = broker_state["positions"]
    broker_orders = broker_state["orders"]
    fixes = []

    # 1. Broker has position, local does not
    for symbol, pos in broker_positions.items():
        if symbol not in tracking:
            fix = {
                "type": "BROKER_HAS_POSITION_NO_LOCAL",
                "symbol": symbol,
                "broker_qty": pos.get("qty"),
                "broker_entry": pos.get("avg_entry_price"),
                "action": "flag_manual_review",
                "ts": now_iso(),
            }
            # Create minimal tracking entry flagged for review
            tracking[symbol] = {
                "planned_entry": float(pos.get("avg_entry_price", 0)),
                "phase": "initial",
                "bars_held": 0,
                "r_per_share": None,
                "atr14_at_entry": None,
                "MANUAL_REVIEW": True,
                "MANUAL_REVIEW_REASON": "broker_position_without_local_tracking",
                "last_reconciled_at": now_iso(),
            }
            fixes.append(fix)
            log_event(bot, "reconcile", "broker_position_no_local",
                      symbol=symbol, reason_code="BROKER_STATE_MISMATCH",
                      extra=fix)

    # 2. Local has position, broker does not
    symbols_to_remove = []
    for symbol, track in tracking.items():
        if track.get("phase") in ("closed", "exit_pending_removed"):
            continue
        if symbol not in broker_positions:
            # Position was closed (filled stop, manual sell, etc.)
            fix = {
                "type": "LOCAL_HAS_POSITION_NO_BROKER",
                "symbol": symbol,
                "local_phase": track.get("phase"),
                "action": "mark_closed",
                "ts": now_iso(),
            }
            track["phase"] = "closed"
            track["closed_reason"] = "reconciliation_broker_no_position"
            track["last_reconciled_at"] = now_iso()
            fixes.append(fix)
            log_event(bot, "reconcile", "local_position_no_broker",
                      symbol=symbol, reason_code="RECONCILIATION_FIX",
                      before_state={"phase": fix["local_phase"]},
                      after_state={"phase": "closed"})

    # 3. For each position that exists on both sides, check protective orders
    for symbol, track in tracking.items():
        if symbol not in broker_positions:
            continue
        if track.get("phase") in ("closed", "pending"):
            continue

        stops, trailing = get_protective_orders(symbol, broker_orders)
        broker_qty = int(float(broker_positions[symbol].get("qty", 0)))

        # Qty mismatch (partial fill)
        tracked_qty = track.get("qty")
        if tracked_qty and int(tracked_qty) != broker_qty:
            fix = {
                "type": "PARTIAL_FILL_QTY_MISMATCH",
                "symbol": symbol,
                "tracked_qty": tracked_qty,
                "broker_qty": broker_qty,
                "action": "update_qty",
                "ts": now_iso(),
            }
            track["qty"] = broker_qty
            track["last_reconciled_at"] = now_iso()
            fixes.append(fix)
            log_event(bot, "reconcile", "qty_mismatch_fixed",
                      symbol=symbol, reason_code="RECONCILIATION_FIX",
                      extra=fix)

        # Phase vs order type mismatch
        has_stop = len(stops) > 0
        has_trailing = len(trailing) > 0
        local_phase = track.get("phase", "initial")

        if has_trailing and local_phase in ("initial", "protected"):
            # Broker already advanced to trailing
            fix = {
                "type": "PHASE_MISMATCH",
                "symbol": symbol,
                "local_phase": local_phase,
                "broker_has": "trailing_stop",
                "action": "sync_up_to_trailing",
                "ts": now_iso(),
            }
            track["phase"] = "trailing"
            track["last_reconciled_at"] = now_iso()
            fixes.append(fix)
            log_event(bot, "reconcile", "phase_synced_up",
                      symbol=symbol, reason_code="RECONCILIATION_FIX",
                      before_state={"phase": local_phase},
                      after_state={"phase": "trailing"})

        elif has_stop and not has_trailing and local_phase == "trailing":
            # Local thinks trailing but broker only has stop
            fix = {
                "type": "PHASE_MISMATCH",
                "symbol": symbol,
                "local_phase": local_phase,
                "broker_has": "stop_only",
                "action": "sync_down_to_protected",
                "ts": now_iso(),
            }
            track["phase"] = "protected"
            track["last_reconciled_at"] = now_iso()
            fixes.append(fix)
            log_event(bot, "reconcile", "phase_synced_down",
                      symbol=symbol, reason_code="RECONCILIATION_FIX",
                      before_state={"phase": "trailing"},
                      after_state={"phase": "protected"})

        elif not has_stop and not has_trailing and local_phase not in ("pending", "closed", "exit_pending"):
            # NO protective order — critical
            fix = {
                "type": "NO_PROTECTIVE_ORDER",
                "symbol": symbol,
                "local_phase": local_phase,
                "action": "flag_for_stop_recovery",
                "ts": now_iso(),
            }
            track["needs_stop_recovery"] = True
            track["last_reconciled_at"] = now_iso()
            fixes.append(fix)
            log_event(bot, "reconcile", "no_protective_order",
                      symbol=symbol, reason_code="MANUAL_REVIEW_REQUIRED",
                      extra=fix)

        # Check for limbo orders
        for o in broker_orders:
            if o.get("symbol") == symbol and o.get("status") in ("pending_cancel", "pending_replace"):
                fix = {
                    "type": "ORDER_LIMBO",
                    "symbol": symbol,
                    "order_id": o.get("id"),
                    "status": o.get("status"),
                    "action": "flag_manual_review",
                    "ts": now_iso(),
                }
                track["MANUAL_REVIEW"] = True
                track["MANUAL_REVIEW_REASON"] = f"order_{o.get('status')}"
                fixes.append(fix)
                log_event(bot, "reconcile", "order_limbo",
                          symbol=symbol, order_id=o.get("id"),
                          reason_code="MANUAL_REVIEW_REQUIRED")

        track["last_reconciled_at"] = now_iso()

    return fixes, tracking


def run_reconciliation(bot):
    """Standalone reconciliation entry point. Loads tracking, reconciles, saves."""
    tracking_path = resolve_state(bot, "position_tracking.json")
    if tracking_path.exists():
        tracking = json.loads(tracking_path.read_text())
    else:
        tracking = {}

    broker_state = fetch_broker_state()
    fixes, updated_tracking = reconcile(bot, tracking, broker_state)

    if fixes:
        save_json(tracking_path, updated_tracking)
        # Save reconciliation report
        report_path = resolve_state(bot, "last_reconciliation.json")
        save_json(report_path, {
            "ts": now_iso(),
            "bot": bot,
            "fixes_count": len(fixes),
            "fixes": fixes,
        })
        print(f"  Reconciliation ({bot}): {len(fixes)} fixes applied")
        for f in fixes:
            print(f"    {f['type']}: {f['symbol']} → {f['action']}")

        # Alert on critical issues
        critical = [f for f in fixes if f["type"] in ("NO_PROTECTIVE_ORDER", "ORDER_LIMBO", "BROKER_HAS_POSITION_NO_LOCAL")]
        if critical:
            msg = f"⚠️ Reconciliation ({bot}): {len(critical)} critical issues\n"
            msg += "\n".join(f"  • {f['symbol']}: {f['type']}" for f in critical)
            send_alert(msg, level="warning")
    else:
        print(f"  Reconciliation ({bot}): all clean ✓")

    return fixes, updated_tracking


if __name__ == "__main__":
    import sys as _sys
    bot = _sys.argv[1] if len(_sys.argv) > 1 else "growth"
    print(f"Running reconciliation for: {bot}")
    fixes, _ = run_reconciliation(bot)
    if not fixes:
        print("No mismatches found.")

