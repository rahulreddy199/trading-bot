"""
Health Check & Daily Summary — Phase 0 Hardening

Generates a structured health summary covering:
- Job statuses (heartbeat freshness)
- Open positions and unmanaged count
- Manual review flags
- Reconciliation corrections
- Duplicate prevention hits
- Slippage stats
- Stale files
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    STATE_SHARED,
    STATE_LOCKS,
    STATE_LOGS,
    STATE_GROWTH,
    get_positions,
    get_account,
    log_event,
    now_iso,
    resolve_state,
    save_json,
    send_alert,
    today_str,
)


def check_heartbeat_freshness(max_stale_hours=4):
    """Check all heartbeat files for staleness."""
    results = {}
    now = datetime.now(MARKET_TZ)

    for hb_file in STATE_DIR.glob("heartbeat_*.json"):
        try:
            data = json.loads(hb_file.read_text())
            ts = datetime.fromisoformat(data.get("timestamp", ""))
            age_hours = (now - ts).total_seconds() / 3600
            results[data.get("name", hb_file.stem)] = {
                "status": data.get("status"),
                "timestamp": data.get("timestamp"),
                "age_hours": round(age_hours, 1),
                "stale": age_hours > max_stale_hours,
            }
        except Exception:
            results[hb_file.stem] = {"status": "error", "stale": True}

    # Also check shared dir
    for hb_file in STATE_SHARED.glob("heartbeat_*.json"):
        try:
            data = json.loads(hb_file.read_text())
            name = data.get("name", hb_file.stem)
            if name not in results:
                ts = datetime.fromisoformat(data.get("timestamp", ""))
                age_hours = (now - ts).total_seconds() / 3600
                results[name] = {
                    "status": data.get("status"),
                    "timestamp": data.get("timestamp"),
                    "age_hours": round(age_hours, 1),
                    "stale": age_hours > max_stale_hours,
                }
        except Exception:
            pass

    return results


def check_manual_review_flags():
    """Count positions flagged for manual review."""
    flags = []
    for bot in ("growth",):
        tracking_path = resolve_state(bot, "position_tracking.json")
        if not tracking_path.exists():
            continue
        try:
            tracking = json.loads(tracking_path.read_text())
            for symbol, track in tracking.items():
                if track.get("MANUAL_REVIEW"):
                    flags.append({
                        "bot": bot,
                        "symbol": symbol,
                        "reason": track.get("MANUAL_REVIEW_REASON", "unknown"),
                    })
        except Exception:
            pass
    return flags


def check_unmanaged_positions():
    """Find broker positions not tracked by the bot."""
    try:
        positions = get_positions()
    except Exception:
        return []

    tracked_symbols = set()
    for bot in ("growth",):
        tracking_path = resolve_state(bot, "position_tracking.json")
        if tracking_path.exists():
            try:
                tracking = json.loads(tracking_path.read_text())
                tracked_symbols.update(tracking.keys())
            except Exception:
                pass

    unmanaged = []
    for pos in positions:
        if pos["symbol"] not in tracked_symbols:
            unmanaged.append({
                "symbol": pos["symbol"],
                "qty": pos.get("qty"),
                "unrealized_pl": pos.get("unrealized_pl"),
            })
    return unmanaged


def check_stale_files():
    """Check for state files that haven't been updated today."""
    stale = []
    today = today_str()
    critical_files = [
        ("growth", "candidates.json"),
    ]
    for bot, name in critical_files:
        path = resolve_state(bot, name)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                file_date = data.get("date", "")
                if file_date and file_date != today:
                    stale.append({"bot": bot, "file": name, "date": file_date})
            except Exception:
                pass
        else:
            stale.append({"bot": bot, "file": name, "date": "missing"})
    return stale


def count_todays_reconciliation_fixes():
    """Count reconciliation fixes from today's log."""
    log_path = STATE_LOGS / f"{today_str()}.jsonl"
    if not log_path.exists():
        return 0
    count = 0
    try:
        for line in log_path.read_text().splitlines():
            event = json.loads(line)
            if event.get("reason") == "RECONCILIATION_FIX":
                count += 1
    except Exception:
        pass
    return count


def count_dedupe_hits_today():
    """Count duplicate prevention hits from today's receipts."""
    hits = 0
    for receipt_file in STATE_LOCKS.glob("*_receipt.json"):
        try:
            receipt = json.loads(receipt_file.read_text())
            if receipt.get("date") == today_str():
                hits += receipt.get("dedupe_hits", 0)
        except Exception:
            pass
    return hits


