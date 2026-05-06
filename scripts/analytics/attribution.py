"""
Attribution — break down metrics by grouping dimensions.
Pure functions: no I/O, no broker calls.
"""
from typing import List, Dict, Callable
from collections import defaultdict
from analytics.metrics import compute_all_metrics


def group_trades(trades: List[Dict], key_fn: Callable[[Dict], str]) -> Dict[str, List[Dict]]:
    """Group trades by an arbitrary key function."""
    groups = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        if k is not None:
            groups[k].append(t)
    return dict(groups)


def attribution_by(trades: List[Dict], dimension: str) -> Dict[str, Dict]:
    """Compute metrics grouped by a dimension field on each trade."""
    key_fns = {
        "bot": lambda t: t.get("bot", "unknown"),
        "setup_type": lambda t: t.get("setup_type", "unknown"),
        "symbol": lambda t: t.get("symbol", "unknown"),
        "sector": lambda t: t.get("sector", "unknown"),
        "regime": lambda t: t.get("regime_at_entry", "unknown"),
        "exit_reason": lambda t: t.get("exit_reason", "unknown"),
        "holding_bucket": lambda t: _holding_bucket(t.get("bars_held")),
        "day_of_week": lambda t: t.get("entry_day_of_week", "unknown"),
    }
    key_fn = key_fns.get(dimension, lambda t: t.get(dimension, "unknown"))
    groups = group_trades(trades, key_fn)
    return {k: compute_all_metrics(v) for k, v in groups.items()}


def _holding_bucket(bars) -> str:
    """Bucket holding period into categories."""
    if bars is None:
        return "unknown"
    if bars <= 2:
        return "1-2_bars"
    elif bars <= 5:
        return "3-5_bars"
    elif bars <= 10:
        return "6-10_bars"
    elif bars <= 20:
        return "11-20_bars"
    else:
        return "21+_bars"


def full_attribution(trades: List[Dict]) -> Dict[str, Dict]:
    """Compute attribution across all standard dimensions."""
    dimensions = ["bot", "setup_type", "symbol", "sector", "regime",
                  "exit_reason", "holding_bucket", "day_of_week"]
    return {dim: attribution_by(trades, dim) for dim in dimensions}


def top_contributors(trades: List[Dict], n: int = 5) -> Dict:
    """Find best and worst contributing symbols by net PnL."""
    by_symbol = group_trades(trades, lambda t: t.get("symbol", "?"))
    symbol_pnl = {sym: sum(t.get("pnl", 0) for t in tlist)
                  for sym, tlist in by_symbol.items()}
    sorted_syms = sorted(symbol_pnl.items(), key=lambda x: x[1], reverse=True)
    return {
        "best": sorted_syms[:n],
        "worst": sorted_syms[-n:] if len(sorted_syms) >= n else sorted_syms,
    }

