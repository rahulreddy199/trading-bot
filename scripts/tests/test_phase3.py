"""
Phase 3 Tests — kill switch, pause rules, pre-trade controls, reconciliation,
health checks, audit logging, manual reset flow.

Run: python -m pytest scripts/tests/test_phase3.py -v
"""
import json
import sys
import os
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

# We need to patch STATE_DIR before importing controls, so tests don't pollute real state
import infra.paths as paths

_TEST_STATE_DIR = None


def setup_module():
    """Create a temporary state directory for tests."""
    global _TEST_STATE_DIR
    _TEST_STATE_DIR = Path(tempfile.mkdtemp(prefix="phase3_test_"))
    paths.STATE_DIR = _TEST_STATE_DIR
    paths.STATE_SHARED = _TEST_STATE_DIR / "shared"
    paths.STATE_SHARED.mkdir(parents=True, exist_ok=True)

    # Patch controls dir
    controls_dir = _TEST_STATE_DIR / "controls"
    controls_dir.mkdir(parents=True, exist_ok=True)
    (controls_dir / "audit").mkdir(parents=True, exist_ok=True)

    # Patch in controls modules
    import controls.audit as audit_mod
    audit_mod.AUDIT_DIR = controls_dir / "audit"

    import controls.kill_switch as ks_mod
    ks_mod.CONTROLS_DIR = controls_dir
    ks_mod.KILL_SWITCH_PATH = controls_dir / "kill_switch.json"

    import controls.pause_rules as pr_mod
    pr_mod.CONTROLS_DIR = controls_dir
    pr_mod.PAUSE_STATE_PATH = controls_dir / "pause_state.json"

    import controls.reconcile as rec_mod
    rec_mod.CONTROLS_DIR = controls_dir

    import controls.health as h_mod
    h_mod.CONTROLS_DIR = controls_dir


def teardown_module():
    """Clean up temporary state directory."""
    global _TEST_STATE_DIR
    if _TEST_STATE_DIR and _TEST_STATE_DIR.exists():
        shutil.rmtree(_TEST_STATE_DIR)


# ══════════════════════════════════════════════════════════════════════════════
# A. Kill Switch Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()

    def test_default_state_inactive(self):
        from controls.kill_switch import load_kill_switch_state, is_kill_switch_active
        state = load_kill_switch_state()
        assert state["active"] is False
        assert is_kill_switch_active() is False

    def test_activate_kill_switch(self):
        from controls.kill_switch import activate_kill_switch, is_kill_switch_active, load_kill_switch_state
        state = activate_kill_switch(
            reason="Test breach",
            triggered_by="test",
            trigger_type="auto",
        )
        assert state["active"] is True
        assert state["reason"] == "Test breach"
        assert state["trigger_type"] == "auto"
        assert state["triggered_by"] == "test"
        assert state["requires_manual_reset"] is True
        assert is_kill_switch_active() is True

    def test_kill_switch_persists(self):
        from controls.kill_switch import activate_kill_switch, load_kill_switch_state, KILL_SWITCH_PATH
        activate_kill_switch(reason="Persistence test", triggered_by="test")
        # Reload from disk
        assert KILL_SWITCH_PATH.exists()
        state = json.loads(KILL_SWITCH_PATH.read_text())
        assert state["active"] is True

    def test_deactivate_kill_switch(self):
        from controls.kill_switch import activate_kill_switch, deactivate_kill_switch, is_kill_switch_active
        activate_kill_switch(reason="To deactivate", triggered_by="test")
        assert is_kill_switch_active() is True
        new_state = deactivate_kill_switch(reset_by="test", reason="Test reset")
        assert new_state["active"] is False
        assert is_kill_switch_active() is False

    def test_cooldown_after_reset(self):
        from controls.kill_switch import activate_kill_switch, deactivate_kill_switch, is_in_cooldown
        activate_kill_switch(reason="Cooldown test", triggered_by="test")
        deactivate_kill_switch(reset_by="test")
        assert is_in_cooldown() is True

    def test_check_kill_switch_blocks(self):
        from controls.kill_switch import activate_kill_switch, check_kill_switch
        activate_kill_switch(reason="Block test", triggered_by="test")
        result = check_kill_switch()
        assert result["blocked"] is True
        assert "kill_switch" in result.get("control", "")

    def test_check_kill_switch_passes_when_inactive(self):
        from controls.kill_switch import check_kill_switch
        result = check_kill_switch()
        assert result["blocked"] is False

    def test_actions_taken_cancel_orders(self):
        from controls.kill_switch import activate_kill_switch
        state = activate_kill_switch(
            reason="With cancel",
            triggered_by="test",
            cancel_orders=True,
            flatten_positions=False,
        )
        assert "cancel_open_orders_requested" in state["actions_taken"]
        assert "flatten_positions_requested" not in state["actions_taken"]


