"""
Unit tests for growth/decisions.py — pure phase-transition logic.

All functions are pure (no broker calls), so testing is straightforward.

Usage:
    python -m pytest scripts/tests/test_decisions.py -v
    python scripts/tests/test_decisions.py
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from growth.decisions import (
    decide_phase_action,
    compute_current_r,
    compute_best_r,
    validate_transition,
    ALLOWED_TRANSITIONS,
)

# Default exit config matching strategy_growth.json
EXIT_CFG = {
    "phase_protected_r": 1.5,
    "phase_trailing_r": 2.5,
    "phase_trailing_bars_in_profit": 5,
    "trailing_atr_multiplier": 3.0,
    "trailing_tight_atr_multiplier": 2.0,
    "trailing_tight_threshold_r": 3.0,
    "protected_stop_buffer_atr": 0.1,
    "time_stop_bars": 10,
    "time_stop_enabled": True,
}


def make_track(phase="initial", r_per_share=10.0, atr=5.0, bars_held=0,
               bars_in_profit=0, last_trail_upgrade_r=3.0, best_price=100):
    return {
        "phase": phase,
        "r_per_share": r_per_share,
        "atr14_at_entry": atr,
        "bars_held": bars_held,
        "bars_in_profit": bars_in_profit,
        "last_trail_upgrade_r": last_trail_upgrade_r,
        "best_price": best_price,
    }


# ── needs_metadata ──────────────────────────────────────────────

def test_needs_metadata_no_r():
    track = make_track()
    track["r_per_share"] = None
    result = decide_phase_action(track, 105, 100, 10, EXIT_CFG)
    assert result["action"] == "needs_metadata"


def test_needs_metadata_no_atr():
    track = make_track()
    track["atr14_at_entry"] = None
    result = decide_phase_action(track, 105, 100, 10, EXIT_CFG)
    assert result["action"] == "needs_metadata"


# ── hold ────────────────────────────────────────────────────────

def test_hold_initial_below_protected():
    """Below 1.5R in initial phase → hold."""
    track = make_track(phase="initial", r_per_share=10, bars_held=3)
    result = decide_phase_action(track, 110, 100, 10, EXIT_CFG)  # 1.0R
    assert result["action"] == "hold"
    assert result["phase"] == "initial"
    assert result["current_r"] == 1.0


def test_hold_protected_below_trailing():
    """In protected phase but below 2.5R → hold."""
    track = make_track(phase="protected", r_per_share=10, bars_held=5, bars_in_profit=3)
    result = decide_phase_action(track, 120, 100, 10, EXIT_CFG)  # 2.0R
    assert result["action"] == "hold"


def test_hold_trailing_no_upgrade():
    """In trailing phase, below next upgrade threshold → hold."""
    track = make_track(phase="trailing", r_per_share=10, last_trail_upgrade_r=3.0)
    result = decide_phase_action(track, 135, 100, 10, EXIT_CFG)  # 3.5R (below 4R)
    assert result["action"] == "hold"


# ── time_stop ───────────────────────────────────────────────────

def test_time_stop_triggered():
    """10 bars held, less than 0.5R progress → time stop."""
    track = make_track(phase="initial", r_per_share=10, bars_held=10)
    result = decide_phase_action(track, 103, 100, 10, EXIT_CFG)  # 0.3R
    assert result["action"] == "time_stop"
    assert result["current_r"] == 0.3
    assert result["bars_held"] == 10


def test_time_stop_not_triggered_good_progress():
    """10 bars but at 0.8R → no time stop, should move to hold."""
    track = make_track(phase="initial", r_per_share=10, bars_held=10)
    result = decide_phase_action(track, 108, 100, 10, EXIT_CFG)  # 0.8R
    assert result["action"] != "time_stop"


def test_time_stop_not_triggered_too_early():
    """Only 5 bars → no time stop even with 0R progress."""
    track = make_track(phase="initial", r_per_share=10, bars_held=5)
    result = decide_phase_action(track, 100, 100, 10, EXIT_CFG)  # 0R
    assert result["action"] != "time_stop"


def test_time_stop_disabled():
    """Time stop disabled in config → no time stop."""
    cfg = {**EXIT_CFG, "time_stop_enabled": False}
    track = make_track(phase="initial", r_per_share=10, bars_held=15)
    result = decide_phase_action(track, 100, 100, 10, cfg)  # 0R, 15 bars
    assert result["action"] != "time_stop"


def test_time_stop_only_in_initial():
    """Time stop should NOT fire in protected or trailing phase."""
    track = make_track(phase="protected", r_per_share=10, bars_held=15)
    result = decide_phase_action(track, 103, 100, 10, EXIT_CFG)
    assert result["action"] != "time_stop"


# ── move_to_protected ──────────────────────────────────────────

def test_initial_to_protected():
    """At 1.5R → move to protected."""
    track = make_track(phase="initial", r_per_share=10, atr=5)
    result = decide_phase_action(track, 115, 100, 10, EXIT_CFG)  # 1.5R exactly
    assert result["action"] == "move_to_protected"
    assert result["current_r"] == 1.5
    # Stop should be entry - 0.1 * ATR = 100 - 0.5 = 99.5
    assert result["stop_price"] == 99.5


def test_initial_to_protected_above_threshold():
    """At 2.0R → still triggers protected (first)."""
    track = make_track(phase="initial", r_per_share=10, atr=5)
    result = decide_phase_action(track, 120, 100, 10, EXIT_CFG)
    assert result["action"] == "move_to_protected"


# ── move_to_trailing ───────────────────────────────────────────

def test_protected_to_trailing_by_r():
    """At 2.5R in protected → move to trailing."""
    track = make_track(phase="protected", r_per_share=10, atr=5, bars_in_profit=3)
    result = decide_phase_action(track, 125, 100, 10, EXIT_CFG)
    assert result["action"] == "move_to_trailing"
    assert result["trail_amount"] == 15.0  # 3.0 * ATR(5)
    assert result["tight"] is False


def test_protected_to_trailing_by_bars():
    """5 bars in profit + above 0.5R → trailing via bars threshold."""
    track = make_track(phase="protected", r_per_share=10, atr=5, bars_in_profit=5)
    result = decide_phase_action(track, 110, 100, 10, EXIT_CFG)  # 1.0R > 0.5
    assert result["action"] == "move_to_trailing"


def test_protected_to_trailing_tight():
    """At 3.0R+ → trailing with tight multiplier."""
    track = make_track(phase="protected", r_per_share=10, atr=5)
    result = decide_phase_action(track, 130, 100, 10, EXIT_CFG)  # 3.0R
    assert result["action"] == "move_to_trailing"
    assert result["trail_amount"] == 10.0  # 2.0 * ATR(5)
    assert result["tight"] is True


# ── trail_upgrade ───────────────────────────────────────────────

def test_trail_upgrade_at_4r():
    """At 4R with last upgrade at 3R → upgrade."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=3.0)
    result = decide_phase_action(track, 140, 100, 10, EXIT_CFG)  # 4.0R
    assert result["action"] == "trail_upgrade"
    assert result["threshold"] == 4.0
    assert result["new_trail"] == 10.0  # 2.0 * ATR(5)


