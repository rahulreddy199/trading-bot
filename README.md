# AI Swing Trading Bot

An autonomous, Claude-powered momentum swing trading system. Scans for breakout/continuation setups, places orders via Alpaca, manages positions with phased exits, and learns from performance over time.

> **Note:** A conservative pullback bot was previously part of this system. It has been moved to `scripts/legacy/` for reference. The growth/momentum bot is the sole production path.

---

## Strategy

The growth bot targets high-momentum names making new highs or pulling back shallowly in strong uptrends.

### Setups

| Setup | Entry Condition | Min Relative Volume |
|-------|----------------|---------------------|
| **Breakout** | Price within 2% of 20-day or 55-day high, above all key SMAs | 1.5× avg |
| **Shallow Pullback** | Pulled back < 1.5 ATR from recent high, still above SMA 20 & 50 | 1.0× avg |
| **Continuation** | ≤ 3 bar pullback, green close, still above SMA 20 | 1.2× avg |

### Ranking
- 50% weight: 3-month relative strength vs SPY
- 30% weight: 6-month relative strength vs SPY
- 20% weight: trend strength (distance above 200 SMA, capped at 15%)
- Only top 25% of scanned universe qualifies

### Risk Management (Growth)

| Parameter | Full Risk | Reduced Risk |
|-----------|-----------|--------------|
| Risk per trade | 0.75% | 0.4% |
| Max positions | 5 | 3 |
| Max portfolio risk | 3% | 1.5% |
| Max per symbol | 25% | 20% |
| Cash reserve | 5% | 10% |

### Volatility-Targeted Position Sizing

Position size is scaled by a volatility bucket system based on ATR as a percentage of price:

| ATR % of Price | Size Scalar | Effect |
|----------------|-------------|--------|
| ≤ 2.5% | 1.0× | Full size (low vol) |
| ≤ 4.0% | 0.85× | Slightly reduced |
| ≤ 6.0% | 0.70× | Moderate reduction |
| > 6.0% | 0.50× | Half size (high vol) |

This prevents oversized positions in volatile names even when the per-trade risk budget allows it.

### Exit System (Growth)

1. **Initial**: stop at wider of (setup low − 0.2×ATR) or (entry − 2.5×ATR)
2. **Protected** (at 1.5R): stop moves to entry − 0.1×ATR (near-breakeven with noise buffer)
3. **Trailing** (at 2.5R + 5 bars in profit): trailing stop at 3.0×ATR below highest close
4. **Tight trailing** (at 3R+): trail tightens to 2.0×ATR — keeps more profit from big runners
5. **Time stop**: if no profit after 10 bars → exit at market

### Trail Upgrades at Milestones
Once trailing is active, the bot progressively tightens the trail as R increases:
- **2.5R + 5 bars in profit**: activate trailing at 3.0×ATR
- **3R+**: tighten to 2.0×ATR (tight trailing)
- **4R+**: tighten to 2.0×ATR (first upgrade checkpoint)
- **5R+**: tighten to 1.75×ATR
- **6R+**: tighten to 1.5×ATR
- **8R+**: tighten to 1.5×ATR (final lock-in)

Each upgrade fires only once per threshold and uses cancel-and-verify before replacing.

### Stop Recovery & Reconciliation

The growth bot has multiple layers of position protection:

1. **Broker-vs-tracking reconciliation**: On every manage run, the bot compares broker state (stop orders, trailing stops) against local tracking phase and auto-corrects mismatches:
   - Broker has trailing but tracking says "protected" → sync tracking up to "trailing"
   - Broker has stop but tracking says "trailing" → sync tracking down to "protected"
   - No protective order exists → immediately re-place stop

2. **Exit-pending recovery**: If a submitted exit order is later canceled/expired/rejected by the broker, the bot reverts the position to "initial" phase for continued management (not left in limbo).

3. **Failure recovery patterns**: Every phase transition (initial→protected, protected→trailing, time stop) follows a "cancel old → place new → on failure restore old" pattern. If the new order fails after the old was cancelled, the old stop is immediately re-created.

4. **Metadata reconstruction**: If tracking data is incomplete (e.g., after a restart), `manage_growth.py` reconstructs missing `r_per_share` and ATR from multiple sources in priority order:
   - `last_orders_growth.json` (closest to executed trade)
   - `order_plan_growth.json`
   - `candidates_growth.json`
   - ATR fallback estimate (flags for `MANUAL_REVIEW`)

