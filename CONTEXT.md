# Trading Bot - Project Context & Status

## Last Updated: May 12, 2026

## Current Status: Paper-Trading Active (Week 2) — Growth Bot Only

## Quick Summary
- **Paper trading with Alpaca** since May 4, 2026 ($20K starting capital)
- **Growth bot is the sole production bot** — conservative bot archived to `scripts/legacy/`
- **3 trades entered**, 1 closed (MU: +$323.36, +2.79R trailing stop exit)
- **2 positions open**: AMD (continuation, initial phase), SMH (breakout, trailing phase)
- **Current equity**: ~$20,500

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
  common.py              — Shared utilities (compatibility facade)
  reconcile.py           — Broker-vs-local state reconciliation
  healthcheck.py         — System health checks
  run.sh                 — Smart runner: auto-detects time, runs correct routine
  analytics/
    pipeline.py          — Daily analytics orchestrator
    metrics.py           — Performance metrics computation
    attribution.py       — Setup-level and grouped attribution
    ai_review.py         — AI review (daily + cumulative history)
    reports.py           — Daily and weekly report generation (enriched for AI learning)
    regime.py            — Market regime analysis
    experiments.py       — A/B experiment tracking
  growth/
    decisions.py         — Pure phase-transition decision logic (no broker calls)
    broker_exec.py       — Broker execution helpers (cancel, replace, submit)
    recovery.py          — Metadata reconstruction and recovery helpers
  infra/
    paths.py, jsonio.py, logging_utils.py, locks.py, dedupe.py,
    broker.py, env.py, time_utils.py, sizing.py, config.py, alerts.py
  backtest/
    growth.py            — Growth bot backtester
    print_results.py     — Backtest results formatter
  legacy/                — Archived conservative bot + old backtests
    research.py, trade.py, manage.py, strategy.json, watchlist.json
    backtest_conservative/ (conservative.py, matrix.py, improvement.py, variants.py)
  tests/
    test_analytics.py, test_recovery.py
config/
  strategy_growth.json   — Strategy parameters
  watchlist_growth.json  — 27 symbols
  guardrails.json        — Safety bounds for auto-tuning
state/
  growth/                — Position tracking, candidates, orders, manage log
  shared/                — Equity curve, performance, AI review, daily/weekly reports
  locks/                 — Job lock files
  logs/                  — Structured JSONL daily logs
  trade_history.json     — All closed trades (format: {"trades": [...]})
journal/                 — Daily markdown journals (pushed to git)
```

## Growth Bot Strategy (Primary)
- **Style:** Aggressive momentum swing trading, daily timeframe
- **Universe:** 27 symbols — 4 ETFs (SPY, QQQ, IWM, SMH) + 23 stocks (tech-heavy)
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

### Growth Watchlist (27 symbols)
ETFs: SPY, QQQ, IWM, SMH
Tech: NVDA, AMD, AVGO, ANET, META, AMZN, MSFT, AAPL, GOOGL, PLTR, MU, CRM, NOW, PANW, CRWD, SNOW, TTD, UBER, SHOP
Communication: NFLX | Consumer: TSLA | Materials: FCX, NUE

## Daily Operations
| Time (ET) | Routine | Command |
|-----------|---------|---------|
| 9:30-11:00 | Morning (research + trade) | `./run.sh` or `orchestrator.py morning` |
| 11:00-16:00 | Midday manage | `./run.sh` or `manage_growth.py` |
| After 16:00 | EOD (manage + perf + journal + analytics) | `./run.sh` or `orchestrator.py afternoon` |
| Saturday 10AM | Weekly review | `orchestrator.py weekly` |

**run.sh** auto-detects ET time and runs the correct routine. Currently running manually (no persistent orchestrator). Uses `ANTHROPIC_API_KEY="" python3 scripts/orchestrator.py ...` for direct mode.

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
- All positions currently in Technology sector (concentration risk flagged)
- 40% slot utilization (2/5) — filters may be strict or market not offering setups

## Phase 0 Hardening (Completed)
- State isolation, job locks, broker reconciliation, structured JSONL logging
- Health summary, recovery tests, manual-review escalation
- Refactored common.py → infra/ modules, manage_growth.py → growth/ modules

## Phase 1 Improvements (Partially Implemented)
- ✅ Intraday manage runs (10:30 AM, 1:00 PM, 4:05 PM)
- ✅ Slippage tracking, gap-up filter, daily circuit breaker
- ✅ Trail upgrades at R milestones (4R/5R/6R/8R)
- ✅ Enriched daily/weekly reports for AI learning
- ✅ Broker stop-price sync on every manage run
- ⬜ Relative volume + liquidity filters (config ready, partially wired)
- ⬜ Volatility-targeted sizing (config ready)
- ⬜ Setup-level performance attribution (full metadata per trade)
- ⬜ Setup-specific ranking (separate scoring per setup type)

## Refactoring History
- **May 12, 2026**: Archived conservative bot to `scripts/legacy/`. Growth bot is now the sole production path. Removed conservative branches from orchestrator, simplified CLI (no more `bot` parameter), updated config loaders to default to growth.

