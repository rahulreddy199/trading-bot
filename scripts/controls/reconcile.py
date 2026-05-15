"""
Phase 3: Order and Broker Reconciliation.

Compares internal state files with broker positions/orders to detect anomalies.
Produces structured JSON + Markdown reports.
"""
import json
from datetime import datetime
from pathlib import Path
from collections import Counter

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from infra.paths import STATE_DIR, CONFIG_DIR, MARKET_TZ
from infra.jsonio import load_json, save_json
from controls.audit import audit_log
from controls.kill_switch import is_kill_switch_active
from controls.pause_rules import is_paused


CONTROLS_DIR = STATE_DIR / "controls"
CONTROLS_DIR.mkdir(parents=True, exist_ok=True)


def load_reconciliation_config():
    path = CONFIG_DIR / "reconciliation.json"
    if path.exists():
        return load_json(path)
    return {"checks": {}, "safe_cleanup": {"enabled": False}}


def reconcile(broker_positions, broker_orders, local_tracking, config=None):
    """
    Run reconciliation checks comparing broker state vs local state.

    Args:
        broker_positions: list of dicts with at least 'symbol', 'qty'
        broker_orders: list of dicts with 'symbol', 'status', 'id', 'side', 'type'
        local_tracking: dict keyed by symbol with position tracking data
        config: reconciliation config (loaded if None)

    Returns:
        Structured reconciliation result dict
    """
    if config is None:
        config = load_reconciliation_config()

    checks = config.get("checks", {})
    anomalies = []
    warnings = []

    broker_symbols = {p["symbol"] for p in broker_positions if float(p.get("qty", 0)) != 0}
    tracked_symbols = set(local_tracking.keys())
    active_order_symbols = {o["symbol"] for o in broker_orders
                           if o.get("status") in ("new", "accepted", "held", "partially_filled")}

    # 1. Orphaned orders — at broker but no local tracking
    if checks.get("orphaned_orders", {}).get("enabled", True):
        for order in broker_orders:
            sym = order.get("symbol", "")
            status = order.get("status", "")
            if status in ("new", "accepted", "held", "partially_filled"):
                if sym not in tracked_symbols and order.get("side") != "buy":
                    anomalies.append({
                        "type": "orphaned_order",
                        "symbol": sym,
                        "order_id": order.get("id"),
                        "status": status,
                        "description": f"Active order for {sym} with no local tracking",
                    })

    # 2. Missing tracking — broker position exists but no local tracking
    if checks.get("missing_tracking", {}).get("enabled", True):
        for sym in broker_symbols:
            if sym not in tracked_symbols:
                anomalies.append({
                    "type": "missing_tracking",
                    "symbol": sym,
                    "description": f"Broker position in {sym} with no local tracking entry",
                })

    # 3. Stale tracking — local tracking for closed positions
    if checks.get("stale_tracking", {}).get("enabled", True):
        for sym in tracked_symbols:
            if sym not in broker_symbols:
                # Check if there's a pending entry order
                has_pending_entry = any(
                    o.get("symbol") == sym and o.get("side") == "buy"
                    and o.get("status") in ("new", "accepted", "held")
                    for o in broker_orders
                )
                if not has_pending_entry:
                    warnings.append({
                        "type": "stale_tracking",
                        "symbol": sym,
                        "description": f"Local tracking for {sym} but no broker position or pending entry",
                    })

    # 4. Duplicate active orders
    if checks.get("duplicate_orders", {}).get("enabled", True):
        order_counts = Counter()
        for order in broker_orders:
            if order.get("status") in ("new", "accepted", "held", "partially_filled"):
                key = (order.get("symbol"), order.get("side"))
                order_counts[key] += 1
        for (sym, side), count in order_counts.items():
            if count > 1 and side == "buy":
                anomalies.append({
                    "type": "duplicate_orders",
                    "symbol": sym,
                    "count": count,
                    "side": side,
                    "description": f"Multiple active {side} orders ({count}) for {sym}",
                })

    # 5. Missing stop after fill
    if checks.get("missing_stop_after_fill", {}).get("enabled", True):
        for sym in broker_symbols:
            has_stop = any(
                o.get("symbol") == sym
                and o.get("type") in ("stop", "stop_limit", "trailing_stop")
                and o.get("status") in ("new", "accepted", "held")
                for o in broker_orders
            )
            if not has_stop:
                anomalies.append({
                    "type": "missing_stop",
                    "symbol": sym,
                    "description": f"Position in {sym} has no active stop order",
                })

    # 6. Symbols tradeable while paused/killed
    if checks.get("symbol_tradeable_while_paused", {}).get("enabled", True):
        if is_kill_switch_active() or is_paused():
            for order in broker_orders:
                if (order.get("side") == "buy"
                        and order.get("status") in ("new", "accepted", "held")):
                    anomalies.append({
                        "type": "order_while_paused",
                        "symbol": order.get("symbol"),
                        "order_id": order.get("id"),
                        "description": f"Buy order active for {order.get('symbol')} while system is paused/killed",
                    })

    result = {
        "timestamp": datetime.now(MARKET_TZ).isoformat(),
        "anomalies": anomalies,
        "warnings": warnings,
        "summary": {
            "anomaly_count": len(anomalies),
            "warning_count": len(warnings),
            "broker_positions": len(broker_symbols),
            "tracked_positions": len(tracked_symbols),
            "active_orders": len(active_order_symbols),
            "healthy": len(anomalies) == 0,
        },
    }

    # Audit log anomalies
    if anomalies:
        audit_log(
            action="reconciliation_anomalies_detected",
            severity="warning",
            module="controls.reconcile",
            reason=f"{len(anomalies)} anomalies detected",
            extra={"anomaly_types": [a["type"] for a in anomalies]},
        )

    return result


def generate_reconciliation_report(result):
    """Generate a Markdown report from reconciliation result."""
    ts = result.get("timestamp", "unknown")
    summary = result.get("summary", {})
    anomalies = result.get("anomalies", [])
    warnings = result.get("warnings", [])

    lines = [
        f"# Reconciliation Report",
        f"**Generated:** {ts}",
        f"",
        f"## Summary",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Broker positions | {summary.get('broker_positions', 0)} |",
        f"| Tracked positions | {summary.get('tracked_positions', 0)} |",
        f"| Active orders | {summary.get('active_orders', 0)} |",
        f"| Anomalies | {summary.get('anomaly_count', 0)} |",
        f"| Warnings | {summary.get('warning_count', 0)} |",
        f"| Healthy | {'✅' if summary.get('healthy') else '❌'} |",
        f"",
    ]

    if anomalies:
        lines.append("## Anomalies")
        for a in anomalies:
            lines.append(f"- **{a['type']}** [{a.get('symbol', '?')}]: {a['description']}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- **{w['type']}** [{w.get('symbol', '?')}]: {w['description']}")
        lines.append("")

    if not anomalies and not warnings:
        lines.append("## Status\n✅ All checks passed. No anomalies detected.\n")

    return "\n".join(lines)


def save_reconciliation_output(result, report_md):
    """Save reconciliation JSON and Markdown to state/controls/."""
    json_path = CONTROLS_DIR / "reconciliation_result.json"
    md_path = CONTROLS_DIR / "reconciliation_report.md"
    save_json(json_path, result)
    md_path.write_text(report_md, encoding="utf-8")
    return {"json_path": str(json_path), "md_path": str(md_path)}