# ══════════════════════════════════════════════════════════════════════════════
# B. Pause Rules Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPauseRules:
    def setup_method(self):
        from controls.pause_rules import PAUSE_STATE_PATH
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def test_default_not_paused(self):
        from controls.pause_rules import is_paused
        assert is_paused() is False

    def test_activate_pause(self):
        from controls.pause_rules import activate_pause, is_paused, load_pause_state
        activate_pause("daily_loss_pct", "Loss exceeded 3%")
        assert is_paused() is True
        state = load_pause_state()
        assert "daily_loss_pct" in state["triggered_rules"]

    def test_multiple_pause_rules(self):
        from controls.pause_rules import activate_pause, load_pause_state
        activate_pause("daily_loss_pct", "Daily loss")
        activate_pause("broker_error_count", "Too many errors")
        state = load_pause_state()
        assert len(state["triggered_rules"]) == 2

    def test_reset_pause(self):
        from controls.pause_rules import activate_pause, reset_pause, is_paused
        activate_pause("test_rule", "Test")
        assert is_paused() is True
        reset_pause(reset_by="test")
        assert is_paused() is False

    def test_check_pause_blocks(self):
        from controls.pause_rules import activate_pause, check_pause
        activate_pause("test_rule", "Test pause")
        result = check_pause()
        assert result["blocked"] is True

    def test_check_pause_passes_when_not_paused(self):
        from controls.pause_rules import check_pause
        result = check_pause()
        assert result["blocked"] is False

    def test_evaluate_daily_loss_triggers(self):
        from controls.pause_rules import evaluate_daily_loss
        config = {"rules": {"daily_loss_pct": {"enabled": True, "threshold": 3.0}}}
        result = evaluate_daily_loss(equity_today=19400, equity_yesterday=20000, config=config)
        assert result is not None
        assert result["rule"] == "daily_loss_pct"

    def test_evaluate_daily_loss_no_breach(self):
        from controls.pause_rules import evaluate_daily_loss
        config = {"rules": {"daily_loss_pct": {"enabled": True, "threshold": 3.0}}}
        result = evaluate_daily_loss(equity_today=19800, equity_yesterday=20000, config=config)
        assert result is None

    def test_evaluate_rolling_drawdown_triggers(self):
        from controls.pause_rules import evaluate_rolling_drawdown
        config = {"rules": {"rolling_drawdown_pct": {"enabled": True, "threshold": 15.0, "lookback_days": 30}}}
        curve = [{"equity": 20000}] * 10 + [{"equity": 16000}]
        result = evaluate_rolling_drawdown(curve, config)
        assert result is not None
        assert result["rule"] == "rolling_drawdown_pct"

    def test_evaluate_rolling_drawdown_no_breach(self):
        from controls.pause_rules import evaluate_rolling_drawdown
        config = {"rules": {"rolling_drawdown_pct": {"enabled": True, "threshold": 15.0, "lookback_days": 30}}}
        curve = [{"equity": 20000}, {"equity": 19500}]
        result = evaluate_rolling_drawdown(curve, config)
        assert result is None

    def test_evaluate_order_rejections(self):
        from controls.pause_rules import evaluate_order_rejections
        config = {"rules": {"order_rejection_count": {"enabled": True, "threshold": 5}}}
        assert evaluate_order_rejections(6, config) is not None
        assert evaluate_order_rejections(3, config) is None

    def test_evaluate_broker_errors(self):
        from controls.pause_rules import evaluate_broker_errors
        config = {"rules": {"broker_error_count": {"enabled": True, "threshold": 10}}}
        assert evaluate_broker_errors(15, config) is not None
        assert evaluate_broker_errors(5, config) is None

    def test_evaluate_heartbeat_missing(self):
        from controls.pause_rules import evaluate_heartbeat_missing
        config = {"rules": {"heartbeat_missing_minutes": {
            "enabled": True, "threshold": 60, "critical_scripts": ["trade_growth"]
        }}}
        old_ts = (datetime.now(paths.MARKET_TZ) - timedelta(minutes=120)).isoformat()
        heartbeats = {"trade_growth": {"timestamp": old_ts}}
        result = evaluate_heartbeat_missing(heartbeats, config)
        assert result is not None
        assert result["rule"] == "heartbeat_missing_minutes"

    def test_evaluate_all_pause_rules(self):
        from controls.pause_rules import evaluate_all_pause_rules
        config = {
            "rules": {
                "daily_loss_pct": {"enabled": True, "threshold": 3.0},
                "rolling_drawdown_pct": {"enabled": True, "threshold": 15.0, "lookback_days": 30},
                "order_rejection_count": {"enabled": True, "threshold": 5},
                "broker_error_count": {"enabled": True, "threshold": 10},
                "heartbeat_missing_minutes": {"enabled": False},
                "duplicate_order_count": {"enabled": True, "threshold": 3},
            }
        }
        context = {
            "equity_today": 19400,
            "equity_yesterday": 20000,
            "equity_curve": [],
            "rejection_count": 6,
            "error_count": 2,
            "heartbeats": {},
            "duplicate_count": 1,
        }
        breaches = evaluate_all_pause_rules(context, config)
        assert len(breaches) == 2  # daily_loss + rejections


