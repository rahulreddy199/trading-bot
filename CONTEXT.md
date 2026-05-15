# Trading Bot - Project Context & Status

## Last Updated: May 14, 2026

## Current Status: Paper-Trading Active (Week 2) — Growth Bot Only | Phase 3 Production Hardening Implemented

## Quick Summary
- **Paper trading with Alpaca** since May 4, 2026 ($20K starting capital)
- **Growth bot is the sole production bot** — conservative bot archived to `scripts/legacy/`
- **3 trades entered**, 1 closed (MU: +$323.36, +2.79R trailing stop exit)
- **2 positions open**: AMD (continuation, initial phase), SMH (breakout, trailing phase)
- **Current equity**: ~$20,540
- **149 unit tests** all passing (decisions, analytics, recovery, Phase 2, Phase 3 controls)
- **Phase 3 production hardening implemented** — kill switch, pause rules, pre-trade controls, reconciliation, health monitoring, alerting, audit logging
- **Phase 2 experiment loop implemented** — controlled backtest evaluation, promotion gates, IS/OOS split
- **Watchlist expanded to 33 symbols** across 8 sectors (added LLY, JPM, FTNT, ZS, FIX, GNRC)

## Architecture
```
scripts/
  orchestrator.py        — Claude-powered autonomous agent (scheduler + AI decision loop)
  research_growth.py     — Scans 27 momentum symbols
  trade_growth.py        — Stop-limit buy orders with correlation cap
  manage_growth.py       — 3-phase exit (initial→protected→trailing) + time stop
  performance.py         — Tracks closed trades, equity curve, stats
  learning.py            — Analyzes performance, proposes strategy tuning
  strategy_manager.py    — Safe parameter changes with snapshots + rollback
  journal.py             — Writes daily markdown journal
  slack_bot.py           — Slack commands (/positions, /sell, /summary, /status, /orders, /kill, /resume)
  common.py              — Pure re-export facade (41 lines, all logic in infra/)
  reconcile.py           — Broker-vs-local state reconciliation
  healthcheck.py         — System health checks
  control_state.py       — Phase 3 CLI: status, kill, pause, health, audit
  reset_controls.py      — Phase 3 CLI: safe manual reset with cooldown
  run.sh                 — Smart runner: auto-detects time, runs correct routine
  controls/
    kill_switch.py       — Phase 3: global kill switch (persistent, cooldown, manual reset)
    pause_rules.py       — Phase 3: automatic pause rules (daily loss, drawdown, errors, heartbeats)
    pretrade.py          — Phase 3: pre-trade control gate (10 checks, structured pass/fail)
    reconcile.py         — Phase 3: broker vs local state reconciliation with anomaly detection
    health.py            — Phase 3: health monitoring, heartbeat checks, status reporting
    alerts.py            — Phase 3: notification abstraction (log-only + webhook modes)
    audit.py             — Phase 3: structured JSONL audit logging for safety actions
  analytics/
    pipeline.py          — Daily analytics orchestrator
    metrics.py           — Performance metrics computation
    attribution.py       — Setup-level and grouped attribution
    ai_review.py         — AI review (daily + cumulative history)
    reports.py           — Daily and weekly report generation (enriched for AI learning)
    regime.py            — Market regime analysis
    experiments.py       — Phase 2: experiment registry with lifecycle (7 statuses)
    variants.py          — Phase 2: apply parameter overrides to baseline strategy
    scorecards.py        — Phase 2: build/compare metric scorecards (IS/OOS)
    promotion.py         — Phase 2: evaluate promotion gates
    evaluate_experiment.py — Phase 2: end-to-end experiment evaluation pipeline
  growth/
    decisions.py         — Pure phase-transition decision logic (no broker calls, 30 unit tests)
    broker_exec.py       — Broker execution helpers (cancel, replace, submit)
    recovery.py          — Metadata reconstruction and recovery helpers
  infra/
    paths.py, jsonio.py, logging_utils.py, locks.py, dedupe.py,
    broker.py, env.py, time_utils.py, sizing.py, config.py, alerts.py
  backtest/
    growth.py            — Growth bot backtester (event-driven, vol sizing)
    walk_forward.py      — Walk-forward out-of-sample validation
    print_results.py     — Backtest results formatter
  legacy/                — Archived conservative bot + old backtests
    research.py, trade.py, manage.py, strategy.json, watchlist.json
    backtest_conservative/ (conservative.py, matrix.py, improvement.py, variants.py)
  tests/
    test_decisions.py    — 30 tests: phase transitions, time stops, trail upgrades, edge cases
    test_analytics.py    — 15 tests: metrics, attribution, AI review, experiments
    test_recovery.py     — 20 tests: recovery, reconciliation, broker mismatch
    test_phase2.py       — 30 tests: variants, scorecards, promotion gates, lifecycle, IS/OOS regression
    test_phase3.py       — 54 tests: kill switch, pause rules, pre-trade controls, reconciliation, health, audit
config/
  strategy_growth.json   — Strategy parameters
  watchlist_growth.json  — 33 symbols across 8 sectors
  guardrails.json        — Safety bounds for auto-tuning
  promotion_rules.json   — Phase 2: experiment promotion gate config
  risk_controls.json     — Phase 3: kill switch, pause rules, pre-trade limits
  alerting.json          — Phase 3: alert routing and event config
  reconciliation.json    — Phase 3: reconciliation checks and safe cleanup settings
  experiments/           — Phase 2: experiment definitions (JSON)
state/
  growth/                — Position tracking, candidates, orders, manage log
  shared/                — Equity curve, performance, AI review, daily/weekly reports
  controls/              — Phase 3: kill switch state, pause state, audit logs
  locks/                 — Job lock files
  logs/                  — Structured JSONL daily logs
  trade_history.json     — All closed trades (format: {"trades": [...]})journal/                 — Daily markdown journals (pushed to git)
```