5. **Stop validation**: Before placing any recovery stop, validates that stop price > 0, stop < current price, and qty > 0.

6. **Broker stop-price sync**: On every manage run, trailing stop prices and high-water marks are synced from broker order state into local tracking, ensuring local data always reflects actual Alpaca stop levels.

### Gap-Up Filter
Skips entries when the current price is already >3% above the trigger price (configurable via `gap_up_max_pct`). Prevents chasing extended breakouts.

### Daily Circuit Breaker
If account equity drops >3% from prior close in a single day, new entries are automatically halted. Position management continues normally. This is separate from the portfolio drawdown breaker (see Safety & Guardrails).

### Portfolio Risk Budget
Before each new entry, the bot calculates total portfolio risk (sum of r_per_share × qty for all open positions). New entries are blocked if adding the trade would exceed the `max_total_portfolio_risk_pct` (3% in full risk mode). Risk per existing position uses a fallback chain: tracked data → last_orders → order_plan → conservative 2.5% estimate.

### Intraday Position Management
Growth positions are managed 3× per day (10:30 AM, 1:00 PM, 4:05 PM ET) to catch intraday phase transitions. All runs are fully idempotent.

### Slippage Tracking
Every fill records planned trigger, planned limit, actual fill price, and slippage in dollars and basis points — fed into the learning module.

### Correlation Cap
- 40-day rolling correlation
- Blocks entry if candidate is > 0.85 correlated with 2+ existing positions
- Configurable fail-open/fail-closed on data errors

### Growth Watchlist (33 Symbols)

| Sector | Symbols |
|--------|---------|
| **ETFs** | SPY, QQQ, IWM, SMH |
| **Technology** | NVDA, AMD, AVGO, ANET, META, AMZN, MSFT, AAPL, GOOGL, PLTR, MU, CRM, NOW, PANW, CRWD, SNOW, TTD, UBER, SHOP, FTNT, ZS |
| **Communication** | NFLX |
| **Consumer** | TSLA |
| **Materials** | FCX, NUE |
| **Healthcare** | LLY |
| **Financials** | JPM |
| **Industrials** | FIX, GNRC |

---

## How The Bot Works

### State Layout

State files live in `state/growth/` for position tracking and candidates, `state/shared/` for equity curves, performance, and reports. A `state_path(bot, name)` helper ensures consistent path resolution.

### Daily Cycle (Fully Autonomous)

| Time (ET) | What Happens |
|-----------|-------------|
| **8:00 AM** | 💓 Heartbeat alert — confirms bot is alive |
| **9:35 AM** | 🌅 **Morning run**: scan watchlist → filter candidates → place stop-limit orders |
| **10:30 AM** | 📈 **Manage run**: intraday phase checks (idempotent) |
| **1:00 PM** | 📈 **Manage run**: intraday phase checks (idempotent) |
| **4:05 PM** | 🌆 **Afternoon run**: manage → performance → journal → analytics |
| **5:00 PM** | 💾 **Daily backup**: local + S3 (if configured) |
| **Saturday 10 AM** | 📊 **Weekly review**: analyze performance → propose parameter tweaks |

### Running the Bot

```bash
# Easiest: smart runner auto-detects ET time
./run.sh

# Or run specific routines directly
python scripts/orchestrator.py morning
python scripts/orchestrator.py afternoon
python scripts/orchestrator.py weekly
```

### Three-Phase Exit System

1. Initial stop → Protected at 1.5R → Trailing at 2.5R (3.0×ATR) + time stop at 10 bars

### Self-Learning System

- `learning.py` analyzes win rate, avg R, exit reasons, and profit factor
- Proposes parameter adjustments based on statistical patterns
- **Safety**: min 30 trades before any tuning, max 2 parameters changed per week, bounded step sizes, strategy snapshots before every change

## Project Structure

