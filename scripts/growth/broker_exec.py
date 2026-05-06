"""
Broker execution helpers for growth bot.

These functions interact with the broker (place/cancel orders).
They do NOT make strategy decisions. They execute actions.
"""
import time
import hashlib
from datetime import datetime

import sys
from pathlib import Path
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ, ACTIVE_ORDER_STATUSES,
    alpaca_get, alpaca_post, cancel_order_and_verify,
)


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


def cancel_all_stops_verified(symbol, fresh_orders):
    """Cancel all stop/trailing orders for symbol. Returns True if all confirmed cancelled."""
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
    """Sanity-check stop parameters. Returns (ok, reason) tuple."""
    if stop_price is None or stop_price <= 0:
        return False, "stop_price_invalid"
    if stop_price >= current_price:
        return False, "stop_above_current_price"
    if qty is None or qty <= 0:
        return False, "qty_invalid"
    return True, "ok"


def execute_cancel_and_replace(symbol, qty, fresh_orders, new_order_fn, fallback_stop=None, dry_run=False):
    """
    Standard cancel→replace→recover pattern.

    1. Cancel all existing stops
    2. Execute new_order_fn() to place new order
    3. On failure, restore fallback_stop

    Returns (success: bool, result: dict)
    """
    all_cancelled = cancel_all_stops_verified(symbol, fresh_orders)
    if not all_cancelled:
        return False, {"error": "cancel_failed", "MANUAL_REVIEW": True}

    time.sleep(0.5)

    try:
        resp = new_order_fn()
        return True, {"order_id": resp.get("id"), "response": resp}
    except Exception as e:
        # Recovery: restore fallback
        if fallback_stop:
            try:
                recovery_resp = submit_stop_order(symbol, qty, fallback_stop, "recovery", dry_run=dry_run)
                return False, {"error": str(e), "recovered": True,
                               "recovery_order_id": recovery_resp.get("id"),
                               "restored_stop": fallback_stop}
            except Exception as e2:
                return False, {"error": str(e), "recovery_error": str(e2),
                               "MANUAL_REVIEW": True}
        return False, {"error": str(e), "MANUAL_REVIEW": True}

