# AI Swing Trading Bot

An autonomous, Claude-powered swing trading system with **two strategies**: a conservative pullback bot and an aggressive growth/momentum bot. Both scan for setups, place orders via Alpaca, manage positions with phased exits, and learn from performance over time.

## Two Bots, One System

| | **Conservative Bot** | **Growth Bot** |
|---|---|---|
| **Style** | Trend pullback + confirmation | Momentum breakout + continuation |
| **Universe** | 62 symbols across 8 sectors | 27 high-beta growth names |
| **Risk/trade** | 0.5% equity | 0.75% equity |
| **Max positions** | 5 | 5 |
| **Entry** | Pullback to 20 SMA + confirmation candle | Breakout near 20/55-day highs |
| **Setups** | Hammer, engulfing, morning star | Breakout, shallow pullback, continuation |
| **Exit phases** | Initial → breakeven (1R) → trail (2R) | Initial → protected (1.5R) → trail (2.5R) |
| **Trailing stop** | 3.0 × ATR | 3.0 × ATR (tightens at milestones) |
| **Time stop** | None | 10 bars without profit → exit |
| **Cash reserve** | 25% (full risk) | 5% (full risk) |

---

## Growth Bot — Strategy Detail

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

### Growth Watchlist (27 Symbols)

| Sector | Symbols |
|--------|---------|
| **ETFs** | SPY, QQQ, IWM, SMH |
| **Technology** | NVDA, AMD, AVGO, ANET, META, AMZN, MSFT, AAPL, GOOGL, PLTR, MU, CRM, NOW, PANW, CRWD, SNOW, TTD, UBER, SHOP |
| **Communication** | NFLX |
| **Consumer** | TSLA |
| **Materials** | FCX, NUE |

---

## Conservative Bot — Strategy Detail

The conservative bot trades confirmed pullbacks in broad uptrends with strict confirmation requirements.

### Strategy Summary

| Parameter | Value |
|-----------|-------|
| **Universe** | 62 symbols — 14 ETFs + 48 stocks across 8 sectors |
| **Regime filter** | SPY & QQQ both above 50-day AND 200-day SMA |
| **Breadth filter** | RSP above 50-day SMA (mapped to 0-100 score) |
| **VIX filter** | VIX > 30 → forced reduced risk mode |
| **Trend filter** | Price > 50 SMA > 200 SMA |
| **Entry trigger** | Pullback (2-12 days), confirmation candle, stop-limit above candle high |
| **Ranking** | Relative strength vs SPY over 126 days — top 20% only |
| **Stop-loss** | Wider of (candle low − 0.1×ATR) or (entry − 2×ATR) |
| **Exit phases** | Initial → breakeven at 1R → trailing at 2R (3.0×ATR trail) |
| **Earnings** | No entries within 7 days of earnings |

### Risk Management (Conservative)

| Parameter | Full Risk | Reduced Risk |
|-----------|-----------|--------------|
| Risk per trade | 0.5% | 0.5% |
| Max positions | 5 | 4 |
| Max portfolio risk | 3% | 3% |
| Cash reserve | 25% | 40% |
| Max ATR % | 6% | 6% |

**Sector limits**: Technology 55%, Financials/Healthcare/Industrials 35%, Consumer/Communication 30%, Energy 25%, Materials 20%.

### Conservative Watchlist (62 Symbols)

| Sector | Symbols |
|--------|---------|
| **Broad Market ETFs** | SPY, QQQ, IWM, MDY |
| **Sector ETFs** | XLK, SMH, XLI, XLF, XLV, XLE, XLC, XLY, XLB, XLP |
| **Technology** | AAPL, MSFT, GOOGL, NVDA, AMZN, META, AVGO, AMD, ANET, CRM, NOW, ORCL, PANW, ADBE, SNPS, KLAC |
| **Healthcare** | LLY, UNH, ISRG, ABT, TMO |
| **Financial Services** | JPM, V, MA, GS, AXP, BLK |
| **Consumer** | HD, COST, TJX, NKE, SBUX |
| **Industrials** | CAT, GE, URI, ETN, DE, PWR, WM |
| **Communication** | NFLX, SPOT, DIS, TMUS |
| **Energy** | XOM, CVX, SLB |
| **Materials** | FCX, NUE |

---

## How The Bot Works

### State Isolation

Each bot has its own namespaced state directory (`state/conservative/`, `state/growth/`) to prevent cross-bot file collisions. Shared data like equity curves, performance stats, and AI review history lives in `state/shared/`. A `state_path(bot, name)` helper ensures consistent path resolution.

### Daily Cycle (Fully Autonomous)

| Time (ET) | What Happens |
|-----------|-------------|
| **8:00 AM** | 💓 Heartbeat alert — confirms bot is alive |
| **9:35 AM** | 🌅 **Morning run**: scan watchlist → filter candidates → place stop-limit orders |
| **10:30 AM** | 📈 **Growth manage run**: intraday phase checks (idempotent) |
| **1:00 PM** | 📈 **Growth manage run**: intraday phase checks (idempotent) |
| **4:05 PM** | 🌆 **Afternoon run**: full manage (both bots) → performance → journal |
| **5:00 PM** | 💾 **Daily backup**: local + S3 (if configured) |
| **Saturday 10 AM** | 📊 **Weekly review**: analyze performance → propose parameter tweaks |

