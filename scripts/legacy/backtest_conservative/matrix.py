"""
Controlled Backtest Matrix for swing trading system.

Runs all variant combinations changing one category at a time,
then best-of-breed combinations. Outputs CSV + ranked summary.
"""
import json
import copy
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, CONFIG_DIR, load_strategy, risk_position_size, save_json
from backtest import (
    download_all_data,
    get_symbol_df,
    add_indicators,
    detect_confirmation_candle,
    detect_pullback,
)

# ── Config ──────────────────────────────────────────────────────────
START_DATE = "2024-01-01"
END_DATE = "2026-05-01"
INITIAL_EQUITY = 20000.0
SLIPPAGE_PCT = 0.001

# ── Watchlists ──────────────────────────────────────────────────────
U0_TICKERS = [
    "SPY","IWM","MDY","XLK","XLI","XLF","XLV","XLE","XLB",
    "AAPL","MSFT","NVDA","AVGO","ANET","PANW","NOW","SNPS",
    "LLY","ISRG","TMO","ABT",
    "JPM","V","MA","GS","BLK",
    "CAT","GE","URI","ETN","PWR","DE",
    "NFLX","TMUS","SPOT",
    "XOM","CVX","SLB",
    "FCX","NUE",
]

U1_EXTRA = ["PH","TT","FAST","MS","ICE","CME","SYK","BSX","EOG","BKNG"]
U2_EXTRA = U1_EXTRA + ["CARR","IR","SCHW","ELV","FANG","LOW","ORLY","ROST","HAL","NEM"]

U3_TICKERS = [t for t in U0_TICKERS if t != "SMH"]  # XLK only (SMH not in U0 anyway)
U4_TICKERS = [t if t != "XLK" else "SMH" for t in U0_TICKERS]  # SMH replaces XLK


def compute_relative_strength(symbol_data, sym, date, lookback=126):
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


def detect_continuation_setup(df, idx, strategy):
    """Continuation setup: close > SMA50, SMA20 > SMA50, pullback 1-2% of SMA20, bullish candle."""
    if idx < 200:
        return None, None, None

    row = df.iloc[idx]
    close = float(row["Close"])
    sma20 = float(row["sma20"]) if not pd.isna(row["sma20"]) else 0
    sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0

    if sma20 == 0 or sma50 == 0:
        return None, None, None

    # Close above SMA50, SMA20 > SMA50
    if not (close > sma50 and sma20 > sma50):
        return None, None, None

    # Within 1-2% of SMA20
    dist = (close / sma20) - 1
    if dist < -0.02 or dist > -0.01:
        return None, None, None

    # Bullish confirmation candle
    pattern, ch, cl = detect_confirmation_candle(df, idx)
    if pattern is None:
        return None, None, None

    return f"continuation_{pattern}", ch, cl