```
config/
  strategy_growth.json     # Strategy parameters
  watchlist_growth.json    # Watchlist (33 symbols, 8 sectors)
  guardrails.json          # Safety bounds for auto-tuning
  promotion_rules.json     # Phase 2: experiment promotion gates
  risk_controls.json       # Phase 3: kill switch, pause rules, pre-trade limits
  alerting.json            # Phase 3: alert routing and event config
  reconciliation.json      # Phase 3: reconciliation checks and cleanup settings
  experiments/             # Phase 2: experiment definitions
scripts/
  orchestrator.py          # Claude-powered autonomous agent (the brain)
  research_growth.py       # Market scan + candidate selection
  trade_growth.py          # Order placement
  manage_growth.py         # Position management (3-phase exit)
  learning.py              # Performance analysis + tuning proposals
  strategy_manager.py      # Safe parameter changes with snapshots
  journal.py               # Daily journal writer
  performance.py           # Performance metrics calculator
  slack_bot.py             # Slack command interface (/positions, /sell, /summary)
  common.py                # Shared utilities + compatibility facade
  reconcile.py             # Broker-vs-local state reconciliation
  healthcheck.py           # System health checks
  control_state.py         # Phase 3: CLI for status, kill, pause, health, audit
  reset_controls.py        # Phase 3: safe manual reset with cooldown
  run.sh                   # Smart runner: auto-detects ET time, runs correct routine
  controls/                # Phase 3: production hardening controls
    kill_switch.py         # Global kill switch (persistent, cooldown, manual reset)
    pause_rules.py         # Automatic pause rules (8 triggers)
    pretrade.py            # Pre-trade control gate (10 checks)
    reconcile.py           # Broker vs local reconciliation + anomaly detection
    health.py              # Health monitoring + heartbeat checks
    alerts.py              # Notification abstraction (log + webhook)
    audit.py               # Structured JSONL audit logging
  analytics/               # Analytics + experiment loop
    pipeline.py            # Daily analytics orchestrator
    metrics.py             # Performance metrics computation
    attribution.py         # Setup-level and grouped attribution
    ai_review.py           # AI review recommendations (daily + cumulative history)
    reports.py             # Daily and weekly report generation (enriched for AI learning)
    regime.py              # Market regime analysis
    experiments.py         # Phase 2: experiment registry with lifecycle management
    variants.py            # Phase 2: apply parameter overrides to baseline strategy
    scorecards.py          # Phase 2: build/compare metric scorecards (IS/OOS)
    promotion.py           # Phase 2: evaluate promotion gates
    evaluate_experiment.py # Phase 2: end-to-end experiment evaluation pipeline
  growth/                  # Core growth bot modules
    decisions.py           # Pure phase-transition decision logic (no broker calls)
    broker_exec.py         # Broker execution helpers (cancel, replace, submit)
    recovery.py            # Metadata reconstruction and recovery helpers
  infra/                   # Infrastructure modules
    paths.py, jsonio.py, logging_utils.py, locks.py, dedupe.py,
    broker.py, env.py, time_utils.py, sizing.py, config.py, alerts.py
  backtest/                # Backtesting modules
    growth.py              # Growth bot backtester (event-driven, bar-by-bar)
    walk_forward.py        # Walk-forward out-of-sample validation
    print_results.py       # Backtest results formatter
  legacy/                  # Archived code (conservative bot)
    research.py, trade.py, manage.py, strategy.json, watchlist.json
    backtest_conservative/
  tests/                   # Test suite (149 tests)
    test_decisions.py      # 30 unit tests for phase-transition logic
    test_analytics.py      # Analytics pipeline tests
    test_recovery.py       # Recovery and reconciliation tests
    test_phase2.py         # Phase 2: experiment loop, scorecards, promotion gates
    test_phase3.py         # Phase 3: kill switch, pause, pre-trade, reconcile, health, audit
state/                     # Runtime state (gitignored except reports)
  growth/                  # Position tracking, candidates, orders, manage log
  shared/                  # Equity curve, AI review history, daily/weekly reports
  controls/                # Phase 3: kill switch state, pause state, audit log
  locks/                   # Job lock files
  logs/                    # Structured JSONL daily logs
journal/                   # Daily journals (pushed to git)
prompts/                   # Prompt templates
docs/                      # Architecture and hardening docs
```

## Quick Start

### 1. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
# Required
ALPACA_API_KEY=your_alpaca_key
ALPACA_SECRET_KEY=your_alpaca_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# For AI orchestration (optional — runs in direct mode without it)
ANTHROPIC_API_KEY=your_anthropic_key

# For Slack alerts + commands (optional)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# For cloud backups (optional)
S3_BACKUP_BUCKET=my-trading-bot-backups

# Safety
ALLOW_LIVE_TRADING=false
```

### 3. Run the bot

```bash
# Easiest: smart runner auto-detects ET time and runs the correct routine
./run.sh

# Or run specific routines
python scripts/orchestrator.py morning     # Research + trade
python scripts/orchestrator.py afternoon   # Manage + performance + journal
python scripts/orchestrator.py weekly      # Weekly review

# Persistent autonomous bot (runs on schedule)
python scripts/orchestrator.py

