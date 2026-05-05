"""
Backtester for v2 pullback strategy.

Downloads all data upfront, then simulates day-by-day:
- Research: regime filter, breadth, trend, pullback, confirmation candle
- Entry: stop-limit trigger above candle high
- Management: initial stop → breakeven at 1R → trailing at 2R
- Early invalidation: close below 50 SMA within 3 bars

Includes slippage and realistic fills.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

from common import STATE_DIR, load_strategy, load_watchlist, risk_position_size, save_json


def download_all_data(symbols, start_date, end_date):
    """Download all historical data upfront to avoid look-ahead bias."""
    print(f"Downloading data for {len(symbols)} symbols...")
    all_symbols = sorted(set(symbols + ["SPY", "QQQ", "RSP"]))
    raw = yf.download(all_symbols, start=start_date, end=end_date,
                      interval="1d", auto_adjust=False, progress=True,
                      group_by="ticker", threads=False)
    return raw


def get_symbol_df(raw, symbol):
    if isinstance(raw.columns, pd.MultiIndex):
        df = raw[symbol].dropna().copy()
    else:
        df = raw.dropna().copy()
    return df


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
    df["high_20d"] = df["High"].rolling(20).max()
    df["avg_volume_10"] = df["Volume"].rolling(10).mean()
    df["recent_volume_3"] = df["Volume"].rolling(3).mean()
    return df


def detect_confirmation_candle(df, idx):
    """Detect hammer, bullish engulfing, or morning star at bar index idx."""
    if idx < 3:
        return None, None, None

    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    body = abs(c - o)
    lower_wick = min(c, o) - l
    upper_wick = h - max(c, o)
    candle_range = h - l

    if candle_range == 0:
        return None, None, None

    # Hammer
    if lower_wick >= 2 * body and upper_wick < body and c >= o:
        if (min(c, o) - l) >= 0.6 * candle_range:
            return "hammer", h, l

    # Bullish engulfing
    prev_o, prev_c = float(prev["Open"]), float(prev["Close"])
    if prev_c < prev_o and c > o:
        if c > prev_o and o < prev_c:
            return "bullish_engulfing", h, l

    # Morning star (3-bar pattern)
    if idx >= 3:
        bar1 = df.iloc[idx - 2]  # First: big red candle
        bar2 = df.iloc[idx - 1]  # Second: small body (star)
        bar3 = row              # Third: big green candle (current)

        b1_o, b1_c = float(bar1["Open"]), float(bar1["Close"])
        b2_o, b2_c, b2_h, b2_l = float(bar2["Open"]), float(bar2["Close"]), float(bar2["High"]), float(bar2["Low"])
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


def detect_pullback(df, idx, strategy):
    """Check for valid 2-7 day pullback at bar index idx."""
    if idx < 20:
        return False, 0

    min_days = strategy["pullback_days_min"]
    max_days = strategy["pullback_days_max"]
    high_20d = float(df["high_20d"].iloc[idx])
    sma50 = float(df["sma50"].iloc[idx])

    pullback_days = 0
    pullback_low = float('inf')
    for i in range(1, min(idx, max_days + 1)):
        bar = df.iloc[idx - i]
        if float(bar["High"]) >= high_20d * 0.998:
            break
        pullback_days += 1
        pullback_low = min(pullback_low, float(bar["Low"]))

    if pullback_days < min_days or pullback_days > max_days:
        return False, 0
    if pullback_low < sma50:
        return False, 0

    return True, pullback_days


def run_backtest(start_date="2024-01-01", end_date=None, initial_equity=100000.0, slippage_pct=0.001):
    strategy = load_strategy()
    symbols = load_watchlist()

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Download lookback buffer
    lookback_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = download_all_data(symbols, lookback_start, end_date)

    # Prepare indicator data for all symbols
    symbol_data = {}
    for sym in symbols + ["SPY", "QQQ", "RSP"]:
        df = get_symbol_df(raw, sym)
        if not df.empty and len(df) > 200:
            symbol_data[sym] = add_indicators(df, strategy)

    if "SPY" not in symbol_data or "QQQ" not in symbol_data:
        raise RuntimeError("Missing benchmark data")

    # Get trading dates
    spy_dates = symbol_data["SPY"].index
    trade_start = pd.Timestamp(start_date)
    trading_days = spy_dates[spy_dates >= trade_start]

    print(f"\nBacktesting {len(trading_days)} trading days from {start_date} to {end_date}")
    print(f"Universe: {len(symbols)} symbols | Initial equity: ${initial_equity:,.0f}")
    print(f"Strategy: {strategy['name']} v{strategy['version']}\n")

    # State
    equity = initial_equity
    cash = initial_equity
    positions = {}  # symbol -> {entry, stop, qty, r, phase, bars_held, highest_close}
    pending_orders = {}  # symbol -> {trigger, limit, stop, qty, days_pending, candle_high}
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

        # --- BREADTH (simplified: RSP above 50 SMA) ---
        breadth_ok = True
        if "RSP" in symbol_data and date in symbol_data["RSP"].index:
            rsp = symbol_data["RSP"].loc[date]
            if not pd.isna(rsp["sma50"]):
                breadth_ok = float(rsp["Close"]) > float(rsp["sma50"])

        # --- CHECK PENDING ORDERS (did price trigger today?) ---
        for sym in list(pending_orders.keys()):
            order = pending_orders[sym]
            if sym not in symbol_data or date not in symbol_data[sym].index:
                continue
            bar = symbol_data[sym].loc[date]
            high = float(bar["High"])

            order["days_pending"] += 1
            if order["days_pending"] > strategy.get("stale_order_cancel_days", 2):
                del pending_orders[sym]
                continue

            # Check if trigger hit
            if high >= order["trigger"]:
                fill_price = order["trigger"] * (1 + slippage_pct)
                if fill_price > order["limit"]:
                    continue  # Would exceed limit, no fill

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
                    trail = strategy.get("trailing_atr_multiplier", 2.5) * atr
                    pos["stop"] = pos["highest_close"] - trail

                elif pos["phase"] == "trailing":
                    trail = strategy.get("trailing_atr_multiplier", 2.5) * atr
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
            for sym in symbols:
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
                sma50 = float(row["sma50"])
                sma200 = float(row["sma200"])
                atr = float(row["atr14"])

                if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma200) or pd.isna(atr):
                    continue
                if atr <= 0:
                    continue

                # Trend filter
                if not (close > sma50 > sma200):
                    continue

                # ATR% filter
                if atr / close > strategy.get("max_atr_percent", 0.06):
                    continue

                # Pullback
                is_pb, pb_days = detect_pullback(df, idx, strategy)
                if not is_pb:
                    continue

                # SMA20 distance
                pct_from_sma20 = (close / sma20) - 1
                if pct_from_sma20 < strategy["pullback_min_distance_from_sma20_pct"]:
                    continue
                if pct_from_sma20 > strategy["pullback_max_distance_from_sma20_pct"]:
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
                if len(pending_orders) >= 2:  # Max 2 new orders per day
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
            "cash": round(cash, 2),
            "open_positions": len(positions),
            "pending_orders": len(pending_orders),
        })

        # Progress
        if day_idx % 60 == 0 and day_idx > 0:
            cur_eq = cash + open_value
            print(f"  {date.date()} | Equity: ${cur_eq:,.0f} | Open: {len(positions)} | Trades: {len(closed_trades)}")

    # Final mark-to-market
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_equity

    # --- REPORT ---
    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS: {start_date} → {end_date}")
    print(f"{'='*60}")
    print(f"Initial Equity : ${initial_equity:,.2f}")
    print(f"Final Equity   : ${final_equity:,.2f} ({(final_equity/initial_equity-1)*100:+.2f}%)")
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
        print(f"Avg Trade      : ${df_trades['pnl'].mean():,.2f}")
        print(f"Avg R-Multiple : {df_trades['r_multiple'].mean():.2f}R")
        print(f"Best Trade     : ${df_trades['pnl'].max():,.2f} ({df_trades['r_multiple'].max():.1f}R)")
        print(f"Worst Trade    : ${df_trades['pnl'].min():,.2f} ({df_trades['r_multiple'].min():.1f}R)")
        print(f"Avg Bars Held  : {df_trades['bars_held'].mean():.1f}")
        print(f"\nExit Reasons:")
        for reason, count in df_trades["exit_reason"].value_counts().items():
            print(f"  {reason}: {count}")

        # Max drawdown
        eq_series = pd.Series([e["equity"] for e in equity_curve])
        peak = eq_series.cummax()
        drawdown = (eq_series - peak) / peak * 100
        print(f"\nMax Drawdown   : {drawdown.min():.2f}%")

    print(f"{'='*60}")

    # Save results
    save_json(STATE_DIR / "backtest_results.json", {
        "start_date": start_date,
        "end_date": end_date,
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "total_trades": len(closed_trades),
        "trades": closed_trades,
    })
    save_json(STATE_DIR / "backtest_equity_curve.json", equity_curve)
    print(f"\nResults saved to state/backtest_results.json")

    return final_equity, closed_trades


if __name__ == "__main__":
    run_backtest(start_date="2024-01-01", initial_equity=100000)