def run_single_backtest(symbols, strategy, symbol_data, trading_days,
                        overrides=None, enable_continuation=False):
    """Run one backtest variant. Returns metrics dict."""
    overrides = overrides or {}

    # Apply overrides to strategy copy
    strat = copy.deepcopy(strategy)
    for key, val in overrides.items():
        if "." in key:
            parts = key.split(".")
            obj = strat
            for p in parts[:-1]:
                obj = obj[p]
            obj[parts[-1]] = val
        else:
            strat[key] = val

    trailing_mult = strat.get("trailing_atr_multiplier", 3.0)
    max_positions_full = strat["breadth_modes"]["full_risk"]["max_open_positions"]
    max_positions_reduced = strat["breadth_modes"]["reduced_risk"]["max_open_positions"]
    risk_per_trade = strat["breadth_modes"]["full_risk"]["risk_per_trade"]

    equity = INITIAL_EQUITY
    cash = INITIAL_EQUITY
    positions = {}
    pending_orders = {}
    closed_trades = []
    equity_curve = []
    total_days = 0
    invested_days = 0

    for day_idx, date in enumerate(trading_days):
        total_days += 1
        spy_row = symbol_data["SPY"].loc[date] if date in symbol_data["SPY"].index else None
        qqq_row = symbol_data["QQQ"].loc[date] if date in symbol_data["QQQ"].index else None
        if spy_row is None or qqq_row is None:
            continue

        spy_ok = float(spy_row["Close"]) > float(spy_row["sma50"]) and float(spy_row["Close"]) > float(spy_row["sma200"])
        qqq_ok = float(qqq_row["Close"]) > float(qqq_row["sma50"]) and float(qqq_row["Close"]) > float(qqq_row["sma200"])
        regime_on = spy_ok and qqq_ok

        breadth_ok = True
        breadth_mode = "full_risk"
        if "RSP" in symbol_data and date in symbol_data["RSP"].index:
            rsp = symbol_data["RSP"].loc[date]
            if not pd.isna(rsp["sma50"]):
                rsp_above = float(rsp["Close"]) > float(rsp["sma50"])
                if not rsp_above:
                    breadth_ok = False
                    breadth_mode = "reduced_risk"

        max_pos = max_positions_full if breadth_mode == "full_risk" else max_positions_reduced

        # Track exposure
        if positions:
            invested_days += 1

        # ── Check pending orders ──
        for sym in list(pending_orders.keys()):
            order = pending_orders[sym]
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue
            bar = symbol_data[sym].loc[date]
            high = float(bar["High"])

            order["days_pending"] += 1
            if order["days_pending"] > strat.get("stale_order_cancel_days", 2):
                del pending_orders[sym]
                continue

            if high >= order["trigger"]:
                fill_price = order["trigger"] * (1 + SLIPPAGE_PCT)
                if fill_price > order["limit"]:
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
                    "setup_type": order.get("setup_type", "pullback"),
                }
                cash -= cost
                del pending_orders[sym]

        # ── Manage positions ──
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

            sma50 = float(bar["sma50"]) if not pd.isna(bar["sma50"]) else 0
            if pos["phase"] == "initial" and pos["bars_held"] <= strat.get("early_invalidation_bars", 3):
                if close < sma50 and sma50 > 0:
                    exit_price = close * (1 - SLIPPAGE_PCT)
                    exit_reason = "early_invalidation"

            if exit_price is None and low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - SLIPPAGE_PCT)
                exit_reason = f"stop_{pos['phase']}"

            if exit_price is None:
                target_1r = pos["entry"] + pos["r"]
                target_2r = pos["entry"] + strat["reward_to_risk"] * pos["r"]

                if pos["phase"] == "initial" and close >= target_1r:
                    pos["phase"] = "breakeven"
                    pos["stop"] = pos["entry"] + strat.get("breakeven_buffer_atr", 0.1) * atr
                elif pos["phase"] == "breakeven" and close >= target_2r:
                    pos["phase"] = "trailing"
                    pos["stop"] = pos["highest_close"] - trailing_mult * atr
                elif pos["phase"] == "trailing":
                    new_stop = pos["highest_close"] - trailing_mult * atr
                    pos["stop"] = max(pos["stop"], new_stop)

            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["qty"]
                r_multiple = (exit_price - pos["entry"]) / pos["r"] if pos["r"] > 0 else 0
                cash += pos["qty"] * exit_price
                equity += pnl
                closed_trades.append({
                    "symbol": sym,
                    "pnl": round(pnl, 2),
                    "r_multiple": round(r_multiple, 2),
                    "bars_held": pos["bars_held"],
                    "exit_reason": exit_reason,
                    "setup_type": pos.get("setup_type", "pullback"),
                })
                del positions[sym]

        # ── Research & new entries ──
        if regime_on and len(positions) + len(pending_orders) < max_pos:
            # RS ranking
            rs_scores = {}
            for sym in symbols:
                if sym in positions or sym in pending_orders:
                    continue
                if sym in ("SPY", "QQQ", "RSP"):
                    continue
                rs = compute_relative_strength(symbol_data, sym, date)
                rs_scores[sym] = rs

            if rs_scores:
                threshold_idx = max(1, int(len(rs_scores) * 0.2))
                sorted_syms = sorted(rs_scores.keys(), key=lambda s: rs_scores[s], reverse=True)
                top_rs = sorted_syms[:threshold_idx]
            else:
                top_rs = []

            orders_today = 0
            for sym in top_rs:
                if orders_today >= 2:
                    break
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
                if not (close > sma50_val > sma200):
                    continue
                if atr / close > strat.get("max_atr_percent", 0.06):
                    continue

                # Try pullback setup first
                setup_type = None
                pattern = None
                candle_high = None
                candle_low = None

                is_pb, pb_days = detect_pullback(df, idx, strat)
                if is_pb:
                    pct_from_sma20 = (close / sma20) - 1
                    if (strat["pullback_min_distance_from_sma20_pct"] <= pct_from_sma20 <= strat.get("pullback_max_distance_from_sma20_pct", 0.07)):
                        vol_10 = float(row["avg_volume_10"]) if not pd.isna(row["avg_volume_10"]) else 1
                        vol_3 = float(row["recent_volume_3"]) if not pd.isna(row["recent_volume_3"]) else vol_10
                        if not (vol_10 > 0 and vol_3 / vol_10 > strat["pullback_volume_ratio_max"]):
                            pattern, candle_high, candle_low = detect_confirmation_candle(df, idx)
                            if pattern:
                                setup_type = "pullback"

                # Try continuation setup
                if setup_type is None and enable_continuation:
                    cont_pattern, cont_ch, cont_cl = detect_continuation_setup(df, idx, strat)
                    if cont_pattern:
                        setup_type = "continuation"
                        pattern = cont_pattern
                        candle_high = cont_ch
                        candle_low = cont_cl

                if setup_type is None:
                    continue

                # Build order
                trigger_buffer = strat.get("entry_trigger_buffer_atr", 0.05)
                limit_buffer = strat.get("entry_limit_buffer_atr", 0.15)
                trigger = candle_high + trigger_buffer * atr
                limit_price = trigger + limit_buffer * atr
                stop_candle = candle_low - strat.get("candle_low_stop_atr_buffer", 0.1) * atr
                stop_atr = trigger - strat.get("atr_stop_multiplier", 2.0) * atr
                stop = min(stop_candle, stop_atr)

                if stop <= 0 or stop >= trigger:
                    continue

                qty = risk_position_size(equity, risk_per_trade, trigger, stop,
                                         strat["max_alloc_fraction_per_symbol"])
                if qty <= 0:
                    continue

                pending_orders[sym] = {
                    "trigger": round(trigger, 2),
                    "limit": round(limit_price, 2),
                    "stop": round(stop, 2),
                    "qty": qty,
                    "days_pending": 0,
                    "setup_type": setup_type,
                }
                orders_today += 1

        # Equity curve
        open_value = sum(
            pos["qty"] * float(symbol_data[sym].loc[date]["Close"])
            for sym, pos in positions.items()
            if sym in symbol_data and date in symbol_data[sym].index
        )
        equity_curve.append(cash + open_value)

    # ── Compute metrics ──
    final_equity = equity_curve[-1] if equity_curve else INITIAL_EQUITY
    return_pct = round((final_equity / INITIAL_EQUITY - 1) * 100, 2)

    n_trades = len(closed_trades)
    if n_trades > 0:
        df_t = pd.DataFrame(closed_trades)
        winners = df_t[df_t["pnl"] > 0]
        losers = df_t[df_t["pnl"] <= 0]
        win_rate = round(len(winners) / n_trades * 100, 1)
        avg_r = round(df_t["r_multiple"].mean(), 2)
        total_wins = winners["pnl"].sum()
        total_losses = abs(losers["pnl"].sum())
        pf = round(total_wins / total_losses, 2) if total_losses > 0 else 99.0
        avg_hold = round(df_t["bars_held"].mean(), 1)

        eq_series = pd.Series(equity_curve)
        peak = eq_series.cummax()
        dd = eq_series - peak
        max_dd = round(dd.min(), 0)
    else:
        win_rate = 0
        avg_r = 0
        pf = 0
        avg_hold = 0
        max_dd = 0

    exposure = round(invested_days / total_days * 100, 1) if total_days > 0 else 0

    # Per-setup breakdown
    setup_breakdown = {}
    if n_trades > 0:
        df_t = pd.DataFrame(closed_trades)
        for st in df_t["setup_type"].unique():
            sub = df_t[df_t["setup_type"] == st]
            w = sub[sub["pnl"] > 0]
            l = sub[sub["pnl"] <= 0]
            setup_breakdown[st] = {
                "trades": len(sub),
                "win_rate": round(len(w) / len(sub) * 100, 1) if len(sub) > 0 else 0,
                "avg_r": round(sub["r_multiple"].mean(), 2),
                "pnl": round(sub["pnl"].sum(), 2),
            }

    return {
        "return_pct": return_pct,
        "trades": n_trades,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "profit_factor": pf,
        "max_drawdown_dollars": max_dd,
        "exposure_pct": exposure,
        "avg_hold_days": avg_hold,
        "setup_breakdown": setup_breakdown,
    }


