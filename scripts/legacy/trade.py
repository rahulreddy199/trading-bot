"""
Trade script v2: places stop-limit buy orders on confirmed pullback setups.

Entry: Stop-limit buy triggered above confirmation candle high.
Stop: Wider of (candle_low - 0.1*ATR) or (entry - 2*ATR).
No take-profit leg — manage.py handles exits via breakeven + trailing stop.
Cancels stale unfilled orders older than 2 days.
"""
import json
import hashlib
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from common import (
    ACTIVE_ORDER_STATUSES,
    MARKET_TZ,
    STATE_DIR,
    alpaca_get,
    alpaca_post,
    cancel_order,
    cancel_order_and_verify,
    enforce_live_guardrails,
    fetch_alpaca_bars,
    get_account,
    get_clock,
    get_positions,
    load_strategy,
    load_watchlist_with_sectors,
    now_iso,
    risk_position_size,
    save_json,
    send_alert,
    today_str,
    write_heartbeat,
)

_corr_returns_cache = {}
_corr_matrix_cache = {}


def load_tracking():
    path = STATE_DIR / "position_tracking.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_tracking(tracking):
    save_json(STATE_DIR / "position_tracking.json", tracking)


def fetch_return_series(symbol, lookback_days):
    """Fetch daily return series for a symbol, with caching and yfinance fallback."""
    cache_key = (symbol, lookback_days)
    if cache_key in _corr_returns_cache:
        return _corr_returns_cache[cache_key]

    series = pd.Series(dtype=float, name=symbol)

    try:
        df = fetch_alpaca_bars(symbol, timeframe="1Day", limit=lookback_days + 20)
        if not df.empty:
            close = df["Close"] if "Close" in df.columns else df["close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = pd.to_numeric(close, errors="coerce").dropna()
            if len(close) >= lookback_days:
                series = close.pct_change().dropna().tail(lookback_days)
                series.name = symbol
    except Exception:
        pass

    if series.empty or len(series) < max(10, int(lookback_days * 0.8)):
        try:
            df = yf.download(
                symbol,
                period="6mo",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if not df.empty:
                close = df["Close"]
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                close = pd.to_numeric(close, errors="coerce").dropna()
                if len(close) >= lookback_days:
                    series = close.pct_change().dropna().tail(lookback_days)
                    series.name = symbol
        except Exception:
            pass

    _corr_returns_cache[cache_key] = series
    return series


def build_correlation_matrix(symbols, lookback_days=40, min_overlap_ratio=0.8):
    """Compute rolling daily-return correlation matrix for given symbols.
    Returns a DataFrame (sym x sym) of pairwise correlations.
    Aligns returns by date index to prevent misalignment across symbols."""
    symbols = sorted(set(symbols))
    if len(symbols) < 2:
        return pd.DataFrame()

    cache_key = (tuple(symbols), lookback_days, min_overlap_ratio)
    if cache_key in _corr_matrix_cache:
        return _corr_matrix_cache[cache_key]

    series_list = []
    for sym in symbols:
        ret = fetch_return_series(sym, lookback_days)
        if not ret.empty:
            series_list.append(ret.rename(sym))

    if len(series_list) < 2:
        corr = pd.DataFrame()
        _corr_matrix_cache[cache_key] = corr
        return corr

    # Align by date index (inner join) to prevent positional misalignment
    ret_df = pd.concat(series_list, axis=1, join="inner").dropna()
    min_required = max(10, int(lookback_days * min_overlap_ratio))
    if ret_df.shape[0] < min_required or ret_df.shape[1] < 2:
        corr = pd.DataFrame()
        _corr_matrix_cache[cache_key] = corr
        return corr

    corr = ret_df.corr()
    _corr_matrix_cache[cache_key] = corr
    return corr


def check_correlation_cap(candidate_symbol, open_symbols, pending_symbols, strategy):
    """Check if candidate is too correlated with existing/pending positions.
    Returns (blocked: bool, reason: str or None, details: dict).
    Fail-open vs fail-closed behavior is configurable via strategy.json."""
    corr_config = strategy.get("correlation_cap", {})
    if not corr_config.get("enabled", False):
        return False, None, {}

    threshold = float(corr_config.get("threshold", 0.80))
    max_correlated = int(corr_config.get("max_correlated_positions", 2))
    lookback = int(corr_config.get("lookback_days", 40))
    fail_open = bool(corr_config.get("fail_open_on_data_error", True))

    existing = sorted(set(open_symbols) | set(pending_symbols))
    if not existing:
        return False, None, {}

    all_symbols = sorted(set(existing + [candidate_symbol]))

    try:
        corr_matrix = build_correlation_matrix(all_symbols, lookback_days=lookback)
    except Exception as e:
        if fail_open:
            return False, None, {"correlation_warning": f"data_error_allow:{str(e)}"}
        return True, "correlation_data_error", {"error": str(e)}

    if corr_matrix.empty or candidate_symbol not in corr_matrix.index:
        if fail_open:
            return False, None, {"correlation_warning": "insufficient_data_allow"}
        return True, "correlation_data_insufficient", {
            "lookback_days": lookback,
            "candidate": candidate_symbol,
        }

    highly_correlated = []
    for sym in existing:
        if sym not in corr_matrix.columns:
            continue
        corr_val = corr_matrix.loc[candidate_symbol, sym]
        if pd.notna(corr_val) and float(corr_val) >= threshold:
            highly_correlated.append({
                "symbol": sym,
                "correlation": round(float(corr_val), 3),
            })

    highly_correlated.sort(key=lambda x: x["correlation"], reverse=True)

    if len(highly_correlated) >= max_correlated:
        return True, "correlation_cap_exceeded", {
            "threshold": threshold,
            "lookback_days": lookback,
            "max_correlated_positions": max_correlated,
            "correlated_count": len(highly_correlated),
            "correlated_with": highly_correlated,
        }

    return False, None, {
        "correlation_checked": True,
        "correlated_count": len(highly_correlated),
        "correlated_with": highly_correlated,
    }


def get_open_buy_orders():
    """Return open buy orders."""
    return alpaca_get("/v2/orders", params={"status": "open", "side": "buy", "limit": 100})



def cancel_stale_orders(stale_days=2):
    """Cancel buy orders older than stale_days. Returns list of cancelled symbols."""
    orders = get_open_buy_orders()
    cutoff = datetime.now(MARKET_TZ) - timedelta(days=stale_days)
    cancelled = []
    for order in orders:
        submitted = order.get("submitted_at", "")
        if submitted:
            try:
                submitted_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                if submitted_dt < cutoff:
                    verified = cancel_order_and_verify(order["id"])
                    cancelled.append({"symbol": order["symbol"], "order_id": order["id"], "age_days": (datetime.now(MARKET_TZ) - submitted_dt).days, "cancel_verified": verified})
            except Exception:
                pass
    return cancelled



def generate_client_order_id(symbol, side="buy"):
    """Generate a deterministic, trackable client_order_id.

    Uses date + symbol + side as the idempotency base. If trade.py's own
    idempotency guard (order_plan.json check) is bypassed, the broker will
    reject a duplicate client_order_id for the same day, preventing double orders.
    """
    date_str = datetime.now(MARKET_TZ).strftime("%Y%m%d")
    # Deterministic hash from date + symbol + side for idempotency
    key = f"{date_str}_{side}_{symbol}"
    hash_suffix = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"bot_{side}_{symbol}_{date_str}_{hash_suffix}"


def check_duplicate_order(client_order_id):
    """Check if an active order with this client_order_id already exists at the broker.
    Only considers open/working orders as duplicates — canceled/rejected/expired are not."""
    try:
        existing = alpaca_get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        if isinstance(existing, dict) and existing.get("id"):
            status = existing.get("status", "")
            if status in ACTIVE_ORDER_STATUSES:
                return existing
    except Exception:
        pass
    return None


def submit_stop_limit_buy_with_stop(symbol, qty, trigger_price, limit_price, stop_price):
    """Submit a one-triggers-other (OTO) order: stop-limit buy entry triggers an attached stop-loss sell.
    Uses deterministic client_order_id for idempotency — rejects if same-day order already exists."""
    client_id = generate_client_order_id(symbol, "buy")

    # Idempotency: check if this exact order already exists at broker
    existing = check_duplicate_order(client_id)
    if existing:
        status = existing.get("status", "unknown")
        print(f"  ⚠️ Duplicate order detected for {symbol} (client_id={client_id}, status={status}) — skipping")
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
        "time_in_force": "day",
        "order_class": "oto",
        "stop_loss": {"stop_price": round(stop_price, 2)},
        "client_order_id": client_id,
    }
    resp = alpaca_post("/v2/orders", payload)
    resp["_client_order_id"] = client_id
    return resp


def main():
    enforce_live_guardrails()

    # Kill switch: touch state/KILL_SWITCH to halt all new entries instantly
    kill_switch_path = STATE_DIR / "KILL_SWITCH"
    if kill_switch_path.exists():
        print("🛑 KILL SWITCH ACTIVE — no new orders. Remove state/KILL_SWITCH to resume.")
        send_alert("🛑 Kill switch active — all new entries blocked", level="error")
        return

    # Circuit breaker: check drawdown directly (not just in orchestrator)
    try:
        eq_curve_path = STATE_DIR / "equity_curve.json"
        guardrails_path = STATE_DIR.parent / "config" / "guardrails.json"
        if eq_curve_path.exists() and guardrails_path.exists():
            eq_curve = json.loads(eq_curve_path.read_text())
            cb = json.loads(guardrails_path.read_text()).get("drawdown_circuit_breaker", {})
            max_dd = cb.get("max_drawdown_pct", 15.0)
            equities = [e.get("equity", 0) for e in eq_curve if e.get("equity")]
            if equities and len(equities) > 1:
                peak = max(equities)
                current = equities[-1]
                dd_pct = (peak - current) / peak * 100 if peak > 0 else 0
                if dd_pct >= max_dd:
                    print(f"🛑 CIRCUIT BREAKER: {dd_pct:.1f}% drawdown exceeds {max_dd}% limit. No new trades.")
                    send_alert(f"🛑 Circuit breaker: {dd_pct:.1f}% drawdown. Entries halted.", level="error")
                    return
    except Exception:
        pass  # Don't block trading if circuit breaker check itself fails

    strategy = load_strategy()
    candidates_path = STATE_DIR / "candidates.json"
    if not candidates_path.exists():
        raise RuntimeError("Missing state/candidates.json. Run research.py first.")

    payload = json.loads(candidates_path.read_text())

    # Dependency check: ensure research ran today (not stale data)
    research_date = payload.get("date", "")
    if research_date != datetime.now(MARKET_TZ).strftime("%Y-%m-%d"):
        print(f"Trade skipped: candidates.json is stale (dated {research_date}, today is {datetime.now(MARKET_TZ).strftime('%Y-%m-%d')})")
        send_alert(f"⚠️ Trade skipped: research data is stale ({research_date})", level="warning")
        return

    # Idempotency guard: prevent duplicate runs on the same day
    order_plan_path = STATE_DIR / "order_plan.json"
    if order_plan_path.exists():
        prior = json.loads(order_plan_path.read_text())
        prior_ts = prior.get("timestamp", "")
        if prior_ts.startswith(datetime.now(MARKET_TZ).strftime("%Y-%m-%d")) and prior.get("orders"):
            print(f"Trade skipped: already executed today ({len(prior['orders'])} orders placed at {prior_ts})")
            send_alert(f"⚠️ Trade skipped: duplicate run blocked (already ran at {prior_ts})", level="warning")
            return

    clock = get_clock()
    account = get_account()
    positions = get_positions()

    # Block orders when market is closed
    if not clock.get("is_open", False):
        plan = {
            "timestamp": now_iso(),
            "market_risk_on": payload.get("market_risk_on"),
            "breadth_mode": payload.get("breadth_mode"),
            "orders": [],
            "skips": [{"reason": "market_closed"}],
            "cancelled_stale": [],
        }
        save_json(STATE_DIR / "order_plan.json", plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": "market_closed"})
        print("Trade skipped: market is closed")
        return

    equity = float(account["equity"])
    cash = float(account["cash"])
    allow_entries = payload.get("allow_new_entries", False)
    breadth_mode = payload.get("breadth_mode", "risk_off")
    risk_per_trade = payload.get("risk_per_trade", 0)
    max_positions = payload.get("max_positions", 0)

    open_symbols = {p["symbol"] for p in positions}
    pending_buy_orders = get_open_buy_orders()
    pending_buy_symbols = {o["symbol"] for o in pending_buy_orders}
    blocked_symbols = open_symbols | pending_buy_symbols
    remaining_slots = max(max_positions - len(open_symbols) - len(pending_buy_symbols), 0)

    # Cancel stale orders first
    stale_days = strategy.get("stale_order_cancel_days", 2)
    cancelled_stale = cancel_stale_orders(stale_days)

    # Re-fetch after stale cancellations so freed slots are available this run
    if cancelled_stale:
        pending_buy_orders = get_open_buy_orders()
        pending_buy_symbols = {o["symbol"] for o in pending_buy_orders}
        blocked_symbols = open_symbols | pending_buy_symbols
        remaining_slots = max(max_positions - len(open_symbols) - len(pending_buy_symbols), 0)

    plan = {
        "timestamp": now_iso(),
        "market_risk_on": payload.get("market_risk_on"),
        "breadth_mode": breadth_mode,
        "breadth": payload.get("breadth_proxy_score"),
        "risk_per_trade": risk_per_trade,
        "equity": equity,
        "cash": cash,
        "open_symbols": sorted(open_symbols),
        "pending_buy_symbols": sorted(pending_buy_symbols),
        "cancelled_stale": cancelled_stale,
        "orders": [],
        "skips": [],
    }

    if not allow_entries:
        reason = "regime_off" if not payload.get("market_risk_on") else f"breadth_mode_{breadth_mode}"
        plan["skips"].append({"reason": reason})
        save_json(STATE_DIR / "order_plan.json", plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": reason})
        print(f"Trade skipped: {reason}")
        return

    if remaining_slots <= 0:
        plan["skips"].append({"reason": "max_open_positions_reached"})
        save_json(STATE_DIR / "order_plan.json", plan)
        write_heartbeat("trade", "ok", {"orders": 0, "reason": "max_open_positions_reached"})
        print("Trade skipped: max positions reached")
        return

    # Determine cash reserve based on breadth mode
    if breadth_mode == "full_risk":
        reserve_cash = equity * strategy["cash_reserve_pct_full"]
    else:
        reserve_cash = equity * strategy["cash_reserve_pct_reduced"]

    max_alloc = strategy["max_alloc_fraction_per_symbol"]
    max_portfolio_risk = strategy.get("max_total_portfolio_risk_pct", 0.03)
    cash_buffer = strategy.get("cash_buffer_pct", 0.005)
    max_atr_pct = strategy.get("max_atr_percent", 0.06)
    sector_limits = strategy.get("sector_limits", {})
    symbol_sectors = load_watchlist_with_sectors()

    # Calculate current sector exposure from open positions
    sector_exposure = {}
    for p in positions:
        sym = p["symbol"]
        sector = symbol_sectors.get(sym, "Unknown")
        sector_exposure[sector] = sector_exposure.get(sector, 0) + float(p.get("market_value", 0))

    # Calculate current total open risk from existing positions
    tracking = load_tracking()
    current_total_risk = 0.0
    for sym in open_symbols:
        if sym in tracking and tracking[sym].get("r_per_share"):
            pos_qty = next((int(float(p["qty"])) for p in positions if p["symbol"] == sym), 0)
            current_total_risk += tracking[sym]["r_per_share"] * pos_qty

    for candidate in payload.get("candidates", []):
        symbol = candidate["symbol"]
        if remaining_slots <= 0:
            break
        if symbol in blocked_symbols:
            plan["skips"].append({"symbol": symbol, "reason": "already_open_or_pending"})
            continue

        candle_high = candidate.get("confirmation_candle_high")
        candle_low = candidate.get("confirmation_candle_low")
        atr = candidate.get("atr14")

        if not all([candle_high, candle_low, atr]):
            plan["skips"].append({"symbol": symbol, "reason": "missing_candle_or_atr_data"})
            continue

        # Entry prices — ATR-based buffers (adapts to each stock's volatility)
        trigger_buffer = strategy.get("entry_trigger_buffer_atr", 0.05)
        limit_buffer = strategy.get("entry_limit_buffer_atr", 0.15)

        trigger_price = candle_high + trigger_buffer * atr
        limit_price = trigger_price + limit_buffer * atr

        # Stop: wider of (candle_low - 0.1*ATR) or (entry - 2*ATR)
        stop_candle = candle_low - strategy.get("candle_low_stop_atr_buffer", 0.1) * atr
        stop_atr = trigger_price - strategy.get("atr_stop_multiplier", 2.0) * atr
        stop_price = min(stop_candle, stop_atr)

        if stop_price <= 0 or stop_price >= trigger_price:
            plan["skips"].append({"symbol": symbol, "reason": "invalid_stop"})
            continue

        # Volatility filter: reject if ATR is too high a % of price (too volatile)
        atr_pct = atr / candle_high if candle_high > 0 else 0
        if atr_pct > max_atr_pct:
            plan["skips"].append({"symbol": symbol, "reason": "atr_too_high", "atr_pct": round(atr_pct, 4)})
            continue

        r_per_share = trigger_price - stop_price

        # Position sizing
        qty = risk_position_size(equity, risk_per_trade, trigger_price, stop_price, max_alloc)
        if qty <= 0:
            plan["skips"].append({"symbol": symbol, "reason": "qty_zero"})
            continue

        # Portfolio-level risk check
        trade_risk_dollars = r_per_share * qty
        if (current_total_risk + trade_risk_dollars) / equity > max_portfolio_risk:
            plan["skips"].append({"symbol": symbol, "reason": "max_portfolio_risk_reached"})
            continue

        # Sector exposure check
        candidate_sector = symbol_sectors.get(symbol, "Unknown")
        sector_limit = sector_limits.get(candidate_sector, sector_limits.get("default", 0.30))
        current_sector_value = sector_exposure.get(candidate_sector, 0)
        new_position_value = qty * trigger_price
        if (current_sector_value + new_position_value) / equity > sector_limit:
            plan["skips"].append({"symbol": symbol, "reason": "sector_limit_reached", "sector": candidate_sector})
            continue

        # Correlation cap check
        corr_blocked, corr_reason, corr_details = check_correlation_cap(
            symbol, list(open_symbols), list(blocked_symbols - open_symbols), strategy
        )
        if corr_blocked:
            plan["skips"].append({"symbol": symbol, "reason": corr_reason, **corr_details})
            continue

        required_cash = qty * limit_price * (1 + cash_buffer)  # Add slippage buffer
        if cash - required_cash < reserve_cash:
            plan["skips"].append({"symbol": symbol, "reason": "cash_reserve_block"})
            continue

        # Submit order
        try:
            response = submit_stop_limit_buy_with_stop(symbol, qty, trigger_price, limit_price, stop_price)
        except Exception as e:
            plan["skips"].append({"symbol": symbol, "reason": "order_submit_failed", "error": str(e)})
            send_alert(f"🚨 Order FAILED for {symbol}: {e}", level="error")
            continue

        # Handle duplicate detection — do NOT mutate local accounting
        if response.get("_duplicate"):
            plan["skips"].append({
                "symbol": symbol,
                "reason": "duplicate_order_blocked",
                "client_order_id": response.get("_client_order_id"),
                "existing_order_id": response.get("id"),
                "existing_status": response.get("status"),
            })
            blocked_symbols.add(symbol)
            continue

        plan["orders"].append({
            "symbol": symbol,
            "qty": qty,
            "trigger": round(trigger_price, 2),
            "limit": round(limit_price, 2),
            "stop": round(stop_price, 2),
            "r_per_share": round(r_per_share, 2),
            "risk_dollars": round(r_per_share * qty, 2),
            "risk_pct_of_equity": round(r_per_share * qty / equity * 100, 3),
            "position_value": round(qty * trigger_price, 2),
            "position_pct_of_equity": round(qty * trigger_price / equity * 100, 2),
            "confirmation_pattern": candidate.get("confirmation_pattern"),
            "candidate_score": candidate.get("score"),
            "candidate_rs": candidate.get("relative_strength"),
            "candidate_atr": candidate.get("atr14"),
            "candidate_close": candidate.get("close"),
            "candidate_sma20": candidate.get("sma20"),
            "candidate_sma50": candidate.get("sma50"),
            "breadth_mode": breadth_mode,
            "alpaca_order_id": response.get("id"),
            "client_order_id": response.get("_client_order_id"),
            "status": response.get("status"),
            "correlation_checked": corr_details.get("correlation_checked", False),
            "correlated_count": corr_details.get("correlated_count", 0),
            "correlated_with": corr_details.get("correlated_with", []),
        })
        send_alert(
            f"📈 ORDER: {symbol} x{qty} | trigger ${trigger_price:.2f} limit ${limit_price:.2f} | "
            f"stop ${stop_price:.2f} | pattern: {candidate.get('confirmation_pattern')}",
            level="trade"
        )

        # Persist planned trade data for manage.py to use actual R
        tracking = load_tracking()
        tracking[symbol] = {
            "planned_entry": round(trigger_price, 2),
            "initial_stop": round(stop_price, 2),
            "r_per_share": round(r_per_share, 2),
            "atr14": atr,
            "sma50": candidate.get("sma50"),
            "phase": "pending",
            "bars_held": 0,
            "order_id": response.get("id"),
            "client_order_id": response.get("_client_order_id"),
            "confirmation_candle_low": candle_low,
            "entry_date": today_str(),
        }
        # Persist the OTO child stop order ID as the initial exit order
        legs = response.get("legs", [])
        if legs:
            child_stop = legs[0]
            tracking[symbol]["exit_order_id"] = child_stop.get("id")
            tracking[symbol]["exit_client_order_id"] = child_stop.get("client_order_id")
            tracking[symbol]["exit_order_type"] = "stop_initial"
        save_tracking(tracking)

        # Update running totals
        cash -= required_cash
        current_total_risk += trade_risk_dollars
        remaining_slots -= 1
        blocked_symbols.add(symbol)
        sector_exposure[candidate_sector] = sector_exposure.get(candidate_sector, 0) + new_position_value

    save_json(STATE_DIR / "order_plan.json", plan)
    save_json(STATE_DIR / "last_orders.json", plan["orders"])
    write_heartbeat("trade", "ok", {"orders": len(plan["orders"]), "cancelled_stale": len(cancelled_stale)})
    print(f"Trade v2: {len(plan['orders'])} orders placed, {len(plan['skips'])} skipped, {len(cancelled_stale)} stale cancelled")


if __name__ == "__main__":
    main()
