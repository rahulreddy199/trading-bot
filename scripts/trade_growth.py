"""
Growth Bot V1 — Trade script.

Places stop-limit buy orders for growth momentum setups.
Uses wider stops and more aggressive sizing than the conservative bot.
"""
import json
import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import sys
from pathlib import Path
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    CONFIG_DIR,
    alpaca_get,
    alpaca_post,
    cancel_order_and_verify,
    enforce_live_guardrails,
    get_account,
    get_clock,
    get_positions,
    now_iso,
    risk_position_size,
    save_json,
    send_alert,
    today_str,
    write_heartbeat,
    ACTIVE_ORDER_STATUSES,
)


def load_growth_strategy():
    return json.loads((CONFIG_DIR / "strategy_growth.json").read_text())


def load_tracking():
    path = STATE_DIR / "position_tracking_growth.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_tracking(tracking):
    save_json(STATE_DIR / "position_tracking_growth.json", tracking)


def get_open_buy_orders():
    return alpaca_get("/v2/orders", params={"status": "open", "side": "buy", "limit": 100})


def cancel_stale_orders(stale_days=2):
    orders = get_open_buy_orders()
    cutoff = datetime.now(MARKET_TZ) - timedelta(days=stale_days)
    cancelled = []
    for order in orders:
        submitted = order.get("submitted_at", "")
        if submitted:
            try:
                submitted_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                if submitted_dt < cutoff:
                    age_hours = (datetime.now(MARKET_TZ) - submitted_dt).total_seconds() / 3600
                    verified = cancel_order_and_verify(order["id"])
                    cancelled.append({
                        "symbol": order["symbol"],
                        "order_id": order["id"],
                        "cancel_verified": verified,
                        "age_hours": round(age_hours, 1),
                    })
            except Exception:
                pass
    if cancelled:
        print(f"  Stale orders cancelled: {len(cancelled)}")
        for c in cancelled:
            print(f"    {c['symbol']}: age={c['age_hours']}h verified={c['cancel_verified']}")
    return cancelled


def generate_client_order_id(symbol, side="buy"):
    date_str = datetime.now(MARKET_TZ).strftime("%Y%m%d")
    key = f"{date_str}_{side}_{symbol}_growth"
    hash_suffix = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"growth_{side}_{symbol}_{date_str}_{hash_suffix}"


def check_duplicate_order(client_order_id):
    try:
        existing = alpaca_get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        if isinstance(existing, dict) and existing.get("id"):
            if existing.get("status", "") in ACTIVE_ORDER_STATUSES:
                return existing
    except Exception:
        pass
    return None


def submit_entry_order(symbol, qty, trigger_price, limit_price, stop_price):
    client_id = generate_client_order_id(symbol, "buy")

    existing = check_duplicate_order(client_id)
    if existing:
        existing["_client_order_id"] = client_id
        existing["_duplicate"] = True
        return existing

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "stop_limit",
        "stop_price": round(trigger_price, 2),
        "limit_price": round(limit_price, 2),
        "time_in_force": "gtc",
        "order_class": "oto",
        "stop_loss": {"stop_price": round(stop_price, 2)},
        "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp["_client_order_id"] = client_id
    return resp


