"""
Strategy Variant Backtester

Tests 5 strategy improvements independently against the baseline:
1. Breadth: RSP proxy vs universe breadth (% of watchlist above 50 SMA)
2. Exits: Current no-partial vs 25% partial at 2R vs 50% partial at 2R
3. Ranking: RS-only vs composite score (RS + pullback tightness + confirmation quality + volume contraction)
4. Correlation: Sector caps only vs sector + correlation cap
5. Calendar: Earnings blackout only vs earnings + FOMC/CPI/NFP skip

Each variant is tested in isolation against a frozen baseline over a 2-year window.
Results saved to state/variant_results/ for comparison.
"""
import json
import math
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, load_json, save_json, load_strategy, load_watchlist

RESULTS_DIR = STATE_DIR / "variant_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────

def download_all_data(symbols, start_date, end_date):
    all_symbols = sorted(set(symbols + ["SPY", "QQQ", "RSP", "^VIX"]))
    print(f"  Downloading {len(all_symbols)} symbols ({start_date} → {end_date})...")
    raw = yf.download(all_symbols, start=start_date, end=end_date,
                      interval="1d", auto_adjust=False, progress=False,
                      group_by="ticker", threads=False)
    return raw


def get_symbol_df(raw, symbol):
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            df = raw[symbol].dropna().copy()
        else:
            df = raw.dropna().copy()
        return df
    except Exception:
        return pd.DataFrame()


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
    # For relative strength
    df["return_126d"] = df["Close"].pct_change(126)
    return df


# ──────────────────────────────────────────────
# CANDLE & PULLBACK DETECTION (from backtest.py)
# ──────────────────────────────────────────────

def detect_confirmation_candle(df, idx):
    if idx < 3:
        return None, None, None, 0.0
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    body = abs(c - o)
    lower_wick = min(c, o) - l
    upper_wick = h - max(c, o)
    candle_range = h - l
    if candle_range == 0:
        return None, None, None, 0.0

    # Quality score: how far close is in upper half of range (0-1)
    close_location = (c - l) / candle_range

    # Hammer
    if lower_wick >= 2 * body and upper_wick < body and c >= o:
        if (min(c, o) - l) >= 0.6 * candle_range:
            return "hammer", h, l, close_location
    # Bullish engulfing
    prev_o, prev_c = float(prev["Open"]), float(prev["Close"])
    if prev_c < prev_o and c > o:
        if c > prev_o and o < prev_c:
            return "bullish_engulfing", h, l, close_location
    # Morning star
    if idx >= 3:
        bar1 = df.iloc[idx - 2]
        bar2 = df.iloc[idx - 1]
        bar3 = row
        b1_o, b1_c = float(bar1["Open"]), float(bar1["Close"])
        b2_o, b2_c, b2_h, b2_l = float(bar2["Open"]), float(bar2["Close"]), float(bar2["High"]), float(bar2["Low"])
        b3_o, b3_c = o, c
        b1_body = abs(b1_c - b1_o)
        b2_body = abs(b2_c - b2_o)
        b3_body = abs(b3_c - b3_o)
        if (b1_c < b1_o and b1_body > 0
                and b2_body < b1_body * 0.4
                and b3_c > b3_o and b3_body > 0
                and b3_c > (b1_o + b1_c) / 2):
            return "morning_star", h, min(l, b2_l), close_location
    return None, None, None, 0.0


def detect_pullback(df, idx, strategy):
    if idx < 20:
        return False, 0, 0.0
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
        return False, 0, 0.0
    if pullback_low < sma50:
        return False, 0, 0.0
    # Tightness: how close the pullback low stayed to sma20
    sma20 = float(df["sma20"].iloc[idx])
    tightness = 1.0 - min(abs(pullback_low - sma20) / sma20, 0.1) / 0.1 if sma20 > 0 else 0.5
    return True, pullback_days, tightness


# ──────────────────────────────────────────────
# FOMC / CPI / NFP CALENDAR (approximate)
# ──────────────────────────────────────────────

