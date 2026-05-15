# ROADMAP

This document records the planned evolution of the trading bot from a stable rules-based system into a more measurable, safer, and eventually semi-adaptive trading platform.

The roadmap is intentionally phased. The goal is to avoid premature complexity, preserve capital, and only increase automation after the system has demonstrated robustness in backtests, paper trading, and operational monitoring.

## Guiding principles

- Paper trading first.
- Measure before optimizing.
- One variable family at a time.
- No automatic strategy changes without validation.
- Operational safety is as important as returns.
- Prefer bounded automation over open-ended self-modification.

## Current baseline

The current system is a rules-based swing trading bot built around daily bars, pullback entries in confirmed uptrends, fixed-risk position sizing, bracket orders, and managed exits with partial profits plus trailing stops. It is designed for paper trading first and includes explicit live-trading guardrails. [README]

Core baseline characteristics:
- Long-only swing trading on daily candles.
- Regime filter based on SPY and QQQ above 50-day SMA.
- Trend filter requiring price > 50 SMA > 200 SMA.
- Relative-strength ranking versus SPY.
- Fixed account risk per trade.
- Bracket-order entry with stop-loss and take-profit.
- Partial exit at 2R, then trailing stop management.
- State files and markdown journaling.
- Paper trading default, live trading blocked unless explicitly enabled.

## Phase 0 — Baseline bot

### Goal
Establish a stable, understandable, rules-based trading engine with explicit safety defaults.

### Scope
- Research pipeline to scan symbols and compute candidates.
- Trade pipeline to place bracket orders.
- Position manager for partial exits and trailing stops.
- Journal output for daily review.
- Runtime state tracking for positions and order plans.
- Live-trading protection via explicit environment guardrails.

### Status
Complete.

### Exit criteria
- End-to-end paper trading workflow runs reliably.
- Entry, exit, and management logic are deterministic.
- Failures are logged and recoverable.
- Manual review is possible from journal and state outputs.

## Phase 1 — Analytics and review

### Goal
Add a measurement layer that explains what the strategy is doing and where it is failing, without changing live behavior automatically.

### Scope
- Post-trade metrics module.
- Attribution module across key dimensions.
- Regime tagging.
- Daily analytics pipeline.
- Daily and weekly markdown reports.
- Experiment registry.
- Recommendation-only AI review.

### Planned module layout
```text
scripts/analytics/
├── __init__.py
├── metrics.py
├── attribution.py
├── regime.py
├── pipeline.py
├── reports.py
├── experiments.py
└── ai_review.py
```

### Target outputs
- Daily analytics JSON.
- Daily markdown review.
- Weekly markdown summary.
- Structured recommendations only.
- No automatic strategy parameter updates.

### Status
Implemented per project summary; GitHub verification still pending.

### Exit criteria
- Analytics pipeline runs daily from state and journal data.
- Reports identify performance trends and operational issues.
- AI review remains recommendation-only.
- Metrics are stable and covered by tests.

## Phase 2 — Controlled experiment loop

### Goal
Turn analytics into disciplined strategy improvement through controlled experiments, not ad hoc tuning.

### Why this phase matters
Backtests alone are not enough. Out-of-sample testing and forward testing are the first defenses against curve fitting and over-optimization. Paper trading should remain the bridge between research and live promotion. [External research references]

### Scope
- Experiment scorecards for every proposed strategy change.
- Promotion criteria encoded in config or code.
- In-sample and out-of-sample comparisons.
- Forward-testing workflow in paper trading.
- Explicit rollback to baseline when a variant underperforms.
- Small, focused experiments instead of broad refactors.

### Suggested experiment targets
1. Trade frequency vs setup quality.
2. Pullback strategy vs growth/momentum behavior.
3. Regime-aware filtering thresholds.
4. Correlation cap tuning.
5. Risk-per-trade and max-open-position limits.
6. Exit logic variations such as trailing distance and partial-exit fraction.
7. Slippage-sensitive filters.
8. Watchlist expansion effects on opportunity set and quality.

