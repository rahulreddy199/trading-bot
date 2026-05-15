"""
Phase 2 Tests — experiment loop, scorecards, promotion gates, variants.
"""
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from analytics.variants import (
    apply_overrides, build_variant, diff_strategies,
    get_override_value, load_baseline_strategy,
)
from analytics.scorecards import (
    build_scorecard, compare_scorecards, split_trades_is_oos,
)
from analytics.promotion import (
    evaluate_gate, evaluate_all_gates, generate_promotion_recommendation,
    load_promotion_rules,
)
from analytics.experiments import (
    VALID_TRANSITIONS, propose_experiment, list_experiments,
    load_experiments, save_experiments, transition_experiment,
    get_experiment, update_experiment, EXPERIMENTS_PATH,
)

# ── Test data ──

WINNING_TRADES = [
    {"symbol": "NVDA", "pnl": 200, "r_multiple": 2.5, "bars_held": 6,
     "setup_type": "breakout", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
    {"symbol": "META", "pnl": 150, "r_multiple": 1.8, "bars_held": 8,
     "setup_type": "breakout", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
    {"symbol": "AMD", "pnl": 100, "r_multiple": 1.2, "bars_held": 5,
     "setup_type": "continuation", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
]

LOSING_TRADES = [
    {"symbol": "TSLA", "pnl": -80, "r_multiple": -1.0, "bars_held": 10,
     "setup_type": "shallow_pullback", "regime_at_entry": "correction", "exit_reason": "time_stop"},
    {"symbol": "COIN", "pnl": -60, "r_multiple": -0.8, "bars_held": 3,
     "setup_type": "breakout", "regime_at_entry": "bull", "exit_reason": "stop_loss"},
]

SAMPLE_BASELINE_TRADES = WINNING_TRADES + LOSING_TRADES

SAMPLE_VARIANT_TRADES = WINNING_TRADES + LOSING_TRADES + [
    {"symbol": "SHOP", "pnl": 120, "r_multiple": 1.5, "bars_held": 7,
     "setup_type": "shallow_pullback", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
    {"symbol": "MRVL", "pnl": -40, "r_multiple": -0.5, "bars_held": 4,
     "setup_type": "shallow_pullback", "regime_at_entry": "correction", "exit_reason": "stop_loss"},
]

# Variant that looks good in-sample but bad out-of-sample
IS_GOOD_TRADES = [
    {"symbol": "A", "pnl": 100, "r_multiple": 2.0, "bars_held": 5,
     "setup_type": "breakout", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
    {"symbol": "B", "pnl": 80, "r_multiple": 1.5, "bars_held": 4,
     "setup_type": "breakout", "regime_at_entry": "bull", "exit_reason": "trailing_stop"},
    {"symbol": "C", "pnl": -30, "r_multiple": -0.5, "bars_held": 3,
     "setup_type": "continuation", "regime_at_entry": "bull", "exit_reason": "stop_loss"},
]
OOS_BAD_TRADES = [
    {"symbol": "D", "pnl": -90, "r_multiple": -1.0, "bars_held": 10,
     "setup_type": "breakout", "regime_at_entry": "correction", "exit_reason": "time_stop"},
    {"symbol": "E", "pnl": -70, "r_multiple": -0.8, "bars_held": 8,
     "setup_type": "breakout", "regime_at_entry": "correction", "exit_reason": "stop_loss"},
    {"symbol": "F", "pnl": 20, "r_multiple": 0.3, "bars_held": 6,
     "setup_type": "continuation", "regime_at_entry": "correction", "exit_reason": "trailing_stop"},
]


# ── Variant tests ──

def test_apply_overrides():
    strategy = {"exit": {"trailing_atr_multiplier": 3.0}, "setups": {"shallow_pullback": {"max_depth_atr": 1.5}}}
    result = apply_overrides(strategy, {"exit.trailing_atr_multiplier": 2.5, "setups.shallow_pullback.max_depth_atr": 2.0})
    assert result["exit"]["trailing_atr_multiplier"] == 2.5, f"Got {result['exit']['trailing_atr_multiplier']}"
    assert result["setups"]["shallow_pullback"]["max_depth_atr"] == 2.0
    # Original unchanged
    assert strategy["exit"]["trailing_atr_multiplier"] == 3.0


def test_apply_overrides_creates_missing_keys():
    strategy = {"a": 1}
    result = apply_overrides(strategy, {"b.c.d": 42})
    assert result["b"]["c"]["d"] == 42
    assert result["a"] == 1


def test_get_override_value():
    strategy = {"exit": {"trailing_atr_multiplier": 3.0}}
    assert get_override_value(strategy, "exit.trailing_atr_multiplier") == 3.0
    assert get_override_value(strategy, "exit.nonexistent", 99) == 99


def test_diff_strategies():
    baseline = {"exit": {"trailing_atr_multiplier": 3.0}}
    variant = {"exit": {"trailing_atr_multiplier": 2.5}}
    diffs = diff_strategies(baseline, variant, {"exit.trailing_atr_multiplier": 2.5})
    assert len(diffs) == 1
    assert diffs[0]["baseline"] == 3.0
    assert diffs[0]["variant"] == 2.5


def test_build_variant_loads_baseline():
    # Should not crash — loads real strategy_growth.json
    variant = build_variant({"exit.trailing_atr_multiplier": 2.5})
    assert variant["exit"]["trailing_atr_multiplier"] == 2.5
    # Other fields preserved
    assert "regime" in variant
    assert "indicators" in variant


# ── Scorecard tests ──

def test_build_scorecard():
    card = build_scorecard(SAMPLE_BASELINE_TRADES, [20000, 20200, 20100, 20300, 20250], "baseline")
    assert card["label"] == "baseline"
    assert card["metrics"]["total_trades"] == 5
    assert "setup_summary" in card
    assert "regime_summary" in card


def test_build_scorecard_empty_trades():
    card = build_scorecard([], None, "empty")
    assert card["metrics"]["total_trades"] == 0
    assert card["metrics"]["win_rate"] == 0


def test_compare_scorecards():
    b_card = build_scorecard(SAMPLE_BASELINE_TRADES, None, "baseline")
    v_card = build_scorecard(SAMPLE_VARIANT_TRADES, None, "variant")
    cmp = compare_scorecards(b_card, v_card)
    assert "deltas" in cmp
    assert "verdicts" in cmp
    # Variant has more trades
    assert cmp["deltas"]["total_trades"]["delta"] > 0


def test_split_trades_is_oos():
    trades = SAMPLE_VARIANT_TRADES  # 7 trades
    split = split_trades_is_oos(trades, [100, 101, 102, 103, 104, 105, 106], 0.30)
    assert split["total_trades"] == 7
    assert len(split["is_trades"]) + len(split["oos_trades"]) == 7
    assert len(split["oos_trades"]) >= 1  # at least 1 OOS trade


def test_split_empty():
    split = split_trades_is_oos([], None, 0.30)
    assert split["is_trades"] == []
    assert split["oos_trades"] == []


# ── Promotion gate tests ──

def test_gate_min_trade_count_pass():
    passed, reason = evaluate_gate("min_trade_count", {"value": 5}, {}, {"total_trades": 10})
    assert passed


def test_gate_min_trade_count_fail():
    passed, reason = evaluate_gate("min_trade_count", {"value": 15}, {}, {"total_trades": 5})
    assert not passed


def test_gate_profit_factor_floor():
    passed, _ = evaluate_gate("profit_factor_floor", {"value": 1.0}, {}, {"profit_factor": 1.5})
    assert passed
    passed, _ = evaluate_gate("profit_factor_floor", {"value": 1.0}, {}, {"profit_factor": 0.8})
    assert not passed


def test_gate_expectancy_improvement():
    passed, _ = evaluate_gate("expectancy_improvement_min", {"value": 5.0},
                              {"expectancy": 50}, {"expectancy": 60})
    assert passed
    passed, _ = evaluate_gate("expectancy_improvement_min", {"value": 5.0},
                              {"expectancy": 50}, {"expectancy": 52})
    assert not passed


def test_gate_drawdown_limit():
    passed, _ = evaluate_gate("max_drawdown_increase_limit", {"value": 0.03},
                              {"max_drawdown": 0.05}, {"max_drawdown": 0.07})
    assert passed  # increase of 0.02 <= 0.03
    passed, _ = evaluate_gate("max_drawdown_increase_limit", {"value": 0.03},
                              {"max_drawdown": 0.05}, {"max_drawdown": 0.09})
    assert not passed  # increase of 0.04 > 0.03


def test_gate_paper_required_missing():
    passed, _ = evaluate_gate("paper_incubation_required", {"value": True}, {}, {}, paper_metrics=None)
    assert not passed


def test_gate_paper_required_present():
    passed, _ = evaluate_gate("paper_incubation_required", {"value": True}, {}, {},
                              paper_metrics={"total_trades": 5})
    assert passed


def test_gate_oos_profit_factor():
    passed, _ = evaluate_gate("oos_profit_factor_floor", {"value": 0.8}, {}, {},
                              oos_metrics={"profit_factor": 1.2})
    assert passed
    passed, _ = evaluate_gate("oos_profit_factor_floor", {"value": 0.8}, {}, {},
                              oos_metrics={"profit_factor": 0.5})
    assert not passed


def test_evaluate_all_gates_pass():
    rules = {
        "gates": {
            "min_trade_count": {"value": 5, "required": True},
            "profit_factor_floor": {"value": 1.0, "required": True},
            "win_rate_floor": {"value": 0.3, "required": True},
        }
    }
    result = evaluate_all_gates(rules,
                                baseline_metrics={"expectancy": 50},
                                variant_metrics={"total_trades": 20, "profit_factor": 1.5, "win_rate": 0.6, "expectancy": 55})
    assert result["passed"]


def test_evaluate_all_gates_fail():
    rules = {
        "gates": {
            "min_trade_count": {"value": 30, "required": True},
            "profit_factor_floor": {"value": 1.0, "required": True},
        }
    }
    result = evaluate_all_gates(rules,
                                baseline_metrics={},
                                variant_metrics={"total_trades": 10, "profit_factor": 0.5})
    assert not result["passed"]
    assert len(result["failed_required"]) == 2


def test_promotion_recommendation():
    comparison = {"deltas": {}, "verdicts": ["✅ More trades"]}
    gate_result = {"passed": True, "summary": "All gates passed", "gates": []}
    diffs = [{"param": "exit.trailing_atr_multiplier", "baseline": 3.0, "variant": 2.5}]
    rec = generate_promotion_recommendation("test_001", comparison, gate_result, diffs)
    assert rec["recommendation"] == "PROMOTE"
    assert rec["gates_passed"]


def test_promotion_recommendation_reject():
    gate_result = {"passed": False, "summary": "Failed gates", "gates": []}
    rec = generate_promotion_recommendation("test_002", {}, gate_result, [])
    assert rec["recommendation"] == "REJECT"


# ── Experiment registry tests ──

def test_experiment_lifecycle():
    """Test full lifecycle: propose → active_backtest → active_paper → promoted."""
    from common import save_json
    backup = None
    if EXPERIMENTS_PATH.exists():
        backup = EXPERIMENTS_PATH.read_text()

    try:
        save_json(EXPERIMENTS_PATH, {"experiments": [], "last_updated": None})

        # Propose
        ok = propose_experiment("lifecycle_test", title="Test lifecycle",
                                hypothesis="Testing transitions",
                                variant_overrides={"exit.trailing_atr_multiplier": 2.5})
        assert ok

        exp = get_experiment("lifecycle_test")
        assert exp is not None
        assert exp["status"] == "proposed"

        # Activate backtest
        assert transition_experiment("lifecycle_test", "active_backtest")
        assert get_experiment("lifecycle_test")["status"] == "active_backtest"

        # Move to paper
        assert transition_experiment("lifecycle_test", "active_paper")
        assert get_experiment("lifecycle_test")["status"] == "active_paper"

        # Promote
        assert transition_experiment("lifecycle_test", "promoted")
        assert get_experiment("lifecycle_test")["status"] == "promoted"

        # Rollback
        assert transition_experiment("lifecycle_test", "rolled_back")
        assert get_experiment("lifecycle_test")["status"] == "rolled_back"

    finally:
        if backup:
            EXPERIMENTS_PATH.write_text(backup)
        elif EXPERIMENTS_PATH.exists():
            EXPERIMENTS_PATH.unlink()


def test_invalid_transition():
    from common import save_json
    backup = None
    if EXPERIMENTS_PATH.exists():
        backup = EXPERIMENTS_PATH.read_text()

    try:
        save_json(EXPERIMENTS_PATH, {"experiments": [], "last_updated": None})
        propose_experiment("invalid_trans", title="Test", hypothesis="Test",
                           variant_overrides={})

        # Can't go directly from proposed to promoted
        assert not transition_experiment("invalid_trans", "promoted")
        assert get_experiment("invalid_trans")["status"] == "proposed"

        # Can't go to invalid status
        assert not transition_experiment("invalid_trans", "flying")

    finally:
        if backup:
            EXPERIMENTS_PATH.write_text(backup)
        elif EXPERIMENTS_PATH.exists():
            EXPERIMENTS_PATH.unlink()


def test_duplicate_experiment():
    from common import save_json
    backup = None
    if EXPERIMENTS_PATH.exists():
        backup = EXPERIMENTS_PATH.read_text()

    try:
        save_json(EXPERIMENTS_PATH, {"experiments": [], "last_updated": None})
        assert propose_experiment("dup_test", title="First", hypothesis="First",
                                  variant_overrides={})
        assert not propose_experiment("dup_test", title="Second", hypothesis="Second",
                                     variant_overrides={})
        assert len(list_experiments()) == 1

    finally:
        if backup:
            EXPERIMENTS_PATH.write_text(backup)
        elif EXPERIMENTS_PATH.exists():
            EXPERIMENTS_PATH.unlink()


def test_update_experiment():
    from common import save_json
    backup = None
    if EXPERIMENTS_PATH.exists():
        backup = EXPERIMENTS_PATH.read_text()

    try:
        save_json(EXPERIMENTS_PATH, {"experiments": [], "last_updated": None})
        propose_experiment("update_test", title="Test", hypothesis="Test",
                           variant_overrides={})
        update_experiment("update_test", notes="updated note", paper_trades=15)
        exp = get_experiment("update_test")
        assert exp["notes"] == "updated note"
        assert exp["paper_trades"] == 15

    finally:
        if backup:
            EXPERIMENTS_PATH.write_text(backup)
        elif EXPERIMENTS_PATH.exists():
            EXPERIMENTS_PATH.unlink()


# ── Regression: IS-good but OOS-bad ──

def test_is_good_oos_bad_regression():
    """A variant that looks good in-sample but fails out-of-sample should not pass OOS gates."""
    all_trades = IS_GOOD_TRADES + OOS_BAD_TRADES
    # Simulate IS/OOS split
    is_card = build_scorecard(IS_GOOD_TRADES, None, "variant_is")
    oos_card = build_scorecard(OOS_BAD_TRADES, None, "variant_oos")
    full_card = build_scorecard(all_trades, None, "variant")

    # IS looks great
    assert is_card["metrics"]["win_rate"] > 0.5
    assert is_card["metrics"]["profit_factor"] > 1.0

    # OOS is bad
    assert oos_card["metrics"]["profit_factor"] < 1.0

    # Gate should catch it
    rules = {
        "gates": {
            "oos_profit_factor_floor": {"value": 0.8, "required": True},
        }
    }
    result = evaluate_all_gates(rules,
                                baseline_metrics={},
                                variant_metrics=full_card["metrics"],
                                oos_metrics=oos_card["metrics"])
    assert not result["passed"], "Variant with bad OOS should fail promotion"


def test_insufficient_data_gates():
    """With very few trades, min trade count gates should fail."""
    few_trades = SAMPLE_BASELINE_TRADES[:2]
    card = build_scorecard(few_trades, None, "variant")
    rules = load_promotion_rules()
    result = evaluate_all_gates(rules,
                                baseline_metrics=card["metrics"],
                                variant_metrics=card["metrics"],
                                oos_metrics={"total_trades": 1, "profit_factor": 0})
    assert not result["passed"], "Too few trades should fail promotion"


# ── Scorecard with attribution ──

def test_scorecard_setup_attribution():
    card = build_scorecard(SAMPLE_VARIANT_TRADES, None, "variant")
    setup = card["setup_summary"]
    assert "breakout" in setup
    assert "shallow_pullback" in setup
    assert setup["breakout"]["trades"] >= 1
    assert setup["shallow_pullback"]["trades"] >= 1


def test_scorecard_regime_attribution():
    card = build_scorecard(SAMPLE_VARIANT_TRADES, None, "variant")
    regime = card["regime_summary"]
    assert "bull" in regime
    assert regime["bull"]["trades"] >= 1




