"""
Phase 3 CLI: Reset Controls — safe manual reset mechanism.

Usage:
    python scripts/reset_controls.py status       # Show what needs resetting
    python scripts/reset_controls.py reset_kill   # Reset kill switch
    python scripts/reset_controls.py reset_pause  # Reset pause state
    python scripts/reset_controls.py reset_all    # Reset both
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from controls.kill_switch import (
    load_kill_switch_state, deactivate_kill_switch, is_kill_switch_active,
)
from controls.pause_rules import load_pause_state, reset_pause, is_paused
from controls.health import generate_health_summary
from controls.reconcile import CONTROLS_DIR
from controls.audit import audit_log
from infra.jsonio import load_json


def cmd_status():
    """Show what needs resetting."""
    ks = load_kill_switch_state()
    ps = load_pause_state()

    print("=" * 60)
    print("CONTROL RESET STATUS")
    print("=" * 60)
    print()

    needs_reset = False

    if ks.get("active"):
        print("🛑 Kill switch is ACTIVE — needs reset")
        print(f"   Reason: {ks.get('reason')}")
        print(f"   Since:  {ks.get('triggered_at')}")
        needs_reset = True
    else:
        print("✅ Kill switch: clear")

    if ps.get("paused"):
        print("⏸️  Pause state is ACTIVE — needs reset")
        print(f"   Rules: {ps.get('triggered_rules', [])}")
        needs_reset = True
    else:
        print("✅ Pause state: clear")

    print()

    # Check reconciliation
    recon_path = CONTROLS_DIR / "reconciliation_result.json"
    if recon_path.exists():
        recon = load_json(recon_path)
        anomalies = recon.get("summary", {}).get("anomaly_count", 0)
        if anomalies > 0:
            print(f"⚠️  Reconciliation has {anomalies} unresolved anomalies")
            needs_reset = True
        else:
            print("✅ Reconciliation: clean")
    else:
        print("ℹ️  No reconciliation run yet")

    print()
    if not needs_reset:
        print("✅ All clear — no resets needed")
    else:
        print("Use 'reset_kill', 'reset_pause', or 'reset_all' to clear")


def cmd_reset_kill():
    """Reset kill switch with safety checks."""
    if not is_kill_switch_active():
        print("✅ Kill switch is not active — nothing to reset")
        return

    ks = load_kill_switch_state()
    print(f"Resetting kill switch...")
    print(f"  Was active since: {ks.get('triggered_at')}")
    print(f"  Reason was: {ks.get('reason')}")

    # Verify no critical anomalies
    summary = generate_health_summary()
    post_kill = summary.get("post_kill_violations", [])
    if post_kill:
        print(f"⚠️  WARNING: {len(post_kill)} post-kill violations detected!")
        print("   Review these before resetting.")

    new_state = deactivate_kill_switch(
        reset_by="cli_manual",
        reason="Manual reset via reset_controls.py",
    )

    print(f"✅ Kill switch DEACTIVATED")
    if new_state.get("cooldown_until"):
        print(f"   Cooldown until: {new_state['cooldown_until']}")
    print()


def cmd_reset_pause():
    """Reset pause state."""
    if not is_paused():
        print("✅ System is not paused — nothing to reset")
        return

    ps = load_pause_state()
    print(f"Resetting pause state...")
    print(f"  Was paused since: {ps.get('paused_at')}")
    print(f"  Rules: {ps.get('triggered_rules', [])}")

    new_state = reset_pause(
        reset_by="cli_manual",
        reason="Manual reset via reset_controls.py",
    )

    print(f"✅ Pause state CLEARED")
    print()


def cmd_reset_all():
    """Reset both kill switch and pause state."""
    cmd_reset_kill()
    cmd_reset_pause()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "reset_kill":
        cmd_reset_kill()
    elif cmd == "reset_pause":
        cmd_reset_pause()
    elif cmd == "reset_all":
        cmd_reset_all()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

