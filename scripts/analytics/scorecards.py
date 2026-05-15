"""
Scorecards — compute comparable metric summaries for baseline vs variant(s).

Pure functions: takes trade lists and equity curves, returns structured dicts.
No I/O except optional save helpers.
"""
from typing import List, Dict, Optional
from analytics.metrics import compute_all_metrics
from analytics.attribution import attribution_by


def build_scorecard(
    trades: List[Dict],
    equity_curve: Optional[List[float]] = None,
    label: str = "unknown",
) -> Dict:
    """Build a single scorecard from a trade list."""
    metrics = compute_all_metrics(trades, equity_curve)

    # Setup-level summary
    setup_attr = attribution_by(trades, "setup_type")
    setup_summary = {}
    for setup, data in setup_attr.items():
        setup_summary[setup] = {
            "trades": data.get("total_trades", 0),
            "win_rate": data.get("win_rate", 0),
            "avg_r": data.get("avg_r", 0),
            "net_pnl": data.get("net_pnl", 0),
        }

    # Regime-level summary
    regime_attr = attribution_by(trades, "regime")
    regime_summary = {}
    for regime, data in regime_attr.items():
        regime_summary[regime] = {
            "trades": data.get("total_trades", 0),
            "win_rate": data.get("win_rate", 0),
            "avg_r": data.get("avg_r", 0),
            "net_pnl": data.get("net_pnl", 0),
        }

    # Exit-reason summary
    exit_attr = attribution_by(trades, "exit_reason")
    exit_summary = {}
    for reason, data in exit_attr.items():
        exit_summary[reason] = {
            "trades": data.get("total_trades", 0),
            "avg_r": data.get("avg_r", 0),
            "net_pnl": data.get("net_pnl", 0),
        }

    return {
        "label": label,
        "metrics": metrics,
        "setup_summary": setup_summary,
        "regime_summary": regime_summary,
        "exit_summary": exit_summary,
    }


def compare_scorecards(baseline: Dict, variant: Dict) -> Dict:
    """
    Compare two scorecards and produce a delta summary.

    Returns dict with metric-level deltas and a human-readable verdict list.
    """
    bm = baseline.get("metrics", {})
    vm = variant.get("metrics", {})

    deltas = {}
    for key in ("total_trades", "net_pnl", "win_rate", "profit_factor",
                "expectancy", "avg_r", "max_drawdown", "avg_hold_time",
                "avg_slippage_bps"):
        b_val = bm.get(key, 0) or 0
        v_val = vm.get(key, 0) or 0
        # Handle inf
        if isinstance(b_val, float) and b_val == float("inf"):
            b_val = 999
        if isinstance(v_val, float) and v_val == float("inf"):
            v_val = 999
        deltas[key] = {
            "baseline": b_val,
            "variant": v_val,
            "delta": round(v_val - b_val, 4),
        }

    # Verdicts
    verdicts = []
    td = deltas["total_trades"]["delta"]
    if td > 0:
        verdicts.append(f"✅ +{td} trades (more opportunities)")
    elif td < 0:
        verdicts.append(f"⚠️ {td} trades (fewer opportunities)")

    ed = deltas["expectancy"]["delta"]
    if ed > 0:
        verdicts.append(f"✅ Expectancy improved by ${ed:.2f}/trade")
    elif ed < 0:
        verdicts.append(f"❌ Expectancy degraded by ${abs(ed):.2f}/trade")

    dd_delta = deltas["max_drawdown"]["delta"]
    if dd_delta > 0.01:
        verdicts.append(f"❌ Max drawdown worse by {dd_delta*100:.1f}%")
    elif dd_delta < -0.01:
        verdicts.append(f"✅ Max drawdown improved by {abs(dd_delta)*100:.1f}%")

    pf_delta = deltas["profit_factor"]["delta"]
    if pf_delta > 0:
        verdicts.append(f"✅ Profit factor improved by {pf_delta:.2f}")
    elif pf_delta < 0:
        verdicts.append(f"⚠️ Profit factor decreased by {abs(pf_delta):.2f}")

    return {
        "baseline_label": baseline.get("label", "baseline"),
        "variant_label": variant.get("label", "variant"),
        "deltas": deltas,
        "verdicts": verdicts,
    }


def split_trades_is_oos(
    trades: List[Dict],
    equity_curve: Optional[List[float]],
    oos_fraction: float = 0.30,
) -> Dict:
    """
    Split trades & equity curve into in-sample and out-of-sample portions.

    Splits by trade index (chronological). Returns dict with is_trades,
    oos_trades, is_equity, oos_equity.
    """
    n = len(trades)
    split_idx = max(1, int(n * (1 - oos_fraction)))

    is_trades = trades[:split_idx]
    oos_trades = trades[split_idx:]

    is_equity = None
    oos_equity = None
    if equity_curve and len(equity_curve) > 1:
        eq_split = max(1, int(len(equity_curve) * (1 - oos_fraction)))
        is_equity = equity_curve[:eq_split]
        oos_equity = equity_curve[eq_split:]

    return {
        "is_trades": is_trades,
        "oos_trades": oos_trades,
        "is_equity": is_equity,
        "oos_equity": oos_equity,
        "split_index": split_idx,
        "total_trades": n,
    }