# ══════════════════════════════════════════════════════════════════════════════
# C. Pre-Trade Controls Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPreTradeControls:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        from controls.pause_rules import PAUSE_STATE_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def _base_order(self):
        return {
            "symbol": "NVDA",
            "qty": 10,
            "side": "buy",
            "limit_price": 500.0,
            "reference_price": 498.0,
            "notional_value": 5000.0,
            "risk_pct": 0.75,
        }

    def _base_context(self):
        return {
            "equity": 20000,
            "open_positions": 2,
            "open_position_symbols": ["AMD", "SMH"],
            "pending_order_symbols": [],
            "total_risk_pct": 1.5,
            "symbol_allocation_pct": {},
            "correlated_count": 0,
        }

    def _base_config(self):
        return {
            "max_position_size_pct": 25.0,
            "max_allocation_per_symbol_pct": 25.0,
            "max_total_open_risk_pct": 5.0,
            "max_open_positions": 5,
            "correlation_cap_threshold": 0.85,
            "max_correlated_positions": 2,
            "no_duplicate_pending_orders": True,
            "price_sanity_max_deviation_pct": 10.0,
            "min_order_qty": 1,
            "max_order_qty": 1000,
            "check_kill_switch_first": True,
            "check_pause_state_first": True,
        }

    def test_all_checks_pass(self):
        from controls.pretrade import check_all
        result = check_all(self._base_order(), self._base_context(), self._base_config())
        assert result["passed"] is True
        assert result["blocked_by"] == []

    def test_blocked_by_kill_switch(self):
        from controls.pretrade import check_all
        from controls.kill_switch import activate_kill_switch
        activate_kill_switch(reason="Test", triggered_by="test")
        result = check_all(self._base_order(), self._base_context(), self._base_config())
        assert result["passed"] is False
        assert "kill_switch" in result["blocked_by"]

    def test_blocked_by_pause(self):
        from controls.pretrade import check_all
        from controls.pause_rules import activate_pause
        activate_pause("test", "Test pause")
        result = check_all(self._base_order(), self._base_context(), self._base_config())
        assert result["passed"] is False
        assert "pause_state" in result["blocked_by"]

    def test_blocked_max_positions(self):
        from controls.pretrade import check_all
        ctx = self._base_context()
        ctx["open_positions"] = 5
        result = check_all(self._base_order(), ctx, self._base_config())
        assert result["passed"] is False
        assert "max_open_positions" in result["blocked_by"]

    def test_blocked_duplicate_pending(self):
        from controls.pretrade import check_all
        ctx = self._base_context()
        ctx["pending_order_symbols"] = ["NVDA"]
        result = check_all(self._base_order(), ctx, self._base_config())
        assert result["passed"] is False
        assert "no_duplicate_pending" in result["blocked_by"]

    def test_blocked_correlation_cap(self):
        from controls.pretrade import check_all
        ctx = self._base_context()
        ctx["correlated_count"] = 2
        result = check_all(self._base_order(), ctx, self._base_config())
        assert result["passed"] is False
        assert "correlation_cap" in result["blocked_by"]

    def test_blocked_qty_too_high(self):
        from controls.pretrade import check_all
        order = self._base_order()
        order["qty"] = 5000
        result = check_all(order, self._base_context(), self._base_config())
        assert result["passed"] is False
        assert "order_qty_sanity" in result["blocked_by"]

    def test_blocked_total_risk(self):
        from controls.pretrade import check_all
        ctx = self._base_context()
        ctx["total_risk_pct"] = 4.5
        result = check_all(self._base_order(), ctx, self._base_config())
        assert result["passed"] is False
        assert "max_total_open_risk" in result["blocked_by"]

    def test_price_sanity_blocks(self):
        from controls.pretrade import check_all
        order = self._base_order()
        order["limit_price"] = 600.0  # 20% above reference of 498
        order["reference_price"] = 498.0
        result = check_all(order, self._base_context(), self._base_config())
        assert result["passed"] is False
        assert "price_sanity" in result["blocked_by"]

    def test_returns_structured_results(self):
        from controls.pretrade import check_all
        result = check_all(self._base_order(), self._base_context(), self._base_config())
        assert "passed" in result
        assert "results" in result
        assert "blocked_by" in result
        assert isinstance(result["results"], list)
        for r in result["results"]:
            assert "control" in r
            assert "passed" in r


