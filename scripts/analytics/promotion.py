"""
Promotion gate evaluation — check whether a variant passes all gates.

Pure functions: takes scorecards + promotion rules, returns structured verdict.
No I/O except optional config loading.
"""
import json
from pathlib import Path
from typing import Dict, Optional, List, Tuple


def load_promotion_rules(path: Optional[Path] = None) -> Dict:
    """Load promotion rules from config."""
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "promotion_rules.json"
    return json.loads(path.read_text())


def evaluate_gate(
    gate_name: str,
    gate_cfg: Dict,
    baseline_metrics: Dict,
    variant_metrics: Dict,
    oos_metrics: Optional[Dict] = None,
    paper_metrics: Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Evaluate a single promotion gate.

    Returns (passed: bool, reason: str).
    """
    value = gate_cfg["value"]

    if gate_name == "min_trade_count":
        total = variant_metrics.get("total_trades", 0)
        passed = total >= value
        return passed, f"total_trades={total} (need >={value})"

    elif gate_name == "min_oos_trade_count":
        oos_total = (oos_metrics or {}).get("total_trades", 0)
        passed = oos_total >= value
        return passed, f"oos_trades={oos_total} (need >={value})"

    elif gate_name == "profit_factor_floor":
        pf = variant_metrics.get("profit_factor", 0)
        if isinstance(pf, float) and pf == float("inf"):
            pf = 999
        passed = pf >= value
        return passed, f"profit_factor={pf} (need >={value})"

    elif gate_name == "expectancy_improvement_min":
        b_exp = baseline_metrics.get("expectancy", 0)
        v_exp = variant_metrics.get("expectancy", 0)
        delta = v_exp - b_exp
        passed = delta >= value
        return passed, f"expectancy delta={delta:.2f} (need >={value})"

    elif gate_name == "max_drawdown_increase_limit":
        b_dd = baseline_metrics.get("max_drawdown", 0)
        v_dd = variant_metrics.get("max_drawdown", 0)
        dd_increase = v_dd - b_dd
        passed = dd_increase <= value
        return passed, f"drawdown increase={dd_increase:.4f} (limit {value})"

    elif gate_name == "win_rate_floor":
        wr = variant_metrics.get("win_rate", 0)
        passed = wr >= value
        return passed, f"win_rate={wr:.4f} (need >={value})"

    elif gate_name == "avg_r_floor":
        ar = variant_metrics.get("avg_r", 0)
        passed = ar >= value
        return passed, f"avg_r={ar} (need >={value})"

    elif gate_name == "oos_profit_factor_floor":
        oos_pf = (oos_metrics or {}).get("profit_factor", 0)
        if isinstance(oos_pf, float) and oos_pf == float("inf"):
            oos_pf = 999
        passed = oos_pf >= value
        return passed, f"oos_profit_factor={oos_pf} (need >={value})"

    elif gate_name == "paper_incubation_required":
        if not value:
            return True, "paper incubation not required"
        has_paper = paper_metrics is not None and paper_metrics.get("total_trades", 0) > 0
        return has_paper, f"paper_data={'present' if has_paper else 'MISSING'}"

    elif gate_name == "paper_min_trades":
        paper_trades = (paper_metrics or {}).get("total_trades", 0)
        passed = paper_trades >= value
        return passed, f"paper_trades={paper_trades} (need >={value})"

    else:
        return True, f"unknown gate '{gate_name}' — skipped"


def evaluate_all_gates(
    promotion_rules: Dict,
    baseline_metrics: Dict,
    variant_metrics: Dict,
    oos_metrics: Optional[Dict] = None,
    paper_metrics: Optional[Dict] = None,
) -> Dict:
    """
    Evaluate all promotion gates.

    Returns {
        "passed": bool,        # all required gates passed
        "gates": [{name, required, passed, reason}, ...],
        "summary": str,
    }
    """
    gates_cfg = promotion_rules.get("gates", {})
    results = []
    all_required_passed = True

    for gate_name, gate_cfg in gates_cfg.items():
        required = gate_cfg.get("required", False)
        passed, reason = evaluate_gate(
            gate_name, gate_cfg,
            baseline_metrics, variant_metrics,
            oos_metrics, paper_metrics,
        )
        results.append({
            "gate": gate_name,
            "required": required,
            "passed": passed,
            "reason": reason,
        })
        if required and not passed:
            all_required_passed = False

    passed_count = sum(1 for r in results if r["passed"])
    failed_required = [r for r in results if r["required"] and not r["passed"]]

    if all_required_passed:
        summary = f"✅ ALL {passed_count}/{len(results)} gates passed — eligible for promotion"
    else:
        failed_names = [r["gate"] for r in failed_required]
        summary = f"❌ {len(failed_required)} required gate(s) failed: {', '.join(failed_names)}"

    return {
        "passed": all_required_passed,
        "gates": results,
        "passed_count": passed_count,
        "total_count": len(results),
        "failed_required": [r["gate"] for r in failed_required],
        "summary": summary,
    }


def generate_promotion_recommendation(
    experiment_id: str,
    comparison: Dict,
    gate_result: Dict,
    parameter_diffs: list,
) -> Dict:
    """
    Produce a structured promotion recommendation.

    This is ADVISORY ONLY — never auto-promotes.
    """
    recommendation = "PROMOTE" if gate_result["passed"] else "REJECT"

    return {
        "experiment_id": experiment_id,
        "recommendation": recommendation,
        "gates_passed": gate_result["passed"],
        "gates_summary": gate_result["summary"],
        "gate_details": gate_result["gates"],
        "parameter_changes": parameter_diffs,
        "metric_deltas": comparison.get("deltas", {}),
        "verdicts": comparison.get("verdicts", []),
        "action_required": "Manual review and explicit approval required before any config change."
            if gate_result["passed"] else "No action needed — variant does not meet promotion criteria.",
    }