def main():
    strategy = load_strategy()

    # Collect ALL unique symbols needed
    all_symbols = sorted(set(
        U0_TICKERS + U1_EXTRA + U2_EXTRA + U4_TICKERS +
        ["SPY", "QQQ", "RSP", "SMH"]
    ))

    print("=" * 70)
    print("  BACKTEST MATRIX")
    print(f"  Period: {START_DATE} → {END_DATE} | Capital: ${INITIAL_EQUITY:,.0f}")
    print(f"  Downloading {len(all_symbols)} symbols...")
    print("=" * 70)

    lookback_start = (datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = download_all_data(all_symbols, lookback_start, END_DATE)

    symbol_data = {}
    for sym in all_symbols:
        try:
            df = get_symbol_df(raw, sym)
            if not df.empty and len(df) > 200:
                symbol_data[sym] = add_indicators(df, strategy)
        except Exception:
            pass

    spy_dates = symbol_data["SPY"].index
    trade_start = pd.Timestamp(START_DATE)
    trading_days = spy_dates[spy_dates >= trade_start]

    print(f"  Trading days: {len(trading_days)} | Symbols loaded: {len(symbol_data)}")
    print()

    results = []

    def run_and_record(run_id, category, variant_name, symbols, overrides=None, enable_continuation=False):
        valid_syms = [s for s in symbols if s in symbol_data]
        print(f"  [{run_id}] {category}: {variant_name} ({len(valid_syms)} syms)...", end="", flush=True)
        m = run_single_backtest(valid_syms, strategy, symbol_data, trading_days,
                                overrides=overrides, enable_continuation=enable_continuation)
        m["run_id"] = run_id
        m["category"] = category
        m["variant_name"] = variant_name
        results.append(m)
        print(f" → {m['return_pct']:+.1f}% | {m['trades']} trades | PF {m['profit_factor']:.2f} | DD ${m['max_drawdown_dollars']:,.0f}")
        return m

    # ══════════════════════════════════════════════════════════════
    # 1. BASELINE
    # ══════════════════════════════════════════════════════════════
    print("── Baseline ──")
    baseline = run_and_record("U0", "Universe", "Baseline 40-sym", U0_TICKERS)

    # ══════════════════════════════════════════════════════════════
    # 2. UNIVERSE TESTS
    # ══════════════════════════════════════════════════════════════
    print("\n── Universe Tests ──")
    run_and_record("U1", "Universe", "+10 quality names", U0_TICKERS + U1_EXTRA)
    run_and_record("U2", "Universe", "+20 quality names", U0_TICKERS + U2_EXTRA)
    run_and_record("U3", "Universe", "XLK only (no SMH)", U3_TICKERS)
    run_and_record("U4", "Universe", "SMH replaces XLK", U4_TICKERS)

    # ══════════════════════════════════════════════════════════════
    # 3. PORTFOLIO TESTS
    # ══════════════════════════════════════════════════════════════
    print("\n── Portfolio Tests ──")
    run_and_record("P1", "Portfolio", "max_positions +1",
                   U0_TICKERS, {"breadth_modes.full_risk.max_open_positions": 6,
                                "breadth_modes.reduced_risk.max_open_positions": 5})
    run_and_record("P2", "Portfolio", "max_positions +2",
                   U0_TICKERS, {"breadth_modes.full_risk.max_open_positions": 7,
                                "breadth_modes.reduced_risk.max_open_positions": 6})

    run_and_record("CR1", "Portfolio", "cash reserve *0.8",
                   U0_TICKERS, {"cash_reserve_pct_full": 0.20, "cash_reserve_pct_reduced": 0.32})

    run_and_record("PR1", "Portfolio", "portfolio risk +0.5%",
                   U0_TICKERS, {"max_total_portfolio_risk_pct": 0.035})
    run_and_record("PR2", "Portfolio", "portfolio risk +1.0%",
                   U0_TICKERS, {"max_total_portfolio_risk_pct": 0.04})

    # Correlation cap (backtest doesn't enforce correlation, so these test max_positions interaction)
    run_and_record("CC1", "Correlation", "threshold 0.85, max_corr 2",
                   U0_TICKERS, {"correlation_cap.threshold": 0.85})
    run_and_record("CC2", "Correlation", "threshold 0.80, max_corr 3",
                   U0_TICKERS, {"correlation_cap.max_correlated_positions": 3})
    run_and_record("CC3", "Correlation", "threshold 0.85, max_corr 3",
                   U0_TICKERS, {"correlation_cap.threshold": 0.85, "correlation_cap.max_correlated_positions": 3})

    # ══════════════════════════════════════════════════════════════
    # 4. PARAMETER TESTS
    # ══════════════════════════════════════════════════════════════
    print("\n── Entry Trigger Buffer ──")
    run_and_record("ETB0", "Params", "trigger_buffer 0.03", U0_TICKERS, {"entry_trigger_buffer_atr": 0.03})
    run_and_record("ETB2", "Params", "trigger_buffer 0.07", U0_TICKERS, {"entry_trigger_buffer_atr": 0.07})

    print("\n── Entry Limit Buffer ──")
    run_and_record("ELB0", "Params", "limit_buffer 0.10", U0_TICKERS, {"entry_limit_buffer_atr": 0.10})
    run_and_record("ELB2", "Params", "limit_buffer 0.20", U0_TICKERS, {"entry_limit_buffer_atr": 0.20})

    print("\n── Initial Stop Multiplier ──")
    run_and_record("ISM0", "Params", "atr_stop_mult 1.75", U0_TICKERS, {"atr_stop_multiplier": 1.75})
    run_and_record("ISM2", "Params", "atr_stop_mult 2.25", U0_TICKERS, {"atr_stop_multiplier": 2.25})

    print("\n── Candle Low Buffer ──")
    run_and_record("CLB0", "Params", "candle_low_buffer 0.05", U0_TICKERS, {"candle_low_stop_atr_buffer": 0.05})
    run_and_record("CLB2", "Params", "candle_low_buffer 0.15", U0_TICKERS, {"candle_low_stop_atr_buffer": 0.15})

    print("\n── ATR Filter ──")
    run_and_record("ATRF0", "Params", "max_atr_pct 0.05", U0_TICKERS, {"max_atr_percent": 0.05})
    run_and_record("ATRF2", "Params", "max_atr_pct 0.07", U0_TICKERS, {"max_atr_percent": 0.07})

    print("\n── Trailing Stop ──")
    run_and_record("TS0", "Params", "trail 2.50 ATR", U0_TICKERS, {"trailing_atr_multiplier": 2.50})
    run_and_record("TS1", "Params", "trail 2.75 ATR", U0_TICKERS, {"trailing_atr_multiplier": 2.75})
    run_and_record("TS3", "Params", "trail 3.25 ATR", U0_TICKERS, {"trailing_atr_multiplier": 3.25})

    print("\n── Early Invalidation ──")
    run_and_record("EIB0", "Params", "early_inval 2 bars", U0_TICKERS, {"early_invalidation_bars": 2})
    run_and_record("EIB2", "Params", "early_inval 4 bars", U0_TICKERS, {"early_invalidation_bars": 4})

    # ══════════════════════════════════════════════════════════════
    # 5. SETUP EXPANSION
    # ══════════════════════════════════════════════════════════════
    print("\n── Setup Expansion ──")
    s1 = run_and_record("S1", "Setup", "pullback + continuation",
                        U0_TICKERS, enable_continuation=True)
    if s1.get("setup_breakdown"):
        for st, info in s1["setup_breakdown"].items():
            print(f"       {st}: {info['trades']} trades, {info['win_rate']}% win, {info['avg_r']}R avg, ${info['pnl']:,.0f}")

    # ══════════════════════════════════════════════════════════════
    # 6. BEST-OF-BREED COMBINATIONS
    # ══════════════════════════════════════════════════════════════
    print("\n── Best-of-Breed Combinations ──")

    # Find best universe, best portfolio tweak, best trailing
    universe_runs = [r for r in results if r["category"] == "Universe"]
    best_universe = max(universe_runs, key=lambda r: r["return_pct"])
    best_u_name = best_universe["run_id"]

    param_runs = [r for r in results if r["category"] == "Params" and "trail" in r["variant_name"]]
    best_trail = max(param_runs, key=lambda r: r["return_pct"]) if param_runs else None

    portfolio_runs = [r for r in results if r["category"] == "Portfolio"]
    best_portfolio = max(portfolio_runs, key=lambda r: r["return_pct"]) if portfolio_runs else None

    # Determine best universe symbols
    if best_u_name == "U1":
        best_u_syms = U0_TICKERS + U1_EXTRA
    elif best_u_name == "U2":
        best_u_syms = U0_TICKERS + U2_EXTRA
    elif best_u_name == "U4":
        best_u_syms = U4_TICKERS
    else:
        best_u_syms = U0_TICKERS

    print(f"  Best universe: {best_universe['variant_name']} ({best_u_name})")
    if best_trail:
        print(f"  Best trail: {best_trail['variant_name']} ({best_trail['run_id']})")
    if best_portfolio:
        print(f"  Best portfolio: {best_portfolio['variant_name']} ({best_portfolio['run_id']})")

    # Combo 1: best universe + best portfolio
    if best_portfolio and best_u_name != "U0":
        # Extract portfolio overrides
        p_overrides = {}
        if "max_positions +1" in best_portfolio["variant_name"]:
            p_overrides = {"breadth_modes.full_risk.max_open_positions": 6,
                           "breadth_modes.reduced_risk.max_open_positions": 5}
        elif "max_positions +2" in best_portfolio["variant_name"]:
            p_overrides = {"breadth_modes.full_risk.max_open_positions": 7,
                           "breadth_modes.reduced_risk.max_open_positions": 6}
        elif "cash reserve" in best_portfolio["variant_name"]:
            p_overrides = {"cash_reserve_pct_full": 0.20, "cash_reserve_pct_reduced": 0.32}
        elif "+0.5%" in best_portfolio["variant_name"]:
            p_overrides = {"max_total_portfolio_risk_pct": 0.035}
        elif "+1.0%" in best_portfolio["variant_name"]:
            p_overrides = {"max_total_portfolio_risk_pct": 0.04}

        if p_overrides:
            run_and_record("C1", "Combo", f"best_universe + best_portfolio",
                           best_u_syms, p_overrides)

    # Combo 2: best universe + best trail
    if best_trail:
        trail_val = float(best_trail["variant_name"].split()[1])
        run_and_record("C2", "Combo", f"best_universe + trail {trail_val}",
                       best_u_syms, {"trailing_atr_multiplier": trail_val})

    # Combo 3: best universe + best portfolio + best trail
    if best_portfolio and best_trail:
        combo_overrides = dict(p_overrides) if p_overrides else {}
        trail_val = float(best_trail["variant_name"].split()[1])
        combo_overrides["trailing_atr_multiplier"] = trail_val
        run_and_record("C3", "Combo", f"best_uni + best_port + best_trail",
                       best_u_syms, combo_overrides)

    # ══════════════════════════════════════════════════════════════
    # OUTPUT
    # ══════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 90)
    print("  FULL RESULTS")
    print("=" * 90)
    header = f"  {'ID':<6} {'Category':<12} {'Variant':<35} {'Ret%':>7} {'Trades':>6} {'Win%':>6} {'AvgR':>6} {'PF':>6} {'MaxDD':>9} {'Exp%':>6} {'Hold':>5}"
    print(header)
    print("  " + "─" * 88)

    for r in results:
        print(f"  {r['run_id']:<6} {r['category']:<12} {r['variant_name']:<35} "
              f"{r['return_pct']:>+6.1f}% {r['trades']:>6} {r['win_rate']:>5.1f}% "
              f"{r['avg_r']:>+5.2f}R {r['profit_factor']:>5.2f} {r['max_drawdown_dollars']:>8,.0f} "
              f"{r['exposure_pct']:>5.1f}% {r['avg_hold_days']:>5.1f}")

    # Rankings
    bl_pf = baseline["profit_factor"]
    bl_trades = baseline["trades"]
    bl_dd = baseline["max_drawdown_dollars"]
    bl_ret = baseline["return_pct"]

    for title, key, reverse in [
        ("TOP 10 BY RETURN", "return_pct", True),
        ("TOP 10 BY PROFIT FACTOR", "profit_factor", True),
        ("TOP 10 BY LOWEST DRAWDOWN", "max_drawdown_dollars", False),
    ]:
        print(f"\n  ── {title} ──")
        sorted_r = sorted(results, key=lambda r: r[key], reverse=reverse)[:10]
        for i, r in enumerate(sorted_r, 1):
            flags = []
            # Flag: trades +15% and PF within 10%
            if r["trades"] >= bl_trades * 1.15 and abs(r["profit_factor"] - bl_pf) / bl_pf <= 0.10:
                flags.append("📈 MORE_TRADES_STABLE_PF")
            # Flag: return improves, DD doesn't worsen materially (>20%)
            if r["return_pct"] > bl_ret and r["max_drawdown_dollars"] >= bl_dd * 1.2:
                flags.append("✅ BETTER_RETURN_OK_DD")
            flag_str = " " + " ".join(flags) if flags else ""
            rec = "PROMOTE" if r["return_pct"] > bl_ret * 1.05 and r["profit_factor"] >= bl_pf * 0.9 else \
                  "WATCH" if r["return_pct"] >= bl_ret * 0.9 else "REJECT"
            print(f"    {i:>2}. [{r['run_id']}] {r['variant_name']:<35} "
                  f"{r[key]:>+8.1f}{'%' if 'pct' in key else ''} | {rec}{flag_str}")

    # Save CSV
    csv_path = STATE_DIR / "backtest_matrix.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "run_id", "category", "variant_name", "return_pct", "trades",
            "win_rate", "avg_r", "profit_factor", "max_drawdown_dollars",
            "exposure_pct", "avg_hold_days",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({k: r[k] for k in writer.fieldnames})
    print(f"\n  CSV saved: {csv_path}")

    # Save full JSON
    save_json(STATE_DIR / "backtest_matrix.json", {
        "config": {"start": START_DATE, "end": END_DATE, "equity": INITIAL_EQUITY},
        "baseline": baseline,
        "results": results,
    })
    print(f"  JSON saved: {STATE_DIR / 'backtest_matrix.json'}")
    print("=" * 90)


if __name__ == "__main__":
    main()

