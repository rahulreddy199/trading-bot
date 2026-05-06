"""
Report generation — daily and weekly markdown reports.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_SHARED, JOURNAL_DIR, save_json, today_str, now_iso


def generate_daily_report(analytics=None, attribution=None):
    """Generate daily markdown review from analytics outputs."""
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

    date = analytics.get("date", today_str())
    regime = analytics.get("regime", {})
    metrics = analytics.get("metrics_all_time", {})
    m7d = analytics.get("metrics_7d", {})
    incidents = analytics.get("incidents", {})

    lines = []
    lines.append(f"# Daily Review — {date}")
    lines.append("")

    # Headline
    lines.append("## Headline Metrics")
    lines.append(f"| Metric | All-Time | 7-Day |")
    lines.append(f"|--------|----------|-------|")
    lines.append(f"| Trades | {metrics.get('total_trades', 0)} | {m7d.get('total_trades', 0)} |")
    lines.append(f"| Net PnL | ${metrics.get('net_pnl', 0):,.2f} | ${m7d.get('net_pnl', 0):,.2f} |")
    lines.append(f"| Win Rate | {metrics.get('win_rate', 0)*100:.1f}% | {m7d.get('win_rate', 0)*100:.1f}% |")
    lines.append(f"| Profit Factor | {metrics.get('profit_factor', 0)} | {m7d.get('profit_factor', 0)} |")
    lines.append(f"| Avg R | {metrics.get('avg_r', 0)} | {m7d.get('avg_r', 0)} |")
    lines.append(f"| Avg Hold | {metrics.get('avg_hold_time', 0)} bars | {m7d.get('avg_hold_time', 0)} bars |")
    lines.append(f"| Slippage | {metrics.get('avg_slippage_bps', 0)} bps | {m7d.get('avg_slippage_bps', 0)} bps |")
    lines.append("")

    # Account
    lines.append("## Account")
    lines.append(f"- Equity: ${analytics.get('equity', 0):,.2f}")
    lines.append(f"- Open positions: {analytics.get('open_positions', 0)}")
    lines.append(f"- Unrealized P&L: ${analytics.get('unrealized_pnl', 0):,.2f}")
    lines.append("")

    # Regime
    lines.append("## Market Regime")
    lines.append(f"- Label: **{regime.get('regime_label', 'unknown')}**")
    lines.append(f"- SPY > 50 SMA: {regime.get('spy_above_50sma', '?')}")
    lines.append(f"- SPY > 200 SMA: {regime.get('spy_above_200sma', '?')}")
    lines.append(f"- VIX: {regime.get('vix_value', '?')} ({regime.get('vix_level', '?')})")
    lines.append("")

    # Contributors
    top = attribution.get("top_contributors", {})
    if top.get("best"):
        lines.append("## Best/Worst Contributors")
        lines.append("| Symbol | Net PnL |")
        lines.append("|--------|---------|")
        for sym, pnl in top.get("best", [])[:5]:
            lines.append(f"| {sym} | ${pnl:,.2f} |")
        lines.append("")
        if top.get("worst"):
            lines.append("**Worst:**")
            for sym, pnl in top.get("worst", [])[:3]:
                lines.append(f"- {sym}: ${pnl:,.2f}")
            lines.append("")

    # Incidents
    lines.append("## Operational Issues")
    total_incidents = sum(incidents.values()) if incidents else 0
    if total_incidents == 0:
        lines.append("- ✅ No incidents today")
    else:
        for k, v in incidents.items():
            if v > 0:
                lines.append(f"- ⚠️ {k}: {v}")
    lines.append("")

    # Manual review
    lines.append("## Open Manual-Review Items")
    health_path = STATE_SHARED / "health_summary.json"
    if health_path.exists():
        health = json.loads(health_path.read_text())
        flags = health.get("manual_review_flags", [])
        if flags:
            for f in flags:
                lines.append(f"- {f['bot']}/{f['symbol']}: {f['reason']}")
        else:
            lines.append("- None")
    else:
        lines.append("- Health summary not available")
    lines.append("")

    # Insufficient evidence
    lines.append("## Insufficient Evidence")
    if metrics.get("total_trades", 0) < 20:
        lines.append(f"- Only {metrics.get('total_trades', 0)} closed trades. Need 20+ for reliable metrics.")
    setup_attr = attribution.get("setup_type", {})
    for setup, data in setup_attr.items():
        if data.get("total_trades", 0) < 5:
            lines.append(f"- Setup '{setup}': only {data['total_trades']} trades (need 5+ for attribution)")
    if not lines[-1].startswith("-"):
        lines.append("- All dimensions have sufficient sample sizes ✅")
    lines.append("")

    # Save
    report_path = STATE_SHARED / f"report_daily_{date}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Daily report written: {report_path}")
    return "\n".join(lines)


def generate_weekly_report(analytics=None, attribution=None, experiments=None):
    """Generate weekly markdown review."""
    if analytics is None:
        path = STATE_SHARED / "analytics_rolling.json"
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

    date = today_str()
    m7d = analytics.get("7d", {})
    m30d = analytics.get("30d", {})
    all_time = analytics.get("all_time", {})

    lines = []
    lines.append(f"# Weekly Review — {date}")
    lines.append("")

    lines.append("## Performance Summary")
    lines.append(f"| Window | Trades | Win Rate | PF | Avg R | Net PnL |")
    lines.append(f"|--------|--------|----------|-----|-------|---------|")
    for label, m in [("7d", m7d), ("30d", m30d), ("All", all_time)]:
        lines.append(f"| {label} | {m.get('total_trades',0)} | {m.get('win_rate',0)*100:.0f}% | {m.get('profit_factor',0)} | {m.get('avg_r',0)} | ${m.get('net_pnl',0):,.0f} |")
    lines.append("")

    # Attribution highlights
    lines.append("## Attribution Highlights")
    for dim in ["setup_type", "regime", "sector"]:
        dim_data = attribution.get(dim, {})
        if dim_data:
            lines.append(f"\n### By {dim}")
            lines.append(f"| {dim} | Trades | Win Rate | Avg R |")
            lines.append(f"|------|--------|----------|-------|")
            for k, v in sorted(dim_data.items(), key=lambda x: x[1].get("net_pnl", 0), reverse=True):
                lines.append(f"| {k} | {v.get('total_trades',0)} | {v.get('win_rate',0)*100:.0f}% | {v.get('avg_r',0)} |")
    lines.append("")

    # Experiments
    lines.append("## Active Experiments")
    active = [e for e in experiments.get("experiments", []) if e.get("status") == "active"]
    if active:
        for exp in active:
            lines.append(f"- **{exp['id']}**: {exp.get('hypothesis', '?')} (window: {exp.get('evaluation_window', '?')})")
    else:
        lines.append("- No active experiments")
    lines.append("")

    # Recommendations placeholder
    lines.append("## Recommended Next Experiments")
    lines.append("- *(See ai_review output for structured recommendations)*")
    lines.append("")

    report_path = STATE_SHARED / f"report_weekly_{date}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Weekly report written: {report_path}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys as _sys
    if "--weekly" in _sys.argv:
        generate_weekly_report()
    else:
        generate_daily_report()

