"""
Audit logging for Phase 3 controls.

All critical safety/control actions are written to a dedicated JSONL audit log.
"""
import json
from datetime import datetime
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import STATE_DIR, MARKET_TZ


AUDIT_DIR = STATE_DIR / "controls" / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def _today_str():
    return datetime.now(MARKET_TZ).strftime("%Y-%m-%d")


def audit_log(action, severity, module, reason, symbol=None,
              control_rule=None, state_change=None, extra=None):
    """Write a structured audit event to today's JSONL audit log."""
    event = {
        "ts": datetime.now(MARKET_TZ).isoformat(),
        "module": module,
        "action": action,
        "severity": severity,
        "reason": reason,
    }
    if symbol:
        event["symbol"] = symbol
    if control_rule:
        event["control_rule"] = control_rule
    if state_change:
        event["state_change"] = state_change
    if extra:
        event.update(extra)

    log_path = AUDIT_DIR / f"audit_{_today_str()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")

    return event


def read_audit_log(date_str=None):
    """Read all audit events for a given date (defaults to today)."""
    date_str = date_str or _today_str()
    log_path = AUDIT_DIR / f"audit_{date_str}.jsonl"
    if not log_path.exists():
        return []
    events = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events