### Deliverables
- `experiments/` or equivalent registry/state store for variants.
- `promotion_criteria.json` or equivalent config.
- `compare_variants.py` or equivalent report generator.
- Paper-trade validation report for every experiment.
- Baseline-vs-challenger summaries in daily/weekly reporting.

### Promotion criteria
A variant should not become active unless it:
- Has enough trades to be statistically meaningful.
- Improves or at least preserves expectancy.
- Does not materially worsen max drawdown.
- Does not create operational instability.
- Holds up in out-of-sample testing.
- Survives a paper-trading incubation period.

### Status
**Implemented.** Core modules complete:
- `scripts/analytics/variants.py` — Apply parameter overrides to baseline strategy
- `scripts/analytics/scorecards.py` — Build and compare metric scorecards (IS/OOS)
- `scripts/analytics/promotion.py` — Evaluate explicit promotion gates
- `scripts/analytics/experiments.py` — Enhanced registry with full lifecycle
- `scripts/analytics/evaluate_experiment.py` — End-to-end evaluation pipeline
- `config/promotion_rules.json` — Configurable promotion criteria
- `config/experiments/*.json` — Experiment definitions
- `scripts/tests/test_phase2.py` — Comprehensive test suite

### How to use
```bash
# 1. Define experiment in config/experiments/my_experiment.json
# 2. Run evaluation:
venv/bin/python scripts/analytics/evaluate_experiment.py config/experiments/my_experiment.json
# 3. Review outputs in state/shared/experiments/
# 4. Manually promote if gates pass and paper incubation confirms
```

### Exit criteria
- Every strategy change is tracked as an experiment.
- Promotion requires objective gates, not intuition alone.
- Paper-trading validation is mandatory before activation.
- Baseline strategy always remains available as fallback.

## Phase 3 — Production hardening

### Goal
Make the bot safer to operate under failure, volatility, and execution anomalies.

### Why this phase matters
A profitable strategy can still fail in production because of stale data, API errors, duplicate orders, slippage spikes, or missing recovery paths. Operational resilience must be treated as a first-class feature. [External research references]

### Scope
- Global kill switch.
- Automatic pause conditions.
- Better alerting and heartbeat monitoring.
- Order-state reconciliation.
- Data freshness checks.
- Broker/API failure handling.
- Safer rollout controls for live changes.
- Manual override and recovery playbooks.

### Required controls
- Pause new entries after daily loss threshold.
- Pause on abnormal drawdown relative to validated expectations.
- Pause on repeated broker/API execution failures.
- Pause on abnormal slippage or order rejection spikes.
- Ability to cancel all open orders quickly.
- Clear separation between strategy decisions and risk-stop controls.

### Deliverables
- `kill_switch` or equivalent global halt mechanism.
- Alerting hooks for failures and abnormal conditions.
- Health checks for data and broker connectivity.
- Recovery scripts or runbooks.
- Enhanced logging and audit trails for execution actions.

### Status
**Implemented.** Core modules complete:
- `scripts/controls/kill_switch.py` — Global kill switch with persistence, cooldown, manual reset
- `scripts/controls/pause_rules.py` — Automatic pause rules (daily loss, drawdown, errors, heartbeats)
- `scripts/controls/pretrade.py` — Pre-trade control gate (10 checks, structured pass/fail)
- `scripts/controls/reconcile.py` — Broker vs local state reconciliation with anomaly detection
- `scripts/controls/health.py` — Health monitoring, heartbeat checks, status reporting
- `scripts/controls/alerts.py` — Notification abstraction (log-only + webhook modes)
- `scripts/controls/audit.py` — Structured JSONL audit logging for all safety actions
- `config/risk_controls.json` — Kill switch, pause rules, pre-trade limits configuration
- `config/alerting.json` — Alert routing and event configuration
- `config/reconciliation.json` — Reconciliation checks and safe cleanup settings
- `scripts/control_state.py` — CLI: view state, activate kill, pause, health check
- `scripts/reset_controls.py` — CLI: safe manual reset with cooldown
- `scripts/tests/test_phase3.py` — 54 tests covering all control paths

