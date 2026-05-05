# AI Swing Trading Bot

An autonomous, Claude-powered swing trading bot that scans for trend-following pullback setups, places orders via Alpaca, manages positions with a three-phase exit system, and learns from its own performance over time.

## What This Does

- **Scans 62 liquid stocks & ETFs** across 8 sectors for pullback entries in confirmed uptrends
- **Market regime filter**: only trades when SPY + QQQ are above their 50-day and 200-day SMAs
- **VIX regime awareness**: automatically reduces risk when VIX > 30
- **Breadth filter**: uses RSP (equal-weight S&P 500) as a market health proxy
- **Ranks candidates** by 6-month relative strength vs SPY (top 20% only)
- **Requires confirmation candles**: hammer, bullish engulfing, or morning star patterns
- **Places stop-limit orders** via Alpaca with attached stop-loss (OTO orders)
- **Three-phase exit system**: initial stop → breakeven at 1R → trailing stop at 2R
- **Autonomous orchestration**: Claude AI runs the daily workflow, sends alerts, and reviews performance weekly
- **Self-tuning**: analyzes trade history and adjusts strategy parameters within strict guardrails
- **Daily backups**: local + optional S3 for disaster recovery

## Strategy Summary

| Parameter | Value |
|-----------|-------|
| **Universe** | 62 symbols — 14 ETFs + 48 stocks across 8 sectors |
| **Timeframe** | Daily candles |
| **Direction** | Long-only (v1) |
| **Regime filter** | SPY & QQQ both above 50-day AND 200-day SMA |
| **Breadth filter** | RSP above 50-day SMA (mapped to 0-100 score) |
| **VIX filter** | VIX > 30 → forced reduced risk mode |
| **Trend filter** | Price > 50 SMA > 200 SMA |
| **Entry trigger** | Pullback (2-12 days), confirmation candle, stop-limit above candle high |
| **Ranking** | Relative strength vs SPY over 126 days — top 20% only |
| **Stop-loss** | Wider of (candle low − 0.1×ATR) or (entry − 2×ATR) |
| **Exit phases** | Initial → breakeven at 1R → trailing at 2R (3.0×ATR trail) |
| **Earnings** | No entries within 7 days of earnings |

## Risk Management

| Parameter | Full Risk Mode | Reduced Risk Mode |
|-----------|---------------|-------------------|
| Risk per trade | 0.5% of equity (conservative start) | 0.5% of equity |
| Max open positions | 5 | 4 |
| Max allocation per symbol | 15% of equity | 15% of equity |
| Max total portfolio risk | 3% | 3% |
| Cash reserve | 25% | 40% |
| Max ATR as % of price | 6% | 6% |

**Sector limits** prevent overconcentration: Technology 55%, Financials/Healthcare/Industrials 35%, Consumer/Communication 30%, Energy 25%, Materials 20%.

**Circuit breaker**: if portfolio drawdown exceeds 15%, all new entries are halted automatically.

## Watchlist (62 Symbols)

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

## How The Bot Works

### Daily Cycle (Fully Autonomous)

| Time (ET) | What Happens |
|-----------|-------------|
| **8:00 AM** | 💓 Heartbeat alert — confirms bot is alive |
| **9:35 AM** | 🌅 **Morning run**: scan watchlist → filter candidates → place stop-limit orders |
| **4:05 PM** | 🌆 **Afternoon run**: manage positions → update performance → write journal |
| **5:00 PM** | 💾 **Daily backup**: local + S3 (if configured) |
| **Saturday 10 AM** | 📊 **Weekly review**: analyze performance → propose parameter tweaks → apply within guardrails |

### Three-Phase Exit System

1. **Phase 1 — Initial Stop**: stop at entry risk level. If price closes below 50-day SMA within 3 bars → early invalidation exit.
2. **Phase 2 — Breakeven**: when profit reaches 1R, stop moves to entry + 0.1×ATR. Free trade.
3. **Phase 3 — Trailing**: when profit reaches 2R, trailing stop activates at 3.0×ATR. Stop only moves up. This lets winners run to 5R, 6R, 7R+.

### Self-Learning System

The bot learns from its own trades:
- `learning.py` analyzes win rate, avg R, exit reasons, and profit factor
- Proposes parameter adjustments based on statistical patterns
- Claude reviews proposals against guardrails before applying
- **Safety**: min 30 trades before any tuning, max 2 parameters changed per week, bounded step sizes, strategy snapshots before every change, one-command rollback

## Project Structure

```
config/
  strategy.json          # All strategy parameters
  watchlist.json         # 62 symbols with sectors
  guardrails.json        # Safety bounds for auto-tuning
scripts/
  orchestrator.py        # Claude-powered autonomous agent (the brain)
  research.py            # Market scan + candidate selection
  trade.py               # Order placement via Alpaca
  manage.py              # Three-phase position management
  learning.py            # Performance analysis + tuning proposals
  strategy_manager.py    # Safe parameter changes with snapshots
  journal.py             # Daily journal writer
  performance.py         # Performance metrics calculator
  backtest.py            # Historical backtester
  common.py              # Shared utilities (API, sizing, alerts)
  backup.sh              # Local + S3 backup script
  run_daily.sh           # Manual cron wrapper (legacy)
state/                   # Runtime state (auto-populated)
journal/                 # Daily journals (auto-populated)
prompts/                 # Prompt templates
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

# For alerts (optional)
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/...

# For cloud backups (optional)
S3_BACKUP_BUCKET=my-trading-bot-backups

# Safety
ALLOW_LIVE_TRADING=false
```

