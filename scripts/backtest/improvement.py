"""
Backtest variant runner — tests improvements to the baseline strategy.

Variants tested:
1. BASELINE: current strategy.json settings
2. STRICT_RS: Higher relative strength threshold (top 10% instead of top 20%)
3. MARKET_BUY: Market buy at open instead of stop-limit (better fill participation)
4. NO_EARNINGS: Skip stocks within 14 days of earnings (wider blackout)
5. TIGHTER_PULLBACK: Tighter pullback distance (3% instead of 7%) + min 3 days
6. COMBINED: Strict RS + tighter pullback (best filters together)

Usage:
    python scripts/backtest_improvement.py
"""
import json
import copy
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

from common import STATE_DIR, load_strategy, load_watchlist, risk_position_size, save_json

# Reuse core functions from backtest.py
from backtest import (
    download_all_data,
    get_symbol_df,
    add_indicators,
    detect_confirmation_candle,
    detect_pullback,
)


def compute_relative_strength(symbol_data, sym, date, lookback=126):
    """Compute RS vs SPY over lookback days."""
    if sym not in symbol_data or "SPY" not in symbol_data:
        return 0
    df = symbol_data[sym]
    spy = symbol_data["SPY"]
    if date not in df.index or date not in spy.index:
        return 0
    idx = df.index.get_loc(date)
    spy_idx = spy.index.get_loc(date)
    if idx < lookback or spy_idx < lookback:
        return 0
    sym_ret = float(df.iloc[idx]["Close"]) / float(df.iloc[idx - lookback]["Close"]) - 1
    spy_ret = float(spy.iloc[spy_idx]["Close"]) / float(spy.iloc[spy_idx - lookback]["Close"]) - 1
    return sym_ret - spy_ret


