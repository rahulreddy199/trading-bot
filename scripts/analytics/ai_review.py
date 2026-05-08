"""
AI Review — recommendation-only layer.

Reads analytics outputs, produces structured recommendations.
Does NOT edit config files, strategies, or parameters.
Output is advisory only.
"""
import json
from pathlib import Path
from datetime import datetime

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_SHARED, save_json, now_iso, today_str


def generate_recommendations(analytics=None, attribution=None, experiments=None):
    """
    Generate structured recommendations from analytics data.

    Each recommendation contains:
        recommendation: str
        reason: str
        supporting_metrics: dict
        confidence: str (high/medium/low)
        sample_size: int
        risk_note: str
        next_action: str (ignore / monitor / backtest / paper_test)
    """
    if analytics is None:
        path = STATE_SHARED / "analytics_daily.json"
        if path.exists():
            analytics = json.loads(path.read_text())
        else:
            analytics = {}

    if attribution is None:
        path = STATE_SHARED / "attribution_daily.json"
        if path.exists():
            attribution = json.loads(path.read_text())
        else:
            attribution = {}

    if experiments is None:
        path = STATE_SHARED / "experiments.json"
        if path.exists():
            experiments = json.loads(path.read_text())
        else:
            experiments = {"experiments": []}

    recommendations = []
    metrics = analytics.get("metrics_all_time", {})
    m7d = analytics.get("metrics_7d", {})
    incidents = analytics.get("incidents", {})
    total_trades = metrics.get("total_trades", 0)

    # Rule 1: Insufficient data warning
    if total_trades < 20:
        recommendations.append({
            "recommendation": "Continue paper trading to accumulate data",
            "reason": f"Only {total_trades} closed trades. Need 20+ for reliable attribution.",
            "supporting_metrics": {"total_trades": total_trades},
            "confidence": "high",
            "sample_size": total_trades,
            "risk_note": "Any parameter changes now would be based on noise, not signal.",
            "next_action": "monitor",
        })

    # Rule 2: High slippage
    slippage = metrics.get("avg_slippage_bps", 0)
    if slippage > 20 and total_trades >= 5:
        recommendations.append({
            "recommendation": "Investigate entry slippage",
            "reason": f"Average slippage is {slippage} bps — consider tighter limit offsets.",
            "supporting_metrics": {"avg_slippage_bps": slippage, "trades": total_trades},
            "confidence": "medium" if total_trades >= 15 else "low",
            "sample_size": total_trades,
            "risk_note": "Tighter limits may reduce fill rate.",
            "next_action": "backtest",
        })

    # Rule 3: Setup-level attribution
    setup_attr = attribution.get("setup_type", {})
    for setup, data in setup_attr.items():
        n = data.get("total_trades", 0)
        wr = data.get("win_rate", 0)
        avg_r_val = data.get("avg_r", 0)
        if n >= 10 and wr < 0.35:
            recommendations.append({
                "recommendation": f"Review '{setup}' setup — low win rate",
                "reason": f"{setup}: {n} trades, {wr*100:.0f}% win rate, avg R={avg_r_val}",
                "supporting_metrics": {"setup": setup, "trades": n, "win_rate": wr, "avg_r": avg_r_val},
                "confidence": "medium",
                "sample_size": n,
                "risk_note": "May be regime-dependent. Check attribution by regime.",
                "next_action": "backtest",
            })
        elif n >= 10 and avg_r_val > 1.0:
            recommendations.append({
                "recommendation": f"Consider increasing allocation to '{setup}' setup",
                "reason": f"{setup}: {n} trades, avg R={avg_r_val}, suggesting strong edge.",
                "supporting_metrics": {"setup": setup, "trades": n, "win_rate": wr, "avg_r": avg_r_val},
                "confidence": "medium",
                "sample_size": n,
                "risk_note": "Survivorship bias possible if only recent winners.",
                "next_action": "paper_test",
            })

    # Rule 4: Operational health
    total_incidents = sum(incidents.values()) if incidents else 0
    if total_incidents > 3:
        recommendations.append({
            "recommendation": "Address operational stability before strategy changes",
            "reason": f"{total_incidents} incidents today. Fix execution bugs first.",
            "supporting_metrics": incidents,
            "confidence": "high",
            "sample_size": total_incidents,
            "risk_note": "Strategy changes on unstable infrastructure compound risk.",
            "next_action": "monitor",
        })

    # Rule 5: Win rate trend (7d vs all-time)
    wr_all = metrics.get("win_rate", 0)
    wr_7d = m7d.get("win_rate", 0)
    if total_trades >= 20 and m7d.get("total_trades", 0) >= 5:
        if wr_7d < wr_all - 0.15:
            recommendations.append({
                "recommendation": "Recent performance degradation detected",
                "reason": f"7d win rate ({wr_7d*100:.0f}%) is significantly below all-time ({wr_all*100:.0f}%).",
                "supporting_metrics": {"win_rate_7d": wr_7d, "win_rate_all": wr_all},
                "confidence": "low",
                "sample_size": m7d.get("total_trades", 0),
                "risk_note": "Short sample. Could be normal variance or regime shift.",
                "next_action": "monitor",
            })

    # Rule 6: Drawdown
    dd = metrics.get("max_drawdown", 0)
    if dd > 0.10:
        recommendations.append({
            "recommendation": "Drawdown exceeding 10% — review risk parameters",
            "reason": f"Max drawdown is {dd*100:.1f}%. Consider reducing position count or sizing.",
            "supporting_metrics": {"max_drawdown": dd},
            "confidence": "medium",
            "sample_size": total_trades,
            "risk_note": "Drawdown may continue. Do not chase by increasing risk.",
            "next_action": "monitor",
        })

    # Output
    output = {
        "date": today_str(),
        "generated_at": now_iso(),
        "total_recommendations": len(recommendations),
        "recommendations": recommendations,
    }
    save_json(STATE_SHARED / "ai_review.json", output)

    # --- Append to cumulative history so AI can learn from trends ---
    history_path = STATE_SHARED / "ai_review_history.json"
    try:
        if history_path.exists():
            history = json.loads(history_path.read_text())
        else:
            history = []
        # Avoid duplicate entries for the same date
        history = [h for h in history if h.get("date") != today_str()]
        history.append(output)
        # Keep last 365 days max
        history = history[-365:]
        save_json(history_path, history)
    except Exception as e:
        print(f"⚠️ Failed to update ai_review_history.json: {e}")

    print(f"AI Review: {len(recommendations)} recommendations generated")
    return output


if __name__ == "__main__":
    output = generate_recommendations()
    for r in output["recommendations"]:
        print(f"  [{r['confidence']}] {r['next_action']}: {r['recommendation']}")