def build_macro_event_dates(start_year, end_year):
    """Build approximate FOMC/CPI/NFP dates. In production, use a calendar API."""
    events = set()
    for year in range(start_year, end_year + 1):
        # FOMC: ~8 meetings per year, roughly every 6 weeks (Wed)
        fomc_months = [1, 3, 5, 6, 7, 9, 11, 12]
        for m in fomc_months:
            # Approximate: 3rd Wednesday of FOMC months
            d = datetime(year, m, 1)
            # Find 3rd Wednesday
            wed_count = 0
            while wed_count < 3:
                if d.weekday() == 2:
                    wed_count += 1
                    if wed_count == 3:
                        break
                d += timedelta(days=1)
            events.add(d.strftime("%Y-%m-%d"))

        # CPI: typically 2nd Tuesday or Wednesday of each month
        for m in range(1, 13):
            d = datetime(year, m, 10)  # Approximate CPI release around 10th-13th
            while d.weekday() >= 5:
                d += timedelta(days=1)
            events.add(d.strftime("%Y-%m-%d"))

        # NFP: first Friday of each month
        for m in range(1, 13):
            d = datetime(year, m, 1)
            while d.weekday() != 4:
                d += timedelta(days=1)
            events.add(d.strftime("%Y-%m-%d"))

    return events


# ──────────────────────────────────────────────
# CORE BACKTEST ENGINE (parameterized)
# ──────────────────────────────────────────────

