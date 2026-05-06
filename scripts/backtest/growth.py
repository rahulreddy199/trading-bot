"""
Backtester for Growth Bot V1 momentum strategy.

Simulates day-by-day:
- Research: regime filter, universal trend filter, RS ranking, setup detection
- Entry: stop-limit trigger above setup high
- Management: initial stop → protected at 1.5R → trailing at 2.5R
- Time stop: exit after 10 bars if < 0.5R progress

Includes slippage and realistic fills.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, CONFIG_DIR, risk_position_size, save_json


def load_growth_strategy():
    return json.loads((CONFIG_DIR / "strategy_growth.json").read_text())


def load_growth_watchlist():
    data = json.loads((CONFIG_DIR / "watchlist_growth.json").read_text())
    return [s["ticker"] for s in data["symbols"] if s.get("enabled", True)]


def download_all_data(symbols, start_date, end_date):
    all_symbols = sorted(set(symbols + ["SPY", "QQQ"]))
    print(f"Downloading data for {len(all_symbols)} symbols...")
    raw = yf.download(all_symbols, start=start_date, end=end_date,
                      interval="1d", auto_adjust=True, progress=True, threads=False)
    return raw


def get_symbol_df(raw, symbol):
    if isinstance(raw.columns, pd.MultiIndex):
        if symbol in raw.columns.get_level_values(0):
            df = raw[symbol].dropna().copy()
        elif symbol in raw.columns.get_level_values(1):
            df = raw.xs(symbol, level=1, axis=1).dropna().copy()
        else:
            return pd.DataFrame()
    else:
        df = raw.dropna().copy()
    return df


def add_indicators(df, strategy):
    ind = strategy["indicators"]
    df = df.copy()
    df["ema10"] = df["Close"].ewm(span=ind["ema_fast"], adjust=False).mean()
    df["sma20"] = df["Close"].rolling(ind["sma_fast"]).mean()
    df["sma50"] = df["Close"].rolling(ind["sma_mid"]).mean()
    df["sma200"] = df["Close"].rolling(ind["sma_slow"]).mean()

    tr = pd.concat([
        (df["High"] - df["Low"]),
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"] - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(ind["atr_period"]).mean()
    df["high_20d"] = df["High"].rolling(ind["high_20d"]).max()
    df["high_55d"] = df["High"].rolling(ind["high_55d"]).max()
    df["avg_volume_20"] = df["Volume"].rolling(ind["volume_avg_period"]).mean()
    df["avg_dollar_volume"] = df["avg_volume_20"] * df["Close"]
    return df


def compute_relative_strength(df, spy_df, idx, lookback):
    if idx < lookback:
        return 0.0
    sym_ret = float(df["Close"].iloc[idx]) / float(df["Close"].iloc[idx - lookback]) - 1
    spy_idx = min(idx, len(spy_df) - 1)
    spy_start = max(0, spy_idx - lookback)
    spy_ret = float(spy_df["Close"].iloc[spy_idx]) / float(spy_df["Close"].iloc[spy_start]) - 1
    return sym_ret - spy_ret


def compute_growth_score(rs_3m, rs_6m, trend_strength, strategy):
    rank_cfg = strategy["ranking"]
    cap = rank_cfg["trend_strength_cap_pct"]
    norm_rs_3m = max(0, min(1, (rs_3m + 0.3) / 0.6))
    norm_rs_6m = max(0, min(1, (rs_6m + 0.3) / 0.6))
    capped_trend = min(max(trend_strength, 0), cap)
    norm_trend = capped_trend / cap if cap > 0 else 0
    score = (norm_rs_3m * rank_cfg["rs_3m_weight"] +
             norm_rs_6m * rank_cfg["rs_6m_weight"] +
             norm_trend * rank_cfg["trend_strength_weight"])
    return round(score * 100, 2)


def detect_breakout(df, idx, strategy):
    if idx < 60:
        return None
    row = df.iloc[idx]
    close = float(row["Close"])
    high_20d = float(row["high_20d"]) if not pd.isna(row["high_20d"]) else 0
    high_55d = float(row["high_55d"]) if not pd.isna(row["high_55d"]) else 0
    sma20 = float(row["sma20"]) if not pd.isna(row["sma20"]) else 0
    sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
    sma200 = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0

    if not (close > sma20 and close > sma50 and close > sma200):
        return None

    near_pct = strategy["setups"]["breakout"]["near_high_pct"]
    is_20d = close >= high_20d * (1 - near_pct)
    is_55d = close >= high_55d * (1 - near_pct)

    if not (is_20d or is_55d):
        return None

    return {"setup_type": "breakout", "setup_high": float(row["High"]), "setup_low": float(row["Low"])}


def detect_continuation(df, idx, strategy):
    if idx < 60:
        return None
    cfg = strategy["setups"]["continuation"]
    row = df.iloc[idx]
    close = float(row["Close"])
    open_price = float(row["Open"])
    sma20 = float(row["sma20"]) if not pd.isna(row["sma20"]) else 0
    sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
    high_20d = float(row["high_20d"]) if not pd.isna(row["high_20d"]) else 0

    if close < sma20 or sma20 < sma50:
        return None

    recent_highs = df["High"].iloc[max(0, idx-10):idx+1]
    if recent_highs.max() < high_20d * 0.98:
        return None

    pullback_bars = 0
    for i in range(2, min(cfg["max_pullback_bars"] + 2, idx)):
        bar = df.iloc[idx - i]
        if float(bar["High"]) < float(df.iloc[idx - i - 1]["High"]):
            pullback_bars += 1
        else:
            break

    if pullback_bars < 1 or pullback_bars > cfg["max_pullback_bars"]:
        return None

    if cfg["require_green_close"] and close <= open_price:
        return None

    return {"setup_type": "continuation", "setup_high": float(row["High"]), "setup_low": float(row["Low"])}


def detect_shallow_pullback(df, idx, strategy):
    if idx < 60:
        return None
    cfg = strategy["setups"]["shallow_pullback"]
    row = df.iloc[idx]
    close = float(row["Close"])
    sma20 = float(row["sma20"]) if not pd.isna(row["sma20"]) else 0
    sma50 = float(row["sma50"]) if not pd.isna(row["sma50"]) else 0
    sma200 = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0
    atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0
    high_20d = float(row["high_20d"]) if not pd.isna(row["high_20d"]) else 0

    if atr <= 0:
        return None
    if not (close > sma20 and close > sma50 and close > sma200):
        return None

    depth = high_20d - close
    depth_atr = depth / atr
    if depth_atr < 0.3 or depth_atr > cfg["max_depth_atr"]:
        return None

    ema10 = float(row["ema10"]) if not pd.isna(row["ema10"]) else sma20
    if close < ema10 * 0.98:
        return None

    return {"setup_type": "shallow_pullback", "setup_high": float(row["High"]), "setup_low": float(row["Low"])}


def run_backtest(start_date="2024-05-01", end_date=None, initial_equity=20000.0, slippage_pct=0.001):
    strategy = load_growth_strategy()
    symbols = load_growth_watchlist()
    filters = strategy["filters"]
    exit_cfg = strategy["exit"]

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    lookback_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = download_all_data(symbols, lookback_start, end_date)

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
    spy_dates = spy_df.index
    trade_start = pd.Timestamp(start_date)
    trading_days = spy_dates[spy_dates >= trade_start]

    print(f"\n{'='*60}")
    print(f"GROWTH BOT BACKTEST")
    print(f"{'='*60}")
    print(f"Period: {start_date} → {end_date} ({len(trading_days)} trading days)")
    print(f"Universe: {len(symbols)} symbols | Initial: ${initial_equity:,.0f}")
    print(f"Strategy: {strategy['name']} v{strategy['version']}")
    print(f"{'='*60}\n")

    # State
    equity = initial_equity
    cash = initial_equity
    positions = {}  # symbol -> {entry, stop, qty, r, phase, bars_held, bars_in_profit, best_price, atr, entry_date}
    pending_orders = {}  # symbol -> {trigger, limit, stop, qty, days_pending, r_per_share, atr}
    closed_trades = []
    equity_curve = []

    for day_idx, date in enumerate(trading_days):
        # --- REGIME CHECK ---
        if date not in spy_df.index:
            continue
        spy_row = spy_df.loc[date]
        qqq_df = symbol_data["QQQ"]
        if date not in qqq_df.index:
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

        # --- CHECK PENDING ORDERS ---
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
                    "entry": fill_price,
                    "stop": order["stop"],
                    "qty": order["qty"],
                    "r": order["r_per_share"],
                    "atr": order["atr"],
                    "phase": "initial",
                    "bars_held": 0,
                    "bars_in_profit": 0,
                    "best_price": fill_price,
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
            pos["best_price"] = max(pos["best_price"], close)
            if close > pos["entry"]:
                pos["bars_in_profit"] += 1

            atr = pos["atr"]
            r = pos["r"]
            current_r = (close - pos["entry"]) / r if r > 0 else 0
            exit_price = None
            exit_reason = None

            # TIME STOP
            if (exit_cfg["time_stop_enabled"] and pos["phase"] == "initial"
                    and pos["bars_held"] >= exit_cfg["time_stop_bars"] and current_r < 0.5):
                exit_price = close * (1 - slippage_pct)
                exit_reason = "time_stop"

            # STOP HIT
            if exit_price is None and low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - slippage_pct)
                exit_reason = f"stop_{pos['phase']}"

            # PHASE TRANSITIONS
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
                    trail = trailing_mult * atr
                    new_stop = pos["best_price"] - trail
                    pos["stop"] = max(pos["stop"], new_stop)

            # EXECUTE EXIT
            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["qty"]
                r_multiple = (exit_price - pos["entry"]) / r if r > 0 else 0
                cash += pos["qty"] * exit_price
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
                    "phase_at_exit": pos["phase"],
                })
                del positions[sym]

        # --- RESEARCH & ENTRIES ---
        if allow_entries and len(positions) + len(pending_orders) < max_positions:
            # Score all symbols
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

                # Filters
                if close < filters["min_price"]:
                    continue
                avg_dv = float(row["avg_dollar_volume"]) if not pd.isna(row["avg_dollar_volume"]) else 0
                if avg_dv < filters["min_avg_dollar_volume"]:
                    continue
                if atr <= 0 or atr / close > filters["max_atr_percent"]:
                    continue

                # Universal trend filter
                if sma200 > 0 and close < sma200:
                    continue
                if sma50 > 0 and sma200 > 0 and sma50 < sma200:
                    continue

                rs_3m = compute_relative_strength(df, spy_df, idx, 63)
                rs_6m = compute_relative_strength(df, spy_df, idx, 126)
                trend_strength = (close - sma50) / sma50 if sma50 > 0 else 0
                score = compute_growth_score(rs_3m, rs_6m, trend_strength, strategy)

                scored.append({"symbol": sym, "score": score, "idx": idx, "atr": atr})

            # Rank and take top percentile
            scored.sort(key=lambda x: x["score"], reverse=True)
            top_pct = strategy["ranking"]["top_percentile"]
            cutoff = max(1, int(len(scored) * top_pct / 100))
            leaders = scored[:cutoff]

            # Detect setups for leaders
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

                # Compute entry/stop
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
                if qty <= 0:
                    continue

                cost = qty * limit_price
                if cash - cost < equity * 0.05:
                    continue

                pending_orders[sym] = {
                    "trigger": round(trigger, 2),
                    "limit": round(limit_price, 2),
                    "stop": round(stop, 2),
                    "qty": qty,
                    "days_pending": 0,
                    "r_per_share": round(r_per_share, 2),
                    "atr": atr,
                    "setup_type": setup["setup_type"],
                }

        # Equity curve
        open_value = sum(
            pos["qty"] * float(symbol_data[sym].loc[date]["Close"])
            for sym, pos in positions.items()
            if sym in symbol_data and date in symbol_data[sym].index
        )
        total_equity = cash + open_value
        equity = total_equity  # Update equity for sizing
        equity_curve.append({
            "date": str(date.date()),
            "equity": round(total_equity, 2),
            "cash": round(cash, 2),
            "open_positions": len(positions),
            "pending_orders": len(pending_orders),
        })

        if day_idx % 60 == 0 and day_idx > 0:
            print(f"  {date.date()} | Equity: ${total_equity:,.0f} ({(total_equity/initial_equity-1)*100:+.1f}%) | Open: {len(positions)} | Trades: {len(closed_trades)}")

    # Final
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_equity

    # --- REPORT ---
    print(f"\n{'='*60}")
    print(f"GROWTH BOT BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"Period         : {start_date} → {end_date}")
    print(f"Initial Equity : ${initial_equity:,.2f}")
    print(f"Final Equity   : ${final_equity:,.2f}")
    print(f"Total Return   : {(final_equity/initial_equity-1)*100:+.2f}%")
    print(f"Total Trades   : {len(closed_trades)}")

    if closed_trades:
        df_trades = pd.DataFrame(closed_trades)
        winners = df_trades[df_trades["pnl"] > 0]
        losers = df_trades[df_trades["pnl"] <= 0]
        total_wins = winners["pnl"].sum()
        total_losses = abs(losers["pnl"].sum())

        print(f"Winners        : {len(winners)} ({len(winners)/len(df_trades)*100:.1f}%)")
        print(f"Losers         : {len(losers)} ({len(losers)/len(df_trades)*100:.1f}%)")
        print(f"Profit Factor  : {total_wins/total_losses:.2f}" if total_losses > 0 else "Profit Factor  : ∞")
        print(f"Total P&L      : ${df_trades['pnl'].sum():,.2f}")
        print(f"Avg Trade P&L  : ${df_trades['pnl'].mean():,.2f}")
        print(f"Avg R-Multiple : {df_trades['r_multiple'].mean():.2f}R")
        print(f"Best Trade     : ${df_trades['pnl'].max():,.2f} ({df_trades['r_multiple'].max():.1f}R)")
        print(f"Worst Trade    : ${df_trades['pnl'].min():,.2f} ({df_trades['r_multiple'].min():.1f}R)")
        print(f"Avg Bars Held  : {df_trades['bars_held'].mean():.1f}")

        print(f"\nExit Reasons:")
        for reason, count in df_trades["exit_reason"].value_counts().items():
            avg_r = df_trades[df_trades["exit_reason"] == reason]["r_multiple"].mean()
            print(f"  {reason:30s}: {count:3d} trades | avg R={avg_r:+.2f}")

        print(f"\nSetup Types (from exit data):")
        # Phase at exit
        print(f"\nPhase at Exit:")
        for phase, count in df_trades["phase_at_exit"].value_counts().items():
            avg_r = df_trades[df_trades["phase_at_exit"] == phase]["r_multiple"].mean()
            print(f"  {phase:20s}: {count:3d} trades | avg R={avg_r:+.2f}")

        # Max drawdown
        eq_series = pd.Series([e["equity"] for e in equity_curve])
        peak = eq_series.cummax()
        drawdown = (eq_series - peak) / peak * 100
        print(f"\nMax Drawdown   : {drawdown.min():.2f}%")

        # Monthly returns
        print(f"\nMonthly Returns:")
        eq_df = pd.DataFrame(equity_curve)
        eq_df["date"] = pd.to_datetime(eq_df["date"])
        eq_df = eq_df.set_index("date")
        monthly = eq_df["equity"].resample("ME").last()
        prev = initial_equity
        for month_end, eq_val in monthly.items():
            ret = (eq_val / prev - 1) * 100
            print(f"  {month_end.strftime('%Y-%m')}: ${eq_val:>10,.0f} ({ret:+.1f}%)")
            prev = eq_val

    print(f"\n{'='*60}")

    # Save results
    save_json(STATE_DIR / "backtest_growth_results.json", {
        "start_date": start_date,
        "end_date": end_date,
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "total_return_pct": round((final_equity / initial_equity - 1) * 100, 2),
        "total_trades": len(closed_trades),
        "trades": closed_trades,
    })
    save_json(STATE_DIR / "backtest_growth_equity_curve.json", equity_curve)
    print(f"\nResults saved to state/backtest_growth_results.json")

    return final_equity, closed_trades


if __name__ == "__main__":
    run_backtest(start_date="2024-05-01", end_date="2026-05-01", initial_equity=20000)