# Run individual scripts
python scripts/research_growth.py
python scripts/trade_growth.py
python scripts/manage_growth.py
python scripts/journal.py
```

### 4. Slack Commands

Once `slack_bot.py` is running:
- `/positions` — view all open positions with P&L
- `/summary` — full status: R, phase, setup, bars held, stop, next target per position
- `/sell SYMBOL PASSCODE` — force-sell a position (records to trade history for learning)
- `/status` — bot health check (heartbeats, equity, kill switch status)
- `/orders` — show all open/pending orders
- `/kill` — activate kill switch (halt all new entries)
- `/resume PASSCODE` — deactivate kill switch

### 5. Deploy on a VPS

```bash
git clone <repo> && cd trading-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your keys

# Run as systemd service:
sudo tee /etc/systemd/system/trading-bot.service << EOF
[Unit]
Description=Trading Bot
After=network.target

[Service]
WorkingDirectory=/path/to/trading-bot
ExecStart=/path/to/trading-bot/venv/bin/python scripts/orchestrator.py
Restart=always
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable trading-bot && sudo systemctl start trading-bot
```

## Enriched Reports (for AI Learning)

Both daily and weekly reports are designed as structured inputs for AI analysis and cumulative learning.

### Daily Report Sections
- **Headline metrics**: win rate, avg R, profit factor, equity
- **Account snapshot**: cash, equity, buying power, open P&L
- **Market regime**: SPY & QQQ status vs 50/200 SMA, regime classification
- **Market context**: SPY & QQQ daily % change
- **Open positions table**: entry, current price, P&L, R-multiple, phase, stop price
- **Position price context**: stop distance %, drawdown from best price
- **Management actions**: phase transitions, stop replacements, trail upgrades
- **Research summary**: candidates scanned, passed, rejected — with top rejection reasons
- **Near-miss candidates**: symbols that failed only 1 filter (close to qualifying)
- **Orders placed/skipped**: with reasoning
- **Trades closed today**: entry/exit/P&L/R/exit type
- **Best/worst contributors**: top P&L and losing positions
- **Correlation & sector concentration**: warnings if concentrated
- **Trading activity summary**: slot utilization, capital deployed %, regime
- **Equity snapshot**: current equity with daily/weekly/monthly change
- **AI recommendations**: from the analytics pipeline
- **Operational issues**: errors or anomalies

### Weekly Report Sections
- **Performance summary**: 7-day, 30-day, and all-time metrics
- **Equity curve table**: daily equity values for the week
- **Full trade history table**: all closed trades with setup, entry, exit, P&L, R, exit type
- **Open positions**: current state of all holdings
- **Attribution**: by setup type, market regime, and sector
- **Daily summaries**: recap of each day's activity
- **AI review trends**: recurring themes from cumulative AI reviews
- **Strategy observations**: pass rate, top rejection reasons, slot utilization, concentration warnings
- **Experiments**: A/B test status and results
- **What to watch next week**: upcoming catalysts and areas to monitor

Reports are saved to `state/shared/` and pushed to git for version tracking.

## Phase 2: Controlled Experiment Loop

Every strategy change goes through a disciplined experiment pipeline — no ad hoc tuning.

### Workflow
1. **Define** — Create `config/experiments/my_experiment.json` with hypothesis + parameter overrides
2. **Evaluate** — Run `python scripts/analytics/evaluate_experiment.py config/experiments/my_experiment.json`
3. **Review** — Read the generated markdown report and promotion gate results in `state/shared/experiments/`
4. **Paper incubate** — If backtest gates pass, run the variant in paper trading
5. **Promote or reject** — After paper incubation confirms, manually apply or reject

### Experiment Lifecycle
```
proposed → active_backtest → active_paper → promoted
                ↓                  ↓              ↓
             rejected          rejected      rolled_back