def run_single_backtest(
    symbol_data,
    symbols,
    strategy,
    trading_days,
    initial_equity=100000.0,
    slippage_pct=0.001,
    # Variant flags
    breadth_mode="rsp",           # "rsp" or "universe"
    exit_mode="no_partial",       # "no_partial", "partial_25", "partial_50"
    ranking_mode="rs_only",       # "rs_only" or "composite"
    correlation_cap=False,        # True to add correlation filtering
    macro_filter=False,           # True to skip FOMC/CPI/NFP days
    label="baseline",
):
    """Run a single backtest with specific variant settings."""
    macro_dates = set()
    if macro_filter:
        macro_dates = build_macro_event_dates(
            trading_days[0].year - 1, trading_days[-1].year + 1
        )

    equity = initial_equity
    cash = initial_equity
    positions = {}
    pending_orders = {}
    closed_trades = []
    equity_curve = []

    for day_idx, date in enumerate(trading_days):
        date_str = str(date.date())

        # --- REGIME CHECK ---
        spy_row = symbol_data["SPY"].loc[date] if date in symbol_data["SPY"].index else None
        qqq_row = symbol_data["QQQ"].loc[date] if date in symbol_data["QQQ"].index else None
        if spy_row is None or qqq_row is None:
            continue
        spy_ok = float(spy_row["Close"]) > float(spy_row["sma50"]) and float(spy_row["Close"]) > float(spy_row["sma200"])
        qqq_ok = float(qqq_row["Close"]) > float(qqq_row["sma50"]) and float(qqq_row["Close"]) > float(qqq_row["sma200"])
        regime_on = spy_ok and qqq_ok

        # --- BREADTH CHECK ---
        if breadth_mode == "rsp":
            breadth_ok = True
            if "RSP" in symbol_data and date in symbol_data["RSP"].index:
                rsp = symbol_data["RSP"].loc[date]
                if not pd.isna(rsp["sma50"]):
                    breadth_ok = float(rsp["Close"]) > float(rsp["sma50"])
        elif breadth_mode == "universe":
            # Count % of tradable universe above their own 50 SMA
            above_50 = 0
            total_checked = 0
            for sym in symbols:
                if sym in symbol_data and date in symbol_data[sym].index:
                    row = symbol_data[sym].loc[date]
                    if not pd.isna(row["sma50"]):
                        total_checked += 1
                        if float(row["Close"]) > float(row["sma50"]):
                            above_50 += 1
            universe_breadth_pct = (above_50 / total_checked * 100) if total_checked > 0 else 50
            breadth_ok = universe_breadth_pct >= 50  # At least 50% of universe above 50 SMA
        else:
            breadth_ok = True

        # --- MACRO FILTER ---
        macro_skip = macro_filter and date_str in macro_dates

        # --- CHECK PENDING ORDERS ---
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
                    "r": fill_price - order["stop"],
                    "phase": "initial",
                    "bars_held": 0,
                    "highest_close": fill_price,
                    "entry_date": date_str,
                    "score": order.get("score", 0),
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
            sma50_val = float(bar["sma50"]) if not pd.isna(bar["sma50"]) else 0
            if pos["phase"] == "initial" and pos["bars_held"] <= strategy.get("early_invalidation_bars", 3):
                if close < sma50_val and sma50_val > 0:
                    exit_price = close * (1 - slippage_pct)
                    exit_reason = "early_invalidation"

            # Stop hit
            if exit_price is None and low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - slippage_pct)
                exit_reason = f"stop_{pos['phase']}"

            # Phase transitions & partial exits
            if exit_price is None:
                target_1r = pos["entry"] + pos["r"]
                target_2r = pos["entry"] + strategy["reward_to_risk"] * pos["r"]

                if pos["phase"] == "initial" and close >= target_1r:
                    pos["phase"] = "breakeven"
                    pos["stop"] = pos["entry"] + strategy.get("breakeven_buffer_atr", 0.1) * atr

                elif pos["phase"] == "breakeven" and close >= target_2r:
                    # --- EXIT VARIANT: partial at 2R ---
                    partial_pct = 0.0
                    if exit_mode == "partial_25":
                        partial_pct = 0.25
                    elif exit_mode == "partial_50":
                        partial_pct = 0.50

                    if partial_pct > 0 and pos["qty"] > 1:
                        partial_qty = max(1, int(pos["qty"] * partial_pct))
                        partial_exit_price = close * (1 - slippage_pct)
                        partial_pnl = (partial_exit_price - pos["entry"]) * partial_qty
                        partial_r = (partial_exit_price - pos["entry"]) / pos["r"] if pos["r"] > 0 else 0
                        cash += partial_qty * partial_exit_price
                        closed_trades.append({
                            "symbol": sym, "entry_price": round(pos["entry"], 2),
                            "exit_price": round(partial_exit_price, 2), "qty": partial_qty,
                            "pnl": round(partial_pnl, 2), "r_multiple": round(partial_r, 2),
                            "bars_held": pos["bars_held"], "exit_reason": f"partial_{int(partial_pct*100)}_at_2R",
                            "entry_date": pos["entry_date"], "exit_date": date_str,
                        })
                        pos["qty"] -= partial_qty

                    pos["phase"] = "trailing"
                    trail = strategy.get("trailing_atr_multiplier", 3.0) * atr
                    pos["stop"] = pos["highest_close"] - trail

                elif pos["phase"] == "trailing":
                    trail = strategy.get("trailing_atr_multiplier", 3.0) * atr
                    new_stop = pos["highest_close"] - trail
                    pos["stop"] = max(pos["stop"], new_stop)

            # Execute exit
            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["qty"]
                r_multiple = (exit_price - pos["entry"]) / pos["r"] if pos["r"] > 0 else 0
                cash += pos["qty"] * exit_price
                closed_trades.append({
                    "symbol": sym, "entry_price": round(pos["entry"], 2),
                    "exit_price": round(exit_price, 2), "qty": pos["qty"],
                    "pnl": round(pnl, 2), "r_multiple": round(r_multiple, 2),
                    "bars_held": pos["bars_held"], "exit_reason": exit_reason,
                    "entry_date": pos["entry_date"], "exit_date": date_str,
                })
                del positions[sym]

        # --- RESEARCH & NEW ENTRIES ---
        max_pos = strategy["breadth_modes"]["full_risk"]["max_open_positions"]
        if regime_on and breadth_ok and not macro_skip and len(positions) + len(pending_orders) < max_pos:
            # Build candidate list with scores
            candidates = []
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
                if pd.isna(sma20) or pd.isna(sma50) or pd.isna(sma200) or pd.isna(atr) or atr <= 0:
                    continue
                if not (close > sma50 > sma200):
                    continue
                if atr / close > strategy.get("max_atr_percent", 0.06):
                    continue

                is_pb, pb_days, pb_tightness = detect_pullback(df, idx, strategy)
                if not is_pb:
                    continue

                pct_from_sma20 = (close / sma20) - 1
                if pct_from_sma20 < strategy["pullback_min_distance_from_sma20_pct"]:
                    continue
                if pct_from_sma20 > strategy["pullback_max_distance_from_sma20_pct"]:
                    continue

                vol_10 = float(row["avg_volume_10"]) if not pd.isna(row["avg_volume_10"]) else 1
                vol_3 = float(row["recent_volume_3"]) if not pd.isna(row["recent_volume_3"]) else vol_10
                if vol_10 > 0 and vol_3 / vol_10 > strategy["pullback_volume_ratio_max"]:
                    continue

                pattern, candle_high, candle_low, candle_quality = detect_confirmation_candle(df, idx)
                if pattern is None:
                    continue

                # Volume contraction score: lower recent vol vs avg = better
                vol_contraction = 1.0 - min(vol_3 / vol_10, 1.5) / 1.5 if vol_10 > 0 else 0.5

                # Relative strength
                rs = float(row["return_126d"]) if not pd.isna(row["return_126d"]) else 0

                # --- RANKING VARIANT ---
                if ranking_mode == "composite":
                    # Composite: RS(40%) + tightness(25%) + candle quality(20%) + vol contraction(15%)
                    score = rs * 0.40 + pb_tightness * 0.25 + candle_quality * 0.20 + vol_contraction * 0.15
                else:
                    score = rs  # RS-only (baseline)

                candidates.append({
                    "symbol": sym, "score": score, "rs": rs,
                    "candle_high": candle_high, "candle_low": candle_low,
                    "atr": atr, "pattern": pattern, "close": close,
                    "pb_tightness": pb_tightness, "candle_quality": candle_quality,
                    "vol_contraction": vol_contraction,
                })

            # Sort by score descending
            candidates.sort(key=lambda x: x["score"], reverse=True)

            # --- CORRELATION VARIANT ---
            if correlation_cap:
                # Simple theme/correlation filter: max 2 from same "cluster"
                # Use return correlation proxy: skip if highly correlated with existing positions
                selected = []
                for cand in candidates:
                    sym = cand["symbol"]
                    # Check if we already have 2 positions with similar recent returns
                    similar_count = 0
                    if sym in symbol_data and date in symbol_data[sym].index:
                        sym_ret = float(symbol_data[sym].loc[date]["return_126d"]) if not pd.isna(symbol_data[sym].loc[date]["return_126d"]) else 0
                        for pos_sym in positions:
                            if pos_sym in symbol_data and date in symbol_data[pos_sym].index:
                                pos_ret = float(symbol_data[pos_sym].loc[date]["return_126d"]) if not pd.isna(symbol_data[pos_sym].loc[date]["return_126d"]) else 0
                                # If both returned >20% and within 10% of each other → "correlated"
                                if abs(sym_ret - pos_ret) < 0.10 and sym_ret > 0.15:
                                    similar_count += 1
                    if similar_count >= 2:
                        continue  # Skip: too correlated with existing positions
                    selected.append(cand)
                candidates = selected

            # Place orders for top candidates
            orders_today = 0
            for cand in candidates:
                if orders_today >= 2:
                    break
                if len(positions) + len(pending_orders) >= max_pos:
                    break
                sym = cand["symbol"]
                atr = cand["atr"]
                trigger_buffer = strategy.get("entry_trigger_buffer_atr", 0.05)
                limit_buffer = strategy.get("entry_limit_buffer_atr", 0.15)
                trigger = cand["candle_high"] + trigger_buffer * atr
                limit_price = trigger + limit_buffer * atr
                stop_candle = cand["candle_low"] - strategy.get("candle_low_stop_atr_buffer", 0.1) * atr
                stop_atr = trigger - strategy.get("atr_stop_multiplier", 2.0) * atr
                stop = min(stop_candle, stop_atr)
                if stop <= 0 or stop >= trigger:
                    continue
                risk_per_trade = strategy["breadth_modes"]["full_risk"]["risk_per_trade"]
                from common import risk_position_size
                qty = risk_position_size(equity, risk_per_trade, trigger, stop,
                                         strategy["max_alloc_fraction_per_symbol"])
                if qty <= 0:
                    continue
                pending_orders[sym] = {
                    "trigger": round(trigger, 2), "limit": round(limit_price, 2),
                    "stop": round(stop, 2), "qty": qty, "days_pending": 0,
                    "score": cand["score"],
                }
                orders_today += 1

        # Equity curve
        open_value = sum(
            pos["qty"] * float(symbol_data[sym].loc[date]["Close"])
            for sym, pos in positions.items()
            if sym in symbol_data and date in symbol_data[sym].index
        )
        equity_curve.append({
            "date": date_str,
            "equity": round(cash + open_value, 2),
            "cash": round(cash, 2),
            "open_positions": len(positions),
        })

    return closed_trades, equity_curve


