"""
Research script v2: scans universe for confirmation-based pullback entries.

Regime: SPY and QQQ above both 50 and 200 SMA + breadth filter.
Entry: Requires confirmed pullback (2-12 days) + bullish candle pattern + breakout trigger.
Ranking: Relative strength vs SPY over 6 months.

All decisions use prior-day daily close bars only.
"""
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

from common import STATE_DIR, load_strategy, load_watchlist, save_json, today_str, write_heartbeat, fetch_alpaca_bars


def add_indicators(df, strategy):
    df = df.copy()
    df["sma20"] = df["Close"].rolling(strategy["sma_fast"]).mean()
    df["sma50"] = df["Close"].rolling(strategy["sma_mid"]).mean()
    df["sma200"] = df["Close"].rolling(strategy["sma_slow"]).mean()
    tr = pd.concat([
        (df["High"] - df["Low"]),
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"] - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(strategy["atr_period"]).mean()
    df["avg_volume_20"] = df["Volume"].rolling(20).mean()
    df["avg_volume_10"] = df["Volume"].rolling(10).mean()
    df["recent_volume_3"] = df["Volume"].rolling(3).mean()
    # For pullback detection
    df["high_20d"] = df["High"].rolling(20).max()
    return df


def compute_breadth(strategy):
    """Compute breadth proxy using RSP (equal-weight S&P 500 ETF).
    Maps RSP's distance from its 50 SMA to a 0-100 score.
    NOTE: This is a proxy, not true constituent-level breadth. RSP being above its
    50 SMA correlates with broad market health because equal-weighting reflects
    the average stock better than cap-weighted SPY."""
    try:
        rsp = yf.download(strategy["breadth_proxy"], period="3mo", interval="1d",
                          auto_adjust=False, progress=False)
        if rsp.empty:
            return 50.0  # Default neutral if data unavailable
        rsp["sma50"] = rsp["Close"].rolling(50).mean()
        last = rsp.iloc[-1]
        # Simple proxy: RSP above its 50 SMA = broad market healthy
        # Scale it: how far above/below as a percentage, mapped to 0-100 breadth
        if pd.isna(last["sma50"]):
            return 50.0
        pct_above = (float(last["Close"]) / float(last["sma50"]) - 1) * 100
        # Map: +5% above = ~80 breadth, 0% = ~50, -5% = ~20
        breadth = max(0, min(100, 50 + pct_above * 6))
        return round(breadth, 1)
    except Exception:
        return 50.0


def compute_relative_strength(df, spy_df, lookback):
    """Compute relative strength vs SPY over lookback period."""
    if len(df) < lookback or len(spy_df) < lookback:
        return np.nan
    symbol_return = (df["Close"].iloc[-1] / df["Close"].iloc[-lookback]) - 1
    spy_return = (spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-lookback]) - 1
    return float(symbol_return - spy_return)


def detect_pullback(df, strategy):
    """Detect if stock is in a valid pullback from recent high.
    Pullback length configured by pullback_days_min/max in strategy.json.
    Returns (is_valid, pullback_days, pullback_low)."""
    if len(df) < 20:
        return False, 0, np.nan

    min_days = strategy["pullback_days_min"]
    max_days = strategy["pullback_days_max"]

    # Find days since the 20-day high
    recent = df.tail(max_days + 1)
    high_20d = float(df["high_20d"].iloc[-1])
    last_close = float(df["Close"].iloc[-1])

    # Count consecutive days below the 20-day high
    pullback_days = 0
    pullback_low = float('inf')
    for i in range(1, min(len(recent), max_days + 1)):
        bar = recent.iloc[-(i + 1)]
        if float(bar["High"]) >= high_20d * 0.998:  # Within 0.2% of high = not pulling back
            break
        pullback_days += 1
        pullback_low = min(pullback_low, float(bar["Low"]))

    if pullback_days < min_days or pullback_days > max_days:
        return False, pullback_days, np.nan

    # Pullback low must hold above 50 SMA
    sma50 = float(df["sma50"].iloc[-1])
    if strategy.get("pullback_low_must_hold_sma50", True) and pullback_low < sma50:
        return False, pullback_days, np.nan

    return True, pullback_days, pullback_low


def detect_confirmation_candle(df, strategy):
    """Detect hammer, bullish engulfing, or morning star on the last bar.
    Returns (pattern_name, candle_high, candle_low) or (None, None, None)."""
    if len(df) < 3:
        return None, None, None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    o = float(last["Open"])
    h = float(last["High"])
    l = float(last["Low"])
    c = float(last["Close"])
    body = abs(c - o)
    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l
    candle_range = h - l

    if candle_range == 0:
        return None, None, None

    # Hammer: small body in upper third, long lower wick >= 2x body
    if lower_wick >= 2 * body and upper_wick < body and c >= o:
        # Body should be in upper portion of range
        if (min(c, o) - l) >= 0.6 * candle_range:
            return "hammer", h, l

    # Bullish engulfing: current green candle body engulfs prior red candle body
    prev_o = float(prev["Open"])
    prev_c = float(prev["Close"])
    if prev_c < prev_o and c > o:  # Prior red, current green
        if c > prev_o and o < prev_c:  # Current body engulfs prior body
            return "bullish_engulfing", h, l

    # Morning star: 3-bar reversal pattern
    if len(df) >= 3:
        bar1 = df.iloc[-3]  # First: big red candle
        bar2 = df.iloc[-2]  # Second: small body (star)
        bar3 = last         # Third: big green candle (current)

        b1_o, b1_c = float(bar1["Open"]), float(bar1["Close"])
        b2_o, b2_c, b2_l = float(bar2["Open"]), float(bar2["Close"]), float(bar2["Low"])
        b3_o, b3_c = o, c

        b1_body = abs(b1_c - b1_o)
        b2_body = abs(b2_c - b2_o)
        b3_body = abs(b3_c - b3_o)

        # Bar1: bearish with decent body, Bar2: small body, Bar3: bullish closes above bar1 midpoint
        if (b1_c < b1_o and b1_body > 0
                and b2_body < b1_body * 0.4
                and b3_c > b3_o and b3_body > 0
                and b3_c > (b1_o + b1_c) / 2):
            return "morning_star", h, min(l, b2_l)

    return None, None, None


def check_earnings_nearby(symbol, blackout_days):
    """Check if earnings are within blackout_days. Returns True if too close."""
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return False
        if isinstance(cal, dict):
            earnings_date = cal.get("Earnings Date")
            if earnings_date:
                if isinstance(earnings_date, list):
                    earnings_date = earnings_date[0]
                if hasattr(earnings_date, 'date'):
                    days_until = (earnings_date.date() - datetime.now().date()).days
                else:
                    days_until = (pd.Timestamp(earnings_date).date() - datetime.now().date()).days
                return 0 <= days_until <= blackout_days
        elif isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.index:
                earnings_date = cal.loc["Earnings Date"].iloc[0]
                days_until = (pd.Timestamp(earnings_date).date() - datetime.now().date()).days
                return 0 <= days_until <= blackout_days
    except Exception:
        pass
    return False


def fetch_history(symbols, max_retries=3):
    """Download history — try Alpaca first for consistency, fall back to yfinance.
    Uses Alpaca as primary source (same as broker), yfinance only for symbols
    Alpaca can't provide."""
    alpaca_data = {}
    missing_symbols = []

    # Try Alpaca for each symbol (same data source as execution)
    for symbol in symbols:
        df = fetch_alpaca_bars(symbol, timeframe="1Day", limit=500)
        if not df.empty:
            alpaca_data[symbol] = df
        else:
            missing_symbols.append(symbol)

    # Fall back to yfinance only for symbols Alpaca couldn't provide
    yf_data = {}
    if missing_symbols:
        for attempt in range(max_retries):
            try:
                raw = yf.download(missing_symbols, period="1y", interval="1d",
                                  auto_adjust=False, progress=False,
                                  group_by="ticker", threads=False)
                if raw is not None and not raw.empty:
                    if isinstance(raw.columns, pd.MultiIndex):
                        for sym in missing_symbols:
                            try:
                                df = raw[sym].dropna()
                                if not df.empty:
                                    yf_data[sym] = df
                            except KeyError:
                                pass
                    else:
                        # Single symbol case
                        yf_data[missing_symbols[0]] = raw.dropna()
                    break
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"  WARNING: yfinance failed after {max_retries} attempts: {e}")
                import time
                time.sleep(2 ** attempt)

    # Combine all data into a single MultiIndex DataFrame
    all_data = {**alpaca_data, **yf_data}
    if not all_data:
        raise RuntimeError("No data available from any source")

    combined = pd.concat(all_data, axis=1)
    combined.columns = pd.MultiIndex.from_tuples(
        [(sym, col) for sym in all_data for col in all_data[sym].columns]
    )

    alpaca_count = len(alpaca_data)
    yf_count = len(yf_data)
    failed = len(symbols) - alpaca_count - yf_count
    print(f"  [data: Alpaca={alpaca_count}, yfinance={yf_count}, failed={failed} of {len(symbols)} symbols]")
    return combined