```

### Promotion Gates (configurable in `config/promotion_rules.json`)
- Minimum total trade count (15) and OOS trade count (8)
- Profit factor ≥ 1.0 overall, ≥ 0.8 out-of-sample
- Expectancy must not degrade vs baseline
- Max drawdown cannot worsen by more than 3%
- Win rate ≥ 30%, avg R ≥ -0.5
- Paper incubation required before promotion
- **No auto-promotion** — output is recommendation-only

### Example Experiments Included
- `exp_wider_pullback_001` — Increase shallow pullback depth from 1.5 to 2.0 ATR
- `exp_trail_tighter_002` — Tighten trailing stop from 3.0 to 2.5 ATR

---

## Phase 3: Production Hardening

Operational safety controls that ensure the bot degrades gracefully under failures, volatility, and execution anomalies.

### Kill Switch
- Persistent state file (survives restarts)
- Triggered manually or auto-triggered by critical failures
- Requires explicit manual reset with cooldown period
- Optionally cancels all open orders on activation

### Automatic Pause Rules (8 triggers)
| Rule | Threshold | Description |
|------|-----------|-------------|
| Daily loss | 3% | Equity drop from prior close |
| Rolling drawdown | 15% | 30-day rolling max drawdown |
| Abnormal slippage | 1.5% | Average slippage over recent fills |
| Order rejections | 5 in 4h | Repeated broker rejections |
| Broker errors | 10 in 1h | API failures |
| Stale data | 30 min | Market data freshness during hours |
| Duplicate orders | 3 | Duplicate order detection |
| Heartbeat missing | 60 min | Critical script heartbeat stale |

All pause rules require **manual reset** — no silent auto-resume.

### Pre-Trade Control Gate (10 checks)
Every order passes through a structured gate before submission:
1. Kill switch check
2. Pause state check
3. Position count limit
4. Per-symbol allocation cap
5. Total portfolio risk budget
6. Correlation cap
7. Duplicate pending order check
8. Price sanity (deviation from recent close)
9. Quantity bounds
10. Price collar

Returns structured pass/fail with specific reasons for each blocked order.

### Reconciliation
- Broker vs local state comparison with anomaly detection
- Detects: orphaned orders, missing tracking, position mismatches
- Generates markdown report for manual review
- Safe cleanup mode (configurable)

### Health Monitoring
- Heartbeat freshness checks for all critical scripts
- Composite health score: HEALTHY / DEGRADED / CRITICAL
- Control state integration (kill switch + pause status)

### Alerting
- Log-only mode (default) — all alerts go to structured logs
- Webhook mode — Slack/custom webhook for critical events
- Progressive rollout: start with logs, add webhooks after validation

### Audit Logging
- All safety events written to JSONL audit log
- Events: kill switch activations, pause triggers, pre-trade blocks, reconciliation anomalies, manual resets
- Structured format for compliance review and debugging

### CLI Tools
```bash
python3 scripts/control_state.py status        # View all control states
python3 scripts/control_state.py kill "reason"  # Activate kill switch
python3 scripts/control_state.py pause "reason" # Manual pause
python3 scripts/control_state.py health         # Health check
python3 scripts/control_state.py audit          # View audit log
python3 scripts/reset_controls.py reset_kill    # Reset kill switch (with cooldown)
python3 scripts/reset_controls.py reset_pause   # Reset pause state
python3 scripts/reset_controls.py status        # Reset status overview
```

---

## Paper Trading Results

Paper trading launched May 4, 2026 with $20K starting capital.

| # | Symbol | Setup | Entry | Exit | P&L | R | Exit Type | Status |
|---|--------|-------|-------|------|-----|---|-----------|--------|
| 1 | MU | — | $572.91 | $734.60 | +$323.36 | 2.79R | trailing_stop | Closed |
| 2 | SMH | breakout | $517.22 | — | — | — | — | Open (trailing) |
| 3 | AMD | continuation | $431.57 | — | — | — | — | Open (initial) |


## Backtest Results

### Walk-Forward Validation (Jan 2024 – May 2026)

Out-of-sample validation using 5 rolling 6-month test windows. Each window starts with equity from the previous — no lookahead bias.

| Window | Period | Return | Trades | Win Rate | P&L |
|--------|--------|--------|--------|----------|-----|
| W1 | Jan–Jun 2024 | +6.1% | 39 | 62% | +$1,249 |
| W2 | Jul–Dec 2024 | +6.1% | 31 | 55% | +$1,108 |
| W3 | Jan–Jun 2025 | +5.0% | 16 | 69% | +$311 |
| W4 | Jul–Dec 2025 | +3.5% | 42 | 55% | +$850 |
| W5 | Jan–May 2026 | +6.7% | 17 | 53% | +$666 |

**Combined:** +30.6% total return, 145 trades, 57.9% WR, 1.81 PF, -6.02% max DD, **5/5 windows profitable**.

Strategy is validated as NOT overfit — walk-forward return matches full-period backtest (+30.7%).

## Cost Estimates

| Item | Cost/month |
|------|-----------|
| Cloud VPS (Hetzner/DigitalOcean) | $4-7 |
| Claude API (~8 calls/day) | $2-3 |
| Alpaca paper trading | Free |
| Slack alerts | Free |
| S3 backups | ~$0.01 |
| **Total** | **~$7-10/month** |

## Safety & Guardrails

- **Paper trading by default** — live trading requires explicit env var
- **Kill switch** — persistent state file, requires manual reset with cooldown (Phase 3)
- **Automatic pause rules** — 8 triggers: daily loss, drawdown, slippage, rejections, errors, stale data, duplicates, heartbeats (Phase 3)
- **Pre-trade control gate** — 10 structured checks before every order (Phase 3)
- **Daily loss breaker** — halts new entries if equity drops >3% from prior close; management continues
- **Portfolio drawdown breaker** — halts new entries if drawdown from equity peak exceeds 15%
- **VIX override** — forces reduced risk when VIX > 30
- **Self-tuning disabled by default** — must be manually enabled after paper validation
- **Idempotency guard** — won't double-place orders if triggered twice
- **Stale data protection** — refuses to execute if research data isn't from today
- **Correlation cap** (growth bot) — blocks concentrated correlated bets
- **Time stop** (growth bot) — exits dead positions after 10 bars
- **Audit logging** — all safety events logged to JSONL for compliance (Phase 3)
- **No averaging down, no revenge trading, no extended hours, no holding through earnings**

## Pre-Live Checklist

- [ ] Paper burn-in: 4+ weeks with zero state/execution bugs
- [ ] Verify OTO → trailing stop transitions on real paper positions
- [ ] Restart recovery test: kill mid-session, restart, confirm state intact
- [ ] Stale order cleanup verified (auto-cancellation after 2 days)
- [ ] Broker reconciliation: compare tracking files vs Alpaca positions
- [ ] Kill switch test: create/remove `state/KILL_SWITCH`
- [ ] Duplicate run test: run trade.py twice, confirm second is blocked
- [ ] Upgrade to Alpaca paid market data plan before live
- [ ] Keep tuning OFF until 30+ trades match paper expectations

## Data Sources

| Data Need | Source | Fallback |
|-----------|--------|----------|
| Candidate screening | Alpaca bars (if subscribed) | yfinance (prior-day close) |
| Order placement & fills | Alpaca API (broker) | None — authoritative |
| Position & account data | Alpaca API (broker) | None — authoritative |
| VIX level | yfinance | Assumes "elevated" if unavailable |
| Breadth proxy (RSP) | yfinance | Defaults to neutral (50) |

**Note:** yfinance is used for screening only (prior-day closes). All execution-critical data comes from Alpaca. For production, upgrade to Alpaca's paid market data plan to get full SIP real-time data.

## Observability & Attribution

Every trade captures rich metadata for post-hoc analysis and future strategy refinement:

| Field | Purpose |
|-------|---------|
| **Setup type** | Breakout, continuation, or shallow pullback |
| **Relative volume** | Volume vs 20-day average at entry |
| **ATR % of price** | Volatility bucket at entry |
| **Volatility scalar** | Position size adjustment applied |
| **Market regime** | SPY/QQQ regime at time of entry |
| **Slippage** | Planned vs actual fill ($ and bps) |
| **R at exit** | Final R-multiple achieved |
| **Exit reason** | Stop, trail, time stop, or manual |

This data feeds `learning.py` for grouped performance stats (e.g., win rate by setup, avg R by volatility bucket) and will drive future setup-specific ranking once 20–30 trades provide statistical significance.

### AI Review History

Daily AI reviews are saved both as a latest snapshot (`ai_review.json`) and appended to a cumulative history (`ai_review_history.json`, up to 365 days). This allows the learning module to track recommendation trends over time — e.g., "slippage warning appeared 5 times in 30 days" — and measure whether prior recommendations led to improvements.

### Structured Audit Logging

Every significant action is logged to daily JSONL files (`state/logs/YYYY-MM-DD.jsonl`) with:
- Timestamp, bot name, stage, symbol
- Action taken and reason code
- Before/after state snapshots
- Order IDs and error details

Standard reason codes include: `ENTRY_ACCEPTED`, `ENTRY_REJECTED_RELVOL`, `STOP_REPLACED`, `BROKER_STATE_MISMATCH`, `MANUAL_REVIEW_REQUIRED`, etc.

## Disclaimer

This is a trading system for educational and research purposes. It is not financial advice. Past backtest performance does not guarantee future results. Always paper trade extensively before risking real capital.
