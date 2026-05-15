"""
Experiment registry — Phase 2 enhanced.

Statuses: proposed → active_backtest → active_paper → completed | rejected | promoted | rolled_back

Stored in state/shared/experiments.json.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_SHARED, save_json, today_str, now_iso


EXPERIMENTS_PATH = STATE_SHARED / "experiments.json"

VALID_STATUSES = (
    "proposed", "active_backtest", "active_paper",
    "completed", "rejected", "promoted", "rolled_back",
)

VALID_TRANSITIONS = {
    "proposed": ("active_backtest", "rejected"),
    "active_backtest": ("active_paper", "rejected", "completed"),
    "active_paper": ("completed", "promoted", "rejected"),
    "completed": ("promoted", "rejected"),
    "promoted": ("rolled_back",),
    "rejected": (),
    "rolled_back": (),
}


def load_experiments() -> Dict:
    if EXPERIMENTS_PATH.exists():
        return json.loads(EXPERIMENTS_PATH.read_text())
    return {"experiments": [], "last_updated": None}


def save_experiments(data: Dict):
    data["last_updated"] = now_iso()
    save_json(EXPERIMENTS_PATH, data)


def get_experiment(experiment_id: str) -> Optional[Dict]:
    """Get a single experiment by ID."""
    data = load_experiments()
    for exp in data["experiments"]:
        if exp["id"] == experiment_id:
            return exp
    return None


def propose_experiment(
    experiment_id: str,
    title: str = "",
    hypothesis: str = "",
    variant_overrides: Optional[Dict] = None,
    strategy_family: str = "growth_momentum",
    owner: str = "manual",
    baseline_ref: str = "config/strategy_growth.json",
    backtest_config: Optional[Dict] = None,
    notes: str = "",
    approval_required: bool = True,
    primary_metric: str = "expectancy",
    secondary_metrics: Optional[list] = None,
    evaluation_window: str = "30 trades",
    # Legacy compat kwargs
    bot: str = "growth",
    candidate_version: str = None,
    baseline_version: str = None,
    **kwargs,
) -> bool:
    """Add a new experiment to the registry."""
    data = load_experiments()

    if any(e["id"] == experiment_id for e in data["experiments"]):
        print(f"Experiment '{experiment_id}' already exists.")
        return False

    exp = {
        "id": experiment_id,
        "title": title or hypothesis,
        "hypothesis": hypothesis,
        "strategy_family": strategy_family,
        "status": "proposed",
        "owner": owner,
        "baseline_ref": baseline_ref,
        "variant_overrides": variant_overrides or {},
        "backtest_config": backtest_config or {},
        "primary_metric": primary_metric,
        "secondary_metrics": secondary_metrics or [],
        "evaluation_window": evaluation_window,
        "approval_required": approval_required,
        "notes": notes,
        "bot": bot,
        # Legacy compat
        "baseline_version": baseline_version or "current",
        "candidate_version": candidate_version,
        # Timestamps
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "proposed_at": now_iso(),
        "activated_at": None,
        "completed_at": None,
        # Results
        "result": None,
        "scorecard": None,
        "promotion_decision": None,
        "rollback_condition": None,
        # Paper incubation
        "paper_started_at": None,
        "paper_trades": 0,
        "paper_metrics": None,
    }
    data["experiments"].append(exp)
    save_experiments(data)
    print(f"Experiment proposed: {experiment_id}")
    return True


def transition_experiment(experiment_id: str, new_status: str, **extra_fields) -> bool:
    """Move experiment to a new status if the transition is valid."""
    if new_status not in VALID_STATUSES:
        print(f"Invalid status: {new_status}")
        return False

    data = load_experiments()
    for exp in data["experiments"]:
        if exp["id"] != experiment_id:
            continue

        current = exp["status"]
        allowed = VALID_TRANSITIONS.get(current, ())
        if new_status not in allowed:
            print(f"Invalid transition: {current} → {new_status} (allowed: {allowed})")
            return False

        exp["status"] = new_status
        exp["updated_at"] = now_iso()

        if new_status == "active_backtest":
            exp["activated_at"] = now_iso()
        elif new_status == "active_paper":
            exp["paper_started_at"] = now_iso()
        elif new_status in ("completed", "rejected", "promoted"):
            exp["completed_at"] = now_iso()

        # Merge extra fields
        for k, v in extra_fields.items():
            exp[k] = v

        save_experiments(data)
        print(f"Experiment {experiment_id}: {current} → {new_status}")
        return True

    print(f"Experiment '{experiment_id}' not found.")
    return False


def update_experiment(experiment_id: str, **fields) -> bool:
    """Update arbitrary fields on an experiment."""
    data = load_experiments()
    for exp in data["experiments"]:
        if exp["id"] == experiment_id:
            for k, v in fields.items():
                exp[k] = v
            exp["updated_at"] = now_iso()
            save_experiments(data)
            return True
    return False


def list_experiments(status: Optional[str] = None) -> List[Dict]:
    """List experiments, optionally filtered by status."""
    data = load_experiments()
    exps = data["experiments"]
    if status:
        exps = [e for e in exps if e["status"] == status]
    return exps


# Legacy compat aliases
def activate_experiment(experiment_id: str) -> bool:
    return transition_experiment(experiment_id, "active_backtest")


def complete_experiment(experiment_id: str, result_summary=None) -> bool:
    return transition_experiment(experiment_id, "completed", result=result_summary)


def load_experiment_config(config_path: Path) -> Dict:
    """Load an experiment definition from config/experiments/*.json."""
    return json.loads(config_path.read_text())


if __name__ == "__main__":
    import sys as _sys
    if "--list" in _sys.argv:
        for e in list_experiments():
            print(f"  [{e['status']}] {e['id']}: {e.get('title', e.get('hypothesis', '?'))}")
    else:
        print(f"Experiments: {len(list_experiments())} total")
        for status in VALID_STATUSES:
            count = len(list_experiments(status))
            if count:
                print(f"  {status}: {count}")