# ══════════════════════════════════════════════════════════════════════════════
# D. Reconciliation Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReconciliation:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        from controls.pause_rules import PAUSE_STATE_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def test_clean_reconciliation(self):
        from controls.reconcile import reconcile
        positions = [{"symbol": "AMD", "qty": "10"}]
        orders = [{"symbol": "AMD", "status": "new", "id": "o1", "type": "stop", "side": "sell"}]
        tracking = {"AMD": {"phase": "initial"}}
        result = reconcile(positions, orders, tracking)
        assert result["summary"]["healthy"] is True
        assert result["summary"]["anomaly_count"] == 0

    def test_missing_tracking_detected(self):
        from controls.reconcile import reconcile
        positions = [{"symbol": "NVDA", "qty": "10"}]
        orders = []
        tracking = {}  # No tracking for NVDA
        result = reconcile(positions, orders, tracking)
        anomalies = [a for a in result["anomalies"] if a["type"] == "missing_tracking"]
        assert len(anomalies) == 1
        assert anomalies[0]["symbol"] == "NVDA"

    def test_stale_tracking_detected(self):
        from controls.reconcile import reconcile
        positions = []  # No broker position
        orders = []
        tracking = {"OLD": {"phase": "trailing"}}  # Stale entry
        result = reconcile(positions, orders, tracking)
        warnings = [w for w in result["warnings"] if w["type"] == "stale_tracking"]
        assert len(warnings) == 1

    def test_duplicate_orders_detected(self):
        from controls.reconcile import reconcile
        positions = []
        orders = [
            {"symbol": "AMD", "status": "new", "id": "o1", "type": "stop_limit", "side": "buy"},
            {"symbol": "AMD", "status": "new", "id": "o2", "type": "stop_limit", "side": "buy"},
        ]
        tracking = {}
        result = reconcile(positions, orders, tracking)
        anomalies = [a for a in result["anomalies"] if a["type"] == "duplicate_orders"]
        assert len(anomalies) == 1

    def test_missing_stop_detected(self):
        from controls.reconcile import reconcile
        positions = [{"symbol": "AMD", "qty": "15"}]
        orders = []  # No stop order
        tracking = {"AMD": {"phase": "initial"}}
        result = reconcile(positions, orders, tracking)
        anomalies = [a for a in result["anomalies"] if a["type"] == "missing_stop"]
        assert len(anomalies) == 1

    def test_order_while_killed(self):
        from controls.reconcile import reconcile
        from controls.kill_switch import activate_kill_switch
        activate_kill_switch(reason="Test kill", triggered_by="test")
        positions = []
        orders = [{"symbol": "NVDA", "status": "new", "id": "o1", "type": "stop_limit", "side": "buy"}]
        tracking = {}
        result = reconcile(positions, orders, tracking)
        anomalies = [a for a in result["anomalies"] if a["type"] == "order_while_paused"]
        assert len(anomalies) == 1

    def test_markdown_report_generated(self):
        from controls.reconcile import reconcile, generate_reconciliation_report
        positions = [{"symbol": "NVDA", "qty": "10"}]
        orders = []
        tracking = {}
        result = reconcile(positions, orders, tracking)
        md = generate_reconciliation_report(result)
        assert "# Reconciliation Report" in md
        assert "missing_tracking" in md


