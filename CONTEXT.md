# Trading Bot - Project Context & Status

## Last Updated: May 3, 2026

## Current Status: Paper-Trading Ready, Cautious Live Ready (v4.5) + Growth Bot V1

## Architecture
```
scripts/
  orchestrator.py   — Claude-powered autonomous agent (scheduler + AI decision loop)
  research.py       — Scans 62 symbols, filters candidates (9:35 AM ET)
  trade.py          — Places OTO stop-limit buy orders (after research)
  manage.py         — Three-phase exit management (4:05 PM ET)
  performance.py    — Tracks closed trades, equity curve, stats
  learning.py       — Analyzes performance, proposes strategy tuning
  strategy_manager.py — Safe parameter changes with snapshots + rollback
  journal.py        — Writes daily markdown journal
  backtest.py       — Historical backtester (no look-ahead bias)
  common.py         — Shared utilities: Alpaca API, sizing, alerts, heartbeat, cancel helpers, order status constants
  research_growth.py — Growth bot: scans momentum universe, ranks leaders, detects setups
  trade_growth.py    — Growth bot: places stop-limit buys for momentum setups
  manage_growth.py   — Growth bot: 3-phase exit management (initial → protected → trailing)
  backup.sh         — Local + S3 backup script
  run_daily.sh      — Legacy cron wrapper (superseded by orchestrator.py)
config/
  strategy.json     — All strategy parameters (including correlation_cap)
  strategy_growth.json — Growth bot strategy parameters (momentum/breakout)
  watchlist.json    — 62 symbols with sectors and type (ETF/stock)
  watchlist_growth.json — Growth bot universe: 30 high-momentum symbols
  guardrails.json   — Safety bounds for auto-tuning
  com.tradingbot.orchestrator.plist — macOS launchd service config
state/
  candidates.json, order_plan.json, position_tracking.json,
  manage_log.json, performance.json, trade_history.json,
  equity_curve.json, api_usage.json, bot_log.json,
  learning_analysis.json, tuning_log.json,
  tuning_weekly_counter.json, last_run_times.json,
  heartbeat_research.json, heartbeat_trade.json,
  heartbeat_manage.json, heartbeat_journal.json,
  heartbeat_performance.json,
  orchestrator.pid
journal/
  Daily markdown logs (auto-generated)
```

## Strategy (v2 — Current)
- **Style:** Long-only swing trading, daily timeframe
- **Universe:** 62 symbols — 14 ETFs + 48 stocks across 8 sectors
- **Regime filter:** SPY + QQQ both above 50-day AND 200-day SMA
- **Breadth filter:** RSP proxy → full risk (≥60%), reduced (40-60%), risk-off (<40%)
- **VIX filter:** VIX > 30 → forced reduced risk mode
- **Entry:** Confirmation candle (hammer/bullish engulfing/morning star) after 2-12 day pullback near 20 SMA
- **Entry order:** Stop-limit buy above candle high with ATR-based buffers
- **Stop:** Wider of (candle low − 0.1×ATR) or (entry − 2×ATR)
- **Exit phases:** Initial stop → Breakeven at 1R → Trailing stop (3.0×ATR) at 2R
- **Early invalidation:** Close below 50 SMA within 3 bars → cancel stops, exit at market
- **Ranking:** Relative strength vs SPY over 126 days — top 20% only (configurable via min_relative_strength_percentile)
- **Confirmation candles:** Configured in strategy.json, currently: hammer, bullish_engulfing, morning_star
- **Earnings blackout:** No entries within 7 days of earnings (stocks only, ETFs exempt via watchlist type)

## Growth Bot V1 (growth_momentum_v1)
- **Style:** Aggressive growth/momentum swing trading, daily timeframe
- **Universe:** ~30 symbols — 4 ETFs + 26 stocks (tech/momentum-heavy)
- **Regime filter:** SPY + QQQ both above 50-day SMA (full_risk / reduced_risk / risk_off)
- **Ranking:** Composite score: RS vs SPY (3m 50%, 6m 30%) + trend strength (20%), top 25% qualify
- **Setups detected:**
  1. **Breakout** — price near 20d/55d high, above all major MAs
  2. **Continuation** — 1-3 bar shallow pullback after recent breakout, green close
  3. **Shallow pullback** — within 1.5 ATR of 20d swing high, above SMA20/50/200