def main(dry_run=False):
    enforce_live_guardrails()

    if not dry_run:
        kill_switch_path = STATE_DIR / "KILL_SWITCH"
        if kill_switch_path.exists():
            print("🛑 KILL SWITCH ACTIVE — no new orders.")
            return

    # Daily drawdown circuit breaker
    try:
        account = get_account()
        equity = float(account["equity"])
        last_equity = float(account.get("last_equity", equity))
        daily_change_pct = (equity - last_equity) / last_equity if last_equity > 0 else 0
        daily_loss_limit = -0.03  # -3% daily loss limit
        if daily_change_pct <= daily_loss_limit:
            msg = f"🛑 DAILY CIRCUIT BREAKER: {daily_change_pct*100:.1f}% today. No new entries."
            print(msg)
            send_alert(msg, level="error")
            return
    except Exception:
        pass  # Don't block trading on account-fetch failure

    strategy = load_growth_strategy()
    candidates_path = STATE_DIR / "candidates_growth.json"
    if not candidates_path.exists():
        raise RuntimeError("Missing candidates_growth.json. Run research_growth.py first.")

    payload = json.loads(candidates_path.read_text())

    # Stale check
    research_date = payload.get("date", "")
    if research_date != datetime.now(MARKET_TZ).strftime("%Y-%m-%d"):
        print(f"Trade skipped: candidates stale ({research_date})")
        return

    # Idempotency
    order_plan_path = STATE_DIR / "order_plan_growth.json"
    if order_plan_path.exists():
        prior = json.loads(order_plan_path.read_text())
        if prior.get("timestamp", "").startswith(datetime.now(MARKET_TZ).strftime("%Y-%m-%d")) and prior.get("orders"):
            print(f"Trade skipped: already ran today")
            return

    clock = get_clock()
    account = get_account()
    positions = get_positions()

    if not clock.get("is_open", False):
        plan = {"timestamp": now_iso(), "orders": [], "skips": [{"reason": "market_closed"}]}
        save_json(order_plan_path, plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": "market_closed"})
        print("Trade skipped: market closed")
        return

    # Regime config
    regime_mode = payload.get("regime_mode", "risk_off")
    regime_cfg = strategy["regime"].get(regime_mode, {})
    allow_entries = payload.get("allow_new_entries", False)
    risk_per_trade = regime_cfg.get("risk_per_trade", 0)
    max_positions = regime_cfg.get("max_open_positions", 0)
    max_alloc = regime_cfg.get("max_alloc_fraction_per_symbol", 0.25)
    max_portfolio_risk = regime_cfg.get("max_total_portfolio_risk_pct", 0.03)
    cash_reserve_pct = regime_cfg.get("cash_reserve_pct", 0.05)

    equity = float(account["equity"])
    cash = float(account["cash"])
    reserve_cash = equity * cash_reserve_pct

    open_symbols = {p["symbol"] for p in positions}
    pending_buy_orders = get_open_buy_orders()
    pending_buy_symbols = {o["symbol"] for o in pending_buy_orders}
    blocked_symbols = open_symbols | pending_buy_symbols
    remaining_slots = max(max_positions - len(open_symbols) - len(pending_buy_symbols), 0)

    # Cancel stale
    cancelled_stale = cancel_stale_orders(strategy["entry"]["stale_order_cancel_days"])
    if cancelled_stale:
        pending_buy_orders = get_open_buy_orders()
        pending_buy_symbols = {o["symbol"] for o in pending_buy_orders}
        blocked_symbols = open_symbols | pending_buy_symbols
        remaining_slots = max(max_positions - len(open_symbols) - len(pending_buy_symbols), 0)

    plan = {
        "bot_name": "growth",
        "timestamp": now_iso(),
        "regime_mode": regime_mode,
        "equity": equity,
        "cash": cash,
        "open_symbols": sorted(open_symbols),
        "cancelled_stale": cancelled_stale,
        "orders": [],
        "skips": [],
    }

    if not allow_entries:
        plan["skips"].append({"reason": f"regime_{regime_mode}"})
        save_json(order_plan_path, plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": regime_mode})
        print(f"Trade skipped: {regime_mode}")
        return

    if remaining_slots <= 0:
        plan["skips"].append({"reason": "max_positions_reached"})
        save_json(order_plan_path, plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": "max_positions"})
        print("Trade skipped: max positions")
        return

    # Current portfolio risk — with fallback chain for missing tracking data
    tracking = load_tracking()
    current_risk = 0.0
    for sym in open_symbols:
        pos_qty = next((int(float(p["qty"])) for p in positions if p["symbol"] == sym), 0)
        if pos_qty <= 0:
            continue

        r_per_share = None

        # 1. Try tracked r_per_share
        if sym in tracking and tracking[sym].get("r_per_share"):
            r_per_share = tracking[sym]["r_per_share"]

        # 2. Try last_orders_growth.json
        if r_per_share is None:
            try:
                last_orders_path = STATE_DIR / "last_orders_growth.json"
                if last_orders_path.exists():
                    last_orders = json.loads(last_orders_path.read_text())
                    for o in last_orders:
                        if o.get("symbol") == sym and o.get("r_per_share"):
                            r_per_share = o["r_per_share"]
                            break
            except Exception:
                pass

        # 3. Try order_plan_growth.json
        if r_per_share is None:
            try:
                order_plan_path_check = STATE_DIR / "order_plan_growth.json"
                if order_plan_path_check.exists():
                    op = json.loads(order_plan_path_check.read_text())
                    for o in op.get("orders", []):
                        if o.get("symbol") == sym and o.get("r_per_share"):
                            r_per_share = o["r_per_share"]
                            break
            except Exception:
                pass

        # 4. Conservative fallback: assume 2.5% of current price as risk per share
        if r_per_share is None:
            pos_price = next((float(p["current_price"]) for p in positions if p["symbol"] == sym), 0)
            if pos_price > 0:
                r_per_share = pos_price * 0.025  # conservative estimate
            else:
                r_per_share = 0

        current_risk += r_per_share * pos_qty

    for candidate in payload.get("candidates", []):
        symbol = candidate["symbol"]
        if remaining_slots <= 0:
            break
        if symbol in blocked_symbols:
            plan["skips"].append({"symbol": symbol, "reason": "already_open_or_pending"})
            continue

        # Correlation cap check (item 10)
        corr_cfg = strategy.get("correlation_cap", {})
        if corr_cfg.get("enabled", False) and open_symbols:
            corr_blocked = False
            try:
                lookback = corr_cfg.get("lookback_days", 40)
                threshold = corr_cfg.get("threshold", 0.85)
                max_corr = corr_cfg.get("max_correlated_positions", 2)

                # Get daily returns for candidate and open positions
                corr_symbols = list(open_symbols) + [symbol]
                data = yf.download(corr_symbols, period=f"{lookback + 10}d",
                                   interval="1d", auto_adjust=True, progress=False, threads=False)
                if isinstance(data.columns, pd.MultiIndex):
                    closes = data["Close"] if "Close" in data.columns.get_level_values(0) else data.xs("Close", level=0, axis=1)
                else:
                    closes = data[["Close"]]

                returns = closes.pct_change().dropna()
                if len(returns) >= lookback and symbol in returns.columns:
                    recent = returns.iloc[-lookback:]
                    correlated_count = 0
                    correlated_with = []
                    for existing_sym in open_symbols:
                        if existing_sym in recent.columns:
                            corr_val = recent[symbol].corr(recent[existing_sym])
                            if not np.isnan(corr_val) and corr_val >= threshold:
                                correlated_count += 1
                                correlated_with.append(existing_sym)
                    if correlated_count >= max_corr:
                        plan["skips"].append({
                            "symbol": symbol, "reason": "correlation_cap",
                            "correlated_count": correlated_count,
                            "correlated_with": correlated_with,
                            "threshold": threshold,
                        })
                        corr_blocked = True
            except Exception:
                # Fail open on data error if configured
                if not corr_cfg.get("fail_open_on_data_error", True):
                    plan["skips"].append({"symbol": symbol, "reason": "correlation_data_error"})
                    corr_blocked = True

            if corr_blocked:
                continue

        trigger_price = candidate["trigger_price"]
        limit_price = candidate["limit_price"]
        stop_price = candidate["stop_price"]
        r_per_share = candidate["r_per_share"]
        atr = candidate["atr14"]

        # Gap-up filter: skip if current price already too far above trigger
        gap_max = strategy.get("filters", {}).get("gap_up_max_pct", 0.03)
        current_price = candidate.get("close", trigger_price)
        if current_price > trigger_price * (1 + gap_max):
            plan["skips"].append({"symbol": symbol, "reason": "gap_too_extended",
                                  "current": round(current_price, 2), "trigger": round(trigger_price, 2),
                                  "gap_pct": round((current_price / trigger_price - 1) * 100, 2)})
            continue

        if stop_price <= 0 or stop_price >= trigger_price:
            plan["skips"].append({"symbol": symbol, "reason": "invalid_stop"})
            continue

        # Position sizing
        qty = risk_position_size(equity, risk_per_trade, trigger_price, stop_price, max_alloc)
        if qty <= 0:
            plan["skips"].append({"symbol": symbol, "reason": "qty_zero"})
            continue

        # Volatility-targeted sizing scalar
        vol_sizing = strategy.get("volatility_sizing", {})
        vol_scalar = 1.0
        if vol_sizing.get("enabled", False) and atr > 0:
            atr_pct = atr / trigger_price
            for bucket in vol_sizing.get("atr_pct_buckets", []):
                if atr_pct <= bucket["max"]:
                    vol_scalar = bucket["scalar"]
                    break
            qty = max(1, int(qty * vol_scalar))

        # Portfolio risk check
        trade_risk = r_per_share * qty
        if (current_risk + trade_risk) / equity > max_portfolio_risk:
            plan["skips"].append({"symbol": symbol, "reason": "max_portfolio_risk"})
            continue

        # Cash check
        required_cash = qty * limit_price * 1.005
        if cash - required_cash < reserve_cash:
            plan["skips"].append({"symbol": symbol, "reason": "cash_reserve"})
            continue

        # Submit order
        try:
            if dry_run:
                response = {"id": "dry_run", "_client_order_id": generate_client_order_id(symbol, "buy"), "legs": []}
                print(f"  [DRY RUN] Would submit: {symbol} x{qty} trigger=${trigger_price:.2f} stop=${stop_price:.2f}")
            else:
                response = submit_entry_order(symbol, qty, trigger_price, limit_price, stop_price)
        except Exception as e:
            plan["skips"].append({"symbol": symbol, "reason": "order_failed", "error": str(e)})
            send_alert(f"🚨 Growth order FAILED: {symbol}: {e}", level="error")
            continue

        if response.get("_duplicate"):
            plan["skips"].append({"symbol": symbol, "reason": "duplicate_order"})
            blocked_symbols.add(symbol)
            continue

        plan["orders"].append({
            "symbol": symbol,
            "qty": qty,
            "setup_type": candidate["setup_type"],
            "score": candidate["score"],
            "trigger": round(trigger_price, 2),
            "limit": round(limit_price, 2),
            "stop": round(stop_price, 2),
            "r_per_share": round(r_per_share, 2),
            "risk_dollars": round(trade_risk, 2),
            "vol_scalar": vol_scalar,
            "rel_volume": candidate.get("rel_volume", 0),
            "order_id": response.get("id"),
            "client_order_id": response.get("_client_order_id"),
        })

        # Persist tracking
        tracking = load_tracking()
        tracking[symbol] = {
            "planned_entry": round(trigger_price, 2),
            "initial_stop": round(stop_price, 2),
            "current_stop": round(stop_price, 2),
            "r_per_share": round(r_per_share, 2),
            "atr14_at_entry": atr,
            "atr_pct_at_entry": round(atr / trigger_price, 4) if trigger_price > 0 else 0,
            "setup_type": candidate["setup_type"],
            "phase": "pending",
            "bars_held": 0,
            "best_price": trigger_price,
            "best_gain_r": 0.0,
            "regime_mode_at_entry": regime_mode,
            "order_id": response.get("id"),
            "client_order_id": response.get("_client_order_id"),
            "entry_date": today_str(),
            "limit_price": round(limit_price, 2),
            "trigger_price": round(trigger_price, 2),
            "candidate_score": candidate["score"],
            "candidate_notes": candidate.get("notes", []),
            "setup_high": candidate.get("setup_high"),
            "setup_low": candidate.get("setup_low"),
            "vol_scalar": vol_scalar,
            "rel_volume": candidate.get("rel_volume", 0),
            "sector": candidate.get("sector", "unknown"),
            "rs_3m": candidate.get("rs_3m", 0),
            "rs_6m": candidate.get("rs_6m", 0),
        }
        # Find the sell stop leg explicitly (not just legs[0])
        legs = response.get("legs", [])
        for leg in legs:
            if leg.get("side") == "sell" and leg.get("type") in ("stop", "stop_limit"):
                tracking[symbol]["exit_order_id"] = leg.get("id")
                tracking[symbol]["exit_order_type"] = "stop_initial"
                break
        save_tracking(tracking)

        send_alert(
            f"📈 GROWTH ORDER: {symbol} x{qty} | {candidate['setup_type']} | "
            f"trigger ${trigger_price:.2f} stop ${stop_price:.2f} | score={candidate['score']}",
            level="trade"
        )

        cash -= required_cash
        current_risk += trade_risk
        remaining_slots -= 1
        blocked_symbols.add(symbol)

    save_json(order_plan_path, plan)
    save_json(STATE_DIR / "last_orders_growth.json", plan["orders"])
    write_heartbeat("trade_growth", "ok", {
        "bot_name": "growth",
        "orders": len(plan["orders"]),
        "skipped": len(plan["skips"]),
        "cancelled_stale": len(cancelled_stale),
    })
    print(f"Growth Trade: {len(plan['orders'])} orders, {len(plan['skips'])} skipped")


if __name__ == "__main__":
    import sys as _sys
    DRY_RUN = "--dry-run" in _sys.argv
    main(dry_run=DRY_RUN)

