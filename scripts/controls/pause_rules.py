"""
Phase 3: Automatic Pause Controls.

Rule-based conditions that block new entries without necessarily flattening positions.
Separate from kill switch — softer intervention, still requires manual reset.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import STATE_DIR, CONFIG_DIR, MARKET_TZ, STATE_SHARED
from infra.jsonio import load_json, save_json
from controls.audit import audit_log
from controls.alerts import send_control_alert


CONTROLS_DIR = STATE_DIR / "controls"
CONTROLS_DIR.mkdir(parents=True, exist_ok=True)

PAUSE_STATE_PATH = CONTROLS_DIR / "pause_state.json"


def _default_pause_state():
    return {
        "paused": False,
        "paused_at": None,
        "pause_reasons": [],
        "triggered_rules": [],
        "requires_manual_reset": True,
    }


def load_pause_state():
    if PAUSE_STATE_PATH.exists():
        return load_json(PAUSE_STATE_PATH)
    return _default_pause_state()


def save_pause_state(state):
    save_json(PAUSE_STATE_PATH, state)


def is_paused():
    """Check if system is currently paused."""
    state = load_pause_state()
    return state.get("paused", False)


def activate_pause(rule_name, reason, extra=None):
    """Activate pause state due to a rule breach."""
    state = load_pause_state()
    state["paused"] = True
    state["paused_at"] = datetime.now(MARKET_TZ).isoformat()
    state["requires_manual_reset"] = True

    breach = {
        "rule": rule_name,
        "reason": reason,
        "triggered_at": datetime.now(MARKET_TZ).isoformat(),
    }
    if extra:
        breach.update(extra)

    state.setdefault("pause_reasons", []).append(breach)
    state.setdefault("triggered_rules", [])
    if rule_name not in state["triggered_rules"]:
        state["triggered_rules"].append(rule_name)

    save_pause_state(state)

    audit_log(
        action="pause_activated",
        severity="warning",
        module="controls.pause_rules",
        reason=reason,
        control_rule=rule_name,
        state_change={"paused": True},
        extra=extra,
    )

    send_control_alert(
        "pause_triggered",
        f"System paused: {reason} (rule: {rule_name})",
        severity="warning",
    )

    return state


def reset_pause(reset_by="manual", reason="Manual reset"):
    """Reset pause state. Requires explicit action."""
    old_state = load_pause_state()
    new_state = _default_pause_state()
    new_state["last_reset_at"] = datetime.now(MARKET_TZ).isoformat()
    new_state["reset_by"] = reset_by

    save_pause_state(new_state)

    audit_log(
        action="pause_reset",
        severity="info",
        module="controls.pause_rules",
        reason=reason,
        state_change={"paused": False, "reset_by": reset_by},
        extra={"previous_rules": old_state.get("triggered_rules", [])},
    )

    return new_state


def check_pause():
    """
    Check pause state for pre-trade use.

    Returns:
        dict with 'blocked' bool and 'reason' if blocked
    """
    if is_paused():
        state = load_pause_state()
        rules = state.get("triggered_rules", [])
        return {
            "blocked": True,
            "reason": f"System paused by rules: {', '.join(rules)}",
            "control": "pause_rules",
            "triggered_rules": rules,
        }
    return {"blocked": False}


# ── Pause Rule Evaluators ──


def evaluate_daily_loss(equity_today, equity_yesterday, config):
    """Check if daily loss threshold is breached."""
    rule = config.get("rules", {}).get("daily_loss_pct", {})
    if not rule.get("enabled", False):
        return None
    threshold = rule.get("threshold", 3.0)
    if equity_yesterday <= 0:
        return None
    loss_pct = ((equity_yesterday - equity_today) / equity_yesterday) * 100
    if loss_pct >= threshold:
        return {
            "rule": "daily_loss_pct",
            "reason": f"Daily loss {loss_pct:.2f}% exceeds {threshold}% threshold",
            "value": loss_pct,
            "threshold": threshold,
        }
    return None


def evaluate_rolling_drawdown(equity_curve, config):
    """Check if rolling drawdown threshold is breached."""
    rule = config.get("rules", {}).get("rolling_drawdown_pct", {})
    if not rule.get("enabled", False):
        return None
    threshold = rule.get("threshold", 15.0)
    lookback = rule.get("lookback_days", 30)

    if not equity_curve or len(equity_curve) < 2:
        return None

    recent = equity_curve[-lookback:]
    peak = max(e.get("equity", e.get("value", 0)) for e in recent)
    current = recent[-1].get("equity", recent[-1].get("value", 0))

    if peak <= 0:
        return None
    drawdown_pct = ((peak - current) / peak) * 100
    if drawdown_pct >= threshold:
        return {
            "rule": "rolling_drawdown_pct",
            "reason": f"Rolling drawdown {drawdown_pct:.2f}% exceeds {threshold}% threshold",
            "value": drawdown_pct,
            "threshold": threshold,
        }
    return None


def evaluate_order_rejections(rejection_count, config):
    """Check if order rejection count exceeds threshold."""
    rule = config.get("rules", {}).get("order_rejection_count", {})
    if not rule.get("enabled", False):
        return None
    threshold = rule.get("threshold", 5)
    if rejection_count >= threshold:
        return {
            "rule": "order_rejection_count",
            "reason": f"Order rejections ({rejection_count}) >= threshold ({threshold})",
            "value": rejection_count,
            "threshold": threshold,
        }
    return None


def evaluate_broker_errors(error_count, config):
    """Check if broker error count exceeds threshold."""
    rule = config.get("rules", {}).get("broker_error_count", {})
    if not rule.get("enabled", False):
        return None
    threshold = rule.get("threshold", 10)
    if error_count >= threshold:
        return {
            "rule": "broker_error_count",
            "reason": f"Broker errors ({error_count}) >= threshold ({threshold})",
            "value": error_count,
            "threshold": threshold,
        }
    return None


def evaluate_heartbeat_missing(heartbeats, config):
    """Check if any critical script heartbeat is missing/stale."""
    rule = config.get("rules", {}).get("heartbeat_missing_minutes", {})
    if not rule.get("enabled", False):
        return None
    threshold_minutes = rule.get("threshold", 60)
    critical_scripts = rule.get("critical_scripts", [])
    now = datetime.now(MARKET_TZ)

    for script in critical_scripts:
        hb = heartbeats.get(script)
        if not hb:
            continue  # Only flag if script SHOULD have run
        ts_str = hb.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            age_minutes = (now - ts).total_seconds() / 60
            if age_minutes > threshold_minutes:
                return {
                    "rule": "heartbeat_missing_minutes",
                    "reason": f"Heartbeat for '{script}' is {age_minutes:.0f}m stale (threshold: {threshold_minutes}m)",
                    "value": age_minutes,
                    "threshold": threshold_minutes,
                    "script": script,
                }
        except (ValueError, TypeError):
            continue
    return None


def evaluate_duplicate_orders(duplicate_count, config):
    """Check if duplicate order count exceeds threshold."""
    rule = config.get("rules", {}).get("duplicate_order_count", {})
    if not rule.get("enabled", False):
        return None
    threshold = rule.get("threshold", 3)
    if duplicate_count >= threshold:
        return {
            "rule": "duplicate_order_count",
            "reason": f"Duplicate orders ({duplicate_count}) >= threshold ({threshold})",
            "value": duplicate_count,
            "threshold": threshold,
        }
    return None


def evaluate_all_pause_rules(context, config=None):
    """
    Evaluate all pause rules against provided context.

    Args:
        context: dict with keys like 'equity_today', 'equity_yesterday',
                 'equity_curve', 'rejection_count', 'error_count',
                 'heartbeats', 'duplicate_count'
        config: pause_rules config dict (loaded from risk_controls.json if None)

    Returns:
        List of breached rules (each a dict), empty if no breaches
    """
    if config is None:
        rc_path = CONFIG_DIR / "risk_controls.json"
        if rc_path.exists():
            rc = load_json(rc_path)
            config = rc.get("pause_rules", {})
        else:
            config = {}

    breaches = []

    # Daily loss
    result = evaluate_daily_loss(
        context.get("equity_today", 0),
        context.get("equity_yesterday", 0),
        config,
    )
    if result:
        breaches.append(result)

    # Rolling drawdown
    result = evaluate_rolling_drawdown(
        context.get("equity_curve", []),
        config,
    )
    if result:
        breaches.append(result)

    # Order rejections
    result = evaluate_order_rejections(
        context.get("rejection_count", 0),
        config,
    )
    if result:
        breaches.append(result)

    # Broker errors
    result = evaluate_broker_errors(
        context.get("error_count", 0),
        config,
    )
    if result:
        breaches.append(result)

    # Heartbeats
    result = evaluate_heartbeat_missing(
        context.get("heartbeats", {}),
        config,
    )
    if result:
        breaches.append(result)

    # Duplicate orders
    result = evaluate_duplicate_orders(
        context.get("duplicate_count", 0),
        config,
    )
    if result:
        breaches.append(result)

    return breaches