# ══════════════════════════════════════════════════════════════════════════════
# E. Health Check Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        from controls.pause_rules import PAUSE_STATE_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def test_healthy_when_no_issues(self):
        from controls.health import generate_health_summary
        summary = generate_health_summary()
        # May show warnings for missing heartbeats, but should not be killed/paused
        assert summary["overall_status"] in ("healthy", "degraded")
        assert summary["kill_switch"]["active"] is False
        assert summary["pause_state"]["paused"] is False

    def test_killed_status_when_kill_active(self):
        from controls.health import generate_health_summary
        from controls.kill_switch import activate_kill_switch
        activate_kill_switch(reason="Health test", triggered_by="test")
        summary = generate_health_summary()
        assert summary["overall_status"] == "killed"

    def test_paused_status_when_paused(self):
        from controls.health import generate_health_summary
        from controls.pause_rules import activate_pause
        activate_pause("test", "Health test pause")
        summary = generate_health_summary()
        assert summary["overall_status"] == "paused"

    def test_stale_heartbeat_detected(self):
        from controls.health import check_heartbeat_health
        old_ts = (datetime.now(paths.MARKET_TZ) - timedelta(hours=24)).isoformat()
        heartbeats = {
            "trade_growth": {"timestamp": old_ts, "status": "ok"},
        }
        issues = check_heartbeat_health(heartbeats)
        stale = [i for i in issues if i["issue"] == "stale_heartbeat"]
        assert len(stale) >= 1

    def test_error_status_detected(self):
        from controls.health import check_heartbeat_health
        recent_ts = datetime.now(paths.MARKET_TZ).isoformat()
        heartbeats = {
            "trade_growth": {"timestamp": recent_ts, "status": "error"},
        }
        issues = check_heartbeat_health(heartbeats)
        errors = [i for i in issues if i["issue"] == "error_status"]
        assert len(errors) == 1

    def test_markdown_report_generation(self):
        from controls.health import generate_health_summary, generate_health_markdown
        summary = generate_health_summary()
        md = generate_health_markdown(summary)
        assert "# System Health Report" in md
        assert "Kill Switch" in md