# ──────────────────────────────────────────────
# METRICS
# ──────────────────────────────────────────────

def compute_metrics(closed_trades, equity_curve, initial_equity, label):
    """Compute comprehensive metrics from backtest results."""
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_equity
    total_return = (final_equity / initial_equity - 1) * 100

    metrics = {
        "label": label,
        "initial_equity": initial_equity,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades": len(closed_trades),
    }

    if not closed_trades:
        return metrics

    df = pd.DataFrame(closed_trades)
    winners = df[df["pnl"] > 0]
    losers = df[df["pnl"] <= 0]
    total_wins = winners["pnl"].sum()
    total_losses = abs(losers["pnl"].sum())

    metrics.update({
        "win_rate_pct": round(len(winners) / len(df) * 100, 1),
        "profit_factor": round(total_wins / total_losses, 2) if total_losses > 0 else 999,
        "total_pnl": round(df["pnl"].sum(), 2),
        "avg_trade_pnl": round(df["pnl"].mean(), 2),
        "avg_r_multiple": round(df["r_multiple"].mean(), 2),
        "median_r_multiple": round(df["r_multiple"].median(), 2),
        "best_r": round(df["r_multiple"].max(), 2),
        "worst_r": round(df["r_multiple"].min(), 2),
        "avg_bars_held": round(df["bars_held"].mean(), 1),
    })

    # Max drawdown
    eq_series = pd.Series([e["equity"] for e in equity_curve])
    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak * 100
    metrics["max_drawdown_pct"] = round(drawdown.min(), 2)

    # Expectancy = avg_win * win_rate - avg_loss * loss_rate
    avg_win = winners["pnl"].mean() if len(winners) > 0 else 0
    avg_loss = abs(losers["pnl"].mean()) if len(losers) > 0 else 0
    win_rate = len(winners) / len(df)
    metrics["expectancy"] = round(avg_win * win_rate - avg_loss * (1 - win_rate), 2)

    # Exit reason breakdown
    exit_reasons = df["exit_reason"].value_counts().to_dict()
    metrics["exit_reasons"] = exit_reasons

    # Avg R by exit reason
    avg_r_by_exit = df.groupby("exit_reason")["r_multiple"].mean().round(2).to_dict()
    metrics["avg_r_by_exit"] = avg_r_by_exit

    return metrics


