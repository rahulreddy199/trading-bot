"""
Experiment registry — track proposed and active experiments.
Stored in state/shared/experiments.json.
"""
import json
from datetime import datetime
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_SHARED, save_json, today_str, now_iso


EXPERIMENTS_PATH = STATE_SHARED / "experiments.json"


def load_experiments():
    if EXPERIMENTS_PATH.exists():
        return json.loads(EXPERIMENTS_PATH.read_text())
    return {"experiments": [], "last_updated": None}


def save_experiments(data):
    data["last_updated"] = now_iso()
    save_json(EXPERIMENTS_PATH, data)


def propose_experiment(experiment_id, bot, hypothesis, primary_metric,
                       secondary_metrics=None, baseline_version=None,
                       candidate_version=None, evaluation_window="30 trades"):
    """Add a proposed experiment to the registry."""
    data = load_experiments()

    # Check for duplicate
    if any(e["id"] == experiment_id for e in data["experiments"]):
        print(f"Experiment '{experiment_id}' already exists.")
        return False

    exp = {
        "id": experiment_id,
        "bot": bot,
        "hypothesis": hypothesis,
        "primary_metric": primary_metric,
        "secondary_metrics": secondary_metrics or [],
        "baseline_version": baseline_version or "current",
        "candidate_version": candidate_version,
        "evaluation_window": evaluation_window,
        "status": "proposed",
        "proposed_at": now_iso(),
        "activated_at": None,
        "completed_at": None,
        "result": None,
    }
    data["experiments"].append(exp)
    save_experiments(data)
    print(f"Experiment proposed: {experiment_id}")
    return True


def activate_experiment(experiment_id):
    """Move experiment from proposed to active."""
    data = load_experiments()
    for exp in data["experiments"]:
        if exp["id"] == experiment_id and exp["status"] == "proposed":
            exp["status"] = "active"
            exp["activated_at"] = now_iso()
            save_experiments(data)
            print(f"Experiment activated: {experiment_id}")
            return True
    return False


def complete_experiment(experiment_id, result_summary):
    """Mark experiment as completed with result."""
    data = load_experiments()
    for exp in data["experiments"]:
        if exp["id"] == experiment_id and exp["status"] == "active":
            exp["status"] = "completed"
            exp["completed_at"] = now_iso()
            exp["result"] = result_summary
            save_experiments(data)
            print(f"Experiment completed: {experiment_id}")
            return True
    return False


def list_experiments(status=None):
    """List experiments, optionally filtered by status."""
    data = load_experiments()
    exps = data["experiments"]
    if status:
        exps = [e for e in exps if e["status"] == status]
    return exps


if __name__ == "__main__":
    import sys as _sys
    if "--list" in _sys.argv:
        for e in list_experiments():
            print(f"  [{e['status']}] {e['id']}: {e['hypothesis']}")
    else:
        print(f"Experiments: {len(list_experiments())} total")
        for status in ("proposed", "active", "completed"):
            count = len(list_experiments(status))
            if count:
                print(f"  {status}: {count}")

