"""
Daily analytics pipeline — reads state files, computes metrics, writes outputs.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    STATE_DIR, STATE_SHARED, STATE_LOGS, STATE_GROWTH,
    save_json, today_str, now_iso, get_positions, get_account,
)
from analytics.metrics import compute_all_metrics
from analytics.attribution import full_attribution, top_contributors
from analytics.regime import tag_regime


def load_trade_history():
    """Load trade history from state."""
    path = STATE_DIR / "trade_history.json"
    if path.exists():
        data = json.loads(path.read_text())
        return data.get("trades", [])
    return []


def load_equity_curve():
    """Load equity curve from state."""
    path = STATE_SHARED / "equity_curve.json"
    if not path.exists():
        path = STATE_DIR / "equity_curve.json"
    if path.exists():
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return [float(e.get("equity", e)) if isinstance(e, dict) else float(e) for e in data]
        elif isinstance(data, dict):
            return [float(v) for v in data.values()]
    return []


def load_jsonl_events(date_str=None):
    """Load structured events from JSONL log."""
    if date_str is None:
        date_str = today_str()
    path = STATE_LOGS / f"{date_str}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def count_operational_incidents(events):
    """Count incidents from JSONL events."""
    incidents = {
        "manual_review": 0,
        "stop_recovery": 0,
        "reconciliation_fix": 0,
        "circuit_breaker": 0,
        "stale_lock": 0,
        "errors": 0,
    }
    for e in events:
        reason = e.get("reason", "")
        if reason == "MANUAL_REVIEW_REQUIRED":
            incidents["manual_review"] += 1
        elif reason == "RECONCILIATION_FIX":
            incidents["reconciliation_fix"] += 1
        elif reason == "CIRCUIT_BREAKER":
            incidents["circuit_breaker"] += 1
        elif reason == "LOCK_STALE_CLEANED":
            incidents["stale_lock"] += 1
        elif "STOP" in reason and "FAILED" in reason:
            incidents["stop_recovery"] += 1
        if "error" in e.get("action", "").lower() or "failed" in e.get("action", "").lower():
            incidents["errors"] += 1
    return incidents


def enrich_trades_with_regime(trades):
    """Add regime_at_entry if not already present (best-effort)."""
    # For now, just tag trades that have entry_date but no regime
    for t in trades:
        if not t.get("regime_at_entry") and t.get("regime_mode_at_entry"):
            t["regime_at_entry"] = t["regime_mode_at_entry"]
    return trades


def run_daily_pipeline():
    """Execute the full daily analytics pipeline."""
    date = today_str()
    trades = load_trade_history()
    trades = enrich_trades_with_regime(trades)
    equity_curve = load_equity_curve()
    events = load_jsonl_events(date)

    # Current state
    try:
        account = get_account()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
    except Exception:
        equity = 0
        cash = 0

    try:
        positions = get_positions()
        open_count = len(positions)
        unrealized_pnl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    except Exception:
        open_count = 0
        unrealized_pnl = 0

    # Metrics
    all_metrics = compute_all_metrics(trades, equity_curve)

    # Rolling windows
    now = datetime.now()
    last_7d = [t for t in trades if _within_days(t, 7, now)]
    last_30d = [t for t in trades if _within_days(t, 30, now)]

    rolling = {
        "7d": compute_all_metrics(last_7d),
        "30d": compute_all_metrics(last_30d),
        "all_time": all_metrics,
    }

    # Operational
    incidents = count_operational_incidents(events)

    # Regime
    regime = tag_regime()

    # Daily output
    daily = {
        "date": date,
        "generated_at": now_iso(),
        "equity": equity,
        "cash": cash,
        "open_positions": open_count,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "regime": regime,
        "metrics_all_time": all_metrics,
        "metrics_7d": rolling["7d"],
        "metrics_30d": rolling["30d"],
        "incidents": incidents,
        "total_events_today": len(events),
    }
    save_json(STATE_SHARED / "analytics_daily.json", daily)

    # Rolling output
    save_json(STATE_SHARED / "analytics_rolling.json", rolling)

    # Attribution
    if trades:
        attr = full_attribution(trades)
        attr["top_contributors"] = top_contributors(trades)
        attr["date"] = date
        save_json(STATE_SHARED / "attribution_daily.json", attr)

    print(f"Analytics pipeline complete: {date}")
    print(f"  Equity: ${equity:,.2f} | Trades: {all_metrics['total_trades']} | Open: {open_count}")
    print(f"  Regime: {regime.get('regime_label', '?')} | VIX: {regime.get('vix_value', '?')}")
    print(f"  Incidents today: {sum(incidents.values())}")

    return daily


def _within_days(trade, days, now):
    """Check if a trade's exit date is within N days of now."""
    exit_date = trade.get("exit_date") or trade.get("closed_at") or trade.get("date")
    if not exit_date:
        return False
    try:
        dt = datetime.fromisoformat(exit_date.replace("Z", "+00:00"))
        return (now - dt.replace(tzinfo=None)).days <= days
    except Exception:
        return False


if __name__ == "__main__":
    run_daily_pipeline()