def get_job_statuses():
    """Get status of all jobs from their receipts."""
    statuses = {}
    for receipt_file in STATE_LOCKS.glob("*_receipt.json"):
        try:
            receipt = json.loads(receipt_file.read_text())
            if receipt.get("date") == today_str():
                statuses[receipt.get("job_name", receipt_file.stem)] = {
                    "status": receipt.get("status"),
                    "run_at": receipt.get("run_at"),
                    "orders": receipt.get("orders_submitted", 0),
                    "errors": len(receipt.get("errors", [])),
                }
        except Exception:
            pass
    return statuses


def get_last_successful_runs():
    """Get the most recent successful run date for each job (regardless of today)."""
    runs = {}
    for receipt_file in STATE_LOCKS.glob("*_receipt.json"):
        try:
            receipt = json.loads(receipt_file.read_text())
            job = receipt.get("job_name", receipt_file.stem)
            if receipt.get("status") == "completed":
                runs[job] = receipt.get("date")
        except Exception:
            pass
    return runs


def generate_health_summary():
    """Generate complete health summary."""
    summary = {
        "date": today_str(),
        "generated_at": now_iso(),
        "heartbeat_status": check_heartbeat_freshness(),
        "job_statuses": get_job_statuses(),
        "manual_review_count": 0,
        "manual_review_flags": [],
        "unmanaged_positions": [],
        "reconciliation_fixes_today": 0,
        "dedupe_hits_today": 0,
        "stale_files": [],
    }

    # Manual review
    flags = check_manual_review_flags()
    summary["manual_review_count"] = len(flags)
    summary["manual_review_flags"] = flags

    # Unmanaged positions
    summary["unmanaged_positions"] = check_unmanaged_positions()

    # Reconciliation
    summary["reconciliation_fixes_today"] = count_todays_reconciliation_fixes()

    # Dedupe
    summary["dedupe_hits_today"] = count_dedupe_hits_today()

    # Stale files
    summary["stale_files"] = check_stale_files()

    # Last successful runs
    summary["last_successful_runs"] = get_last_successful_runs()

    # Account snapshot
    try:
        account = get_account()
        summary["equity"] = float(account.get("equity", 0))
        summary["cash"] = float(account.get("cash", 0))
    except Exception:
        summary["equity"] = None
        summary["cash"] = None

    # Determine overall health
    issues = []
    stale_hbs = [k for k, v in summary["heartbeat_status"].items() if v.get("stale")]
    if stale_hbs:
        issues.append(f"stale_heartbeats: {stale_hbs}")
    if summary["manual_review_count"] > 0:
        issues.append(f"manual_review: {summary['manual_review_count']}")
    if summary["unmanaged_positions"]:
        issues.append(f"unmanaged: {[p['symbol'] for p in summary['unmanaged_positions']]}")

    summary["overall_health"] = "healthy" if not issues else "needs_attention"
    summary["issues"] = issues

    # Save
    save_json(STATE_SHARED / "health_summary.json", summary)
    log_event("shared", "healthcheck", "summary_generated",
              extra={"health": summary["overall_health"], "issues_count": len(issues)})

    return summary


def print_summary(summary):
    """Print a human-readable health summary."""
    print(f"\n{'='*50}")
    print(f"HEALTH SUMMARY — {summary['date']}")
    print(f"{'='*50}")
    print(f"Overall: {summary['overall_health'].upper()}")
    if summary.get("equity"):
        print(f"Equity: ${summary['equity']:,.2f}")
    print()

    # Heartbeats
    print("Heartbeats:")
    for name, hb in summary.get("heartbeat_status", {}).items():
        status = "🟢" if not hb.get("stale") else "🔴"
        print(f"  {status} {name}: {hb.get('age_hours', '?')}h ago")
    print()

    # Jobs
    jobs = summary.get("job_statuses", {})
    if jobs:
        print("Today's jobs:")
        for name, j in jobs.items():
            print(f"  • {name}: {j.get('status')} (orders={j.get('orders', 0)}, errors={j.get('errors', 0)})")
        print()

    # Issues
    if summary.get("issues"):
        print("⚠️ Issues:")
        for issue in summary["issues"]:
            print(f"  • {issue}")
        print()

    # Flags
    if summary.get("manual_review_flags"):
        print("🔍 Manual review required:")
        for f in summary["manual_review_flags"]:
            print(f"  • {f['bot']}/{f['symbol']}: {f['reason']}")
        print()

    print(f"Reconciliation fixes today: {summary.get('reconciliation_fixes_today', 0)}")
    print(f"Dedupe hits today: {summary.get('dedupe_hits_today', 0)}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    summary = generate_health_summary()
    print_summary(summary)