def test_trail_upgrade_at_5r():
    """At 5R → tighter trail."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=4.0)
    result = decide_phase_action(track, 150, 100, 10, EXIT_CFG)  # 5.0R
    assert result["action"] == "trail_upgrade"
    assert result["threshold"] == 5.0
    assert result["new_trail"] == 8.75  # 1.75 * ATR(5)


def test_trail_upgrade_at_6r():
    """At 6R → tightest trail."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=5.0)
    result = decide_phase_action(track, 160, 100, 10, EXIT_CFG)  # 6.0R
    assert result["action"] == "trail_upgrade"
    assert result["threshold"] == 6.0
    assert result["new_trail"] == 7.5  # 1.5 * ATR(5)


def test_trail_upgrade_at_8r():
    """At 8R → final lock."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=6.0)
    result = decide_phase_action(track, 180, 100, 10, EXIT_CFG)  # 8.0R
    assert result["action"] == "trail_upgrade"
    assert result["threshold"] == 8.0
    assert result["new_trail"] == 7.5  # 1.5 * ATR(5)


def test_trail_no_double_upgrade():
    """Already upgraded at 4R → should not fire again at 4R."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=4.0)
    result = decide_phase_action(track, 140, 100, 10, EXIT_CFG)  # 4.0R
    assert result["action"] == "hold"