## Growth Bot Strategy (Primary)
- **Style:** Aggressive momentum swing trading, daily timeframe
- **Universe:** 33 symbols — 4 ETFs (SPY, QQQ, IWM, SMH) + 29 stocks (8 sectors)
- **Regime filter:** SPY + QQQ both above 50-day SMA (full_risk / reduced_risk / risk_off)
- **Ranking:** Composite score: RS vs SPY (3m 50%, 6m 30%) + trend strength (20%), top 25% qualify
- **Setups:**
  1. **Breakout** — price near 20d/55d high, above all major MAs, rel vol ≥1.5×
  2. **Continuation** — ≤3 bar pullback, green close, above SMA20, rel vol ≥1.2×
  3. **Shallow pullback** — within 1.5 ATR of high, above SMA20/50, rel vol ≥1.0×
- **Entry:** Stop-limit buy (trigger=0.05×ATR, limit=0.15×ATR above setup high)
- **Stop:** Wider of (setup_low − 0.2×ATR) or (entry − 2.5×ATR)
- **Exit phases:**
  1. Initial: hold original stop
  2. Protected (1.5R): stop → entry − 0.1×ATR
  3. Trailing (2.5R + 5 bars in profit): 3.0×ATR trail
  4. Trail upgrades: 3R→2.0×ATR, 4R→2.0×ATR, 5R→1.75×ATR, 6R→1.5×ATR, 8R→1.5×ATR
- **Time stop:** Exit after 10 bars if < 0.5R progress
- **Gap-up filter:** Skip if price >3% above trigger
- **Daily circuit breaker:** Halt entries if equity drops >3% from prior close
- **Correlation cap:** 0.85 threshold, 40-day lookback, max 2 correlated positions
- **Broker stop sync:** Every manage run syncs trailing stop price + HWM from Alpaca into local tracking

### Growth Risk Parameters
| Mode | Risk/Trade | Max Pos | Cash Reserve | Portfolio Risk | Max/Symbol |
|------|-----------|---------|-------------|---------------|------------|
| Full Risk | 0.75% | 5 | 5% | 3% | 25% |
| Reduced Risk | 0.4% | 3 | 10% | 1.5% | 20% |

### Volatility-Targeted Sizing
| ATR % | Scalar |
|-------|--------|
| ≤2.5% | 1.0× |
| ≤4.0% | 0.85× |
| ≤6.0% | 0.70× |
| >6.0% | 0.50× |

### Growth Watchlist (33 symbols, 8 sectors)
ETFs: SPY, QQQ, IWM, SMH
Tech: NVDA, AMD, AVGO, ANET, META, AMZN, MSFT, AAPL, GOOGL, PLTR, MU, CRM, NOW, PANW, CRWD, SNOW, TTD, UBER, SHOP, FTNT, ZS
Communication: NFLX | Consumer: TSLA | Materials: FCX, NUE
Healthcare: LLY | Financials: JPM | Industrials: FIX, GNRC