- **Entry:** Stop-limit buy above setup high (trigger_buffer=0.05×ATR, limit_buffer=0.15×ATR)
- **Stop:** Wider of (setup_low − 0.2×ATR) or (entry − 2.5×ATR)
- **Exit phases:** Initial → Protected at 1.5R → Trailing (3×ATR) at 2.5R or 5 bars in profit
- **Time stop:** Exit after 10 bars if < 0.5R progress
- **Risk:** Full risk mode: 0.75%/trade, 5 positions, 25% max per symbol; Reduced: 0.4%/trade, 3 positions
- **Filters:** Min price $20, min avg dollar volume $25M, max ATR 8%
- **Correlation cap:** 0.85 threshold, 40-day lookback, max 2 correlated positions

## Risk Parameters (Conservative Start)
| Mode | Risk/Trade | Max Positions | Cash Reserve | Portfolio Risk Cap |
|------|-----------|---------------|-------------|-------------------|
| Full Risk | 0.5% | 5 | 25% | 3% |
| Reduced Risk | 0.5% | 4 | 40% | 3% |
| Risk Off | 0% | 0 | — | — |

- Max allocation per symbol: 15%
- Max ATR as % of price: 6%
- Sector limits: Tech 55%, Financials/Healthcare/Industrials 35%, Consumer/Communication 30%, Energy 25%, Materials 20%

## Correlation Cap (Portfolio Diversification)
- **Enabled by default** in strategy.json
- **Lookback:** 40 trading days rolling window
- **Threshold:** 0.80 — blocks when candidate is highly correlated with existing/pending positions
- **Max correlated positions:** 2 — allows 0 or 1 highly correlated existing names, blocks at 2+
- **Date-aligned returns:** Uses `pd.concat(join="inner")` on date-indexed Series (not raw array position)
- **Per-run caching:** `_corr_returns_cache` and `_corr_matrix_cache` avoid refetching for every candidate
- **Adjusted prices:** Yahoo fallback uses `auto_adjust=True` for corporate-action-correct returns
- **Configurable fail behavior:** `fail_open_on_data_error: true` — allows trade on data failure (configurable to fail-closed)
- **Journaling:** Accepted orders log `correlation_checked`, `correlated_count`, `correlated_with`; blocked trades log full details

## Backtest Results (Jan 2024 – May 2026, $100K)
| Metric | Value |
|--------|-------|
| Total Return | +30.66% |
| Total Trades | 85 (~3/month) |
| Win Rate | 52.9% |
| Avg R-Multiple | 0.56R |
| Profit Factor | 2.00 |
| Best Trade | +7.2R |
| Worst Trade | -2.0R |
| Max Drawdown | -6.70% |
| Avg Hold Time | 27 days |

## Safety & Guardrails
- **Paper trading by default** — live requires ALLOW_LIVE_TRADING=true + acknowledgement
- **Kill switch (file-based)** — `touch state/KILL_SWITCH` halts all entries instantly
- **Kill switch (config)** — `kill_switch: true` in guardrails.json halts all tuning
- **Self-tuning disabled** — `tuning_enabled: false` by default, requires 30+ trades + cooldown
- **Weekly tuning limit** — max 2 parameter changes per ISO week, enforced in code (not just prompt)
- **Idempotency guard** — trade.py blocks duplicate runs on same day
- **Idempotent order submission** — deterministic client_order_id (date+symbol+side hash), broker-side duplicate check before submission
- **Idempotent manage.py orders** — deterministic client_order_id (date+symbol+phase hash) for all stop/trailing/market exits
- **Safe POST retry** — `_request_with_retry()` reconciles by client_order_id before retrying POST requests after connection/5xx errors
- **Stale data protection** — trade.py refuses if research data isn't from today
- **Circuit breaker** — enforced in both orchestrator AND trade.py directly, halts at 15% drawdown
- **Trade time-window guard** — orders blocked outside 9:30–11:00 AM in both AI and direct mode
- **Single-instance PID lock** — orchestrator.pid prevents duplicate processes (all entry points)
- **VIX override** — VIX > 30 forces reduced risk regardless of breadth
- **Timezone-safe** — all date/time logic uses `America/New_York` via `MARKET_TZ` (ZoneInfo), no naive `datetime.now()`
- **Per-job heartbeats** — each job writes `heartbeat_{name}.json` (no single-writer-wins race)
- **Centralized cancel helpers** — `cancel_order()` (retry-aware, 404/422 tolerant) and `cancel_order_and_verify()` in common.py, shared by trade.py and manage.py
- **Canonical active-order statuses** — `ACTIVE_ORDER_STATUSES` in common.py includes `pending_new`, `pending_cancel`, `pending_replace`; used everywhere
- **Deferred cleanup protection** — when `_record_closed_trade()` returns "deferred", tracking is preserved for next run (not deleted)
- **No averaging down, no revenge trading, no extended hours, no holding through earnings**