### Running a Specific Bot

```bash
# Growth bot only
python scripts/orchestrator.py morning growth
python scripts/orchestrator.py afternoon growth

# Conservative bot only
python scripts/orchestrator.py morning conservative
python scripts/orchestrator.py afternoon conservative

# Both bots (default)
python scripts/orchestrator.py morning
python scripts/orchestrator.py afternoon
```

### Three-Phase Exit System

**Conservative:**
1. Initial stop → Breakeven at 1R → Trailing at 2R (3.0×ATR)

**Growth:**
1. Initial stop → Protected at 1.5R → Trailing at 2.5R (3.0×ATR) + time stop at 10 bars

### Self-Learning System

- `learning.py` analyzes win rate, avg R, exit reasons, and profit factor
- Proposes parameter adjustments based on statistical patterns
- **Safety**: min 30 trades before any tuning, max 2 parameters changed per week, bounded step sizes, strategy snapshots before every change

## Project Structure

```
config/
  strategy.json            # Conservative bot parameters
  strategy_growth.json     # Growth bot parameters
  watchlist.json           # Conservative watchlist (62 symbols)
  watchlist_growth.json    # Growth watchlist (27 symbols)
  guardrails.json          # Safety bounds for auto-tuning
scripts/
  orchestrator.py          # Claude-powered autonomous agent (the brain)
  research.py              # Conservative: market scan + candidate selection
  research_growth.py       # Growth: momentum scan + candidate selection
  trade.py                 # Conservative: order placement
  trade_growth.py          # Growth: order placement
  manage.py                # Conservative: position management
  manage_growth.py         # Growth: position management
  learning.py              # Performance analysis + tuning proposals
  strategy_manager.py      # Safe parameter changes with snapshots
  journal.py               # Daily journal writer
  performance.py           # Performance metrics calculator
  slack_bot.py             # Slack command interface (/positions, /sell, /summary)
  common.py                # Shared utilities (API, sizing, alerts) + compatibility facade
  reconcile.py             # Broker-vs-local state reconciliation
  healthcheck.py           # System health checks
  backup.sh                # Local + S3 backup script
  analytics/               # Analytics pipeline
    pipeline.py            # Daily analytics orchestrator
    metrics.py             # Performance metrics computation
    attribution.py         # Setup-level and grouped attribution
    ai_review.py           # AI review recommendations (daily + cumulative history)
    reports.py             # Daily report generation
    regime.py              # Market regime analysis
    experiments.py         # A/B experiment tracking
  growth/                  # Growth bot modules (refactored)
    decisions.py           # Pure phase-transition decision logic (no broker calls)
    broker_exec.py         # Broker execution helpers (cancel, replace, submit)
    recovery.py            # Metadata reconstruction and recovery helpers
  infra/                   # Infrastructure modules (refactored from common.py)
    paths.py               # Path resolution helpers
    jsonio.py              # JSON file I/O
    logging_utils.py       # Structured JSONL logging
    locks.py               # Job locks and deduplication
    dedupe.py              # Order deduplication
    broker.py              # Alpaca API helpers
    env.py                 # Environment and config loading
    time_utils.py          # Timezone and market-time helpers
    sizing.py              # Position sizing helpers
    config.py              # Config loading
    alerts.py              # Slack/webhook alert helpers
  backtest/                # Backtesting modules
    growth.py              # Growth bot backtester
    conservative.py        # Conservative bot backtester
    matrix.py              # Multi-variant backtest runner
  tests/                   # Test suite
    test_analytics.py      # Analytics pipeline tests
    test_recovery.py       # Recovery and reconciliation tests
state/                     # Runtime state (auto-populated)
  conservative/            # Conservative bot state files
  growth/                  # Growth bot state files
  shared/                  # Shared analytics, equity curve, AI review history
  locks/                   # Job lock files
  logs/                    # Structured JSONL daily logs
journal/                   # Daily journals (auto-populated)
prompts/                   # Prompt templates
growthBot/                 # Growth bot design docs
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
# Persistent autonomous bot (recommended)
python scripts/orchestrator.py

# Single bot run
python scripts/orchestrator.py morning growth
python scripts/orchestrator.py afternoon growth

# Run scripts manually
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

## Backtest Results

### Conservative Bot (Jan 2024 – May 2026)

| Metric | Value |
|--------|-------|
| Total Return | +30.66% |
| Total Trades | 85 |
| Win Rate | 52.9% |
| Profit Factor | 2.00 |
| Avg R-Multiple | 0.56R |
| Max Drawdown | -6.70% |

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
- **Kill switch** — create `state/KILL_SWITCH` to instantly halt all entries
- **Daily loss breaker** — halts new entries if equity drops >3% from prior close; management continues
- **Portfolio drawdown breaker** — halts new entries if drawdown from equity peak exceeds 15%
- **VIX override** — forces reduced risk when VIX > 30
- **Self-tuning disabled by default** — must be manually enabled after paper validation
- **Idempotency guard** — won't double-place orders if triggered twice
- **Stale data protection** — refuses to execute if research data isn't from today
- **Correlation cap** (growth bot) — blocks concentrated correlated bets
- **Time stop** (growth bot) — exits dead positions after 10 bars
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