### 3. Run the bot

```bash
# Option A: Persistent autonomous bot (recommended)
python scripts/orchestrator.py

# Option B: Test individual runs
python scripts/orchestrator.py morning     # Run morning scan + trade
python scripts/orchestrator.py afternoon   # Run manage + journal
python scripts/orchestrator.py weekly      # Run learning review

# Option C: Run scripts manually
python scripts/research.py
python scripts/trade.py
python scripts/manage.py
python scripts/journal.py
```

### 4. Deploy on a VPS (recommended for 24/7 operation)
```bash
# On your VPS (Ubuntu/Debian):
git clone <repo> && cd trading-agent-starter
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your keys

# Run as a systemd service:
sudo tee /etc/systemd/system/trading-bot.service << EOF
[Unit]
Description=Trading Bot
After=network.target

[Service]
WorkingDirectory=/path/to/trading-agent-starter
ExecStart=/path/to/trading-agent-starter/venv/bin/python scripts/orchestrator.py
Restart=always
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable trading-bot
sudo systemctl start trading-bot
```

## Backtest Results

Over Jan 2024 – May 2026 (28 months):

| Metric | Value |
|--------|-------|
| Total Return | +30.66% |
| Total Trades | 85 |
| Win Rate | 52.9% |
| Avg R-Multiple | 0.56R |
| Profit Factor | 2.00 |
| Best Trade | +7.2R |
| Worst Trade | -2.0R |
| Max Drawdown | -6.70% |
| Avg Hold Time | 27 days |

## Cost Estimates

| Item | Cost/month |
|------|-----------|
| Cloud VPS (Hetzner/DigitalOcean) | $4-7 |
| Claude API (~8 calls/day) | $2-3 |
| Alpaca paper trading | Free |
| Slack/Discord alerts | Free |
| S3 backups | ~$0.01 |
| **Total** | **~$7-10/month** |

Monthly API spending is capped at $10 (configurable in `guardrails.json`). If budget is exceeded, the bot continues operating in direct mode without AI orchestration.

## Safety & Guardrails

- **Paper trading by default** — live trading requires explicit env var acknowledgement
- **Kill switch (file-based)** — create `state/KILL_SWITCH` to instantly halt all new entries without code edits. Remove the file to resume.
- **Kill switch (config-based)** — set `kill_switch: true` in guardrails.json to halt all tuning
- **Self-tuning disabled by default** — `tuning_enabled: false` in guardrails.json. Must be manually enabled after paper validation confirms fills match expectations
- **Idempotency guard** — trade.py blocks duplicate runs on the same day (won't double-place orders if triggered twice)
- **Stale data protection** — trade.py refuses to execute if research data isn't from today
- **Drawdown circuit breaker** — halts all new entries if drawdown exceeds 15%
- **VIX override** — forces reduced risk mode when VIX > 30
- **Auto-tuning bounds** — every parameter has min/max/step limits; changes are snapshotted and reversible
- **No averaging down, no revenge trading, no extended hours, no holding through earnings**

## Data Sources

| Data Need | Source | Fallback |
|-----------|--------|----------|
| Candidate screening (SMA, ATR, pullback) | Alpaca bars (if subscribed) | yfinance (prior-day close) |
| Order placement & fills | Alpaca API (broker) | None — broker is authoritative |
| Position & account data | Alpaca API (broker) | None — broker is authoritative |
| VIX level | yfinance | Assumes "elevated" if unavailable |
| Breadth proxy (RSP) | yfinance | Defaults to neutral (50) |
| Earnings calendar | yfinance | Fails open (no blackout applied) |

**Note:** yfinance is used for screening only (prior-day closes). All execution-critical data comes directly from Alpaca. For production, upgrade to Alpaca's paid market data plan (Algo Trader Plus) to get full SIP data and eliminate yfinance dependency without strategy changes, assuming the Alpaca provider switch is validated in paper first.

## Pre-Live Checklist

Before deploying with real capital, complete all items:

- [ ] **Paper burn-in**: 4+ weeks with zero unresolved state or execution bugs
- [ ] **OTO → trailing stop flow**: verify manage.py handles 1R (breakeven) and 2R (trailing) transitions correctly on real paper positions
- [ ] **Restart recovery**: kill orchestrator mid-session, restart, confirm state is intact
- [ ] **Stale order cleanup**: let orders sit unfilled for 2+ days, verify auto-cancellation
- [ ] **Broker reconciliation**: compare `position_tracking.json` vs Alpaca positions weekly
- [ ] **Kill switch test**: create `state/KILL_SWITCH`, confirm bot halts entries, remove file, confirm it resumes
- [ ] **Duplicate run test**: run trade.py twice in a row, confirm second run is blocked
- [ ] **Trade reconstruction**: pick any closed trade, reconstruct it fully from `order_plan.json` + `trade_history.json`
- [ ] **Data source upgrade**: upgrade to Alpaca's paid market data plan before live trading
- [ ] **Tuning stays OFF**: keep `tuning_enabled: false` until 30+ live trades match paper expectations

## Dependencies

- `pandas` / `numpy` — data analysis
- `yfinance` — historical price data (free)
- `requests` — Alpaca REST API
- `anthropic` — Claude AI for orchestration
- `schedule` — task scheduling
- `boto3` — S3 backups (optional)
- `python-dotenv` — environment variable management

## Disclaimer

This is a trading system for educational and research purposes. It is not financial advice. Past backtest performance does not guarantee future results. Always paper trade extensively before risking real capital.
