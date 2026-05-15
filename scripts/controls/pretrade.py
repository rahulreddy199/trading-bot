"""
Phase 3: Pre-Trade Controls.

Configurable checks that must pass before any order is submitted.
Returns structured pass/fail results for each control.
"""
import json
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import CONFIG_DIR
from infra.jsonio import load_json
from controls.kill_switch import check_kill_switch
from controls.pause_rules import check_pause
from controls.audit import audit_log


def load_pretrade_config():
    rc_path = CONFIG_DIR / "risk_controls.json"
    if rc_path.exists():
        rc = load_json(rc_path)
        return rc.get("pretrade", {})
    return {}


def check_all(order, portfolio_context, config=None):
    """
    Run all pre-trade controls against a proposed order.

    Args:
        order: dict with keys: symbol, qty, side, limit_price, stop_price, notional_value
        portfolio_context: dict with keys: equity, open_positions, open_position_symbols,
                          pending_order_symbols, total_risk_pct, symbol_allocation_pct,
                          correlated_count (for the proposed symbol)
        config: optional override for pretrade config

    Returns:
        dict with:
            - passed: bool (True if all controls pass)
            - results: list of individual check results
            - blocked_by: list of control names that blocked (empty if passed)
    """
    if config is None:
        config = load_pretrade_config()

    results = []
    blocked_by = []

    # 1. Kill switch check (always first)
    if config.get("check_kill_switch_first", True):
        ks = check_kill_switch()
        result = {"control": "kill_switch", "passed": not ks["blocked"]}
        if ks["blocked"]:
            result["reason"] = ks["reason"]
            blocked_by.append("kill_switch")
        results.append(result)

    # 2. Pause state check
    if config.get("check_pause_state_first", True):
        ps = check_pause()
        result = {"control": "pause_state", "passed": not ps["blocked"]}
        if ps["blocked"]:
            result["reason"] = ps["reason"]
            blocked_by.append("pause_state")
        results.append(result)

    # 3. Max open positions
    max_pos = config.get("max_open_positions", 5)
    current_pos = portfolio_context.get("open_positions", 0)
    passed = current_pos < max_pos
    result = {"control": "max_open_positions", "passed": passed,
              "value": current_pos, "limit": max_pos}
    if not passed:
        result["reason"] = f"Open positions ({current_pos}) >= limit ({max_pos})"
        blocked_by.append("max_open_positions")
    results.append(result)

    # 4. Max allocation per symbol
    max_alloc = config.get("max_allocation_per_symbol_pct", 25.0)
    symbol = order.get("symbol", "")
    equity = portfolio_context.get("equity", 0)
    notional = order.get("notional_value", 0)
    if equity > 0:
        alloc_pct = (notional / equity) * 100
        existing_alloc = portfolio_context.get("symbol_allocation_pct", {}).get(symbol, 0)
        total_alloc = alloc_pct + existing_alloc
        passed = total_alloc <= max_alloc
        result = {"control": "max_allocation_per_symbol", "passed": passed,
                  "value": total_alloc, "limit": max_alloc}
        if not passed:
            result["reason"] = f"Symbol allocation ({total_alloc:.1f}%) > limit ({max_alloc}%)"
            blocked_by.append("max_allocation_per_symbol")
        results.append(result)

    # 5. Max total open risk
    max_risk = config.get("max_total_open_risk_pct", 5.0)
    current_risk = portfolio_context.get("total_risk_pct", 0)
    order_risk = order.get("risk_pct", 0)
    projected_risk = current_risk + order_risk
    passed = projected_risk <= max_risk
    result = {"control": "max_total_open_risk", "passed": passed,
              "value": projected_risk, "limit": max_risk}
    if not passed:
        result["reason"] = f"Projected total risk ({projected_risk:.2f}%) > limit ({max_risk}%)"
        blocked_by.append("max_total_open_risk")
    results.append(result)

    # 6. No duplicate pending orders
    if config.get("no_duplicate_pending_orders", True):
        pending = portfolio_context.get("pending_order_symbols", [])
        passed = symbol not in pending
        result = {"control": "no_duplicate_pending", "passed": passed, "symbol": symbol}
        if not passed:
            result["reason"] = f"Duplicate pending order for {symbol}"
            blocked_by.append("no_duplicate_pending")
        results.append(result)

    # 7. Correlation cap
    corr_threshold = config.get("correlation_cap_threshold", 0.85)
    max_corr = config.get("max_correlated_positions", 2)
    corr_count = portfolio_context.get("correlated_count", 0)
    passed = corr_count < max_corr
    result = {"control": "correlation_cap", "passed": passed,
              "value": corr_count, "limit": max_corr}
    if not passed:
        result["reason"] = f"Correlated positions ({corr_count}) >= limit ({max_corr})"
        blocked_by.append("correlation_cap")
    results.append(result)

    # 8. Order quantity sanity
    qty = order.get("qty", 0)
    min_qty = config.get("min_order_qty", 1)
    max_qty = config.get("max_order_qty", 1000)
    passed = min_qty <= qty <= max_qty
    result = {"control": "order_qty_sanity", "passed": passed,
              "value": qty, "min": min_qty, "max": max_qty}
    if not passed:
        result["reason"] = f"Order qty ({qty}) outside bounds [{min_qty}, {max_qty}]"
        blocked_by.append("order_qty_sanity")
    results.append(result)

    # 9. Price sanity (if limit price provided)
    limit_price = order.get("limit_price", 0)
    reference_price = order.get("reference_price", 0)
    max_dev = config.get("price_sanity_max_deviation_pct", 10.0)
    if limit_price > 0 and reference_price > 0:
        deviation_pct = abs(limit_price - reference_price) / reference_price * 100
        passed = deviation_pct <= max_dev
        result = {"control": "price_sanity", "passed": passed,
                  "value": deviation_pct, "limit": max_dev}
        if not passed:
            result["reason"] = f"Price deviation ({deviation_pct:.2f}%) > limit ({max_dev}%)"
            blocked_by.append("price_sanity")
        results.append(result)

    # 10. Max position size
    max_pos_size = config.get("max_position_size_pct", 25.0)
    if equity > 0 and notional > 0:
        pos_size_pct = (notional / equity) * 100
        passed = pos_size_pct <= max_pos_size
        result = {"control": "max_position_size", "passed": passed,
                  "value": pos_size_pct, "limit": max_pos_size}
        if not passed:
            result["reason"] = f"Position size ({pos_size_pct:.1f}%) > limit ({max_pos_size}%)"
            blocked_by.append("max_position_size")
        results.append(result)

    overall_passed = len(blocked_by) == 0

    # Log blocked orders
    if not overall_passed:
        audit_log(
            action="order_blocked_pretrade",
            severity="warning",
            module="controls.pretrade",
            reason=f"Blocked by: {', '.join(blocked_by)}",
            symbol=symbol,
            control_rule=blocked_by[0],
            extra={"all_blocked_by": blocked_by, "order": order},
        )

    return {
        "passed": overall_passed,
        "results": results,
        "blocked_by": blocked_by,
    }

