"""
Strategy manager: validates, snapshots, applies, and rolls back strategy changes.
All changes are logged and reversible.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from common import CONFIG_DIR, STATE_DIR, save_json, load_json

HISTORY_DIR = CONFIG_DIR / "strategy_history"
TUNING_LOG = STATE_DIR / "tuning_log.json"
GUARDRAILS_PATH = CONFIG_DIR / "guardrails.json"
STRATEGY_PATH = CONFIG_DIR / "strategy.json"


def load_guardrails():
    return load_json(GUARDRAILS_PATH)


def _get_nested(d, dotted_path):
    keys = dotted_path.split(".")
    for k in keys:
        d = d[k]
    return d


def _set_nested(d, dotted_path, value):
    keys = dotted_path.split(".")
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = value


def snapshot_strategy(reason="auto"):
    """Save a timestamped copy of current strategy.json."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = HISTORY_DIR / f"strategy_{ts}.json"
    shutil.copy2(STRATEGY_PATH, dest)
    return str(dest)


def validate_change(param_name, new_value, guardrails=None):
    """Validate a proposed parameter change against guardrails.
    Returns (is_valid, reason)."""
    if guardrails is None:
        guardrails = load_guardrails()

    if guardrails.get("kill_switch"):
        return False, "Kill switch is active — no tuning allowed"

    if not guardrails.get("tuning_enabled", False):
        return False, "Tuning is disabled — set tuning_enabled: true in guardrails.json after paper validation"

    params = guardrails.get("parameters", {})
    if param_name not in params:
        return False, f"Unknown parameter: {param_name}"

    rule = params[param_name]
    if new_value < rule["min"] or new_value > rule["max"]:
        return False, f"{param_name}={new_value} outside bounds [{rule['min']}, {rule['max']}]"

    # Check step size from current value
    strategy = load_json(STRATEGY_PATH)
    current = _get_nested(strategy, rule["path"])
    delta = abs(new_value - current)
    if delta > rule["max_step"] * 1.01:  # small float tolerance
        return False, f"{param_name} step {delta:.4f} exceeds max_step {rule['max_step']}"

    return True, "ok"


def apply_changes(changes, reason="auto_tune"):
    """Apply a dict of {param_name: new_value} changes.
    Returns (applied, rejected) lists."""
    guardrails = load_guardrails()
    strategy = load_json(STRATEGY_PATH)

    max_changes = guardrails.get("max_parameters_changed_per_cycle", 2)
    if len(changes) > max_changes:
        return [], [{"error": f"Too many changes ({len(changes)} > {max_changes})"}]

    # Enforce min_trades_before_tuning
    min_trades = guardrails.get("min_trades_before_tuning", 30)
    trade_history_path = STATE_DIR / "trade_history.json"
    trade_count = 0
    if trade_history_path.exists():
        try:
            trade_count = len(load_json(trade_history_path).get("trades", []))
        except Exception:
            pass
    if trade_count < min_trades:
        return [], [{"error": f"Not enough trades ({trade_count} < {min_trades} required)"}]

    # Enforce tuning_cooldown_weeks (only count entries that actually applied changes)
    cooldown_weeks = guardrails.get("tuning_cooldown_weeks", 2)
    if TUNING_LOG.exists():
        try:
            log = load_json(TUNING_LOG)
            for entry in reversed(log):
                if entry.get("applied"):  # Only count entries with actual changes
                    last_ts = entry.get("timestamp", "")
                    if last_ts:
                        last_dt = datetime.fromisoformat(last_ts)
                        days_since = (datetime.now() - last_dt).days
                        if days_since < cooldown_weeks * 7:
                            return [], [{"error": f"Cooldown active: last tuning {days_since} days ago (need {cooldown_weeks * 7})"}]
                    break
        except Exception:
            pass

    # Snapshot before changes
    snapshot_path = snapshot_strategy(reason)

    applied = []
    rejected = []
    for param_name, new_value in changes.items():
        is_valid, msg = validate_change(param_name, new_value, guardrails)
        if not is_valid:
            rejected.append({"param": param_name, "value": new_value, "reason": msg})
            continue

        rule = guardrails["parameters"][param_name]
        old_value = _get_nested(strategy, rule["path"])
        _set_nested(strategy, rule["path"], new_value)
        applied.append({
            "param": param_name,
            "old_value": old_value,
            "new_value": new_value,
            "path": rule["path"],
        })

    if applied:
        save_json(STRATEGY_PATH, strategy)

    # Log the tuning event
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "snapshot": snapshot_path,
        "applied": applied,
        "rejected": rejected,
    }
    log = []
    if TUNING_LOG.exists():
        log = load_json(TUNING_LOG)
    log.append(log_entry)
    save_json(TUNING_LOG, log)

    return applied, rejected


def rollback(snapshot_filename=None):
    """Rollback to a previous strategy snapshot.
    If no filename given, rolls back to the most recent snapshot."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = sorted(HISTORY_DIR.glob("strategy_*.json"))
    if not snapshots:
        return False, "No snapshots found"

    if snapshot_filename:
        target = HISTORY_DIR / snapshot_filename
        if not target.exists():
            return False, f"Snapshot not found: {snapshot_filename}"
    else:
        target = snapshots[-1]

    # Snapshot current before rollback
    snapshot_strategy("pre_rollback")
    shutil.copy2(target, STRATEGY_PATH)
    return True, f"Rolled back to {target.name}"


def get_tuning_history():
    if TUNING_LOG.exists():
        return load_json(TUNING_LOG)
    return []

