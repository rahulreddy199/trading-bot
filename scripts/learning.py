"""
Learning module: analyzes recent performance and proposes strategy adjustments.
Runs weekly. Claude reviews proposals before applying.
"""
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from common import STATE_DIR, CONFIG_DIR, load_json, save_json


def load_trade_history():
    path = STATE_DIR / "trade_history.json"
    if path.exists():
        return load_json(path).get("trades", [])
    # Fall back to backtest results for initial learning
    bt = STATE_DIR / "backtest_results.json"
    if bt.exists():
        return load_json(bt).get("trades", [])
    return []


def analyze_performance(trades, lookback_days=60):
    """Analyze recent trade performance and return metrics + proposals."""
    if not trades:
        return {"status": "no_trades", "proposals": []}

    # Filter to recent trades
    cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    recent = [t for t in trades if t.get("closed_at", t.get("exit_date", "")) >= cutoff]

    if len(recent) < 5:
        recent = trades[-20:]  # Use last 20 trades if not enough recent ones

    # Compute metrics
    r_multiples = [t.get("r_multiple", 0) for t in recent if t.get("r_multiple") is not None]
    pnls = [t.get("pnl", 0) for t in recent if t.get("pnl") is not None]

    if not r_multiples:
        return {"status": "no_r_data", "proposals": []}

    winners = [r for r in r_multiples if r > 0]
    losers = [r for r in r_multiples if r <= 0]
    win_rate = len(winners) / len(r_multiples) * 100 if r_multiples else 0
    avg_r = statistics.mean(r_multiples) if r_multiples else 0
    avg_winner = statistics.mean(winners) if winners else 0
    avg_loser = statistics.mean(losers) if losers else 0

    # Exit reason analysis
    exit_reasons = {}
    for t in recent:
        reason = t.get("exit_reason", t.get("phase_at_exit", "unknown"))
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Bars held analysis
    bars = [t.get("bars_held", 0) for t in recent if t.get("bars_held")]
    avg_bars = statistics.mean(bars) if bars else 0

    # Exit reason analysis — identify what's costing money
    exit_costs = {}
    for t in recent:
        reason = t.get("exit_reason", t.get("phase_at_exit", "unknown"))
        r = t.get("r_multiple", 0) or 0
        if reason not in exit_costs:
            exit_costs[reason] = {"count": 0, "total_r": 0, "avg_r": 0}
        exit_costs[reason]["count"] += 1
        exit_costs[reason]["total_r"] = round(exit_costs[reason]["total_r"] + r, 3)
    for reason in exit_costs:
        c = exit_costs[reason]
        c["avg_r"] = round(c["total_r"] / c["count"], 3) if c["count"] > 0 else 0

    metrics = {
        "total_trades": len(recent),
        "win_rate": round(win_rate, 1),
        "avg_r": round(avg_r, 3),
        "avg_winner_r": round(avg_winner, 3),
        "avg_loser_r": round(avg_loser, 3),
        "avg_bars_held": round(avg_bars, 1),
        "exit_reasons": exit_reasons,
        "exit_reason_performance": exit_costs,
        "total_pnl": round(sum(pnls), 2) if pnls else 0,
    }

    # Generate proposals based on patterns
    proposals = []

    # If winners are being cut too short
    if avg_winner < 1.5 and win_rate > 45:
        proposals.append({
            "param": "trailing_atr_multiplier",
            "direction": "increase",
            "reason": f"Avg winner ({avg_winner:.2f}R) is small — wider trail may let winners run",
            "suggested_step": 0.2,
        })

    # If win rate is low, tighten entry
    if win_rate < 40:
        proposals.append({
            "param": "pullback_max_distance_from_sma20_pct",
            "direction": "decrease",
            "reason": f"Win rate ({win_rate:.1f}%) is low — tighter SMA20 distance may improve quality",
            "suggested_step": -0.01,
        })

    # If avg loser is too big (beyond -1.1R), tighten stops
    if avg_loser < -1.2:
        proposals.append({
            "param": "atr_stop_multiplier",
            "direction": "decrease",
            "reason": f"Avg loser ({avg_loser:.2f}R) too large — tighter initial stop",
            "suggested_step": -0.15,
        })

    # If too many initial stops, consider wider stop
    initial_stop_pct = exit_reasons.get("stop_initial", 0) / len(recent) * 100 if recent else 0
    if initial_stop_pct > 55:
        proposals.append({
            "param": "atr_stop_multiplier",
            "direction": "increase",
            "reason": f"{initial_stop_pct:.0f}% of exits are initial stops — stops may be too tight",
            "suggested_step": 0.15,
        })

    # If profit factor is strong, consider slightly more risk
    if avg_r > 0.5 and win_rate > 50:
        proposals.append({
            "param": "risk_per_trade",
            "direction": "increase",
            "reason": f"Strong edge (avg R={avg_r:.2f}, WR={win_rate:.0f}%) — room for slightly more risk",
            "suggested_step": 0.001,
        })

    return {
        "status": "ok",
        "analysis_date": datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "metrics": metrics,
        "proposals": proposals,
    }


def main():
    trades = load_trade_history()
    result = analyze_performance(trades)
    save_json(STATE_DIR / "learning_analysis.json", result)
    print(f"Learning analysis: {result['status']}")
    if result.get("metrics"):
        m = result["metrics"]
        print(f"  Trades: {m['total_trades']} | WR: {m['win_rate']}% | Avg R: {m['avg_r']} | P&L: ${m['total_pnl']:,.2f}")
    if result.get("proposals"):
        print(f"  Proposals: {len(result['proposals'])}")
        for p in result["proposals"]:
            print(f"    - {p['param']} → {p['direction']}: {p['reason']}")
    else:
        print("  No parameter changes proposed")
    return result


if __name__ == "__main__":
    main()