# ══════════════════════════════════════════════════════════════════════════════
# F. Audit Logging Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLogging:
    def test_audit_log_writes(self):
        from controls.audit import audit_log, read_audit_log
        event = audit_log(
            action="test_action",
            severity="info",
            module="test",
            reason="Testing audit log",
            symbol="NVDA",
            control_rule="test_rule",
        )
        assert event["action"] == "test_action"
        assert event["severity"] == "info"

        events = read_audit_log()
        matching = [e for e in events if e.get("action") == "test_action"]
        assert len(matching) >= 1

    def test_audit_log_with_state_change(self):
        from controls.audit import audit_log, read_audit_log
        event = audit_log(
            action="state_changed",
            severity="warning",
            module="test",
            reason="State transition",
            state_change={"before": "active", "after": "inactive"},
        )
        assert event["state_change"]["before"] == "active"

    def test_kill_switch_creates_audit_entry(self):
        from controls.kill_switch import activate_kill_switch, KILL_SWITCH_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        activate_kill_switch(reason="Audit test", triggered_by="test")
        from controls.audit import read_audit_log
        events = read_audit_log()
        ks_events = [e for e in events if e.get("action") == "kill_switch_activated"]
        assert len(ks_events) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# G. Manual Reset Flow Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestManualResetFlow:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        from controls.pause_rules import PAUSE_STATE_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def test_full_kill_reset_cycle(self):
        from controls.kill_switch import (
            activate_kill_switch, deactivate_kill_switch,
            is_kill_switch_active, is_in_cooldown,
        )
        # Activate
        activate_kill_switch(reason="Cycle test", triggered_by="test")
        assert is_kill_switch_active() is True

        # Reset
        deactivate_kill_switch(reset_by="test")
        assert is_kill_switch_active() is False
        assert is_in_cooldown() is True

    def test_full_pause_reset_cycle(self):
        from controls.pause_rules import activate_pause, reset_pause, is_paused
        activate_pause("test_rule", "Cycle test")
        assert is_paused() is True
        reset_pause(reset_by="test")
        assert is_paused() is False

    def test_pretrade_passes_after_reset(self):
        from controls.kill_switch import activate_kill_switch, deactivate_kill_switch, KILL_SWITCH_PATH
        from controls.pretrade import check_all

        # Activate kill
        activate_kill_switch(reason="Reset test", triggered_by="test")

        # Verify blocked
        order = {"symbol": "AMD", "qty": 5, "notional_value": 2000, "risk_pct": 0.5}
        ctx = {"equity": 20000, "open_positions": 1, "pending_order_symbols": [],
               "total_risk_pct": 0.5, "symbol_allocation_pct": {}, "correlated_count": 0}
        config = {
            "max_open_positions": 5, "max_allocation_per_symbol_pct": 25,
            "max_total_open_risk_pct": 5, "no_duplicate_pending_orders": True,
            "max_correlated_positions": 2, "min_order_qty": 1, "max_order_qty": 1000,
            "check_kill_switch_first": True, "check_pause_state_first": True,
            "max_position_size_pct": 25, "price_sanity_max_deviation_pct": 10,
        }
        result = check_all(order, ctx, config)
        assert result["passed"] is False

        # Reset kill switch and remove cooldown for test
        deactivate_kill_switch(reset_by="test")
        # Manually clear cooldown for test
        from controls.kill_switch import load_kill_switch_state, save_kill_switch_state
        state = load_kill_switch_state()
        state["cooldown_until"] = None
        save_kill_switch_state(state)

        # Should pass now
        result = check_all(order, ctx, config)
        assert result["passed"] is True

    def test_no_entries_while_killed(self):
        """Ensure kill switch blocks ALL entry attempts."""
        from controls.kill_switch import activate_kill_switch
        from controls.pretrade import check_all

        activate_kill_switch(reason="Block all", triggered_by="test")

        orders = [
            {"symbol": "NVDA", "qty": 5, "notional_value": 2500, "risk_pct": 0.5},
            {"symbol": "AMD", "qty": 10, "notional_value": 4000, "risk_pct": 0.75},
            {"symbol": "META", "qty": 3, "notional_value": 1500, "risk_pct": 0.3},
        ]
        ctx = {"equity": 20000, "open_positions": 0, "pending_order_symbols": [],
               "total_risk_pct": 0, "symbol_allocation_pct": {}, "correlated_count": 0}
        config = {
            "max_open_positions": 5, "max_allocation_per_symbol_pct": 25,
            "max_total_open_risk_pct": 5, "no_duplicate_pending_orders": True,
            "max_correlated_positions": 2, "min_order_qty": 1, "max_order_qty": 1000,
            "check_kill_switch_first": True, "check_pause_state_first": True,
            "max_position_size_pct": 25, "price_sanity_max_deviation_pct": 10,
        }

        for order in orders:
            result = check_all(order, ctx, config)
            assert result["passed"] is False
            assert "kill_switch" in result["blocked_by"]


