"""
Pure decision functions for the growth bot position state machine.

These functions compute what SHOULD happen based on current state.
They do NOT call broker APIs. They return action descriptors that
the executor layer carries out.

Phase transitions:
    pending → initial  (on fill detected)
    initial → protected (at protected_r)
    initial → exit_pending (time stop)
    protected → trailing (at trailing_r or bars_in_profit threshold)
    trailing → trailing (trail upgrades at R milestones)
    any → exit_pending (manual sell, time stop)
"""

# Valid phase transitions
ALLOWED_TRANSITIONS = {
    "pending": {"initial"},
    "initial": {"protected", "exit_pending"},
    "protected": {"trailing", "exit_pending", "initial"},  # initial = recovery downgrade
    "trailing": {"exit_pending", "protected"},  # protected = sync-down recovery
    "exit_pending": {"initial", "closed"},  # initial = exit order rejected recovery
    "closed": set(),
}


def decide_phase_action(track, current_price, avg_entry, qty, exit_cfg):
    """
    Pure decision: given tracking state and config, return what action to take.

    Returns a dict with:
        action: str — one of "hold", "time_stop", "move_to_protected",
                "move_to_trailing", "trail_upgrade", "needs_metadata", "skip"
        + relevant computed values (stop_price, trail_amount, etc.)

    Does NOT call any broker APIs.
    """
    phase = track.get("phase", "initial")
    r_per_share = track.get("r_per_share")
    atr = track.get("atr14_at_entry")

    # Can't decide without metadata
    if r_per_share is None or atr is None:
        return {"action": "needs_metadata"}

    current_r = (current_price - avg_entry) / r_per_share if r_per_share > 0 else 0
    bars_held = track.get("bars_held", 0)
    bars_in_profit = track.get("bars_in_profit", 0)

    protected_r = exit_cfg["phase_protected_r"]
    trailing_r = exit_cfg["phase_trailing_r"]
    trailing_bars_threshold = exit_cfg["phase_trailing_bars_in_profit"]
    trailing_mult = exit_cfg["trailing_atr_multiplier"]
    trailing_tight_mult = exit_cfg.get("trailing_tight_atr_multiplier", 2.0)
    trailing_tight_threshold_r = exit_cfg.get("trailing_tight_threshold_r", 3.0)
    protected_buffer = exit_cfg["protected_stop_buffer_atr"]
    time_stop_bars = exit_cfg["time_stop_bars"]
    time_stop_enabled = exit_cfg["time_stop_enabled"]

    # TIME STOP
    if (time_stop_enabled and phase == "initial"
            and bars_held >= time_stop_bars and current_r < 0.5):
        return {
            "action": "time_stop",
            "current_r": round(current_r, 2),
            "bars_held": bars_held,
        }

    # INITIAL → PROTECTED
    if phase == "initial" and current_r >= protected_r:
        protected_stop = avg_entry - protected_buffer * atr
        return {
            "action": "move_to_protected",
            "stop_price": round(protected_stop, 2),
            "current_r": round(current_r, 2),
        }

    # PROTECTED → TRAILING
    should_trail = (current_r >= trailing_r) or (bars_in_profit >= trailing_bars_threshold and current_r > 0.5)
    if phase == "protected" and should_trail:
        trail_amount = trailing_mult * atr
        if current_r >= trailing_tight_threshold_r:
            trail_amount = trailing_tight_mult * atr
        return {
            "action": "move_to_trailing",
            "trail_amount": round(trail_amount, 2),
            "current_r": round(current_r, 2),
            "tight": current_r >= trailing_tight_threshold_r,
        }

    # TRAIL UPGRADE (while trailing)
    if phase == "trailing":
        last_upgrade_r = track.get("last_trail_upgrade_r", trailing_tight_threshold_r)
        upgrade_thresholds = [4.0, 5.0, 6.0, 8.0]
        next_upgrade = None
        for t in upgrade_thresholds:
            if current_r >= t and last_upgrade_r < t:
                next_upgrade = t

        if next_upgrade:
            if next_upgrade >= 6.0:
                new_trail = 1.5 * atr
            elif next_upgrade >= 5.0:
                new_trail = 1.75 * atr
            else:
                new_trail = trailing_tight_mult * atr
            return {
                "action": "trail_upgrade",
                "new_trail": round(new_trail, 2),
                "threshold": next_upgrade,
                "current_r": round(current_r, 2),
            }

    # HOLD
    return {
        "action": "hold",
        "phase": phase,
        "current_r": round(current_r, 2),
        "bars_held": bars_held,
        "bars_in_profit": bars_in_profit,
    }


def compute_current_r(track, current_price, avg_entry):
    """Compute current R-multiple for a position."""
    r_per_share = track.get("r_per_share", 0)
    if r_per_share <= 0:
        return 0
    return (current_price - avg_entry) / r_per_share


def compute_best_r(track, avg_entry):
    """Compute best R-multiple achieved."""
    r_per_share = track.get("r_per_share", 0)
    best_price = track.get("best_price", avg_entry)
    if r_per_share <= 0:
        return 0
    return (best_price - avg_entry) / r_per_share


def validate_transition(from_phase, to_phase):
    """Check if a phase transition is allowed. Returns (ok, reason)."""
    allowed = ALLOWED_TRANSITIONS.get(from_phase, set())
    if to_phase in allowed:
        return True, "ok"
    return False, f"transition_{from_phase}_to_{to_phase}_not_allowed"