## Order Tracking (client_order_id)
All orders include deterministic `client_order_id` for exact broker reconciliation and idempotency:
- Entry: `bot_buy_{SYMBOL}_{YYYYMMDD}_{sha256_hash}` (trade.py — deterministic per date+symbol)
- Stop: `bot_stop_{SYMBOL}_{YYYYMMDD}_{sha256_hash}` (manage.py — deterministic per date+symbol+phase)
- Trailing: `bot_trail_{SYMBOL}_{YYYYMMDD}_{sha256_hash}` (manage.py — deterministic per date+symbol)
- Market exit: `bot_exit_{SYMBOL}_{YYYYMMDD}_{sha256_hash}` (manage.py — deterministic per date+symbol+reason)

### Order Idempotency
- Entry orders use deterministic client_order_id derived from `date + symbol + side`
- Exit orders use deterministic client_order_id derived from `date + symbol + phase`
- Before submission, `check_duplicate_order()` queries broker by client_order_id
- Only orders in `ACTIVE_ORDER_STATUSES` are treated as duplicates — canceled/rejected are ignored
- If duplicate detected, response tagged `_duplicate: true` and main() skips all local accounting (cash, risk, slots, tracking)
- Alpaca rejects duplicate client_order_ids natively as a third layer of protection
- POST retry in `_request_with_retry()` reconciles by client_order_id before retrying, preventing ghost duplicates

IDs persisted in: position_tracking.json → _record_closed_trade() → trade_history.json
Performance.py dedup uses sell_order_id + client_order_id (no symbol/date heuristics)

### Canonical exit fields (always current)
- `exit_order_id` — broker order ID of the active protective sell
- `exit_client_order_id` — client-generated ID for exact Alpaca lookup
- `exit_order_type` — one of: `stop_initial`, `stop_breakeven`, `trailing_stop`, `market_early_invalidation`
- Updated on every sell order transition (OTO child → breakeven → trailing → recovery)

### _record_closed_trade lookup order
1. Exact: `GET /v2/orders/{exit_order_id}`
2. Exact: `GET /v2/orders:by_client_order_id?client_order_id=...`
3. Strict fallback: scan recent filled sells matching symbol + side + qty (no loose symbol-only match)
- `closed_at` uses broker's `filled_at` (not `now_iso()`)
- Returns status: `recorded`, `deferred`, `no_exit_price`
- `exit_price_source` field tracks provenance: `exact_id`, `fallback_qty_match`, or `missing`
- `deferred` status preserves tracking entry for next-run reconciliation (not deleted)

## Exit State Machine
```
pending → initial → breakeven → trailing → (position closed by trailing stop)
                  ↓                         ↓
            exit_pending              exit_pending
                  ↓                         ↓
           (position gone from broker → _record_closed_trade → cleanup)
```
- `exit_pending` used instead of `exited` — position stays managed until broker confirms gone
- `bars_held` increments once per trading day only (idempotent via last_bar_date)

## Data Sources
| Data Need | Primary | Fallback |
|-----------|---------|----------|
| Candidate screening | Alpaca bars (if paid plan) | yfinance (prior-day close) |
| Order placement & fills | Alpaca API | None — broker authoritative |
| Position & account data | Alpaca API | None — broker authoritative |
| VIX level | yfinance | Assumes "elevated" if unavailable |
| Breadth (RSP) | yfinance | Defaults to neutral (50) |
| Earnings calendar | yfinance | Fails open (no blackout) |

- Alpaca data feed configurable via `ALPACA_DATA_FEED` env var (default: iex)
- ETF vs stock detection is data-driven from watchlist.json `type` field

