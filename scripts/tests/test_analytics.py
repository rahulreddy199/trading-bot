"""
Phase 1 Analytics Tests — pure metric/attribution/decision tests.
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from analytics.metrics import (
    win_rate, profit_factor, expectancy, avg_r,
    net_pnl, max_drawdown, avg_hold_time, compute_all_metrics,
)
from analytics.attribution import attribution_by, full_attribution, top_contributors, group_trades
from analytics.experiments import propose_experiment, list_experiments, load_experiments
from analytics.ai_review import generate_recommendations


# Sample trades for testing
SAMPLE_TRADES = [
    {"symbol": "NVDA", "pnl": 150.0, "r_multiple": 2.1, "bars_held": 5,
     "setup_type": "breakout", "sector": "Technology", "bot": "growth",
     "regime_at_entry": "bull", "exit_reason": "trailing_stop", "entry_day_of_week": "Mon"},
    {"symbol": "AMD", "pnl": -80.0, "r_multiple": -1.0, "bars_held": 10,
     "setup_type": "continuation", "sector": "Technology", "bot": "growth",
     "regime_at_entry": "bull", "exit_reason": "time_stop", "entry_day_of_week": "Tue"},
    {"symbol": "META", "pnl": 200.0, "r_multiple": 3.0, "bars_held": 8,
     "setup_type": "breakout", "sector": "Technology", "bot": "growth",
     "regime_at_entry": "bull", "exit_reason": "trailing_stop", "entry_day_of_week": "Wed"},
    {"symbol": "TSLA", "pnl": -50.0, "r_multiple": -0.5, "bars_held": 3,
     "setup_type": "shallow_pullback", "sector": "Consumer", "bot": "growth",
     "regime_at_entry": "correction", "exit_reason": "stop_loss", "entry_day_of_week": "Thu"},
    {"symbol": "AAPL", "pnl": 75.0, "r_multiple": 1.2, "bars_held": 7,
     "setup_type": "breakout", "sector": "Technology", "bot": "conservative",
     "regime_at_entry": "bull", "exit_reason": "trailing_stop", "entry_day_of_week": "Fri"},
]


class TestResult:
    def __init__(self, name):
        self.name = name
        self.passed = False
        self.message = ""

    def ok(self, msg=""):
        self.passed = True
        self.message = msg
        return self

    def fail(self, msg):
        self.passed = False
        self.message = msg
        return self


def test_win_rate():
    result = TestResult("win_rate")
    wr = win_rate(SAMPLE_TRADES)
    # 3 winners out of 5
    if abs(wr - 0.6) < 0.01:
        return result.ok(f"win_rate={wr}")
    return result.fail(f"Expected 0.6, got {wr}")


def test_profit_factor():
    result = TestResult("profit_factor")
    pf = profit_factor(SAMPLE_TRADES)
    # gross_profit=425, gross_loss=130 → 3.27
    if abs(pf - 3.27) < 0.1:
        return result.ok(f"profit_factor={pf}")
    return result.fail(f"Expected ~3.27, got {pf}")


def test_expectancy():
    result = TestResult("expectancy")
    exp = expectancy(SAMPLE_TRADES)
    # (150-80+200-50+75)/5 = 59
    if abs(exp - 59.0) < 0.1:
        return result.ok(f"expectancy={exp}")
    return result.fail(f"Expected 59.0, got {exp}")


def test_avg_r():
    result = TestResult("avg_r")
    ar = avg_r(SAMPLE_TRADES)
    # (2.1 - 1.0 + 3.0 - 0.5 + 1.2) / 5 = 0.96
    if abs(ar - 0.96) < 0.01:
        return result.ok(f"avg_r={ar}")
    return result.fail(f"Expected 0.96, got {ar}")


def test_net_pnl():
    result = TestResult("net_pnl")
    pnl = net_pnl(SAMPLE_TRADES)
    if abs(pnl - 295.0) < 0.01:
        return result.ok(f"net_pnl={pnl}")
    return result.fail(f"Expected 295.0, got {pnl}")


def test_max_drawdown():
    result = TestResult("max_drawdown")
    curve = [100, 105, 110, 95, 98, 90, 100, 108]
    dd = max_drawdown(curve)
    # Peak 110, trough 90 → dd = 20/110 = 0.1818
    if abs(dd - 0.1818) < 0.001:
        return result.ok(f"max_dd={dd}")
    return result.fail(f"Expected ~0.1818, got {dd}")


def test_avg_hold_time():
    result = TestResult("avg_hold_time")
    aht = avg_hold_time(SAMPLE_TRADES)
    # (5+10+8+3+7)/5 = 6.6
    if abs(aht - 6.6) < 0.1:
        return result.ok(f"avg_hold={aht}")
    return result.fail(f"Expected 6.6, got {aht}")


def test_attribution_by_setup():
    result = TestResult("attribution_by_setup")
    attr = attribution_by(SAMPLE_TRADES, "setup_type")
    if "breakout" in attr and attr["breakout"]["total_trades"] == 3:
        return result.ok(f"breakout trades={attr['breakout']['total_trades']}")
    return result.fail(f"Got: {attr.get('breakout')}")


def test_attribution_by_bot():
    result = TestResult("attribution_by_bot")
    attr = attribution_by(SAMPLE_TRADES, "bot")
    if "growth" in attr and attr["growth"]["total_trades"] == 4:
        return result.ok(f"growth trades={attr['growth']['total_trades']}")
    return result.fail(f"Got: {attr}")


def test_top_contributors():
    result = TestResult("top_contributors")
    top = top_contributors(SAMPLE_TRADES, n=3)
    best = top["best"]
    if best[0][0] == "META" and best[0][1] == 200.0:
        return result.ok(f"top={best[0]}")
    return result.fail(f"Expected META first, got {best}")


def test_full_attribution():
    result = TestResult("full_attribution")
    attr = full_attribution(SAMPLE_TRADES)
    dims = ["bot", "setup_type", "symbol", "sector", "regime", "exit_reason", "holding_bucket", "day_of_week"]
    missing = [d for d in dims if d not in attr]
    if not missing:
        return result.ok(f"All {len(dims)} dimensions present")
    return result.fail(f"Missing dimensions: {missing}")


def test_compute_all_metrics():
    result = TestResult("compute_all_metrics")
    m = compute_all_metrics(SAMPLE_TRADES, [100, 110, 95, 108])
    required = ["total_trades", "net_pnl", "win_rate", "profit_factor",
                "expectancy", "avg_r", "avg_hold_time", "max_drawdown"]
    missing = [k for k in required if k not in m]
    if not missing and m["total_trades"] == 5:
        return result.ok(f"All fields present, trades={m['total_trades']}")
    return result.fail(f"Missing: {missing}")


def test_ai_review_low_data():
    result = TestResult("ai_review_low_data")
    analytics = {
        "metrics_all_time": {"total_trades": 5, "win_rate": 0.6, "avg_slippage_bps": 10},
        "metrics_7d": {"total_trades": 2, "win_rate": 0.5},
        "incidents": {"manual_review": 0, "errors": 0},
    }
    output = generate_recommendations(analytics=analytics, attribution={}, experiments={})
    recs = output["recommendations"]
    has_data_warning = any("accumulate data" in r["recommendation"] for r in recs)
    if has_data_warning:
        return result.ok("Correctly warns about insufficient data")
    return result.fail(f"No data warning in: {[r['recommendation'] for r in recs]}")


def test_experiment_registry():
    result = TestResult("experiment_registry")
    # Clean state
    from common import STATE_SHARED
    exp_path = STATE_SHARED / "experiments.json"
    if exp_path.exists():
        import json
        backup = exp_path.read_text()
    else:
        backup = None

    try:
        # Write clean
        from common import save_json
        save_json(exp_path, {"experiments": [], "last_updated": None})

        ok = propose_experiment(
            "test_exp_001", "growth",
            "Tighter trail at 3R improves avg exit R",
            "avg_r", ["win_rate", "profit_factor"],
            evaluation_window="20 trades"
        )
        exps = list_experiments()
        if ok and len(exps) == 1 and exps[0]["status"] == "proposed":
            return result.ok("Experiment proposed and retrievable")
        return result.fail(f"Unexpected: ok={ok}, exps={exps}")
    finally:
        # Restore
        if backup:
            exp_path.write_text(backup)
        elif exp_path.exists():
            exp_path.unlink()


ALL_TESTS = [
    test_win_rate,
    test_profit_factor,
    test_expectancy,
    test_avg_r,
    test_net_pnl,
    test_max_drawdown,
    test_avg_hold_time,
    test_attribution_by_setup,
    test_attribution_by_bot,
    test_top_contributors,
    test_full_attribution,
    test_compute_all_metrics,
    test_ai_review_low_data,
    test_experiment_registry,
]


def run_all():
    print(f"\n{'='*50}")
    print("PHASE 1 ANALYTICS TESTS")
    print(f"{'='*50}\n")

    passed = 0
    failed = 0
    for test_fn in ALL_TESTS:
        try:
            r = test_fn()
        except Exception as e:
            r = TestResult(test_fn.__name__)
            r.fail(f"Exception: {e}")
        icon = "✅" if r.passed else "❌"
        print(f"  {icon} {r.name}: {r.message}")
        if r.passed:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    print(f"{'='*50}\n")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)

