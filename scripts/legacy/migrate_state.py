"""
State Migration Script — Phase 0

Copies existing flat state files into the new namespaced directory structure.
Safe to run multiple times (checks before overwriting).

Old:  state/candidates_growth.json
New:  state/growth/candidates.json

Old:  state/candidates.json
New:  state/conservative/candidates.json
"""
import json
import shutil
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, STATE_GROWTH, STATE_CONSERVATIVE, STATE_SHARED

# Growth bot files: old_name → new_name
GROWTH_FILES = {
    "candidates_growth.json": "candidates.json",
    "candidates_growth.csv": "candidates.csv",
    "order_plan_growth.json": "order_plan.json",
    "last_orders_growth.json": "last_orders.json",
    "position_tracking_growth.json": "position_tracking.json",
    "manage_log_growth.json": "manage_log.json",
    "rejected_growth.csv": "rejected.csv",
}

# Conservative bot files
CONSERVATIVE_FILES = {
    "candidates.json": "candidates.json",
    "candidates.csv": "candidates.csv",
    "order_plan.json": "order_plan.json",
    "last_orders.json": "last_orders.json",
    "position_tracking.json": "position_tracking.json",
    "manage_log.json": "manage_log.json",
    "rejected.csv": "rejected.csv",
}

# Shared files
SHARED_FILES = {
    "performance.json": "performance.json",
    "equity_curve.json": "equity_curve.json",
    "bot_log.json": "bot_log.json",
}


def migrate(dry_run=True):
    print(f"{'DRY RUN' if dry_run else 'MIGRATING'}: state files → namespaced dirs\n")

    migrated = 0
    skipped = 0

    for old_name, new_name in GROWTH_FILES.items():
        old_path = STATE_DIR / old_name
        new_path = STATE_GROWTH / new_name
        if old_path.exists():
            if new_path.exists():
                print(f"  SKIP (exists): {old_name} → growth/{new_name}")
                skipped += 1
            else:
                print(f"  COPY: {old_name} → growth/{new_name}")
                if not dry_run:
                    shutil.copy2(old_path, new_path)
                migrated += 1
        else:
            print(f"  MISS: {old_name} (not found)")

    for old_name, new_name in CONSERVATIVE_FILES.items():
        old_path = STATE_DIR / old_name
        new_path = STATE_CONSERVATIVE / new_name
        if old_path.exists():
            if new_path.exists():
                print(f"  SKIP (exists): {old_name} → conservative/{new_name}")
                skipped += 1
            else:
                print(f"  COPY: {old_name} → conservative/{new_name}")
                if not dry_run:
                    shutil.copy2(old_path, new_path)
                migrated += 1

    for old_name, new_name in SHARED_FILES.items():
        old_path = STATE_DIR / old_name
        new_path = STATE_SHARED / new_name
        if old_path.exists():
            if new_path.exists():
                print(f"  SKIP (exists): {old_name} → shared/{new_name}")
                skipped += 1
            else:
                print(f"  COPY: {old_name} → shared/{new_name}")
                if not dry_run:
                    shutil.copy2(old_path, new_path)
                migrated += 1

    print(f"\nDone: {migrated} copied, {skipped} skipped")
    if dry_run:
        print("Re-run with --execute to apply.")


if __name__ == "__main__":
    dry = "--execute" not in sys.argv
    migrate(dry_run=dry)