## Daily Operations
| Time (ET) | Routine | Command |
|-----------|---------|---------|
| 9:30-11:00 | Morning (research + trade) | `./run.sh` or `orchestrator.py morning` |
| 11:00-16:00 | Midday manage | `./run.sh` or `manage_growth.py` |
| After 16:00 | EOD (manage + perf + journal + analytics) | `./run.sh` or `orchestrator.py afternoon` |
| Saturday 10AM | Weekly review | `orchestrator.py weekly` |

**run.sh** auto-detects ET time and runs the correct routine. Currently running manually (no persistent orchestrator). Uses `ANTHROPIC_API_KEY="" python3 scripts/orchestrator.py ...` for direct mode. No `bot` parameter needed — growth is the only bot.

## Key State Files
| File | Location | Purpose |
|------|----------|---------|
| position_tracking.json | state/growth/ | Phase, R, stops, bars, setup per position |
| candidates.json | state/growth/ | Research output with rejected reasons |
| order_plan.json | state/growth/ | Trade decisions (orders + skips) |
| manage_log.json | state/growth/ | Position management actions |
| trade_history.json | state/ | All closed trades `{"trades": [...]}` |
| equity_curve.json | state/shared/ | Daily equity snapshots |
| ai_review_history.json | state/shared/ | Cumulative AI reviews (365 days) |
| report_daily_*.md | state/shared/ | Enriched daily reports (in git) |
| report_weekly_*.md | state/shared/ | Enriched weekly reports (in git) |
| experiments.json | state/shared/ | Phase 2: experiment registry |
| experiments/*_result.json | state/shared/ | Phase 2: experiment evaluation results |
| experiments/*_report.md | state/shared/ | Phase 2: experiment comparison reports |
| kill_switch.json | state/controls/ | Phase 3: kill switch state (active/reason/timestamp) |
| pause_state.json | state/controls/ | Phase 3: pause state (active/triggered rules) |
| audit.jsonl | state/controls/ | Phase 3: structured safety event audit log |

## Reports (Enriched for AI Learning)

### Daily Report includes:
Headline metrics, account, market regime, **open positions table** (entry/current/P&L/R/phase/stop), **management actions**, **research summary with top rejection reasons**, orders placed/skipped, **trades closed today**, best/worst contributors, operational issues, AI recommendations, **equity snapshot**, **market context (SPY/QQQ % change)**, **position price context (stop distance, drawdown from best)**, **near-miss candidates**, **correlation & sector concentration**, **trading activity summary**

### Weekly Report includes:
Performance (7d/30d/all), **equity curve table**, **full trade history table**, open positions, attribution by setup/regime/sector, **daily summaries for the week**, **AI review trends**, **strategy observations (pass rate, rejections, utilization, concentration warnings)**, experiments, **what to watch next week**

## Manage Growth Position Phases
```
pending → initial → protected (1.5R) → trailing (2.5R) → (trailing stop fills)
                 ↓                                       ↓
           exit_pending                            trail upgrades (4R/5R/6R/8R)
                 ↓
          (broker confirms → cleanup)
```

## Safety & Guardrails
- Paper trading by default, kill switch, daily loss breaker (3%), portfolio drawdown breaker (15%)
- VIX override (>30), deterministic client_order_id, post-action reconciliation
- Single-instance PID lock, trade time-window (9:30-11:00 AM), stale data protection
- Self-tuning disabled until 30+ trades, weekly tuning limit (2 changes/week)
- Cancel-and-verify before all stop replacements, recovery on failure

## Slack Commands
`/positions` `/summary` `/sell SYMBOL PASSCODE` `/status` `/orders` `/kill` `/resume PASSCODE`

## Git Repository
- **Repo:** github.com/rahulreddy199/trading-bot (private)
- **Tracked:** code, config, daily reports, weekly reports, journals
- **Gitignored:** .env, venv, state JSON (except reports), locks, logs

## Paper Trading Results (as of May 12)
| Trade | Symbol | Setup | Entry | Exit | P&L | R | Exit Type | Status |
|-------|--------|-------|-------|------|-----|---|-----------|--------|
| 1 | MU | — | $572.91 | $734.60 | +$323.36 | 2.79R | trailing_stop | Closed |
| 2 | SMH | breakout | $517.22 | — | ~+$173 | ~1.4R | — | Open (trailing) |
| 3 | AMD | continuation | $431.57 | — | ~+$16 | ~0.27R | — | Open (initial) |

## Known Issues
- Trade history setup_type/bars_held show "?" for order_scan-detected closes
- Attribution shows "unknown" for closed trades (needs richer recording at close time)
- Equity curve has limited data points (started May 4)
- Sector diversification improved (8 sectors) but still tech-heavy (23/33 symbols)
- 40% slot utilization (2/5) — filters may be strict or market not offering setups
- Paper incubation tracking for experiments requires manual data entry (future: auto-tag)

## Backtesting Assessment
Current backtests are **event-driven** (bar-by-bar), with **0.1% flat slippage**, **stop-limit fill logic**, and **volatility-targeted sizing**.
- ✅ **Walk-forward validation completed** — 5/5 windows profitable, +30.6% total (matches full-period +30.7%)
- ✅ Commissions not modeled but Alpaca is commission-free (realistic)
- ✅ Stale order expiry, regime/breadth filters, phase transitions all match live bot
- ✅ Vol sizing active in both backtest and live
- ⚠️ Volume-based fill feasibility not modeled (no check if order qty vs bar volume)

### Walk-Forward Results (Jan 2024 – May 2026)
| Window | Period | Return | Trades | WR | P&L |
|--------|--------|--------|--------|-----|-----|
| W1 | Jan–Jun 2024 | +6.1% | 39 | 62% | +$1,249 |
| W2 | Jul–Dec 2024 | +6.1% | 31 | 55% | +$1,108 |
| W3 | Jan–Jun 2025 | +5.0% | 16 | 69% | +$311 |
| W4 | Jul–Dec 2025 | +3.5% | 42 | 55% | +$850 |
| W5 | Jan–May 2026 | +6.7% | 17 | 53% | +$666 |

**Combined:** 145 trades, 57.9% WR, 1.81 PF, -6.02% max DD. Strategy is NOT overfit.

Key insights:
- Time stops are 58% of exits (most trades don't reach trailing)
- Continuation setups: best WR (74%), fewest trades
- Shallow pullbacks: break even (avg R=0.00) — watch in live trading

## Phase 0 Hardening (Completed)
- State isolation, job locks, broker reconciliation, structured JSONL logging
- Health summary, recovery tests, manual-review escalation
- Refactored common.py → infra/ modules, manage_growth.py → growth/ modules

## Phase 1 Improvements (Complete)
- ✅ Intraday manage runs (10:30 AM, 1:00 PM, 4:05 PM)
- ✅ Slippage tracking, gap-up filter, daily circuit breaker
- ✅ Trail upgrades at R milestones (4R/5R/6R/8R)
- ✅ Enriched daily/weekly reports for AI learning
- ✅ Broker stop-price sync on every manage run
- ✅ Conservative bot archived, growth-only codebase
- ✅ Unit tests for decisions.py (30 tests, all passing)
- ✅ Performance stats fixed (largest loser no longer shows winners)
- ✅ Walk-forward backtest validation (5/5 windows profitable, NOT overfit)
- ✅ Volatility-targeted sizing (live in trade_growth.py + backtest)
- ✅ Equity curve path fix (performance.py now writes to state/shared/)
- ✅ Position tracking path fix (performance.py reads growth tracking)
- ⬜ Relative volume + liquidity filters (config ready, partially wired)
- ⬜ Setup-level performance attribution (full metadata per trade)
- ⬜ Setup-specific ranking (separate scoring per setup type)

## Phase 2: Controlled Experiment Loop (Implemented May 14, 2026)

### Modules
- `analytics/variants.py` — Apply dotted-path parameter overrides to baseline strategy
- `analytics/scorecards.py` — Build comparable metric scorecards, IS/OOS split
- `analytics/promotion.py` — Evaluate explicit promotion gates against configurable thresholds
- `analytics/evaluate_experiment.py` — End-to-end pipeline: backtest → scorecard → gates → report
- `analytics/experiments.py` — Enhanced registry: 7 statuses with validated transitions

### Config
- `config/promotion_rules.json` — 10 configurable promotion gates
- `config/experiments/` — Experiment definitions (JSON), 2 examples included

### Workflow
```
1. Define: config/experiments/my_experiment.json (hypothesis + overrides)
2. Run:    python scripts/analytics/evaluate_experiment.py config/experiments/my_experiment.json
3. Review: state/shared/experiments/my_experiment_report.md
4. Paper:  Transition to active_paper in registry, track paper trades
5. Decide: Manually promote or reject based on gate results + paper data
```

### Promotion Gates
- min_trade_count (15), min_oos_trade_count (8)
- profit_factor ≥ 1.0, oos_profit_factor ≥ 0.8
- expectancy must not degrade, max_drawdown increase ≤ 3%
- win_rate ≥ 30%, avg_r ≥ -0.5
- Paper incubation required before promotion
- **All advisory only — no auto-promotion, no config mutation**

### Safety
- Baseline strategy_growth.json is NEVER modified
- All output is recommendation-only
- Rollback path always preserved
- 30 dedicated tests covering variants, scorecards, gates, lifecycle, IS-good-OOS-bad regression

### Example Experiments
- `exp_wider_pullback_001` — Increase shallow pullback max_depth_atr from 1.5 → 2.0
- `exp_trail_tighter_002` — Tighten trailing_atr_multiplier from 3.0 → 2.5

## Phase 3: Production Hardening (Implemented May 14, 2026)

### Modules (`scripts/controls/`)
- `kill_switch.py` — Global kill switch with persistence, cooldown, requires manual reset
- `pause_rules.py` — Automatic pause rules: daily loss, rolling drawdown, slippage, rejections, broker errors, stale data, duplicate orders, heartbeat missing
- `pretrade.py` — Pre-trade control gate: 10 checks (kill switch, pause, position limits, allocation, risk budget, correlation, dedup, price sanity, qty bounds, price collar)
- `reconcile.py` — Broker vs local state reconciliation with anomaly detection and markdown report
- `health.py` — Health monitoring: heartbeat freshness, job status, control state, composite health score
- `alerts.py` — Notification abstraction: log-only mode (default) + webhook mode for Slack/custom
- `audit.py` — Structured JSONL audit logging for all safety actions (kill, pause, pre-trade block, reconciliation anomaly)

### CLI Tools
- `scripts/control_state.py` — View status, activate kill switch, activate pause, run health check, view audit log
- `scripts/reset_controls.py` — Safe manual reset with cooldown enforcement, status overview

### Config
- `config/risk_controls.json` — Kill switch settings, 8 pause rules with thresholds, pre-trade limits
- `config/alerting.json` — Alert routing: which events trigger which notification channels
- `config/reconciliation.json` — Reconciliation checks, safe cleanup settings

### Key Features
- Kill switch is persistent (survives restarts), requires explicit manual reset
- Pause rules auto-trigger but require manual reset (no silent auto-resume)
- Pre-trade gate returns structured pass/fail with reasons — integrates before any order placement
- All safety events written to audit log (JSONL) for compliance and debugging
- Health check provides composite status: HEALTHY / DEGRADED / CRITICAL
- Alerts support log-only (default) and webhook modes for progressive rollout
- 54 dedicated tests covering all control paths

### Usage
```bash
python3 scripts/control_state.py status       # View all control states
python3 scripts/control_state.py kill "reason" # Activate kill switch
python3 scripts/control_state.py pause "reason" # Manual pause
python3 scripts/control_state.py health        # Health check
python3 scripts/control_state.py audit         # View audit log
python3 scripts/reset_controls.py reset_kill   # Reset kill (with cooldown)
python3 scripts/reset_controls.py reset_pause  # Reset pause
python3 scripts/reset_controls.py status       # Reset status overview
```

## Refactoring History
- **May 14, 2026 (Phase 3)**: Production hardening implemented — kill switch, pause rules (8 auto-triggers), pre-trade control gate (10 checks), reconciliation with anomaly detection, health monitoring, alerting abstraction, structured audit logging. CLI tools for control_state and reset_controls. 54 new tests (149 total). Config: risk_controls.json, alerting.json, reconciliation.json.
- **May 14, 2026 (Phase 2)**: Controlled experiment loop with variants, scorecards, IS/OOS split, promotion gates, evaluation pipeline. 30 new tests (95 total). Watchlist expanded from 27 → 33 symbols (added LLY, JPM, FTNT, ZS, FIX, GNRC for sector diversification: Healthcare, Financials, Industrials). README and CONTEXT updated.
- **May 13, 2026**: Full script audit — all 22 modules import clean, all scripts run successfully, 65 tests pass. SSH key setup for dual GitHub accounts. Pushed to git.
- **May 12, 2026 (late)**: Walk-forward backtest implemented (5 windows, 100% profitable). Vol sizing added to backtest engine. Fixed performance.py equity curve path (was state/ → now state/shared/) and position tracking path (was old conservative → now growth). CONTEXT.md and README.md updated.
- **May 12, 2026 (evening)**: Added 30 unit tests for growth/decisions.py. Fixed performance.py largest_loser bug. Fixed slack_bot.py /summary indentation. Pushed to git.
- **May 12, 2026**: Archived conservative bot to `scripts/legacy/`. Growth bot is now the sole production path. Removed conservative branches from orchestrator, simplified CLI (no more `bot` parameter), updated config loaders to default to growth. Cleaned up healthcheck, reports, slack_bot conservative references.
