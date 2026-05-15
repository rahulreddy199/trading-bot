"""
Phase 3: Global Kill Switch.

Prevents any new entries from being placed. Persists state to disk.
Can be triggered manually or automatically by rule breaches.
"""
import json
from datetime import datetime
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import STATE_DIR, CONFIG_DIR, MARKET_TZ
from infra.jsonio import load_json, save_json
from controls.audit import audit_log
from controls.alerts import send_control_alert


CONTROLS_DIR = STATE_DIR / "controls"
CONTROLS_DIR.mkdir(parents=True, exist_ok=True)

KILL_SWITCH_PATH = CONTROLS_DIR / "kill_switch.json"


def _default_state():
    return {
        "active": False,
        "triggered_at": None,
        "trigger_type": None,
        "reason": None,
        "triggered_by": None,
        "requires_manual_reset": True,
        "actions_taken": [],
    }


def load_kill_switch_state():
    """Load kill switch state from disk."""
    if KILL_SWITCH_PATH.exists():
        return load_json(KILL_SWITCH_PATH)
    return _default_state()


def save_kill_switch_state(state):
    """Persist kill switch state to disk."""
    save_json(KILL_SWITCH_PATH, state)


def is_kill_switch_active():
    """Check if kill switch is currently active. Pure read."""
    state = load_kill_switch_state()
    return state.get("active", False)


def activate_kill_switch(reason, triggered_by="manual", trigger_type="manual",
                         cancel_orders=None, flatten_positions=None):
    """
    Activate the global kill switch.

    Args:
        reason: Human-readable explanation
        triggered_by: Who/what triggered it (script name, rule, user)
        trigger_type: 'manual' or 'auto'
        cancel_orders: Override config to cancel open orders
        flatten_positions: Override config to flatten positions

    Returns:
        The new kill switch state dict
    """
    config = _load_config()
    ks_config = config.get("kill_switch", {})

    if cancel_orders is None:
        cancel_orders = ks_config.get("cancel_open_orders_on_activate", True)
    if flatten_positions is None:
        flatten_positions = ks_config.get("flatten_positions_on_activate", False)

    actions_taken = []
    if cancel_orders:
        actions_taken.append("cancel_open_orders_requested")
    if flatten_positions:
        actions_taken.append("flatten_positions_requested")

    state = {
        "active": True,
        "triggered_at": datetime.now(MARKET_TZ).isoformat(),
        "trigger_type": trigger_type,
        "reason": reason,
        "triggered_by": triggered_by,
        "requires_manual_reset": ks_config.get("requires_manual_reset", True),
        "actions_taken": actions_taken,
    }

    save_kill_switch_state(state)

    # Audit and alert
    audit_log(
        action="kill_switch_activated",
        severity="critical",
        module="controls.kill_switch",
        reason=reason,
        state_change={"active": True, "trigger_type": trigger_type},
        extra={"triggered_by": triggered_by, "actions_taken": actions_taken},
    )

    send_control_alert(
        "kill_switch_triggered",
        f"Kill switch activated: {reason} (by: {triggered_by})",
        severity="critical",
    )

    return state


def deactivate_kill_switch(reset_by="manual", reason="Manual reset"):
    """
    Deactivate the kill switch. Only allowed through explicit reset.

    Returns:
        The new (inactive) kill switch state dict
    """
    old_state = load_kill_switch_state()
    config = _load_config()
    cooldown = config.get("kill_switch", {}).get("cooldown_minutes_after_reset", 5)

    new_state = _default_state()
    new_state["last_reset_at"] = datetime.now(MARKET_TZ).isoformat()
    new_state["reset_by"] = reset_by
    new_state["cooldown_until"] = _cooldown_until(cooldown)

    save_kill_switch_state(new_state)

    audit_log(
        action="kill_switch_deactivated",
        severity="info",
        module="controls.kill_switch",
        reason=reason,
        state_change={"active": False, "reset_by": reset_by},
        extra={"previous_reason": old_state.get("reason"), "cooldown_minutes": cooldown},
    )

    return new_state


def is_in_cooldown():
    """Check if system is still in post-reset cooldown."""
    state = load_kill_switch_state()
    cooldown_until = state.get("cooldown_until")
    if not cooldown_until:
        return False
    now = datetime.now(MARKET_TZ)
    cooldown_dt = datetime.fromisoformat(cooldown_until)
    return now < cooldown_dt


def check_kill_switch():
    """
    Check kill switch status for pre-trade use.

    Returns:
        dict with 'blocked' bool and 'reason' if blocked
    """
    if is_kill_switch_active():
        state = load_kill_switch_state()
        return {
            "blocked": True,
            "reason": f"Kill switch active: {state.get('reason', 'unknown')}",
            "control": "kill_switch",
        }
    if is_in_cooldown():
        return {
            "blocked": True,
            "reason": "System in post-reset cooldown period",
            "control": "kill_switch_cooldown",
        }
    return {"blocked": False}


def _load_config():
    path = CONFIG_DIR / "risk_controls.json"
    if path.exists():
        return load_json(path)
    return {}


def _cooldown_until(minutes):
    from datetime import timedelta
    return (datetime.now(MARKET_TZ) + timedelta(minutes=minutes)).isoformat()

