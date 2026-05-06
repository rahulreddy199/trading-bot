"""Alerting via webhook (Slack, Discord, etc)."""
import requests
from infra.env import get_env


def send_alert(message, level="info"):
    """Send alert via webhook. Fails silently if not configured."""
    webhook_url = get_env("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        emoji = {"info": "ℹ️", "trade": "📈", "warning": "⚠️", "error": "🚨"}.get(level, "📋")
        payload = {"text": f"{emoji} *Trading Bot* [{level.upper()}]\n{message}"}
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass

