"""
Pure metric calculations. No I/O, no broker calls.
All functions take trade lists or numeric sequences and return computed values.
"""
from typing import List, Dict, Optional
import statistics


def win_rate(trades: List[Dict]) -> float:
    """Fraction of trades with positive PnL."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return round(wins / len(trades), 4)


def profit_factor(trades: List[Dict]) -> float:
    """Gross profit / gross loss. Returns 0 if no losses."""
    gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 2)


def expectancy(trades: List[Dict]) -> float:
    """Average PnL per trade (expectancy in dollars)."""
    if not trades:
        return 0.0
    return round(sum(t.get("pnl", 0) for t in trades) / len(trades), 2)


def avg_r(trades: List[Dict]) -> float:
    """Average R-multiple across trades."""
    r_values = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    if not r_values:
        return 0.0
    return round(statistics.mean(r_values), 2)


def net_pnl(trades: List[Dict]) -> float:
    """Total net PnL."""
    return round(sum(t.get("pnl", 0) for t in trades), 2)


def max_drawdown(equity_curve: List[float]) -> float:
    """Maximum peak-to-trough drawdown as a fraction (e.g. 0.05 = 5%)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def avg_hold_time(trades: List[Dict]) -> float:
    """Average holding period in bars."""
    bars = [t["bars_held"] for t in trades if t.get("bars_held") is not None]
    if not bars:
        return 0.0
    return round(statistics.mean(bars), 1)


def avg_slippage_bps(trades: List[Dict]) -> float:
    """Average slippage in basis points."""
    slippages = []
    for t in trades:
        slip = t.get("slippage_bps")
        if slip is None and isinstance(t.get("slippage"), dict):
            slip = t["slippage"].get("slippage_bps")
        if slip is not None:
            slippages.append(slip)
    if not slippages:
        return 0.0
    return round(statistics.mean(slippages), 1)


def compute_all_metrics(trades: List[Dict], equity_curve: Optional[List[float]] = None) -> Dict:
    """Compute all standard metrics from a trade list."""
    result = {
        "total_trades": len(trades),
        "net_pnl": net_pnl(trades),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "expectancy": expectancy(trades),
        "avg_r": avg_r(trades),
        "avg_hold_time": avg_hold_time(trades),
        "avg_slippage_bps": avg_slippage_bps(trades),
    }
    if equity_curve:
        result["max_drawdown"] = max_drawdown(equity_curve)
    if trades:
        pnls = [t.get("pnl", 0) for t in trades]
        result["largest_winner"] = round(max(pnls), 2)
        result["largest_loser"] = round(min(pnls), 2)
        result["total_winners"] = sum(1 for p in pnls if p > 0)
        result["total_losers"] = sum(1 for p in pnls if p < 0)
    return result

