"""
Growth Bot V1 — Research script.

Scans the growth universe, ranks momentum leaders, detects setups:
  1. Breakout (20d/55d high)
  2. Continuation (shallow 1-3 bar pullback in strong trend)
  3. Shallow pullback (within 1.5 ATR of swing high)

Outputs state/candidates.json for trade.py.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    CONFIG_DIR,
    save_json,
    send_alert,
    write_heartbeat,
    now_iso,
    today_str,
    enforce_live_guardrails,
)


def load_growth_strategy():
    path = CONFIG_DIR / "strategy_growth.json"
    return json.loads(path.read_text())


def load_growth_watchlist():
    path = CONFIG_DIR / "watchlist_growth.json"
    data = json.loads(path.read_text())
    return [s["ticker"] for s in data["symbols"] if s.get("enabled", True)]


def download_data(symbols, period="1y"):
    """Download daily data for all symbols."""
    all_syms = sorted(set(symbols + ["SPY", "QQQ"]))
    print(f"  Downloading {len(all_syms)} symbols...")
    raw = yf.download(all_syms, period=period, interval="1d",
                      auto_adjust=True, progress=False, threads=False)
    return raw


def get_symbol_df(raw, symbol):
    if isinstance(raw.columns, pd.MultiIndex):
        if symbol in raw.columns.get_level_values(0):
            df = raw[symbol].dropna().copy()
        else:
            df = raw.xs(symbol, level=1, axis=1).dropna().copy() if symbol in raw.columns.get_level_values(1) else pd.DataFrame()
    else:
        df = raw.dropna().copy()
    return df


def add_indicators(df, strategy):
    """Add all growth bot indicators."""
    ind = strategy["indicators"]
    df = df.copy()

    df["ema10"] = df["Close"].ewm(span=ind["ema_fast"], adjust=False).mean()
    df["sma20"] = df["Close"].rolling(ind["sma_fast"]).mean()
    df["sma50"] = df["Close"].rolling(ind["sma_mid"]).mean()
    df["sma200"] = df["Close"].rolling(ind["sma_slow"]).mean()

    # ATR
    tr = pd.concat([
        (df["High"] - df["Low"]),
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"] - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(ind["atr_period"]).mean()

    # Highs
    df["high_20d"] = df["High"].rolling(ind["high_20d"]).max()
    df["high_55d"] = df["High"].rolling(ind["high_55d"]).max()

    # Volume
    df["avg_volume_20"] = df["Volume"].rolling(ind["volume_avg_period"]).mean()
    df["avg_dollar_volume"] = df["avg_volume_20"] * df["Close"]

    return df


def compute_regime(spy_df, qqq_df):
    """Determine regime mode from latest bar."""
    spy_close = float(spy_df["Close"].iloc[-1])
    spy_sma50 = float(spy_df["sma50"].iloc[-1])
    qqq_close = float(qqq_df["Close"].iloc[-1])
    qqq_sma50 = float(qqq_df["sma50"].iloc[-1])

    spy_ok = spy_close > spy_sma50
    qqq_ok = qqq_close > qqq_sma50

    if spy_ok and qqq_ok:
        return "full_risk", {"SPY": {"close": spy_close, "sma50": spy_sma50, "risk_on": True},
                             "QQQ": {"close": qqq_close, "sma50": qqq_sma50, "risk_on": True}}
    elif spy_ok or qqq_ok:
        return "reduced_risk", {"SPY": {"close": spy_close, "sma50": spy_sma50, "risk_on": spy_ok},
                                "QQQ": {"close": qqq_close, "sma50": qqq_sma50, "risk_on": qqq_ok}}
    else:
        return "risk_off", {"SPY": {"close": spy_close, "sma50": spy_sma50, "risk_on": False},
                            "QQQ": {"close": qqq_close, "sma50": qqq_sma50, "risk_on": False}}


def compute_relative_strength(sym_df, spy_df, lookback_days):
    """RS = symbol return - SPY return over lookback."""
    if len(sym_df) < lookback_days or len(spy_df) < lookback_days:
        return 0.0
    sym_ret = float(sym_df["Close"].iloc[-1]) / float(sym_df["Close"].iloc[-lookback_days]) - 1
    spy_ret = float(spy_df["Close"].iloc[-1]) / float(spy_df["Close"].iloc[-lookback_days]) - 1
    return sym_ret - spy_ret


def compute_growth_score(rs_3m, rs_6m, trend_strength, strategy):
    """Composite growth score. All components normalized to comparable 0-1 scale."""
    rank_cfg = strategy["ranking"]
    cap = rank_cfg["trend_strength_cap_pct"]

    # Normalize RS values to roughly 0-1 range (typical RS spread is -0.3 to +0.3)
    norm_rs_3m = max(0, min(1, (rs_3m + 0.3) / 0.6))
    norm_rs_6m = max(0, min(1, (rs_6m + 0.3) / 0.6))

    # Cap and normalize trend strength
    capped_trend = min(max(trend_strength, 0), cap)
    norm_trend = capped_trend / cap if cap > 0 else 0

    score = (norm_rs_3m * rank_cfg["rs_3m_weight"] +
             norm_rs_6m * rank_cfg["rs_6m_weight"] +
             norm_trend * rank_cfg["trend_strength_weight"])
    return round(score * 100, 2)


def detect_breakout(df, strategy):
    """Detect breakout: close near 20d or 55d high."""
    if len(df) < 60:
        return None

    row = df.iloc[-1]
    close = float(row["Close"])
    high_20d = float(row["high_20d"])
    high_55d = float(row["high_55d"])
    sma20 = float(row["sma20"])
    sma50 = float(row["sma50"])
    sma200 = float(row["sma200"])

    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma200):
        return None

    # Must be above all major MAs
    if not (close > sma20 and close > sma50 and close > sma200):
        return None

    near_pct = strategy["setups"]["breakout"]["near_high_pct"]

    is_20d_breakout = close >= high_20d * (1 - near_pct)
    is_55d_breakout = close >= high_55d * (1 - near_pct)

    if not (is_20d_breakout or is_55d_breakout):
        return None

    # Volume confirmation (optional)
    vol_ratio = 0.0
    avg_vol = float(row["avg_volume_20"]) if not pd.isna(row["avg_volume_20"]) else 0
    if avg_vol > 0:
        vol_ratio = float(row["Volume"]) / avg_vol

    # Volume confirmation gate
    breakout_cfg = strategy["setups"]["breakout"]
    if breakout_cfg.get("require_volume_confirmation", False):
        min_ratio = breakout_cfg.get("volume_confirmation_ratio", 1.2)
        if vol_ratio < min_ratio:
            return None  # rejected: breakout_volume_not_confirmed

    breakout_type = "55d_breakout" if is_55d_breakout else "20d_breakout"

    return {
        "setup_type": "breakout",
        "setup_high": float(row["High"]),
        "setup_low": float(row["Low"]),
        "volume_ratio": round(vol_ratio, 2),
        "notes": [breakout_type, f"vol_ratio={vol_ratio:.2f}"],
    }


def detect_continuation(df, strategy):
    """Detect continuation: strong trend, 1-3 bar shallow pullback, closes green."""
    if len(df) < 60:
        return None

    cfg = strategy["setups"]["continuation"]
    row = df.iloc[-1]
    close = float(row["Close"])
    open_price = float(row["Open"])
    sma20 = float(row["sma20"])
    sma50 = float(row["sma50"])
    high_20d = float(row["high_20d"])

    if pd.isna(sma20) or pd.isna(sma50):
        return None

    # Must be above SMA20
    if close < sma20:
        return None

    # Must be in uptrend (SMA20 > SMA50)
    if sma20 < sma50:
        return None

    # Must have had a recent breakout (within 10 bars hit 20d high)
    recent_highs = df["High"].iloc[-10:]
    if recent_highs.max() < high_20d * 0.98:
        return None

    # Count pullback bars (bars below prior high)
    pullback_bars = 0
    for i in range(2, min(cfg["max_pullback_bars"] + 2, len(df))):
        bar = df.iloc[-i]
        if float(bar["High"]) < float(df.iloc[-i-1]["High"]):
            pullback_bars += 1
        else:
            break

    if pullback_bars < 1 or pullback_bars > cfg["max_pullback_bars"]:
        return None

    # Last bar should close green
    if cfg["require_green_close"] and close <= open_price:
        return None

    return {
        "setup_type": "continuation",
        "setup_high": float(row["High"]),
        "setup_low": float(row["Low"]),
        "pullback_bars": pullback_bars,
        "notes": [f"pullback_{pullback_bars}_bars", "green_close"],
    }


def detect_shallow_pullback(df, strategy):
    """Detect shallow pullback: within 1.5 ATR of recent swing high."""
    if len(df) < 60:
        return None

    cfg = strategy["setups"]["shallow_pullback"]
    row = df.iloc[-1]
    close = float(row["Close"])
    sma20 = float(row["sma20"])
    sma50 = float(row["sma50"])
    sma200 = float(row["sma200"])
    atr = float(row["atr14"])
    high_20d = float(row["high_20d"])

    if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma200) or pd.isna(atr) or atr <= 0:
        return None

    # Must be above SMA20 and SMA50
    if not (close > sma20 and close > sma50 and close > sma200):
        return None

    # Must be pulling back (not at the high)
    depth = high_20d - close
    depth_atr = depth / atr

    if depth_atr < 0.3 or depth_atr > cfg["max_depth_atr"]:
        return None

    # Must not be breaking down (close above EMA10 or very close)
    ema10 = float(row["ema10"]) if not pd.isna(row["ema10"]) else sma20
    if close < ema10 * 0.98:
        return None

    return {
        "setup_type": "shallow_pullback",
        "setup_high": float(row["High"]),
        "setup_low": float(row["Low"]),
        "pullback_depth_atr": round(depth_atr, 2),
        "notes": [f"depth={depth_atr:.2f}_ATR", f"from_20d_high"],
    }


def compute_stop(trigger_price, setup_low, atr, strategy):
    """Wider of: setup_low - 0.2*ATR or entry - 2.5*ATR."""
    stop_cfg = strategy["stop"]
    stop_candle = setup_low - stop_cfg["setup_low_buffer_atr"] * atr
    stop_atr = trigger_price - stop_cfg["atr_stop_multiplier"] * atr
    return min(stop_candle, stop_atr)


def main():
    enforce_live_guardrails()

    strategy = load_growth_strategy()
    symbols = load_growth_watchlist()
    filters = strategy["filters"]

    print(f"Growth Research: scanning {len(symbols)} symbols...")

    # Download data
    raw = download_data(symbols)

    # Prepare data
    symbol_data = {}
    for sym in symbols + ["SPY", "QQQ"]:
        try:
            df = get_symbol_df(raw, sym)
            if not df.empty and len(df) > 200:
                symbol_data[sym] = add_indicators(df, strategy)
        except Exception:
            pass

    if "SPY" not in symbol_data or "QQQ" not in symbol_data:
        raise RuntimeError("Missing SPY or QQQ data")

    spy_df = symbol_data["SPY"]
    qqq_df = symbol_data["QQQ"]

    # Regime
    regime_mode, market_regime = compute_regime(spy_df, qqq_df)
    regime_cfg = strategy["regime"].get(regime_mode, {})
    allow_entries = regime_cfg.get("allow_new_entries", regime_mode != "risk_off")
    risk_per_trade = regime_cfg.get("risk_per_trade", 0)
    max_positions = regime_cfg.get("max_open_positions", 0)

    print(f"  Regime: {regime_mode} | Entries allowed: {allow_entries}")

    # Score and rank all symbols
    scored = []
    rejected = []

    for sym in symbols:
        if sym in ("SPY", "QQQ"):
            continue
        if sym not in symbol_data:
            rejected.append({"symbol": sym, "reasons": ["no_data"]})
            continue

        df = symbol_data[sym]
        row = df.iloc[-1]
        close = float(row["Close"])
        atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0
        sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
        sma200 = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0

        # Base filters
        reasons = []
        if close < filters["min_price"]:
            reasons.append("price_too_low")
        avg_dv = float(row["avg_dollar_volume"]) if not pd.isna(row["avg_dollar_volume"]) else 0
        if avg_dv < filters["min_avg_dollar_volume"]:
            reasons.append("low_dollar_volume")
        if atr <= 0:
            reasons.append("invalid_atr")
        if atr / close > filters["max_atr_percent"] and atr > 0:
            reasons.append("atr_too_high")

        if reasons:
            rejected.append({"symbol": sym, "reasons": reasons})
            continue

        # Universal trend sanity filter: close > sma200 and sma50 > sma200
        if sma200 > 0 and close < sma200:
            rejected.append({"symbol": sym, "reasons": ["below_sma200"]})
            continue
        if sma50 > 0 and sma200 > 0 and sma50 < sma200:
            rejected.append({"symbol": sym, "reasons": ["sma50_below_sma200"]})
            continue

        # Compute RS
        rs_3m = compute_relative_strength(df, spy_df, 63)
        rs_6m = compute_relative_strength(df, spy_df, 126)

        # Trend strength = distance above 50 SMA as pct
        trend_strength = (close - sma50) / sma50 if sma50 > 0 else 0

        score = compute_growth_score(rs_3m, rs_6m, trend_strength, strategy)

        scored.append({
            "symbol": sym,
            "score": score,
            "close": round(close, 2),
            "ema10": round(float(row["ema10"]), 2) if not pd.isna(row["ema10"]) else None,
            "sma20": round(float(row["sma20"]), 2) if not pd.isna(row["sma20"]) else None,
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "atr14": round(atr, 2),
            "avg_dollar_volume": round(avg_dv, 0),
            "rs_3m": round(rs_3m, 4),
            "rs_6m": round(rs_6m, 4),
            "trend_strength": round(trend_strength, 4),
        })

    # Rank and filter top percentile
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_pct = strategy["ranking"]["top_percentile"]
    cutoff = max(1, int(len(scored) * top_pct / 100))
    leaders = scored[:cutoff]
    non_leaders = scored[cutoff:]

    for s in non_leaders:
        rejected.append({"symbol": s["symbol"], "score": s["score"], "reasons": ["below_rank_cutoff"]})

    print(f"  Scored: {len(scored)} | Leaders (top {top_pct}%): {len(leaders)}")

    # Detect setups for leaders
    candidates = []
    for leader in leaders:
        sym = leader["symbol"]
        df = symbol_data[sym]

        # Try each setup type
        setup = None
        for detector in [detect_breakout, detect_continuation, detect_shallow_pullback]:
            result = detector(df, strategy)
            if result:
                setup = result
                break

        if setup is None:
            # Check if breakout was rejected due to volume
            breakout_cfg = strategy["setups"]["breakout"]
            if breakout_cfg.get("require_volume_confirmation", False):
                # Quick check if it would have been a breakout without volume gate
                row = df.iloc[-1]
                close = float(row["Close"])
                high_20d = float(row["high_20d"])
                high_55d = float(row["high_55d"])
                near_pct = breakout_cfg["near_high_pct"]
                sma20_val = float(row["sma20"]) if not pd.isna(row["sma20"]) else 0
                sma50_val = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
                sma200_val = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0
                if (close > sma20_val and close > sma50_val and close > sma200_val and
                        (close >= high_20d * (1 - near_pct) or close >= high_55d * (1 - near_pct))):
                    rejected.append({"symbol": sym, "score": leader["score"],
                                     "reasons": ["breakout_volume_not_confirmed"]})
                    continue
            rejected.append({"symbol": sym, "score": leader["score"], "reasons": ["no_setup_detected"]})
            continue

        # Compute entry/stop
        trigger_buffer = strategy["entry"]["trigger_buffer_atr"]
        limit_buffer = strategy["entry"]["limit_buffer_atr"]
        atr = leader["atr14"]

        # --- Relative volume filter (setup-specific thresholds) ---
        row = df.iloc[-1]
        avg_vol = float(row["avg_volume_20"]) if not pd.isna(row["avg_volume_20"]) else 0
        rel_volume = float(row["Volume"]) / avg_vol if avg_vol > 0 else 0
        setup_type = setup["setup_type"]

        min_rv_key = f"min_rel_volume_{setup_type}"
        min_rv = strategy.get("filters", {}).get(min_rv_key, 0)
        if min_rv > 0 and rel_volume < min_rv:
            rejected.append({"symbol": sym, "score": leader["score"],
                             "reasons": [f"low_rel_volume_{setup_type}", f"rv={rel_volume:.2f}<{min_rv}"]})
            continue

        trigger_price = setup["setup_high"] + trigger_buffer * atr
        limit_price = trigger_price + limit_buffer * atr
        stop_price = compute_stop(trigger_price, setup["setup_low"], atr, strategy)

        if stop_price <= 0 or stop_price >= trigger_price:
            rejected.append({"symbol": sym, "score": leader["score"], "reasons": ["invalid_stop"]})
            continue

        r_per_share = trigger_price - stop_price

        candidate = {
            **leader,
            "setup_type": setup["setup_type"],
            "trigger_price": round(trigger_price, 2),
            "limit_price": round(limit_price, 2),
            "setup_high": round(setup["setup_high"], 2),
            "setup_low": round(setup["setup_low"], 2),
            "stop_price": round(stop_price, 2),
            "r_per_share": round(r_per_share, 2),
            "pullback_bars": setup.get("pullback_bars", 0),
            "pullback_depth_atr": setup.get("pullback_depth_atr", 0.0),
            "volume_ratio": setup.get("volume_ratio", 0.0),
            "rel_volume": round(rel_volume, 2),
            "regime_mode": regime_mode,
            "correlation_blocked": False,
            "notes": setup.get("notes", []),
        }
        candidates.append(candidate)

    # Sort candidates by score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Build output
    output = {
        "bot_name": "growth",
        "date": today_str(),
        "timestamp": now_iso(),
        "regime_mode": regime_mode,
        "allow_new_entries": allow_entries,
        "risk_per_trade": risk_per_trade,
        "max_positions": max_positions,
        "market_regime": market_regime,
        "candidates": candidates,
        "rejected": rejected,
    }

    save_json(STATE_DIR / "candidates_growth.json", output)

    # CSV for quick review — always rewrite (header-only if no candidates)
    csv_lines = ["symbol,score,setup_type,trigger,stop,r_per_share,rs_3m"]
    for c in candidates:
        csv_lines.append(f"{c['symbol']},{c['score']},{c['setup_type']},{c['trigger_price']},{c['stop_price']},{c['r_per_share']},{c['rs_3m']}")
    (STATE_DIR / "candidates_growth.csv").write_text("\n".join(csv_lines))

    # Rejected CSV — always write
    rej_lines = ["symbol,score,reasons"]
    for r in rejected:
        rej_lines.append(f"{r.get('symbol','')},{r.get('score','')},\"{';'.join(r.get('reasons',[]))}\"")
    (STATE_DIR / "rejected_growth.csv").write_text("\n".join(rej_lines))

    write_heartbeat("research_growth", "ok", {
        "bot_name": "growth",
        "regime": regime_mode,
        "candidates": len(candidates),
        "rejected": len(rejected),
        "leaders": len(leaders),
    })

    print(f"Growth Research: {len(candidates)} candidates, {len(rejected)} rejected | regime={regime_mode}")
    for c in candidates[:5]:
        print(f"  {c['symbol']:>5} | score={c['score']:>6.1f} | {c['setup_type']:<18} | trigger=${c['trigger_price']:.2f} | R=${c['r_per_share']:.2f}")

    if candidates:
        send_alert(
            f"📊 Growth scan: {len(candidates)} candidates | Regime: {regime_mode}\n"
            + "\n".join(f"  • {c['symbol']} ({c['setup_type']}, score={c['score']})" for c in candidates[:5]),
            level="info"
        )
    else:
        # Alert with top rejection reasons for debugging
        from collections import Counter
        reason_counts = Counter()
        for r in rejected:
            for reason in r.get("reasons", []):
                reason_counts[reason] += 1
        top_reasons = ", ".join(f"{r}({n})" for r, n in reason_counts.most_common(5))
        send_alert(
            f"📊 Growth scan: 0 candidates | Regime: {regime_mode} | Top rejections: {top_reasons}",
            level="info"
        )


if __name__ == "__main__":
    main()

