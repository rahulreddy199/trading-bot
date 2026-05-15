"""
Phase 3: Alerting hooks.

Notification abstraction supporting log-only and webhook modes.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import CONFIG_DIR, MARKET_TZ
from infra.jsonio import load_json
from controls.audit import audit_log

logger = logging.getLogger(__name__)

ALERTING_CONFIG_PATH = CONFIG_DIR / "alerting.json"


def load_alerting_config():
    if ALERTING_CONFIG_PATH.exists():
        return load_json(ALERTING_CONFIG_PATH)
    return {"mode": "log_only", "events": {}}


def send_control_alert(event_type, message, severity="warning", extra=None):
    """
    Send an alert through configured channels.
    Always logs to audit. Optionally sends webhook.
    """
    config = load_alerting_config()
    event_config = config.get("events", {}).get(event_type, {})

    if not event_config.get("enabled", True):
        return

    # Always audit log
    audit_log(
        action=f"alert_{event_type}",
        severity=severity,
        module="controls.alerts",
        reason=message,
        extra=extra,
    )

    mode = config.get("mode", "log_only")

    if mode in ("webhook", "all"):
        _send_webhook(config, event_type, message, severity)

    return {"event_type": event_type, "message": message, "severity": severity}


def _send_webhook(config, event_type, message, severity):
    """Send webhook notification. Fails silently."""
    import os
    webhook_config = config.get("webhook", {})
    url_env = webhook_config.get("url_env_var", "ALERT_WEBHOOK_URL")
    url = os.environ.get(url_env, "")
    if not url:
        return

    try:
        import requests
        emoji = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(severity, "📋")
        payload = {
            "text": f"{emoji} *Trading Bot Control* [{severity.upper()}] {event_type}\n{message}"
        }
        timeout = webhook_config.get("timeout_seconds", 10)
        requests.post(url, json=payload, timeout=timeout)
    except Exception as e:
        logger.warning(f"Webhook send failed: {e}")