def run_variant(variant_name, strategy, symbols, symbol_data, trading_days,
                initial_equity=20000.0, slippage_pct=0.001, overrides=None):
    """Run a single backtest variant with optional overrides."""
    overrides = overrides or {}

    # Variant-specific settings
    rs_percentile = overrides.get("rs_percentile", 80)  # top X%
    use_market_buy = overrides.get("use_market_buy", False)
    earnings_blackout = overrides.get("earnings_blackout_days", 7)
    max_pullback_pct = overrides.get("max_pullback_pct", strategy["pullback_max_distance_from_sma20_pct"])
    min_pullback_days = overrides.get("min_pullback_days", strategy["pullback_days_min"])
    trailing_mult = overrides.get("trailing_atr_multiplier", strategy.get("trailing_atr_multiplier", 3.0))

    equity = initial_equity
    cash = initial_equity
    positions = {}
    pending_orders = {}
    closed_trades = []
    equity_curve = []

    for day_idx, date in enumerate(trading_days):
        # --- REGIME CHECK ---
        spy_row = symbol_data["SPY"].loc[date] if date in symbol_data["SPY"].index else None
        qqq_row = symbol_data["QQQ"].loc[date] if date in symbol_data["QQQ"].index else None
        if spy_row is None or qqq_row is None:
            continue

        spy_ok = float(spy_row["Close"]) > float(spy_row["sma50"]) and float(spy_row["Close"]) > float(spy_row["sma200"])
        qqq_ok = float(qqq_row["Close"]) > float(qqq_row["sma50"]) and float(qqq_row["Close"]) > float(qqq_row["sma200"])
        regime_on = spy_ok and qqq_ok

        # Breadth
        breadth_ok = True
        if "RSP" in symbol_data and date in symbol_data["RSP"].index:
            rsp = symbol_data["RSP"].loc[date]
            if not pd.isna(rsp["sma50"]):
                breadth_ok = float(rsp["Close"]) > float(rsp["sma50"])

        # --- CHECK PENDING ORDERS ---
        for sym in list(pending_orders.keys()):
            order = pending_orders[sym]
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue
            bar = symbol_data[sym].loc[date]
            high = float(bar["High"])
            open_price = float(bar["Open"])

            order["days_pending"] += 1
            if order["days_pending"] > strategy.get("stale_order_cancel_days", 2):
                del pending_orders[sym]
                continue

            # Fill logic
            fill_price = None
            if use_market_buy:
                # Market buy at next day's open
                fill_price = open_price * (1 + slippage_pct)
            else:
                # Stop-limit: only if high reaches trigger
                if high >= order["trigger"]:
                    fill_price = order["trigger"] * (1 + slippage_pct)
                    if fill_price > order["limit"]:
                        continue

            if fill_price is None:
                continue

            cost = order["qty"] * fill_price
            if cash < cost:
                del pending_orders[sym]
                continue

            positions[sym] = {
                "entry": fill_price,
                "stop": order["stop"],
                "qty": order["qty"],
                "r": fill_price - order["stop"],
                "phase": "initial",
                "bars_held": 0,
                "highest_close": fill_price,
                "entry_date": str(date.date()),
            }
            cash -= cost
            del pending_orders[sym]

        # --- MANAGE POSITIONS ---
        for sym in list(positions.keys()):
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue

            pos = positions[sym]
            bar = symbol_data[sym].loc[date]
            close = float(bar["Close"])
            low = float(bar["Low"])
            pos["bars_held"] += 1
            pos["highest_close"] = max(pos["highest_close"], close)

            atr = float(bar["atr14"]) if not pd.isna(bar["atr14"]) else pos["r"] / 2
            exit_price = None
            exit_reason = None

            # Early invalidation
            sma50 = float(bar["sma50"]) if not pd.isna(bar["sma50"]) else 0
            if pos["phase"] == "initial" and pos["bars_held"] <= strategy.get("early_invalidation_bars", 3):
                if close < sma50 and sma50 > 0:
                    exit_price = close * (1 - slippage_pct)
                    exit_reason = "early_invalidation"

            # Stop hit
            if exit_price is None and low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - slippage_pct)
                exit_reason = f"stop_{pos['phase']}"

            # Phase transitions
            if exit_price is None:
                target_1r = pos["entry"] + pos["r"]
                target_2r = pos["entry"] + strategy["reward_to_risk"] * pos["r"]

                if pos["phase"] == "initial" and close >= target_1r:
                    pos["phase"] = "breakeven"
                    pos["stop"] = pos["entry"] + strategy.get("breakeven_buffer_atr", 0.1) * atr

                elif pos["phase"] == "breakeven" and close >= target_2r:
                    pos["phase"] = "trailing"
                    trail = trailing_mult * atr
                    pos["stop"] = pos["highest_close"] - trail

                elif pos["phase"] == "trailing":
                    trail = trailing_mult * atr
                    new_stop = pos["highest_close"] - trail
                    pos["stop"] = max(pos["stop"], new_stop)

            # Execute exit
            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["qty"]
                r_multiple = (exit_price - pos["entry"]) / pos["r"] if pos["r"] > 0 else 0
                cash += pos["qty"] * exit_price
                equity += pnl
                closed_trades.append({
                    "symbol": sym,
                    "entry_price": round(pos["entry"], 2),
                    "exit_price": round(exit_price, 2),
                    "qty": pos["qty"],
                    "pnl": round(pnl, 2),
                    "r_multiple": round(r_multiple, 2),
                    "bars_held": pos["bars_held"],
                    "exit_reason": exit_reason,
                    "entry_date": pos["entry_date"],
                    "exit_date": str(date.date()),
                })
                del positions[sym]

        # --- RESEARCH & NEW ENTRIES ---
        if regime_on and breadth_ok and len(positions) + len(pending_orders) < strategy["breadth_modes"]["full_risk"]["max_open_positions"]:

            # Compute RS for all symbols to rank
            rs_scores = {}
            for sym in symbols:
                if sym in positions or sym in pending_orders:
                    continue
                rs = compute_relative_strength(symbol_data, sym, date, lookback=126)
                rs_scores[sym] = rs

            # Filter to top N percentile
            if rs_scores:
                threshold_idx = max(1, int(len(rs_scores) * (1 - rs_percentile / 100)))
                sorted_syms = sorted(rs_scores.keys(), key=lambda s: rs_scores[s], reverse=True)
                top_rs_symbols = sorted_syms[:threshold_idx]
            else:
                top_rs_symbols = []

            for sym in top_rs_symbols:
                if sym in positions or sym in pending_orders:
                    continue
                if sym not in symbol_data or date not in symbol_data[sym].index:
                    continue

                df = symbol_data[sym]
                idx = df.index.get_loc(date)
                if idx < 200:
                    continue

                row = df.iloc[idx]
                close = float(row["Close"])
                sma20 = float(row["sma20"])
                sma50_val = float(row["sma50"])
                sma200 = float(row["sma200"])
                atr = float(row["atr14"])

                if pd.isna(sma20) or pd.isna(sma50_val) or pd.isna(sma200) or pd.isna(atr):
                    continue
                if atr <= 0:
                    continue

                # Trend filter
                if not (close > sma50_val > sma200):
                    continue

                # ATR% filter
                if atr / close > strategy.get("max_atr_percent", 0.06):
                    continue

                # Pullback (with variant overrides)
                is_pb, pb_days = detect_pullback(df, idx, strategy)
                if not is_pb:
                    continue
                if pb_days < min_pullback_days:
                    continue

                # SMA20 distance (variant can tighten)
                pct_from_sma20 = (close / sma20) - 1
                if pct_from_sma20 < strategy["pullback_min_distance_from_sma20_pct"]:
                    continue
                if pct_from_sma20 > max_pullback_pct:
                    continue

                # Volume
                vol_10 = float(row["avg_volume_10"]) if not pd.isna(row["avg_volume_10"]) else 1
                vol_3 = float(row["recent_volume_3"]) if not pd.isna(row["recent_volume_3"]) else vol_10
                if vol_10 > 0 and vol_3 / vol_10 > strategy["pullback_volume_ratio_max"]:
                    continue

                # Confirmation candle
                pattern, candle_high, candle_low = detect_confirmation_candle(df, idx)
                if pattern is None:
                    continue

                # Entry order
                trigger_buffer = strategy.get("entry_trigger_buffer_atr", 0.05)
                limit_buffer = strategy.get("entry_limit_buffer_atr", 0.15)
                trigger = candle_high + trigger_buffer * atr
                limit_price = trigger + limit_buffer * atr
                stop_candle = candle_low - strategy.get("candle_low_stop_atr_buffer", 0.1) * atr
                stop_atr = trigger - strategy.get("atr_stop_multiplier", 2.0) * atr
                stop = min(stop_candle, stop_atr)

                if stop <= 0 or stop >= trigger:
                    continue

                risk_per_trade = strategy["breadth_modes"]["full_risk"]["risk_per_trade"]
                qty = risk_position_size(equity, risk_per_trade, trigger, stop,
                                         strategy["max_alloc_fraction_per_symbol"])
                if qty <= 0:
                    continue

                pending_orders[sym] = {
                    "trigger": round(trigger, 2),
                    "limit": round(limit_price, 2),
                    "stop": round(stop, 2),
                    "qty": qty,
                    "days_pending": 0,
                    "candle_high": candle_high,
                }
                if len(pending_orders) >= 2:
                    break

        # Equity curve
        open_value = sum(
            pos["qty"] * float(symbol_data[sym].loc[date]["Close"])
            for sym, pos in positions.items()
            if sym in symbol_data and date in symbol_data[sym].index
        )
        equity_curve.append({
            "date": str(date.date()),
            "equity": round(cash + open_value, 2),
        })

    # Results
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_equity
    return final_equity, closed_trades, equity_curve