# ══════════════════════════════════════════════════════════════════════════════
# H. Integration / End-to-End Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    def setup_method(self):
        from controls.kill_switch import KILL_SWITCH_PATH
        from controls.pause_rules import PAUSE_STATE_PATH
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        if PAUSE_STATE_PATH.exists():
            PAUSE_STATE_PATH.unlink()

    def test_pause_rule_breach_blocks_pretrade(self):
        """When a pause rule triggers, pre-trade checks should block."""
        from controls.pause_rules import evaluate_daily_loss, activate_pause
        from controls.pretrade import check_all

        config = {"rules": {"daily_loss_pct": {"enabled": True, "threshold": 3.0}}}
        breach = evaluate_daily_loss(19000, 20000, config)
        assert breach is not None

        # Activate pause
        activate_pause(breach["rule"], breach["reason"])

        # Attempt trade
        order = {"symbol": "NVDA", "qty": 5, "notional_value": 2500, "risk_pct": 0.5}
        ctx = {"equity": 19000, "open_positions": 0, "pending_order_symbols": [],
               "total_risk_pct": 0, "symbol_allocation_pct": {}, "correlated_count": 0}
        pt_config = {
            "max_open_positions": 5, "max_allocation_per_symbol_pct": 25,
            "max_total_open_risk_pct": 5, "no_duplicate_pending_orders": True,
            "max_correlated_positions": 2, "min_order_qty": 1, "max_order_qty": 1000,
            "check_kill_switch_first": True, "check_pause_state_first": True,
            "max_position_size_pct": 25, "price_sanity_max_deviation_pct": 10,
        }
        result = check_all(order, ctx, pt_config)
        assert result["passed"] is False
        assert "pause_state" in result["blocked_by"]

    def test_reconcile_then_health(self):
        """Reconciliation result should appear in health summary."""
        from controls.reconcile import reconcile, save_reconciliation_output, generate_reconciliation_report
        from controls.health import generate_health_summary

        positions = [{"symbol": "NVDA", "qty": "10"}]
        orders = []
        tracking = {}
        result = reconcile(positions, orders, tracking)
        md = generate_reconciliation_report(result)
        save_reconciliation_output(result, md)

        summary = generate_health_summary()
        assert summary["reconciliation"].get("anomaly_count", 0) > 0