## Autonomous Operation (orchestrator.py)
- **Morning (9:35 AM ET):** research → trade → alert
- **Afternoon (4:05 PM ET):** manage → performance → journal → alert
- **Saturday (10:00 AM):** learning review → tuning proposals (disabled until tuning_enabled=true)
- **Daily 8:00 AM:** heartbeat alert
- **Daily 5:00 PM:** backup (local + S3 if configured)
- Falls back to direct mode (no AI) if ANTHROPIC_API_KEY not set or budget exceeded
- API budget capped at $10/month, auto-tracked in state/api_usage.json
- **Process supervision:** macOS launchd plist for auto-restart, log capture, run-at-load
- **PID locking:** single-instance guard on ALL entry points (daemon + CLI)
- **Missed-job recovery:** catches up same-day missed jobs on restart; failed runs retry
- **Trade window enforcement:** catch-up morning runs outside 9:30–11:00 AM run research-only
- **Structured run status:** each run records success/partial/failed — not just timestamp
- **Tool failure tracking:** agent loop tracks individual tool failures for accurate status
- **Post-action reconciliation:** local tracking vs broker positions compared after trade/manage, alerts on mismatch
- **Startup self-check:** verifies config files, env vars, writable dirs, API connectivity before entering schedule loop
- **Compact run summaries:** log_event("run_complete") includes summarize_result() for behavior trend analysis

## Operational Controls (v3.5)
| Control | Enforcement Layer | Notes |
|---------|------------------|-------|
| Trade time window | `handle_tool_call` + `run_direct_mode` | 9:30–11:00 AM only |
| Weekly tuning limit | `handle_tool_call` (code, not prompt) | Max 2/week via counter file |
| Single instance | `setup_instance_lock()` at `__main__` | PID file + process check, called once |
| Run status tracking | `record_run(status=)` | success/partial/failed |
| Missed-job retry | `ran_successfully_today()` | Failed runs eligible for catch-up |
| Circuit breaker | orchestrator + trade.py | Dual enforcement, 15% drawdown |
| Drawdown halt | Both AI and direct paths | Cannot be bypassed |
| Research-only catch-up | `morning_run(allow_trade=False)` | Tool whitelist removes `run_trade` from agent |
| Idempotent orders | deterministic ID + broker check + `_duplicate` branch | Local accounting untouched on duplicates |
| Post-action reconciliation | `reconcile_after_action()` | 4-way: pending↔buys, open↔positions, exits↔sells, untracked |
| Reconciliation→status | morning/afternoon runs | `match:False` downgrades run to `partial` |
| Startup self-check | `startup_self_check()` | Config, env vars, writable dirs, API health; fatal on missing keys |

## Centralized Infrastructure (common.py)
| Component | Purpose |
|-----------|---------|
| `MARKET_TZ` | `ZoneInfo("America/New_York")` — all date/time logic |
| `ACTIVE_ORDER_STATUSES` | Canonical tuple for all broker-state checks |
| `cancel_order()` | Retry-aware DELETE, 404/422 tolerant |
| `cancel_order_and_verify()` | Cancel + poll to confirm no longer active |
| `_request_with_retry()` | Exponential backoff; POST reconciles by client_order_id |
| `_reconcile_by_client_id()` | Checks if order exists at broker before POST retry |
| `write_heartbeat()` | Per-job files: `heartbeat_{name}.json` |
| `today_str()` / `now_iso()` | Timezone-aware (America/New_York) |

## Strategy Manager Guardrails
- `tuning_enabled: false` by default — must be manually enabled
- `min_trades_before_tuning: 30` — enforced in code, not just config
- `tuning_cooldown_weeks: 2` — only counts log entries with actual applied changes
- `max_parameters_changed_per_cycle: 2`
- Every parameter has min/max/step bounds
- Strategy snapshots saved before every change
- One-command rollback: `python -c "from strategy_manager import rollback; rollback()"`

## .env Variables
```
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_FEED=iex          # Set to "sip" with paid Alpaca data plan
ALLOW_LIVE_TRADING=false
ANTHROPIC_API_KEY=your_key    # Optional — runs in direct mode without it
ALERT_WEBHOOK_URL=            # Slack/Discord webhook (optional)
S3_BACKUP_BUCKET=             # For cloud backups (optional)
TIMEZONE=America/New_York
```

## Running the Bot
```bash
# Recommended: autonomous persistent bot (with PID lock)
python scripts/orchestrator.py

# macOS launchd service (auto-restart, log capture)
cp config/com.tradingbot.orchestrator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tradingbot.orchestrator.plist

# Test individual runs (also acquires PID lock)
python scripts/orchestrator.py morning
python scripts/orchestrator.py afternoon
python scripts/orchestrator.py weekly

# Run scripts directly (no orchestration)
python scripts/research.py
python scripts/trade.py
python scripts/manage.py
python scripts/performance.py
python scripts/journal.py

# Backtest
python scripts/backtest.py
```