def print_results(name, initial, final, trades):
    """Print summary for one variant."""
    ret_pct = (final / initial - 1) * 100
    print(f"\n{'─'*50}")
    print(f"  {name}")
    print(f"{'─'*50}")
    print(f"  Return: {ret_pct:+.2f}% (${final:,.0f})")
    print(f"  Trades: {len(trades)}")

    if trades:
        df = pd.DataFrame(trades)
        winners = df[df["pnl"] > 0]
        losers = df[df["pnl"] <= 0]
        total_wins = winners["pnl"].sum()
        total_losses = abs(losers["pnl"].sum())

        print(f"  Win Rate: {len(winners)/len(df)*100:.1f}%")
        print(f"  Avg R: {df['r_multiple'].mean():.2f}R")
        print(f"  Profit Factor: {total_wins/total_losses:.2f}" if total_losses > 0 else "  Profit Factor: ∞")
        print(f"  Best: {df['r_multiple'].max():.1f}R | Worst: {df['r_multiple'].min():.1f}R")
        print(f"  Avg Hold: {df['bars_held'].mean():.0f} days")

        # Max drawdown
        eq_series = pd.Series([initial] + [initial * (1 + ret_pct/100)])  # simplified
        if len(trades) > 2:
            cum_pnl = df["pnl"].cumsum()
            peak = cum_pnl.cummax()
            dd = cum_pnl - peak
            print(f"  Max Drawdown: ${dd.min():,.0f}")


