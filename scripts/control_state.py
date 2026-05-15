"""
Phase 3 CLI: Control State — view, activate kill switch, pause state.

Usage:
    python scripts/control_state.py status          # Show all control state
    python scripts/control_state.py kill <reason>   # Activate kill switch
    python scripts/control_state.py pause <reason>  # Activate manual pause
    python scripts/control_state.py health          # Run health check
    python scripts/control_state.py reconcile       # Run reconciliation (offline, no broker calls)
"""
import sys
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from controls.kill_switch import (
    load_kill_switch_state, activate_kill_switch, is_kill_switch_active, is_in_cooldown,
)
from controls.pause_rules import load_pause_state, activate_pause, is_paused
from controls.health import generate_health_summary, generate_health_markdown, save_health_output
from controls.audit import read_audit_log


def cmd_status():
    """Show current control state."""
    ks = load_kill_switch_state()
    ps = load_pause_state()

    print("=" * 60)
    print("TRADING BOT CONTROL STATE")
    print("=" * 60)
    print()

    # Kill switch
    if ks.get("active"):
        print("🛑 KILL SWITCH: ACTIVE")
        print(f"   Reason:      {ks.get('reason', '?')}")
        print(f"   Triggered:   {ks.get('triggered_at', '?')}")
        print(f"   Type:        {ks.get('trigger_type', '?')}")
        print(f"   By:          {ks.get('triggered_by', '?')}")
        print(f"   Actions:     {ks.get('actions_taken', [])}")
    else:
        print("✅ KILL SWITCH: Off")
        if is_in_cooldown():
            print("   ⏳ In cooldown period")
    print()

    # Pause state
    if ps.get("paused"):
        print("⏸️  PAUSE STATE: PAUSED")
        print(f"   Rules:       {ps.get('triggered_rules', [])}")
        print(f"   Paused at:   {ps.get('paused_at', '?')}")
    else:
        print("✅ PAUSE STATE: Running")
    print()

    # Trading allowed?
    can_trade = not ks.get("active") and not ps.get("paused") and not is_in_cooldown()
    if can_trade:
        print("📈 NEW ENTRIES: ALLOWED")
    else:
        print("🚫 NEW ENTRIES: BLOCKED")
    print()


def cmd_kill(reason):
    """Activate kill switch."""
    if is_kill_switch_active():
        print("⚠️  Kill switch is already active.")
        state = load_kill_switch_state()
        print(f"   Reason: {state.get('reason')}")
        return

    state = activate_kill_switch(
        reason=reason,
        triggered_by="cli_manual",
        trigger_type="manual",
    )
    print(f"🛑 Kill switch ACTIVATED")
    print(f"   Reason: {reason}")
    print(f"   Time:   {state['triggered_at']}")
    print(f"   Actions: {state['actions_taken']}")
    print()
    print("To reset: python scripts/reset_controls.py reset_kill")


def cmd_pause(reason):
    """Activate manual pause."""
    state = activate_pause(
        rule_name="manual_pause",
        reason=reason,
        extra={"triggered_by": "cli_manual"},
    )
    print(f"⏸️  System PAUSED")
    print(f"   Reason: {reason}")
    print()
    print("To reset: python scripts/reset_controls.py reset_pause")


def cmd_health():
    """Run health check and display results."""
    summary = generate_health_summary()
    md = generate_health_markdown(summary)
    save_health_output(summary)
    print(md)


def cmd_audit(date=None):
    """Show today's audit log."""
    events = read_audit_log(date)
    if not events:
        print("No audit events found.")
        return
    print(f"Audit events ({len(events)}):")
    print("-" * 60)
    for e in events:
        sev = e.get("severity", "?")
        emoji = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(sev, "📋")
        print(f"  {emoji} [{e.get('ts', '?')[:19]}] {e.get('action', '?')} — {e.get('reason', '')}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "kill":
        reason = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Manual kill via CLI"
        cmd_kill(reason)
    elif cmd == "pause":
        reason = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Manual pause via CLI"
        cmd_pause(reason)
    elif cmd == "health":
        cmd_health()
    elif cmd == "audit":
        date = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_audit(date)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