def get_symbol_frame(data, symbol):
    if isinstance(data.columns, pd.MultiIndex):
        df = data[symbol].dropna().copy()
    else:
        df = data.dropna().copy()
    return df


def main():
    strategy = load_strategy()
    symbols = load_watchlist()
    benchmarks = strategy["benchmarks"]
    all_symbols = sorted(set(symbols + benchmarks + [strategy["breadth_proxy"]]))
    raw = fetch_history(all_symbols)

    # --- Breadth calculation ---
    breadth = compute_breadth(strategy)

    # --- VIX regime check ---
    vix_level = None
    vix_regime = "normal"
    try:
        vix_data = yf.download("^VIX", period="5d", interval="1d",
                                auto_adjust=False, progress=False)
        if not vix_data.empty:
            close_col = vix_data["Close"]
            if isinstance(close_col, pd.DataFrame):
                close_col = close_col.iloc[:, 0]
            vix_level = round(float(close_col.iloc[-1]), 2)
            if vix_level > 30:
                vix_regime = "high"  # Reduce risk, wider stops
            elif vix_level > 20:
                vix_regime = "elevated"  # Slightly cautious
    except Exception:
        vix_regime = "elevated"  # Data unavailable → assume cautious

    # Determine breadth mode
    breadth_modes = strategy["breadth_modes"]
    if breadth >= breadth_modes["full_risk"]["min_breadth"]:
        breadth_mode = "full_risk"
        risk_per_trade = breadth_modes["full_risk"]["risk_per_trade"]
        max_positions = breadth_modes["full_risk"]["max_open_positions"]
    elif breadth >= breadth_modes["reduced_risk"]["min_breadth"]:
        breadth_mode = "reduced_risk"
        risk_per_trade = breadth_modes["reduced_risk"]["risk_per_trade"]
        max_positions = breadth_modes["reduced_risk"]["max_open_positions"]
    else:
        breadth_mode = "risk_off"
        risk_per_trade = 0
        max_positions = 0

    # VIX override: high VIX forces reduced risk regardless of breadth
    if vix_regime == "high" and breadth_mode == "full_risk":
        breadth_mode = "reduced_risk"
        risk_per_trade = breadth_modes["reduced_risk"]["risk_per_trade"]
        max_positions = breadth_modes["reduced_risk"]["max_open_positions"]

    # --- Regime check (SPY & QQQ above BOTH 50 and 200 SMA) ---
    spy_df = get_symbol_frame(raw, "SPY")
    if not spy_df.empty:
        spy_df = add_indicators(spy_df, strategy)

    regime = {}
    for symbol in benchmarks:
        df = get_symbol_frame(raw, symbol)
        if df.empty:
            print(f"WARNING: No data for benchmark {symbol}, marking risk-off")
            regime[symbol] = {"close": 0, "sma50": 0, "sma200": 0, "risk_on": False}
            continue
        df = add_indicators(df, strategy)
        last = df.iloc[-1]
        close_val = float(last["Close"])
        sma50_val = float(last["sma50"])
        sma200_val = float(last["sma200"])
        regime[symbol] = {
            "close": round(close_val, 2),
            "sma50": round(sma50_val, 2),
            "sma200": round(sma200_val, 2),
            "risk_on": bool(close_val > sma50_val and close_val > sma200_val),
        }

    market_risk_on = all(regime[s]["risk_on"] for s in benchmarks)
    allow_new_entries = market_risk_on and breadth_mode != "risk_off"

    # --- Scan candidates ---
    candidates = []
    rejected = []
    rs_lookback = strategy["relative_strength_lookback_days"]
    # Build ETF set from watchlist data (data-driven, not hardcoded)
    try:
        from common import load_json, CONFIG_DIR
        watchlist_data = load_json(CONFIG_DIR / "watchlist.json")
        etf_symbols = {s["ticker"] for s in watchlist_data.get("symbols", []) if s.get("type") == "ETF"}
    except Exception:
        etf_symbols = {"SPY", "QQQ", "IWM", "MDY", "XLK", "SMH", "XLI", "XLF", "XLV", "XLE", "XLC", "XLY", "XLB", "XLP", "RSP"}

    # Compute RS for all symbols to determine percentile cutoff
    rs_scores = {}
    for symbol in symbols:
        df = get_symbol_frame(raw, symbol)
        if df.empty:
            continue
        df = add_indicators(df, strategy)
        rs = compute_relative_strength(df, spy_df, rs_lookback) if not spy_df.empty else 0.0
        if not pd.isna(rs):
            rs_scores[symbol] = rs

    # Determine RS percentile cutoff from config (default top 20%)
    rs_percentile = strategy.get("min_relative_strength_percentile", 80)
    if rs_scores:
        rs_values = sorted(rs_scores.values(), reverse=True)
        import math as _math
        cutoff_idx = _math.ceil(len(rs_values) * (100 - rs_percentile) / 100) - 1
        cutoff_idx = max(0, min(cutoff_idx, len(rs_values) - 1))
        rs_cutoff = rs_values[cutoff_idx]
    else:
        rs_cutoff = -999

    for symbol in symbols:
        df = get_symbol_frame(raw, symbol)
        if df.empty:
            rejected.append({"symbol": symbol, "reasons": ["no_data"]})
            continue
        df = add_indicators(df, strategy)
        last = df.iloc[-1]
        close = float(last["Close"])
        sma20 = float(last["sma20"])
        sma50 = float(last["sma50"])
        sma200 = float(last["sma200"])
        atr14 = float(last["atr14"])
        avg_volume_20 = float(last["avg_volume_20"])
        avg_dollar_vol = avg_volume_20 * close

        rs = rs_scores.get(symbol, np.nan)

        reasons = []

        # Basic filters
        if close < strategy["min_price"]:
            reasons.append("below_min_price")
        if avg_dollar_vol < strategy["min_avg_dollar_volume"]:
            reasons.append("below_min_dollar_volume")
        if not (close > sma50 > sma200):
            reasons.append("trend_filter_failed")
        if pd.isna(rs) or rs < rs_cutoff:
            reasons.append("relative_strength_too_low")
        if atr14 <= 0 or pd.isna(atr14):
            reasons.append("bad_atr")

        # Pullback detection
        is_pullback, pullback_days, pullback_low = detect_pullback(df, strategy)
        if not is_pullback:
            reasons.append("no_valid_pullback")

        # Distance from SMA20 check
        pct_from_sma20 = (close / sma20) - 1 if sma20 else np.nan
        if pd.isna(pct_from_sma20):
            reasons.append("no_sma20")
        elif pct_from_sma20 < strategy["pullback_min_distance_from_sma20_pct"]:
            reasons.append("too_far_below_sma20")
        elif pct_from_sma20 > strategy["pullback_max_distance_from_sma20_pct"]:
            reasons.append("too_far_above_sma20")

        # Pullback volume check — always report, even if other filters already failed
        avg_vol_10 = float(last["avg_volume_10"]) if not pd.isna(last["avg_volume_10"]) else avg_volume_20
        recent_vol = float(last["recent_volume_3"]) if not pd.isna(last["recent_volume_3"]) else avg_volume_20
        vol_ratio = recent_vol / avg_vol_10 if avg_vol_10 > 0 else 1.0
        if vol_ratio > strategy["pullback_volume_ratio_max"]:
            reasons.append("pullback_volume_too_high")

        # Confirmation candle detection — only accept patterns listed in config
        allowed_patterns = strategy.get("confirmation_candles", ["hammer", "bullish_engulfing", "morning_star"])
        pattern, candle_high, candle_low = detect_confirmation_candle(df, strategy)
        if pattern is None or pattern not in allowed_patterns:
            reasons.append("no_confirmation_candle")

        # Earnings blackout (stocks only)
        if not reasons and symbol not in etf_symbols:
            if check_earnings_nearby(symbol, strategy["earnings_blackout_days"]):
                reasons.append("earnings_blackout")

        record = {
            "symbol": symbol,
            "close": round(close, 2),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "atr14": round(atr14, 2),
            "avg_dollar_volume": int(avg_dollar_vol),
            "pct_from_sma20": round(float(pct_from_sma20), 4) if not pd.isna(pct_from_sma20) else None,
            "relative_strength": round(rs, 4) if not pd.isna(rs) else None,
            "volume_ratio": round(vol_ratio, 2),
            "pullback_days": pullback_days,
            "pullback_low": round(pullback_low, 2) if not pd.isna(pullback_low) and pullback_low != float('inf') else None,
            "confirmation_pattern": pattern,
            "confirmation_candle_high": round(candle_high, 2) if candle_high else None,
            "confirmation_candle_low": round(candle_low, 2) if candle_low else None,
        }

        if reasons:
            record["reasons"] = reasons
            rejected.append(record)
        else:
            # Score: RS dominates, tighter pullback is bonus
            rs_score = (rs * 100) if not pd.isna(rs) else 0
            record["score"] = round(float(rs_score), 4)
            candidates.append(record)

    # Sort by score descending (strongest relative strength first)
    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)

    payload = {
        "date": today_str(),
        "market_risk_on": market_risk_on,
        "allow_new_entries": allow_new_entries,
        "breadth_proxy_score": breadth,
        "breadth_proxy_note": "RSP distance from 50 SMA mapped to 0-100 scale, not true constituent breadth",
        "vix_level": vix_level,
        "vix_regime": vix_regime,
        "breadth_mode": breadth_mode,
        "risk_per_trade": risk_per_trade,
        "max_positions": max_positions,
        "regime": regime,
        "candidates": candidates,
        "rejected": rejected,
    }
    save_json(STATE_DIR / "candidates.json", payload)
    pd.DataFrame(candidates).to_csv(STATE_DIR / "candidates.csv", index=False)
    pd.DataFrame(rejected).to_csv(STATE_DIR / "rejected.csv", index=False)
    write_heartbeat("research", "ok", {
        "market_risk_on": market_risk_on,
        "breadth_proxy_score": breadth,
        "breadth_mode": breadth_mode,
        "candidate_count": len(candidates),
    })
    print(f"Research v2: {len(candidates)} candidates, {len(rejected)} rejected | "
          f"regime={'ON' if market_risk_on else 'OFF'} | breadth_proxy={breadth}% ({breadth_mode}) | "
          f"VIX={vix_level} ({vix_regime})")


if __name__ == "__main__":
    main()
