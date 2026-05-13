"""
Walk-Forward Backtest — Out-of-sample validation for the growth bot.

Instead of optimizing on the full period (overfitting risk), this:
1. Splits history into rolling TRAIN (optimize) + TEST (validate) windows
2. Runs the strategy on each TEST window using the same fixed parameters
3. Stitches TEST results together for a realistic performance estimate

This tells you if the strategy works on data it has NEVER seen.

Usage:
    python scripts/backtest/walk_forward.py
    python scripts/backtest/walk_forward.py --train-months 12 --test-months 6
"""
import json
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from backtest.growth import (
    load_growth_strategy,
    load_growth_watchlist,
    download_all_data,
    get_symbol_df,
    add_indicators,
    compute_relative_strength,
    compute_growth_score,
    detect_breakout,
    detect_continuation,
    detect_shallow_pullback,
)
from common import CONFIG_DIR, STATE_DIR, save_json
from infra.sizing import risk_position_size


def run_single_window(symbol_data, spy_df, strategy, symbols,
                      start_date, end_date, initial_equity,
                      slippage_pct=0.001, label=""):
    """Run the growth strategy on a single date window. Returns (final_equity, trades, equity_curve)."""
    filters = strategy["filters"]
    exit_cfg = strategy["exit"]

    trade_start = pd.Timestamp(start_date)
    trade_end = pd.Timestamp(end_date)
    spy_dates = spy_df.index
    trading_days = spy_dates[(spy_dates >= trade_start) & (spy_dates <= trade_end)]

    if len(trading_days) == 0:
        return initial_equity, [], []

    equity = initial_equity
    cash = initial_equity
    positions = {}
    pending_orders = {}
    closed_trades = []
    equity_curve = []

    for day_idx, date in enumerate(trading_days):
        if date not in spy_df.index:
            continue
        spy_row = spy_df.loc[date]
        qqq_df = symbol_data.get("QQQ")
        if qqq_df is None or date not in qqq_df.index:
            continue
        qqq_row = qqq_df.loc[date]

        spy_close = float(spy_row["Close"])
        spy_sma50 = float(spy_row["sma50"]) if not pd.isna(spy_row["sma50"]) else 0
        qqq_close = float(qqq_row["Close"])
        qqq_sma50 = float(qqq_row["sma50"]) if not pd.isna(qqq_row["sma50"]) else 0

        spy_ok = spy_close > spy_sma50
        qqq_ok = qqq_close > qqq_sma50

        if spy_ok and qqq_ok:
            regime_mode = "full_risk"
        elif spy_ok or qqq_ok:
            regime_mode = "reduced_risk"
        else:
            regime_mode = "risk_off"

        regime_cfg = strategy["regime"].get(regime_mode, {})
        allow_entries = regime_cfg.get("allow_new_entries", regime_mode != "risk_off")
        risk_per_trade = regime_cfg.get("risk_per_trade", 0)
        max_positions = regime_cfg.get("max_open_positions", 0)
        max_alloc = regime_cfg.get("max_alloc_fraction_per_symbol", 0.25)

        # Check pending orders
        for sym in list(pending_orders.keys()):
            order = pending_orders[sym]
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue
            bar = symbol_data[sym].loc[date]
            high = float(bar["High"])

            order["days_pending"] += 1
            if order["days_pending"] > strategy["entry"].get("stale_order_cancel_days", 2):
                del pending_orders[sym]
                continue

            if high >= order["trigger"]:
                fill_price = order["trigger"] * (1 + slippage_pct)
                if fill_price > order["limit"]:
                    continue
                cost = order["qty"] * fill_price
                if cash < cost:
                    del pending_orders[sym]
                    continue

                positions[sym] = {
                    "entry": fill_price, "stop": order["stop"],
                    "qty": order["qty"], "r": order["r_per_share"],
                    "atr": order["atr"], "phase": "initial",
                    "bars_held": 0, "bars_in_profit": 0,
                    "best_price": fill_price, "entry_date": str(date.date()),
                    "setup_type": order.get("setup_type", "?"),
                }
                cash -= cost
                del pending_orders[sym]

        # Manage positions
        for sym in list(positions.keys()):
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue
            pos = positions[sym]
            bar = symbol_data[sym].loc[date]
            close = float(bar["Close"])
            low = float(bar["Low"])
            pos["bars_held"] += 1
            pos["best_price"] = max(pos["best_price"], close)
            if close > pos["entry"]:
                pos["bars_in_profit"] += 1

            atr = pos["atr"]
            r = pos["r"]
            current_r = (close - pos["entry"]) / r if r > 0 else 0
            exit_price = None
            exit_reason = None

            # Time stop
            if (exit_cfg["time_stop_enabled"] and pos["phase"] == "initial"
                    and pos["bars_held"] >= exit_cfg["time_stop_bars"] and current_r < 0.5):
                exit_price = close * (1 - slippage_pct)
                exit_reason = "time_stop"

            # Stop hit
            if exit_price is None and low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - slippage_pct)
                exit_reason = f"stop_{pos['phase']}"

            # Phase transitions
            if exit_price is None:
                protected_r = exit_cfg["phase_protected_r"]
                trailing_r = exit_cfg["phase_trailing_r"]
                trailing_bars = exit_cfg["phase_trailing_bars_in_profit"]
                trailing_mult = exit_cfg["trailing_atr_multiplier"]
                protected_buffer = exit_cfg["protected_stop_buffer_atr"]

                if pos["phase"] == "initial" and current_r >= protected_r:
                    pos["phase"] = "protected"
                    pos["stop"] = pos["entry"] - protected_buffer * atr

                elif pos["phase"] == "protected":
                    should_trail = (current_r >= trailing_r) or (pos["bars_in_profit"] >= trailing_bars and current_r > 0.5)
                    if should_trail:
                        pos["phase"] = "trailing"
                        trail = trailing_mult * atr
                        pos["stop"] = pos["best_price"] - trail

                elif pos["phase"] == "trailing":
                    # Trail upgrades
                    trail = trailing_mult * atr
                    tight_mult = exit_cfg.get("trailing_tight_atr_multiplier", 2.0)
                    tight_r = exit_cfg.get("trailing_tight_threshold_r", 3.0)
                    if current_r >= tight_r:
                        trail = tight_mult * atr
                    if current_r >= 5.0:
                        trail = 1.75 * atr
                    if current_r >= 6.0:
                        trail = 1.5 * atr
                    new_stop = pos["best_price"] - trail
                    pos["stop"] = max(pos["stop"], new_stop)

            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["qty"]
                r_multiple = (exit_price - pos["entry"]) / r if r > 0 else 0
                cash += pos["qty"] * exit_price
                closed_trades.append({
                    "symbol": sym, "entry_price": round(pos["entry"], 2),
                    "exit_price": round(exit_price, 2), "qty": pos["qty"],
                    "pnl": round(pnl, 2), "r_multiple": round(r_multiple, 2),
                    "bars_held": pos["bars_held"], "exit_reason": exit_reason,
                    "entry_date": pos["entry_date"], "exit_date": str(date.date()),
                    "phase_at_exit": pos["phase"], "setup_type": pos.get("setup_type", "?"),
                    "window": label,
                })
                del positions[sym]

        # Research & entries
        if allow_entries and len(positions) + len(pending_orders) < max_positions:
            scored = []
            for sym in symbols:
                if sym in ("SPY", "QQQ") or sym in positions or sym in pending_orders:
                    continue
                if sym not in symbol_data or date not in symbol_data[sym].index:
                    continue
                df = symbol_data[sym]
                idx = df.index.get_loc(date)
                if idx < 200:
                    continue

                row = df.iloc[idx]
                close = float(row["Close"])
                atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0
                sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
                sma200 = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0

                if close < filters["min_price"]:
                    continue
                avg_dv = float(row["avg_dollar_volume"]) if not pd.isna(row["avg_dollar_volume"]) else 0
                if avg_dv < filters["min_avg_dollar_volume"]:
                    continue
                if atr <= 0 or atr / close > filters["max_atr_percent"]:
                    continue
                if sma200 > 0 and close < sma200:
                    continue
                if sma50 > 0 and sma200 > 0 and sma50 < sma200:
                    continue

                rs_3m = compute_relative_strength(df, spy_df, idx, 63)
                rs_6m = compute_relative_strength(df, spy_df, idx, 126)
                trend_strength = (close - sma50) / sma50 if sma50 > 0 else 0
                score = compute_growth_score(rs_3m, rs_6m, trend_strength, strategy)
                scored.append({"symbol": sym, "score": score, "idx": idx, "atr": atr})

            scored.sort(key=lambda x: x["score"], reverse=True)
            top_pct = strategy["ranking"]["top_percentile"]
            cutoff = max(1, int(len(scored) * top_pct / 100))
            leaders = scored[:cutoff]

            for leader in leaders:
                if len(positions) + len(pending_orders) >= max_positions:
                    break
                sym = leader["symbol"]
                df = symbol_data[sym]
                idx = leader["idx"]
                atr = leader["atr"]

                setup = None
                for detector in [detect_breakout, detect_continuation, detect_shallow_pullback]:
                    result = detector(df, idx, strategy)
                    if result:
                        setup = result
                        break
                if setup is None:
                    continue

                trigger_buffer = strategy["entry"]["trigger_buffer_atr"]
                limit_buffer = strategy["entry"]["limit_buffer_atr"]
                trigger = setup["setup_high"] + trigger_buffer * atr
                limit_price = trigger + limit_buffer * atr

                stop_cfg = strategy["stop"]
                stop_candle = setup["setup_low"] - stop_cfg["setup_low_buffer_atr"] * atr
                stop_atr = trigger - stop_cfg["atr_stop_multiplier"] * atr
                stop = min(stop_candle, stop_atr)

                if stop <= 0 or stop >= trigger:
                    continue

                r_per_share = trigger - stop
                qty = risk_position_size(equity, risk_per_trade, trigger, stop, max_alloc)

                # Volatility-targeted sizing
                vol_cfg = strategy.get("volatility_sizing", {})
                if vol_cfg.get("enabled", False) and atr > 0:
                    atr_pct = atr / trigger
                    for bucket in vol_cfg.get("atr_pct_buckets", []):
                        if atr_pct <= bucket["max"]:
                            qty = max(1, int(qty * bucket["scalar"]))
                            break

                if qty <= 0:
                    continue
                cost = qty * limit_price
                if cash - cost < equity * 0.05:
                    continue

                pending_orders[sym] = {
                    "trigger": round(trigger, 2), "limit": round(limit_price, 2),
                    "stop": round(stop, 2), "qty": qty, "days_pending": 0,
                    "r_per_share": round(r_per_share, 2), "atr": atr,
                    "setup_type": setup["setup_type"],
                }

        # Equity
        open_value = sum(
            pos["qty"] * float(symbol_data[sym].loc[date]["Close"])
            for sym, pos in positions.items()
            if sym in symbol_data and date in symbol_data[sym].index
        )
        total_equity = cash + open_value
        equity = total_equity
        equity_curve.append({
            "date": str(date.date()), "equity": round(total_equity, 2),
            "open_positions": len(positions),
        })

    return equity, closed_trades, equity_curve