## Pre-Live Checklist
- [ ] Paper burn-in: 4+ weeks, zero unresolved bugs
- [ ] OTO → trailing stop flow validated on real paper positions
- [ ] Restart recovery: kill orchestrator, restart, state intact
- [ ] Stale order cleanup: orders auto-cancelled after 2 days
- [ ] Broker reconciliation: tracking matches Alpaca positions
- [ ] Kill switch test: create/remove state/KILL_SWITCH
- [ ] Duplicate run test: trade.py blocks second run same day
- [ ] Trade reconstruction from logs in <5 minutes
- [ ] Upgrade to Alpaca paid data plan
- [ ] Tuning stays OFF until 30+ live trades match paper

## Reviewer Feedback History
- v1.0: 6.0/10 — basic scanner + order submitter
- v1.5: 7.5/10 — added manage.py, fixed order safety
- v2.0: 8.5/10 — confirmation candles, breadth, no partials, learning module
- v2.1: 9.0/10 — VIX filter, circuit breaker, exit reason tracking
- v2.2: 9.0/10 — conservative sizing (0.5%), kill switch, idempotency, decision logging
- v2.3: 9.0/10 — strategy_manager enforces min_trades/cooldown, manage.py exit hygiene, ETF data-driven, config wired
- v2.4: 9.0/10 — client_order_id on all orders, exit_pending state, bars_held idempotent, dedup by order ID
- v2.5: — _record_closed_trade writes sell_order_id/client_order_id, full reconciliation chain
- v2.6: — canonical exit IDs on ALL paths (OTO child, breakeven, trailing, early exit), trailing_already_exists refreshes exit IDs, exact client_order_id lookup via Alpaca endpoint, closed_at uses broker filled_at
- v2.7: — exact lookups validate symbol match, unfilled exit orders defer recording instead of falling through to symbol scan
- v2.8: — orchestrator retry logic (2 retries per script), independent afternoon steps, morning skips trade if research fails
- v2.9: 8.8/10 — weekly tuning counter (code-enforced), launchd plist, missed-job detection, backup error handling, scheduler exception protection
- v3.0: 8.9/10 — hard trade-window guard in handle_tool_call, single-instance PID lock, status-aware run recording, Saturday/Sunday prompt fix
- v3.1: 8.9/10 — trade-window guard in direct mode, status-aware missed-job retry (failed runs catch up), structured workflow results from run_agent_loop (tool failure tracking), PID lock on CLI entry points, morning_run(allow_trade=False) for research-only catch-up
- **v3.2: 9.1/10** — fixed double-lock bug (setup_instance_lock only at `__main__`), tool whitelisting in run_agent_loop (allow_trade=False now hard-removes run_trade from agent tools)
- **v3.3: 9.2/10** — deterministic client_order_id for idempotent order submission with broker-side dedup, post-action reconciliation (local vs broker after trade/manage), startup self-check (config/env/API), removed dead code (call_claude, unused timedelta), uniform direct mode returns, compact result summaries in run logs
- **v3.4: 9.4/10** — fixed duplicate-order accounting bug (_duplicate responses no longer mutate local cash/risk/slots/tracking), 3-way reconciliation (local tracking vs broker positions vs broker open orders), check_duplicate_order only blocks on active orders (not canceled/rejected), startup self-check now fatal for missing Alpaca keys/venv, removed unused uuid import
- **v3.5: 9.6/10** — reconciliation result wired into run status (match:False downgrades to partial), `exited` phase excluded from local_open (prevents false alerts after exits), 4-way reconciliation checks open positions have protective sell orders, reconciliation result persisted in run_complete log
- **v3.6: 9.6/10** — broker_open_sells in reconciliation log, reconciliation errors logged as warnings
- **v3.7: 9.6/10** — reconciliation errors (match:None) now downgrade run to partial (not just warning), ensuring missed-job retry catches incomplete verification
- **v3.8** — correlation cap implementation: date-aligned returns, per-run caching, adjusted Yahoo fallback, configurable fail-open/fail-closed, correlation details in order journal
- **v3.9** — removed "filled" from duplicate-order blocking in trade.py, removed unused numpy import
- **v4.0** — manage.py breakeven stop churn prevention (skip cancel/replace if existing stop ≥ breakeven price), initial phase exit_order_id reconciliation (recovers lost OTO child stops)
- **v4.1** — tightened `_record_closed_trade()` fallback (requires symbol+side+qty match, not loose symbol-only), returns status string, `exit_price_source` field for audit, recording warnings surfaced in manage_log
- **v4.2** — idempotent manage.py orders (deterministic client_order_id per phase), safe POST retry with client_order_id reconciliation, expanded `ACTIVE_ORDER_STATUSES` (includes `pending_cancel`/`pending_replace`), deferred cleanup bug fix (tracking preserved when recording deferred), per-job heartbeat files, timezone-safe helpers (`MARKET_TZ`), cancel_order retry + 404/422 tolerance
- **v4.3** — centralized `ACTIVE_ORDER_STATUSES`, `cancel_order()`, and `cancel_order_and_verify()` in common.py (single source of truth), trade.py uses shared cancel helpers (no more raw DELETE), `check_duplicate_order()` uses canonical `ACTIVE_ORDER_STATUSES`, journal.py runtime bug fixed (`lines` initialized before use)
- **v4.4** — all cancel/replace paths now use `cancel_order_and_verify()`: early invalidation verifies all stops cancelled before market sell (aborts with MANUAL_REVIEW if not), initial→breakeven verifies old stops cancelled before placing new one, breakeven→trailing uses verified cancel (removes raw sleep-and-hope), trade.py `cancel_stale_orders` uses verified cancel with `cancel_verified` field in result, removed unused `symbol` param from `cancel_order_and_verify()`
- **v4.5 (current)** — `cancel_order_and_verify()` no longer returns True on transient errors (only on explicit non-active broker status or 404 not-found), `_request_with_retry()` catches `Timeout`/`ReadTimeout` and reconciles by client_order_id before retrying POSTs (matches Alpaca's documented ambiguous-timeout behavior)

### Reviewer assessment (v3.7) — Multi-reviewer consensus
Three independent reviews completed. Scores: **9.5/10**, **9.6/10**, **9.7/10**.

| Reviewer | Score | Key Quote |
|----------|-------|-----------|
| Reviewer A | 9.5/10 | "Professional-grade autonomous trading system. Every critical failure mode has at least one layer of defense, most have two or three." |
| Reviewer B | 9.6/10 | "Production-shaped for paper trading. Remaining issues are normal operational edge cases, not design flaws." |
| Reviewer C | 9.7/10 | "One of the strongest retail systematic trading setups I've seen. No longer a hobby project — a well-engineered systematic trading operation." |

**Component scores (Reviewer A):**
| Component | Score |
|-----------|-------|
| Architecture | 9.8/10 |
| Risk Management | 9.8/10 |
| Research/Entry | 9.3/10 |
| Exit Management | 9.5/10 |
| Learning/Tuning | 9.0/10 |
| Operational | 9.5/10 |

**What reviewers highlighted:**
- Layered idempotency (PID lock → trade window → order_plan guard → deterministic client_order_id → broker dedup → _duplicate branch)
- 4-way reconciliation (pending↔buys, open↔positions, positions↔sells, untracked detection)
- Kill switch + circuit breaker enforced in both orchestrator AND trade.py independently
- exit_pending phase preventing lost trade records during fill timing gaps
- bars_held idempotency via date-based tracking
- Trailing stop recovery when broker-side protection disappears
- Startup self-check preventing silent degraded operation
- `partial` runs eligible for retry — safe due to multi-layer idempotency

**Operational note:** `partial` runs are eligible for same-day catch-up on restart — safe because trade.py has deterministic client_order_id + order_plan.json guard, and manage.py has broker-side recovery logic

### Pre-paper-trading recommendations (reviewer consensus)
1. Paper trade **8–12 weeks minimum**, focus on fill quality and slippage
2. Keep `tuning_enabled: false` until **50+ real paper trades** with consistent results
3. Monitor avg R captured in paper vs backtest
4. Monitor frequency of reconciliation mismatches
5. Track whether weekly tuning proposals are sensible before enabling

### Top remaining gaps (priority order)
1. Extended paper burn-in with fill quality/slippage tracking
2. No re-entry mechanism for stopped-out winners that resume trending (design choice, monitor in exit reason analysis after 30+ trades)
3. Strategy performance comparison across versions (tag trades with active strategy version at entry)
4. Broker notification webhooks for near-real-time fill reaction (not necessary for daily swing trading)
5. Run-level state files (run_intent.json / run_result.json) for safe retry distinction

### Future enhancements (nice-to-have)
- Paper trading monitoring dashboard (Streamlit/Gradio)
- Alert message refinements
- Live transition plan
- Websocket streaming for order updates