def main():
    strategy = load_strategy()
    symbols = load_watchlist()
    initial_equity = 20000.0

    start_date = "2024-01-01"
    end_date = "2026-05-01"

    print("=" * 60)
    print("  BACKTEST VARIANT COMPARISON")
    print(f"  Period: {start_date} → {end_date} | Start: ${initial_equity:,.0f}")
    print("=" * 60)

    # Download data once
    lookback_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = download_all_data(symbols, lookback_start, end_date)

    symbol_data = {}
    for sym in symbols + ["SPY", "QQQ", "RSP"]:
        df = get_symbol_df(raw, sym)
        if not df.empty and len(df) > 200:
            symbol_data[sym] = add_indicators(df, strategy)

    if "SPY" not in symbol_data or "QQQ" not in symbol_data:
        raise RuntimeError("Missing benchmark data")

    spy_dates = symbol_data["SPY"].index
    trade_start = pd.Timestamp(start_date)
    trading_days = spy_dates[spy_dates >= trade_start]

    print(f"\n  Trading days: {len(trading_days)} | Symbols: {len(symbols)}")
    print(f"  Downloading complete. Running variants...\n")

    # Define variants
    variants = {
        "1. BASELINE (current)": {},
        "2. STRICT RS (top 10%)": {"rs_percentile": 90},
        "3. MARKET BUY (open fill)": {"use_market_buy": True},
        "4. TIGHTER PULLBACK (3%)": {"max_pullback_pct": 0.03, "min_pullback_days": 3},
        "5. TIGHT TRAIL (2.5 ATR)": {"trailing_atr_multiplier": 2.5},
        "6. COMBINED (strict RS + tight PB)": {"rs_percentile": 90, "max_pullback_pct": 0.03, "min_pullback_days": 3},
    }

    results = {}
    for name, overrides in variants.items():
        print(f"  Running: {name}...")
        final, trades, eq_curve = run_variant(
            name, strategy, symbols, symbol_data, trading_days,
            initial_equity=initial_equity, overrides=overrides,
        )
        results[name] = {"final": final, "trades": trades, "equity_curve": eq_curve}
        print_results(name, initial_equity, final, trades)

    # Comparison table
    print(f"\n\n{'='*60}")
    print(f"  COMPARISON SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Variant':<35} {'Return':>8} {'Trades':>7} {'Win%':>6} {'Avg R':>6}")
    print(f"  {'─'*35} {'─'*8} {'─'*7} {'─'*6} {'─'*6}")

    for name, data in results.items():
        ret = (data["final"] / initial_equity - 1) * 100
        n_trades = len(data["trades"])
        if n_trades > 0:
            df = pd.DataFrame(data["trades"])
            win_pct = len(df[df["pnl"] > 0]) / len(df) * 100
            avg_r = df["r_multiple"].mean()
        else:
            win_pct = 0
            avg_r = 0
        print(f"  {name:<35} {ret:>+7.1f}% {n_trades:>7} {win_pct:>5.1f}% {avg_r:>+5.2f}R")

    print(f"\n{'='*60}")

    # Save all results
    save_json(STATE_DIR / "variant_comparison.json", {
        "start_date": start_date,
        "end_date": end_date,
        "initial_equity": initial_equity,
        "variants": {
            name: {
                "final_equity": data["final"],
                "return_pct": round((data["final"] / initial_equity - 1) * 100, 2),
                "total_trades": len(data["trades"]),
                "trades": data["trades"],
            }
            for name, data in results.items()
        }
    })
    print(f"\nFull results saved to state/variant_comparison.json")


if __name__ == "__main__":
    main()