def run_walk_forward(train_months=12, test_months=6, initial_equity=20000.0,
                     start_date="2024-01-01", end_date=None, slippage_pct=0.001):
    """
    Walk-forward analysis with rolling train/test windows.

    Each window:
    - TRAIN: used for context (indicators need lookback), but no parameter optimization
      since we're using fixed strategy params. The train window ensures indicators are warm.
    - TEST: out-of-sample results that get stitched together.

    For a true parameter optimization walk-forward, you'd vary params in train
    and pick the best for test. Here we validate that fixed params work across
    different market regimes (which is the right first step).
    """
    strategy = load_growth_strategy()
    symbols = load_growth_watchlist()

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Download all data once (with lookback for indicators)
    data_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=500)).strftime("%Y-%m-%d")
    raw = download_all_data(symbols, data_start, end_date)

    symbol_data = {}
    for sym in symbols + ["SPY", "QQQ"]:
        try:
            df = get_symbol_df(raw, sym)
            if not df.empty and len(df) > 200:
                symbol_data[sym] = add_indicators(df, strategy)
        except Exception:
            pass

    if "SPY" not in symbol_data or "QQQ" not in symbol_data:
        raise RuntimeError("Missing SPY/QQQ data")

    spy_df = symbol_data["SPY"]

    # Generate test windows
    current_start = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    windows = []
    while current_start < end_dt:
        window_end = min(current_start + timedelta(days=test_months * 30), end_dt)
        windows.append((current_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
        current_start = window_end

    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD ANALYSIS — Growth Bot")
    print(f"{'='*70}")
    print(f"  Strategy    : {strategy['name']} v{strategy['version']}")
    print(f"  Full period : {start_date} → {end_date}")
    print(f"  Test windows: {len(windows)} × {test_months} months")
    print(f"  Universe    : {len(symbols)} symbols")
    print(f"  Vol sizing  : {'ON' if strategy.get('volatility_sizing', {}).get('enabled') else 'OFF'}")
    print(f"  Initial     : ${initial_equity:,.0f}")
    print(f"{'='*70}\n")

    # Run each window
    all_trades = []
    all_equity = []
    window_results = []
    running_equity = initial_equity

    for i, (w_start, w_end) in enumerate(windows):
        label = f"W{i+1}"
        print(f"  {label}: {w_start} → {w_end} (starting equity: ${running_equity:,.0f})")

        final_eq, trades, eq_curve = run_single_window(
            symbol_data, spy_df, strategy, symbols,
            w_start, w_end, running_equity,
            slippage_pct=slippage_pct, label=label,
        )

        ret_pct = (final_eq / running_equity - 1) * 100
        wins = len([t for t in trades if t["pnl"] > 0])
        losses = len([t for t in trades if t["pnl"] <= 0])
        total_pnl = sum(t["pnl"] for t in trades)

        window_results.append({
            "window": label, "start": w_start, "end": w_end,
            "start_equity": round(running_equity, 2),
            "end_equity": round(final_eq, 2),
            "return_pct": round(ret_pct, 2),
            "trades": len(trades), "wins": wins, "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        })

        print(f"         → ${final_eq:,.0f} ({ret_pct:+.1f}%) | {len(trades)} trades ({wins}W/{losses}L) | P&L: ${total_pnl:+,.0f}")

        all_trades.extend(trades)
        all_equity.extend(eq_curve)
        running_equity = final_eq

    # ── COMBINED RESULTS ──
    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD COMBINED RESULTS (Out-of-Sample)")
    print(f"{'='*70}")

    final_equity = running_equity
    total_return = (final_equity / initial_equity - 1) * 100
    print(f"  Initial Equity : ${initial_equity:,.2f}")
    print(f"  Final Equity   : ${final_equity:,.2f}")
    print(f"  Total Return   : {total_return:+.2f}%")
    print(f"  Total Trades   : {len(all_trades)}")

    if all_trades:
        df_trades = pd.DataFrame(all_trades)
        winners = df_trades[df_trades["pnl"] > 0]
        losers = df_trades[df_trades["pnl"] <= 0]
        total_wins = winners["pnl"].sum()
        total_losses = abs(losers["pnl"].sum())

        win_rate = len(winners) / len(df_trades) * 100
        pf = total_wins / total_losses if total_losses > 0 else float('inf')
        avg_r = df_trades["r_multiple"].mean()

        print(f"  Win Rate       : {win_rate:.1f}%")
        print(f"  Profit Factor  : {pf:.2f}" if pf != float('inf') else "  Profit Factor  : ∞")
        print(f"  Avg R-Multiple : {avg_r:.2f}R")
        print(f"  Avg Trade P&L  : ${df_trades['pnl'].mean():,.2f}")
        print(f"  Best Trade     : ${df_trades['pnl'].max():,.2f} ({df_trades['r_multiple'].max():.1f}R)")
        print(f"  Worst Trade    : ${df_trades['pnl'].min():,.2f} ({df_trades['r_multiple'].min():.1f}R)")
        print(f"  Avg Bars Held  : {df_trades['bars_held'].mean():.1f}")

        # Max drawdown from equity curve
        if all_equity:
            eq_series = pd.Series([e["equity"] for e in all_equity])
            peak = eq_series.cummax()
            drawdown = (eq_series - peak) / peak * 100
            print(f"  Max Drawdown   : {drawdown.min():.2f}%")

        # Exit reasons
        print(f"\n  Exit Reasons:")
        for reason, count in df_trades["exit_reason"].value_counts().items():
            avg_r = df_trades[df_trades["exit_reason"] == reason]["r_multiple"].mean()
            print(f"    {reason:30s}: {count:3d} trades | avg R={avg_r:+.2f}")

        # Setup types
        if "setup_type" in df_trades.columns:
            print(f"\n  Setup Types:")
            for setup, count in df_trades["setup_type"].value_counts().items():
                avg_r = df_trades[df_trades["setup_type"] == setup]["r_multiple"].mean()
                wr = len(df_trades[(df_trades["setup_type"] == setup) & (df_trades["pnl"] > 0)]) / count * 100
                print(f"    {setup:30s}: {count:3d} trades | WR={wr:.0f}% | avg R={avg_r:+.2f}")

        # Per-window table
        print(f"\n  Per-Window Breakdown:")
        print(f"  {'Window':<8} {'Period':<25} {'Return':>8} {'Trades':>7} {'WR':>6} {'P&L':>10}")
        print(f"  {'-'*8} {'-'*25} {'-'*8} {'-'*7} {'-'*6} {'-'*10}")
        for w in window_results:
            print(f"  {w['window']:<8} {w['start']} → {w['end']:<10} {w['return_pct']:>+7.1f}% "
                  f"{w['trades']:>6}  {w['win_rate']:>5.0f}% ${w['total_pnl']:>+9,.0f}")

        # Consistency check
        positive_windows = len([w for w in window_results if w["return_pct"] > 0])
        total_windows = len(window_results)
        print(f"\n  Consistency: {positive_windows}/{total_windows} windows profitable "
              f"({positive_windows/total_windows*100:.0f}%)")

    print(f"\n{'='*70}")

    # Compare with full-period backtest
    print(f"\n  💡 Compare this with a full-period backtest to check for overfitting.")
    print(f"     If walk-forward results are significantly worse, the strategy")
    print(f"     may be curve-fit to historical data.")

    # Save results
    results = {
        "type": "walk_forward",
        "start_date": start_date,
        "end_date": end_date,
        "train_months": train_months,
        "test_months": test_months,
        "initial_equity": initial_equity,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades": len(all_trades),
        "windows": window_results,
        "trades": all_trades,
    }
    save_json(STATE_DIR / "backtest_walk_forward.json", results)
    print(f"\n  Results saved to state/backtest_walk_forward.json")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward backtest for growth bot")
    parser.add_argument("--train-months", type=int, default=12, help="Train window months (for indicator warmup)")
    parser.add_argument("--test-months", type=int, default=6, help="Test window months")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--equity", type=float, default=20000, help="Initial equity")
    args = parser.parse_args()

    run_walk_forward(
        train_months=args.train_months,
        test_months=args.test_months,
        initial_equity=args.equity,
        start_date=args.start,
        end_date=args.end,
    )

