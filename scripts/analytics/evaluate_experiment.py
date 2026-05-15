"""
Experiment evaluation pipeline — Phase 2.

Loads experiment config, runs backtests for baseline and variant,
builds scorecards, evaluates promotion gates, writes reports.

This is the main entry point for evaluating experiments.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, STATE_SHARED, CONFIG_DIR, save_json, now_iso, today_str
from analytics.variants import (
    load_baseline_strategy, build_variant, diff_strategies,
)
from analytics.scorecards import (
    build_scorecard, compare_scorecards, split_trades_is_oos,
)
from analytics.promotion import (
    load_promotion_rules, evaluate_all_gates, generate_promotion_recommendation,
)
from analytics.experiments import (
    load_experiment_config, get_experiment,
    transition_experiment, update_experiment,
)


# ── Backtest adapter ──

def _run_backtest_with_strategy(strategy: Dict, start_date: str, end_date: str,
                                 initial_equity: float = 20000.0):
    """
    Run the growth backtester with an arbitrary strategy dict.

    Returns (final_equity, closed_trades, equity_curve).
    """
    from backtest.growth import (
        load_growth_watchlist, download_all_data, get_symbol_df,
        add_indicators, compute_relative_strength, compute_growth_score,
        detect_breakout, detect_continuation, detect_shallow_pullback,
        run_backtest,
    )
    import pandas as pd
    from datetime import timedelta

    # Monkey-patch: override the strategy loader for this run
    import backtest.growth as bt_module
    _orig_load = bt_module.load_growth_strategy
    bt_module.load_growth_strategy = lambda: strategy

    try:
        final_equity, closed_trades = run_backtest(
            start_date=start_date,
            end_date=end_date,
            initial_equity=initial_equity,
        )
    finally:
        bt_module.load_growth_strategy = _orig_load

    # Load equity curve from state (written by run_backtest)
    eq_path = STATE_DIR / "backtest_growth_equity_curve.json"
    equity_curve = []
    if eq_path.exists():
        eq_data = json.loads(eq_path.read_text())
        equity_curve = [float(e.get("equity", e)) if isinstance(e, dict) else float(e) for e in eq_data]

    return final_equity, closed_trades, equity_curve


# ── Report generation ──

def _generate_markdown_report(
    experiment_id: str,
    experiment_cfg: Dict,
    baseline_scorecard: Dict,
    variant_scorecard: Dict,
    baseline_is_card: Dict,
    baseline_oos_card: Dict,
    variant_is_card: Dict,
    variant_oos_card: Dict,
    comparison: Dict,
    oos_comparison: Dict,
    gate_result: Dict,
    recommendation: Dict,
    parameter_diffs: list,
) -> str:
    """Generate a comprehensive markdown report for the experiment."""
    lines = []
    lines.append(f"# Experiment Report: {experiment_id}")
    lines.append(f"_Generated: {now_iso()}_")
    lines.append("")

    # Metadata
    lines.append("## Experiment")
    lines.append(f"- **Title**: {experiment_cfg.get('title', '?')}")
    lines.append(f"- **Hypothesis**: {experiment_cfg.get('hypothesis', '?')}")
    lines.append(f"- **Owner**: {experiment_cfg.get('owner', '?')}")
    lines.append("")

    # Parameter changes
    lines.append("## Parameter Changes")
    lines.append("| Parameter | Baseline | Variant |")
    lines.append("|-----------|----------|---------|")
    for d in parameter_diffs:
        lines.append(f"| `{d['param']}` | {d['baseline']} | {d['variant']} |")
    lines.append("")

    # Overall comparison
    lines.append("## Overall Comparison (Full Period)")
    _add_comparison_table(lines, comparison, baseline_scorecard, variant_scorecard)
    lines.append("")

    # Verdicts
    lines.append("### Verdicts")
    for v in comparison.get("verdicts", []):
        lines.append(f"- {v}")
    lines.append("")

    # IS vs OOS
    lines.append("## In-Sample vs Out-of-Sample")
    lines.append("### In-Sample")
    _add_metrics_table(lines, baseline_is_card, variant_is_card)
    lines.append("")
    lines.append("### Out-of-Sample")
    _add_metrics_table(lines, baseline_oos_card, variant_oos_card)
    lines.append("")

    # OOS verdicts
    lines.append("### OOS Verdicts")
    for v in oos_comparison.get("verdicts", []):
        lines.append(f"- {v}")
    lines.append("")

    # Setup-level attribution
    lines.append("## Setup Attribution (Variant, Full Period)")
    v_setup = variant_scorecard.get("setup_summary", {})
    if v_setup:
        lines.append("| Setup | Trades | Win Rate | Avg R | Net PnL |")
        lines.append("|-------|--------|----------|-------|---------|")
        for setup, data in sorted(v_setup.items()):
            lines.append(f"| {setup} | {data['trades']} | {data['win_rate']*100:.0f}% | {data['avg_r']} | ${data['net_pnl']:,.2f} |")
    lines.append("")

    # Promotion gates
    lines.append("## Promotion Gates")
    lines.append(f"**{gate_result['summary']}**")
    lines.append("")
    lines.append("| Gate | Required | Passed | Detail |")
    lines.append("|------|----------|--------|--------|")
    for g in gate_result.get("gates", []):
        icon = "✅" if g["passed"] else "❌"
        req = "yes" if g["required"] else "no"
        lines.append(f"| {g['gate']} | {req} | {icon} | {g['reason']} |")
    lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append(f"**{recommendation['recommendation']}**")
    lines.append("")
    lines.append(f"_{recommendation['action_required']}_")
    lines.append("")

    return "\n".join(lines)


def _add_comparison_table(lines, comparison, baseline_card, variant_card):
    bm = baseline_card.get("metrics", {})
    vm = variant_card.get("metrics", {})
    lines.append("| Metric | Baseline | Variant | Delta |")
    lines.append("|--------|----------|---------|-------|")
    for key in ("total_trades", "net_pnl", "win_rate", "profit_factor",
                "expectancy", "avg_r", "max_drawdown", "avg_hold_time"):
        d = comparison.get("deltas", {}).get(key, {})
        b = d.get("baseline", bm.get(key, 0))
        v = d.get("variant", vm.get(key, 0))
        delta = d.get("delta", 0)
        lines.append(f"| {key} | {_fmt(b)} | {_fmt(v)} | {_fmt_delta(delta)} |")


def _add_metrics_table(lines, baseline_card, variant_card):
    bm = baseline_card.get("metrics", {})
    vm = variant_card.get("metrics", {})
    lines.append("| Metric | Baseline | Variant |")
    lines.append("|--------|----------|---------|")
    for key in ("total_trades", "net_pnl", "win_rate", "profit_factor",
                "expectancy", "avg_r", "max_drawdown"):
        b = bm.get(key, 0)
        v = vm.get(key, 0)
        lines.append(f"| {key} | {_fmt(b)} | {_fmt(v)} |")


def _fmt(val):
    if val is None:
        return "—"
    if isinstance(val, float):
        if val == float("inf"):
            return "∞"
        return f"{val:.4f}" if abs(val) < 1 else f"{val:.2f}"
    return str(val)


def _fmt_delta(val):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:+.4f}" if abs(val) < 1 else f"{val:+.2f}"
    return f"{val:+}"


# ── Main pipeline ──

def evaluate_experiment(
    experiment_config_path: Optional[Path] = None,
    experiment_cfg: Optional[Dict] = None,
    skip_backtest: bool = False,
    baseline_trades: Optional[list] = None,
    variant_trades: Optional[list] = None,
    baseline_equity: Optional[list] = None,
    variant_equity: Optional[list] = None,
) -> Dict:
    """
    Full evaluation pipeline for a single experiment.

    1. Load experiment config
    2. Build baseline & variant strategies
    3. Run backtests (or use provided trades)
    4. Split IS/OOS
    5. Build scorecards + comparisons
    6. Evaluate promotion gates
    7. Generate recommendation
    8. Write JSON + Markdown outputs
    9. Update experiment registry

    Returns the full result dict.
    """
    # Load experiment config
    if experiment_cfg is None:
        if experiment_config_path is None:
            raise ValueError("Must provide experiment_config_path or experiment_cfg")
        experiment_cfg = load_experiment_config(experiment_config_path)

    experiment_id = experiment_cfg["experiment_id"]
    overrides = experiment_cfg.get("variant_overrides", {})
    bt_cfg = experiment_cfg.get("backtest", {})
    start_date = bt_cfg.get("start_date", "2024-06-01")
    end_date = bt_cfg.get("end_date", "2026-05-01")
    initial_equity = bt_cfg.get("initial_equity", 20000)

    # Build strategies
    baseline = load_baseline_strategy()
    variant = build_variant(overrides, baseline)
    parameter_diffs = diff_strategies(baseline, variant, overrides)

    print(f"\n{'='*60}")
    print(f"EXPERIMENT EVALUATION: {experiment_id}")
    print(f"{'='*60}")
    print(f"Title: {experiment_cfg.get('title', '?')}")
    print(f"Overrides: {overrides}")
    print(f"Period: {start_date} → {end_date}")
    print(f"{'='*60}\n")

    # Run backtests
    if not skip_backtest:
        print("▶ Running BASELINE backtest...")
        _, baseline_trades, baseline_equity = _run_backtest_with_strategy(
            baseline, start_date, end_date, initial_equity)

        print("\n▶ Running VARIANT backtest...")
        _, variant_trades, variant_equity = _run_backtest_with_strategy(
            variant, start_date, end_date, initial_equity)

    if baseline_trades is None or variant_trades is None:
        raise ValueError("No trade data available. Run backtests or provide trade lists.")

    # Load promotion rules
    promotion_rules = load_promotion_rules()
    oos_fraction = promotion_rules.get("is_oos_split", {}).get("oos_fraction", 0.30)

    # Split IS/OOS
    b_split = split_trades_is_oos(baseline_trades, baseline_equity, oos_fraction)
    v_split = split_trades_is_oos(variant_trades, variant_equity, oos_fraction)

    # Build scorecards
    baseline_card = build_scorecard(baseline_trades, baseline_equity, "baseline")
    variant_card = build_scorecard(variant_trades, variant_equity, "variant")
    baseline_is_card = build_scorecard(b_split["is_trades"], b_split["is_equity"], "baseline_is")
    baseline_oos_card = build_scorecard(b_split["oos_trades"], b_split["oos_equity"], "baseline_oos")
    variant_is_card = build_scorecard(v_split["is_trades"], v_split["is_equity"], "variant_is")
    variant_oos_card = build_scorecard(v_split["oos_trades"], v_split["oos_equity"], "variant_oos")

    # Compare
    comparison = compare_scorecards(baseline_card, variant_card)
    oos_comparison = compare_scorecards(baseline_oos_card, variant_oos_card)

    # Evaluate gates
    gate_result = evaluate_all_gates(
        promotion_rules,
        baseline_metrics=baseline_card["metrics"],
        variant_metrics=variant_card["metrics"],
        oos_metrics=variant_oos_card["metrics"],
        paper_metrics=None,  # No paper data yet
    )

    # Generate recommendation
    recommendation = generate_promotion_recommendation(
        experiment_id, comparison, gate_result, parameter_diffs)

    # Generate markdown report
    report_md = _generate_markdown_report(
        experiment_id, experiment_cfg,
        baseline_card, variant_card,
        baseline_is_card, baseline_oos_card,
        variant_is_card, variant_oos_card,
        comparison, oos_comparison,
        gate_result, recommendation, parameter_diffs,
    )

    # Full result
    result = {
        "experiment_id": experiment_id,
        "evaluated_at": now_iso(),
        "config": experiment_cfg,
        "parameter_diffs": parameter_diffs,
        "baseline_scorecard": baseline_card,
        "variant_scorecard": variant_card,
        "baseline_is_scorecard": baseline_is_card,
        "baseline_oos_scorecard": baseline_oos_card,
        "variant_is_scorecard": variant_is_card,
        "variant_oos_scorecard": variant_oos_card,
        "comparison": comparison,
        "oos_comparison": oos_comparison,
        "gate_result": gate_result,
        "recommendation": recommendation,
    }

    # Save outputs
    output_dir = STATE_SHARED / "experiments"
    output_dir.mkdir(parents=True, exist_ok=True)

    save_json(output_dir / f"{experiment_id}_result.json", result)
    (output_dir / f"{experiment_id}_report.md").write_text(report_md, encoding="utf-8")

    # Update experiment registry
    exp_in_registry = get_experiment(experiment_id)
    if exp_in_registry:
        update_experiment(experiment_id,
                          scorecard=variant_card,
                          result=recommendation,
                          promotion_decision=recommendation["recommendation"])
        if exp_in_registry.get("status") == "proposed":
            transition_experiment(experiment_id, "active_backtest")

    # Print summary
    print(f"\n{'='*60}")
    print(f"EXPERIMENT RESULT: {experiment_id}")
    print(f"{'='*60}")
    print(f"Baseline: {baseline_card['metrics']['total_trades']} trades, "
          f"PF={baseline_card['metrics'].get('profit_factor', 0)}, "
          f"Avg R={baseline_card['metrics'].get('avg_r', 0)}")
    print(f"Variant:  {variant_card['metrics']['total_trades']} trades, "
          f"PF={variant_card['metrics'].get('profit_factor', 0)}, "
          f"Avg R={variant_card['metrics'].get('avg_r', 0)}")
    print(f"\nGates: {gate_result['summary']}")
    print(f"Recommendation: {recommendation['recommendation']}")
    print(f"\nOutputs:")
    print(f"  JSON: {output_dir / f'{experiment_id}_result.json'}")
    print(f"  Report: {output_dir / f'{experiment_id}_report.md'}")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) < 2:
        print("Usage: python evaluate_experiment.py <experiment_config.json>")
        print("  Or:  python evaluate_experiment.py config/experiments/exp_wider_pullback_001.json")
        _sys.exit(1)

    config_path = Path(_sys.argv[1])
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    if not config_path.exists():
        print(f"Experiment config not found: {config_path}")
        _sys.exit(1)

    evaluate_experiment(experiment_config_path=config_path)

