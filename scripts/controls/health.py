"""
Phase 3: Health Monitoring and Heartbeats.

Extends existing heartbeat system with anomaly detection and health summaries.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import STATE_DIR, STATE_SHARED, MARKET_TZ
from infra.jsonio import load_json, save_json
from controls.kill_switch import load_kill_switch_state, is_kill_switch_active
from controls.pause_rules import load_pause_state, is_paused
from controls.audit import read_audit_log, audit_log


CONTROLS_DIR = STATE_DIR / "controls"
CONTROLS_DIR.mkdir(parents=True, exist_ok=True)

EXPECTED_SCRIPTS = [
    "research_growth",
    "trade_growth",
    "manage_growth",
    "performance",
    "journal",
]

# Maximum acceptable age for each script heartbeat (in minutes)
HEARTBEAT_MAX_AGE = {
    "research_growth": 360,   # Should run at least daily during market days
    "trade_growth": 360,
    "manage_growth": 180,     # Runs multiple times per day
    "performance": 1440,      # Daily
    "journal": 1440,          # Daily
}


def load_heartbeat(name):
    """Load heartbeat for a specific script."""
    # Check both shared and root state locations
    for parent in (STATE_SHARED, STATE_DIR):
        path = parent / f"heartbeat_{name}.json"
        if path.exists():
            return load_json(path)
    return None


def load_all_heartbeats():
    """Load heartbeats for all expected scripts."""
    heartbeats = {}
    for name in EXPECTED_SCRIPTS:
        hb = load_heartbeat(name)
        if hb:
            heartbeats[name] = hb
    return heartbeats


def check_heartbeat_health(heartbeats=None):
    """
    Check health of all heartbeats.

    Returns:
        List of issues (empty if all healthy)
    """
    if heartbeats is None:
        heartbeats = load_all_heartbeats()

    now = datetime.now(MARKET_TZ)
    issues = []

    for name in EXPECTED_SCRIPTS:
        hb = heartbeats.get(name)
        max_age = HEARTBEAT_MAX_AGE.get(name, 360)

        if not hb:
            issues.append({
                "script": name,
                "issue": "no_heartbeat",
                "description": f"No heartbeat file found for {name}",
                "severity": "warning",
            })
            continue

        ts_str = hb.get("timestamp")
        status = hb.get("status", "unknown")

        if not ts_str:
            issues.append({
                "script": name,
                "issue": "no_timestamp",
                "description": f"Heartbeat for {name} has no timestamp",
                "severity": "warning",
            })
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
            age_minutes = (now - ts).total_seconds() / 60
            if age_minutes > max_age:
                issues.append({
                    "script": name,
                    "issue": "stale_heartbeat",
                    "age_minutes": round(age_minutes),
                    "max_age_minutes": max_age,
                    "description": f"Heartbeat for {name} is {age_minutes:.0f}m old (max: {max_age}m)",
                    "severity": "warning",
                })
        except (ValueError, TypeError):
            issues.append({
                "script": name,
                "issue": "invalid_timestamp",
                "description": f"Heartbeat for {name} has invalid timestamp: {ts_str}",
                "severity": "warning",
            })

        if status == "error":
            issues.append({
                "script": name,
                "issue": "error_status",
                "description": f"Heartbeat for {name} reports error status",
                "severity": "error",
            })

    return issues


def check_post_kill_activity():
    """Check if any trading activity occurred after kill switch activation."""
    ks_state = load_kill_switch_state()
    if not ks_state.get("active"):
        return []

    triggered_at = ks_state.get("triggered_at")
    if not triggered_at:
        return []

    # Check today's audit log for order attempts after kill
    events = read_audit_log()
    violations = []
    for event in events:
        if event.get("action") in ("order_blocked_pretrade",) and event.get("ts", "") > triggered_at:
            # This is expected — blocked is good
            pass
        # Look for any unblocked trade events after kill
        if (event.get("action") == "order_submitted" and event.get("ts", "") > triggered_at):
            violations.append({
                "issue": "trade_after_kill",
                "description": f"Order submitted after kill switch at {triggered_at}",
                "event": event,
                "severity": "critical",
            })

    return violations


def generate_health_summary():
    """
    Generate a comprehensive health summary.

    Returns:
        dict with health status and details
    """
    heartbeats = load_all_heartbeats()
    heartbeat_issues = check_heartbeat_health(heartbeats)
    kill_state = load_kill_switch_state()
    pause_state = load_pause_state()
    post_kill = check_post_kill_activity()

    # Load reconciliation result if available
    recon_path = CONTROLS_DIR / "reconciliation_result.json"
    recon_summary = {}
    if recon_path.exists():
        recon = load_json(recon_path)
        recon_summary = recon.get("summary", {})

    # Determine overall health
    critical_issues = [i for i in heartbeat_issues if i.get("severity") == "critical"]
    critical_issues.extend(post_kill)

    if kill_state.get("active"):
        overall = "killed"
    elif pause_state.get("paused"):
        overall = "paused"
    elif critical_issues:
        overall = "critical"
    elif heartbeat_issues:
        overall = "degraded"
    else:
        overall = "healthy"

    summary = {
        "timestamp": datetime.now(MARKET_TZ).isoformat(),
        "overall_status": overall,
        "kill_switch": {
            "active": kill_state.get("active", False),
            "reason": kill_state.get("reason"),
            "triggered_at": kill_state.get("triggered_at"),
        },
        "pause_state": {
            "paused": pause_state.get("paused", False),
            "rules": pause_state.get("triggered_rules", []),
        },
        "heartbeats": {
            "total_expected": len(EXPECTED_SCRIPTS),
            "issues": heartbeat_issues,
            "issue_count": len(heartbeat_issues),
        },
        "reconciliation": recon_summary,
        "post_kill_violations": post_kill,
        "alert_status": overall in ("killed", "paused", "critical"),
    }

    return summary


def generate_health_markdown(summary=None):
    """Generate a human-readable Markdown health report."""
    if summary is None:
        summary = generate_health_summary()

    status_emoji = {
        "healthy": "✅",
        "degraded": "⚠️",
        "paused": "⏸️",
        "killed": "🛑",
        "critical": "🚨",
    }

    overall = summary.get("overall_status", "unknown")
    emoji = status_emoji.get(overall, "❓")

    lines = [
        f"# System Health Report",
        f"**Generated:** {summary.get('timestamp', 'unknown')}",
        f"**Status:** {emoji} {overall.upper()}",
        f"",
        f"## Control State",
        f"| Control | Status |",
        f"|---------|--------|",
        f"| Kill Switch | {'🛑 ACTIVE' if summary['kill_switch']['active'] else '✅ Off'} |",
        f"| Pause State | {'⏸️ PAUSED' if summary['pause_state']['paused'] else '✅ Running'} |",
        f"",
    ]

    if summary["kill_switch"]["active"]:
        lines.append(f"**Kill switch reason:** {summary['kill_switch'].get('reason', 'unknown')}")
        lines.append(f"**Triggered at:** {summary['kill_switch'].get('triggered_at', '?')}")
        lines.append("")

    if summary["pause_state"]["paused"]:
        rules = summary["pause_state"].get("rules", [])
        lines.append(f"**Pause rules triggered:** {', '.join(rules)}")
        lines.append("")

    # Heartbeats
    lines.append("## Heartbeats")
    issues = summary.get("heartbeats", {}).get("issues", [])
    if issues:
        for issue in issues:
            lines.append(f"- ⚠️ **{issue['script']}**: {issue['description']}")
    else:
        lines.append("✅ All heartbeats healthy")
    lines.append("")

    # Reconciliation
    recon = summary.get("reconciliation", {})
    if recon:
        lines.append("## Reconciliation")
        lines.append(f"- Anomalies: {recon.get('anomaly_count', 0)}")
        lines.append(f"- Warnings: {recon.get('warning_count', 0)}")
        lines.append(f"- Healthy: {'✅' if recon.get('healthy', True) else '❌'}")
        lines.append("")

    return "\n".join(lines)


def save_health_output(summary=None):
    """Save health summary JSON and Markdown."""
    if summary is None:
        summary = generate_health_summary()
    md = generate_health_markdown(summary)

    json_path = CONTROLS_DIR / "health_summary.json"
    md_path = CONTROLS_DIR / "health_report.md"
    save_json(json_path, summary)
    md_path.write_text(md, encoding="utf-8")
    return {"json_path": str(json_path), "md_path": str(md_path)}