### How to use
```bash
# View control state
python3 scripts/control_state.py status

# Activate kill switch
python3 scripts/control_state.py kill "Reason for halt"

# Activate manual pause
python3 scripts/control_state.py pause "Reason for pause"

# Run health check
python3 scripts/control_state.py health

# View audit log
python3 scripts/control_state.py audit

# Reset kill switch (with cooldown)
python3 scripts/reset_controls.py reset_kill

# Reset pause state
python3 scripts/reset_controls.py reset_pause

# Reset status overview
python3 scripts/reset_controls.py status

# Run tests
python3 -m pytest scripts/tests/test_phase3.py -v
```

### Exit criteria
- Bot can be paused quickly and safely. ✅
- Recovery from common failure modes is documented and testable. ✅
- Strategy cannot continue trading silently under degraded conditions. ✅
- Live operations are observable in near real time. ✅

## Phase 4 — Bounded self-improvement

### Goal
Allow limited, safe automation in strategy selection or parameter adjustment without turning the system into an unconstrained black box.

### What this phase is not
- Not autonomous strategy invention.
- Not unrestricted parameter optimization.
- Not direct AI control of live order execution logic.

### Scope
- Candidate ranking among approved strategy variants.
- Bounded parameter selection within safe ranges.
- Auto-promotion only when strict criteria are met.
- Automatic rollback when live behavior deviates materially from expected behavior.
- Human-readable rationale for every recommendation or switch.

### Examples of acceptable bounded automation
- Select among pre-approved trailing-stop multipliers.
- Choose between baseline and challenger variant after passing promotion gates.
- Reduce risk automatically when drawdown or slippage crosses thresholds.
- Disable specific setups that fall below minimum recent quality thresholds.

### Guardrails
- Only pre-approved parameters may change.
- All bounds must be explicit in config.
- Every change must be logged with timestamp and reason.
- Rollback path must always exist.
- Risk controls remain independent of strategy logic.

### Status
Future, after Phase 2 and Phase 3 are proven.

### Exit criteria
- Enough validated history exists across multiple market conditions.
- Promotion and rollback logic are trustworthy.
- Bounded automation improves outcomes without hiding decision logic.
- Human review remains possible and practical.

## Milestones

### Short-term
- Freeze and verify Phase 1 implementation.
- Standardize analytics outputs and report formats.
- Define experiment metadata schema.
- Encode promotion criteria.

### Medium-term
- Run 3 to 5 controlled experiments.
- Compare in-sample, out-of-sample, and paper-trading results.
- Add kill switch and pause controls.
- Improve monitoring and alerting.

### Longer-term
- Introduce bounded variant selection.
- Add automatic rollback on live underperformance.
- Expand watchlist and strategy family only after the experimentation loop is stable.

## Rules for future changes

Any future strategy or automation change should answer these questions before activation:

1. What problem is this change trying to solve?
2. Which metric or report identified the problem?
3. What is the baseline?
4. What is the candidate change?
5. How will success be measured?
6. What is the out-of-sample result?
7. What happened in paper trading?
8. What is the rollback condition?
9. What operational risks does the change add?
10. Can the system explain the change in plain English?

## Practical next step

The next milestone is **Phase 4**:
- Introduce bounded variant selection among pre-approved parameters,
- Add automatic rollback on live underperformance,
- Expand watchlist and strategy family only after Phases 2–3 are proven stable.

That keeps the project aligned with its current design: systematic, explainable, and safe-by-default.

Phases 2 and 3 are now complete. The system has a controlled experiment loop and production-grade safety controls. Phase 4 can begin once enough paper trading data validates the operational stability of the current infrastructure.
