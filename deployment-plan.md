# Deployment Plan — Phased Rollout

## Recommended approach: 3 phases, start local, no rush to cloud

---

## Phase 1: Local Direct Mode (Week 1–2)
**Goal:** Validate the full pipeline works end-to-end on real paper market data.

**What to do:**
```bash
# Run each script manually once per day to verify output
cd ~/Downloads/trading-agent-starter\ 2

# Morning (~9:35 AM ET): scan + place orders
python scripts/research.py
python scripts/trade.py

# Afternoon (~4:05 PM ET): manage exits + record
python scripts/manage.py
python scripts/performance.py
python scripts/journal.py
```

**What to check daily:**
- `state/candidates.json` — are candidates reasonable?
- `state/order_plan.json` — orders placed or skipped with correct reasons?
- `state/manage_log.json` — exits firing at right phases?
- `journal/YYYY-MM-DD.md` — journal makes sense?
- No `MANUAL_REVIEW_REQUIRED` actions in manage_log
- Reconciliation: do positions in Alpaca match `position_tracking.json`?

**Why not orchestrator yet:** You want to see each step's output in real-time, catch any env/config issues, and build confidence before automating.

**Duration:** 5–7 trading days minimum. Stop if you see any state inconsistency.

---

## Phase 2: Local Orchestrator, No AI (Week 3–4)
**Goal:** Let the scheduler run autonomously in direct mode (no Anthropic key needed).

**What to do:**
```bash
# Start the persistent orchestrator (no ANTHROPIC_API_KEY = direct mode)
# It will run research→trade at 9:35 AM, manage→perf→journal at 4:05 PM
python scripts/orchestrator.py
```

Or use launchd for auto-restart:
```bash
cp config/com.tradingbot.orchestrator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tradingbot.orchestrator.plist
```

**What to check:**
- `state/heartbeat_trade.json`, `heartbeat_manage.json` — timestamps current?
- `state/bot_log.json` — run statuses are `success`, not `partial` or `failed`
- Slack/Discord alerts working (if `ALERT_WEBHOOK_URL` configured)
- After a week: do you have paper fills? Are stops placed? Did any hit breakeven/trailing?

**Why no AI yet:** Direct mode runs the exact same scripts in the exact same order. Claude adds flexibility for edge cases but isn't needed for the core loop. This proves the scheduler, PID lock, missed-job recovery, and launchd supervision all work.

**Duration:** 2 weeks. You want to see at least a few complete trade lifecycles (entry → breakeven → trailing or stop-out).

---

## Phase 3: Enable AI + Optional Cloud (Week 5+)
**Goal:** Add Claude for smarter decision-making, then optionally move to cloud.

### 3a. Add Anthropic (still local)
```bash
# Add to .env
ANTHROPIC_API_KEY=your_key
```
That's it. The orchestrator auto-detects the key and switches from direct mode to AI mode. The $10/month budget cap is already enforced. Claude will:
- Decide whether market conditions warrant trading
- Summarize daily activity in alerts
- Run weekly learning analysis (but tuning stays OFF until you enable it)

### 3b. Cloud deployment (optional, only if your Mac isn't always on)

**Cheapest options:**
| Platform | Cost | Notes |
|----------|------|-------|
| Always-on Mac with launchd | $0 | Already configured. Best if machine is reliable. |
| AWS EC2 t3.micro | ~$8/mo | Persistent, runs orchestrator.py as systemd service |
| Railway / Render | ~$5–7/mo | Container-based, good for always-on processes |
| Google Cloud Run | Not ideal | Designed for request-response, not persistent schedulers |

**Cloud is optional** because the bot only needs to be awake during market hours (9:30 AM – 5:00 PM ET). A Mac that's on during the day with launchd is perfectly fine.

---

## What NOT to do
- ❌ Don't go live (real money) until you have 30+ paper trades with consistent results
- ❌ Don't enable `tuning_enabled` until you've reviewed 50+ paper trades
- ❌ Don't deploy to cloud before Phase 2 is stable locally
- ❌ Don't skip Phase 1 — manual runs catch config issues that automated runs hide

## Quick timeline
| Week | Phase | Mode | AI? | Where |
|------|-------|------|-----|-------|
| 1–2 | Phase 1 | Manual scripts | No | Local |
| 3–4 | Phase 2 | Orchestrator direct mode | No | Local (launchd) |
| 5+ | Phase 3a | Orchestrator AI mode | Yes | Local (launchd) |
| When stable | Phase 3b | Same | Yes | Cloud (optional) |

## First step right now
Set up your `.env` file with Alpaca paper credentials and run `research.py` once to see if data fetching works:
```bash
python scripts/research.py
cat state/candidates.json | python -m json.tool | head -50
```