def test_trail_skip_upgrade_jump():
    """Jump from 3R to 6R → should pick highest eligible (6R)."""
    track = make_track(phase="trailing", r_per_share=10, atr=5, last_trail_upgrade_r=3.0)
    result = decide_phase_action(track, 160, 100, 10, EXIT_CFG)  # 6.0R
    assert result["action"] == "trail_upgrade"
    # Should pick 6R since it iterates to the last qualifying threshold
    assert result["threshold"] == 6.0


# ── compute_current_r ───────────────────────────────────────────

def test_compute_current_r_positive():
    track = {"r_per_share": 10}
    assert compute_current_r(track, 115, 100) == 1.5


def test_compute_current_r_negative():
    track = {"r_per_share": 10}
    assert compute_current_r(track, 95, 100) == -0.5


def test_compute_current_r_zero_r():
    track = {"r_per_share": 0}
    assert compute_current_r(track, 110, 100) == 0


# ── compute_best_r ──────────────────────────────────────────────

def test_compute_best_r():
    track = {"r_per_share": 10, "best_price": 125}
    assert compute_best_r(track, 100) == 2.5


def test_compute_best_r_no_best_price():
    track = {"r_per_share": 10}
    assert compute_best_r(track, 100) == 0  # best_price defaults to avg_entry


# ── validate_transition ────────────────────────────────────────

def test_valid_transitions():
    assert validate_transition("initial", "protected") == (True, "ok")
    assert validate_transition("initial", "exit_pending") == (True, "ok")
    assert validate_transition("protected", "trailing") == (True, "ok")
    assert validate_transition("trailing", "exit_pending") == (True, "ok")
    assert validate_transition("exit_pending", "initial") == (True, "ok")
    assert validate_transition("pending", "initial") == (True, "ok")


def test_invalid_transitions():
    ok, reason = validate_transition("initial", "trailing")
    assert ok is False
    assert "not_allowed" in reason

    ok, reason = validate_transition("trailing", "initial")
    assert ok is False

    ok, reason = validate_transition("closed", "initial")
    assert ok is False


# ── priority: time_stop before phase transition ─────────────────

def test_time_stop_beats_protected():
    """At 1.5R but 10 bars with < 0.5R should... actually not fire
    because 1.5R > 0.5R threshold. This tests the R check."""
    track = make_track(phase="initial", r_per_share=10, bars_held=10)
    # At 1.5R, time stop won't fire because current_r >= 0.5
    result = decide_phase_action(track, 115, 100, 10, EXIT_CFG)
    assert result["action"] == "move_to_protected"  # Protected wins


def test_time_stop_fires_before_protected_when_low_r():
    """10 bars, 0.3R → time stop fires even though bars check is met."""
    track = make_track(phase="initial", r_per_share=10, bars_held=10)
    result = decide_phase_action(track, 103, 100, 10, EXIT_CFG)  # 0.3R
    assert result["action"] == "time_stop"


# ── Run tests directly ──────────────────────────────────────────

if __name__ == "__main__":
    import inspect
    tests = [(name, fn) for name, fn in inspect.getmembers(sys.modules[__name__])
             if name.startswith("test_") and callable(fn)]

    passed = 0
    failed = 0
    for name, fn in sorted(tests):
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 {name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'='*50}")
    sys.exit(1 if failed else 0)