def print_comparison(all_metrics):
    """Print a comparison table across all variants."""
    print(f"\n{'='*100}")
    print("VARIANT COMPARISON")
    print(f"{'='*100}")

    headers = ["Variant", "Return%", "Trades", "WinRate%", "PF", "AvgR", "MedR", "MaxDD%", "Expectancy"]
    fmt = "{:<30} {:>8} {:>7} {:>8} {:>6} {:>6} {:>6} {:>7} {:>10}"
    print(fmt.format(*headers))
    print("-" * 100)

    for m in all_metrics:
        print(fmt.format(
            m.get("label", "?")[:30],
            f"{m.get('total_return_pct', 0):+.1f}",
            str(m.get("total_trades", 0)),
            f"{m.get('win_rate_pct', 0):.1f}",
            f"{m.get('profit_factor', 0):.2f}",
            f"{m.get('avg_r_multiple', 0):.2f}",
            f"{m.get('median_r_multiple', 0):.2f}",
            f"{m.get('max_drawdown_pct', 0):.1f}",
            f"${m.get('expectancy', 0):,.0f}",
        ))
    print(f"{'='*100}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    start_date = "2024-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    initial_equity = 100000.0

    strategy = load_strategy()
    symbols = load_watchlist()

    print("=" * 70)
    print("  STRATEGY VARIANT BACKTESTER")
    print(f"  Period: {start_date} → {end_date}")
    print(f"  Universe: {len(symbols)} symbols | Equity: ${initial_equity:,.0f}")
    print("=" * 70)

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

    print(f"\n  Trading days: {len(trading_days)}")
    print(f"  Symbols with data: {len(symbol_data) - 3}\n")

    # Define all variants
    variants = [
        # BASELINE
        {"label": "BASELINE (current)", "breadth_mode": "rsp", "exit_mode": "no_partial",
         "ranking_mode": "rs_only", "correlation_cap": False, "macro_filter": False},

        # TEST 1: Breadth
        {"label": "1a. Universe breadth (50%)", "breadth_mode": "universe", "exit_mode": "no_partial",
         "ranking_mode": "rs_only", "correlation_cap": False, "macro_filter": False},

        # TEST 2: Exits
        {"label": "2a. 25% partial at 2R", "breadth_mode": "rsp", "exit_mode": "partial_25",
         "ranking_mode": "rs_only", "correlation_cap": False, "macro_filter": False},
        {"label": "2b. 50% partial at 2R", "breadth_mode": "rsp", "exit_mode": "partial_50",
         "ranking_mode": "rs_only", "correlation_cap": False, "macro_filter": False},

        # TEST 3: Ranking
        {"label": "3a. Composite ranking", "breadth_mode": "rsp", "exit_mode": "no_partial",
         "ranking_mode": "composite", "correlation_cap": False, "macro_filter": False},

        # TEST 4: Correlation
        {"label": "4a. Sector + correlation cap", "breadth_mode": "rsp", "exit_mode": "no_partial",
         "ranking_mode": "rs_only", "correlation_cap": True, "macro_filter": False},

        # TEST 5: Macro calendar
        {"label": "5a. FOMC/CPI/NFP skip", "breadth_mode": "rsp", "exit_mode": "no_partial",
         "ranking_mode": "rs_only", "correlation_cap": False, "macro_filter": True},
    ]

    all_metrics = []

    for i, variant in enumerate(variants):
        label = variant["label"]
        print(f"\n{'─'*60}")
        print(f"  Running: {label}")
        print(f"{'─'*60}")

        trades, eq_curve = run_single_backtest(
            symbol_data=symbol_data,
            symbols=symbols,
            strategy=strategy,
            trading_days=trading_days,
            initial_equity=initial_equity,
            breadth_mode=variant["breadth_mode"],
            exit_mode=variant["exit_mode"],
            ranking_mode=variant["ranking_mode"],
            correlation_cap=variant["correlation_cap"],
            macro_filter=variant["macro_filter"],
            label=label,
        )

        metrics = compute_metrics(trades, eq_curve, initial_equity, label)
        all_metrics.append(metrics)

        # Save individual results
        safe_name = label.replace(" ", "_").replace("/", "-").replace("(", "").replace(")", "").replace("%", "pct")
        save_json(RESULTS_DIR / f"{safe_name}_metrics.json", metrics)
        save_json(RESULTS_DIR / f"{safe_name}_trades.json", trades)

        # Print summary
        print(f"  Return: {metrics.get('total_return_pct', 0):+.1f}% | "
              f"Trades: {metrics.get('total_trades', 0)} | "
              f"WinRate: {metrics.get('win_rate_pct', 0):.1f}% | "
              f"PF: {metrics.get('profit_factor', 0):.2f} | "
              f"AvgR: {metrics.get('avg_r_multiple', 0):.2f} | "
              f"MaxDD: {metrics.get('max_drawdown_pct', 0):.1f}%")

    # Print comparison table
    print_comparison(all_metrics)

    # Save combined results
    save_json(RESULTS_DIR / "all_variants_comparison.json", all_metrics)
    print(f"\nAll results saved to {RESULTS_DIR}/")

    # Print recommendations
    print(f"\n{'='*70}")
    print("  ANALYSIS NOTES")
    print(f"{'='*70}")
    baseline = all_metrics[0]
    for m in all_metrics[1:]:
        delta_r = m.get("total_return_pct", 0) - baseline.get("total_return_pct", 0)
        delta_dd = m.get("max_drawdown_pct", 0) - baseline.get("max_drawdown_pct", 0)
        delta_pf = m.get("profit_factor", 0) - baseline.get("profit_factor", 0)
        better = []
        worse = []
        if delta_r > 1:
            better.append(f"return +{delta_r:.1f}%")
        elif delta_r < -1:
            worse.append(f"return {delta_r:.1f}%")
        if delta_dd > 0.5:  # Less negative = better
            better.append(f"drawdown improved {delta_dd:.1f}%")
        elif delta_dd < -0.5:
            worse.append(f"drawdown worsened {delta_dd:.1f}%")
        if delta_pf > 0.1:
            better.append(f"PF +{delta_pf:.2f}")
        elif delta_pf < -0.1:
            worse.append(f"PF {delta_pf:.2f}")

        status = "✅ BETTER" if len(better) > len(worse) else ("⚠️ MIXED" if better else "❌ WORSE")
        print(f"  {m['label']}: {status}")
        if better:
            print(f"    Improvements: {', '.join(better)}")
        if worse:
            print(f"    Regressions:  {', '.join(worse)}")

    print(f"\n  Next step: combine the best-performing individual changes and re-test.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()

