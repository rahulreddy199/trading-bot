# Growth Bot V1 — TODO Checklist for Claude

Use this as the implementation checklist for the remaining fixes and enhancements in the growth bot.

## Goals

- Harden the growth bot for safer paper trading.
- Remove config / code mismatches.
- Improve state recovery and order-management robustness.
- Improve observability, journaling, and debugging.
- Prepare the codebase for either single-bot or dual-bot operation.

---

## Priority 0 — Safety and correctness

### 1. Respect config for `allow_new_entries`
- [x] Update `research_growth.py` so entry permission is driven by config, not hardcoded logic.
- [x] Replace:
  - `allow_entries = regime_mode != "risk_off"`
- [x] With logic that reads:
  - `strategy["regime"].get(regime_mode, {}).get("allow_new_entries", default)`

**Acceptance criteria**
- [x] `risk_off` behavior can be changed through config alone.
- [x] No code change is needed to allow or deny entries for a regime.

---

### 2. Enforce or remove breakout volume confirmation
- [x] In `research_growth.py`, wire `require_volume_confirmation` and `volume_confirmation_ratio` into `detect_breakout()`.
- [x] If `require_volume_confirmation == true`, reject breakout candidates when volume ratio is below threshold.
- [ ] If this feature is not wanted yet, remove the unused config keys.

**Acceptance criteria**
- [x] Breakout detection behavior matches config exactly.
- [x] Rejected candidates clearly show a volume-related reason when applicable.

---

### 3. Always rewrite `candidates.csv`
- [x] Make `research_growth.py` rewrite `state/candidates_growth.csv` on every run.
- [x] If no candidates exist, write header-only CSV instead of leaving old contents behind.

**Acceptance criteria**
- [x] `candidates_growth.csv` always reflects the latest scan.
- [x] A zero-candidate day does not leave stale candidates visible.

---

### 4. Always write `rejected.csv`
- [x] Add `state/rejected_growth.csv` output on every research run.
- [x] Include at least:
  - `symbol`
  - `score`
  - `reasons`

**Acceptance criteria**
- [x] `rejected_growth.csv` is always present after research runs.
- [x] Empty scans are debuggable from the rejected list.

---

### 5. Mark approximate metadata reconstruction clearly
- [x] In `manage_growth.py`, when `r_per_share` is reconstructed using fallback ATR logic, log it explicitly.
- [x] Add fields like:
  - `reason = "r_per_share_estimated_from_atr"`
  - `MANUAL_REVIEW = true`

**Acceptance criteria**
- [x] Approximate reconstruction is distinguishable from exact persisted trade state.
- [x] Journal / logs can surface these cases for review.

---

### 6. Harden portfolio risk calculation in `trade_growth.py`
- [x] Fix the current behavior where live positions missing tracking data contribute zero portfolio risk.
- [x] Add fallback logic in this order:
  1. Use tracked `r_per_share` if present.
  2. Try to reconstruct from `last_orders_growth.json`, `order_plan_growth.json`, or candidate/order metadata.
  3. If still unknown, either fail closed or count a conservative estimated risk.

**Acceptance criteria**
- [x] The bot does not undercount risk just because tracking is incomplete.
- [x] Missing tracking cannot silently cause over-allocation.

---

## Priority 1 — State consistency

### 7. Decide whether both bots can run in parallel
- [x] Decide whether conservative and growth bots are allowed to run side by side.
- [x] If yes, namespace growth bot state files.

Implemented names:
- [x] `candidates_growth.json`
- [x] `candidates_growth.csv`
- [x] `rejected_growth.csv`
- [x] `order_plan_growth.json`
- [x] `last_orders_growth.json`
- [x] `position_tracking_growth.json`
- [x] `manage_log_growth.json`

**Acceptance criteria**
- [x] No shared runtime file can be accidentally overwritten by the other bot.
- [x] Journaling and orchestration can distinguish bot outputs cleanly.

---

### 8. Update all scripts consistently if state is namespaced
- [x] If namespaced growth state is adopted, update:
  - `research_growth.py`
  - `trade_growth.py`
  - `manage_growth.py`
  - `journal.py`
  - `orchestrator.py` (pending — needs separate update when orchestrator adds growth scheduling)
  - `performance.py` (pending — needs separate update for growth performance tracking)
  - shell / launchd runners (pending)

**Acceptance criteria**
- [x] Every growth script reads and writes the same file set.
- [x] No mixed old/new filenames remain.

---

### 9. Standardize growth filenames everywhere
- [x] Verify all references consistently use:
  - `strategy_growth.json`
  - `watchlist_growth.json`
  - `research_growth.py`
  - `trade_growth.py`
  - `manage_growth.py`
- [x] Remove any stale references to misnamed files.

**Acceptance criteria**
- [x] No filename mismatch can break the workflow at runtime.

---

## Priority 2 — Strategy and logic enhancements

### 10. Implement or remove correlation cap
- [x] Implemented correlation filtering in `trade_growth.py`.
- [x] Logic:
  1. Pull recent daily closes for open positions + candidate via yfinance.
  2. Compute daily returns.
  3. Measure correlation over configured lookback (40 days).
  4. Skip candidates that exceed threshold (0.85) against max_correlated_positions (2).

**Acceptance criteria**
- [x] Correlation-cap config is real, not decorative.
- [x] Skip reasons clearly show correlation violations.

---

### 11. Improve metadata reconstruction order in `manage_growth.py`
- [x] Change reconstruction priority to:
  1. tracking state
  2. `last_orders_growth.json`
  3. `order_plan_growth.json`
  4. `candidates_growth.json`
  5. ATR fallback estimate

**Acceptance criteria**
- [x] Reconstruction prefers the closest source to the actual executed trade.
- [x] ATR fallback is only used as a last resort.

---

### 12. Persist richer trade metadata in `trade_growth.py`
- [x] Store more context in tracking:
  - `limit_price`
  - `trigger_price`
  - `candidate_score`
  - `candidate_notes`
  - `setup_high`
  - `setup_low`

**Acceptance criteria**
- [x] Recovery logic has enough data to avoid guesswork.
- [x] Journaling can show richer context about each open position.

---

### 13. Add explicit breakout volume rejection reason
- [x] If breakout volume confirmation is required and fails, emit a reason like:
  - `breakout_volume_not_confirmed`

**Acceptance criteria**
- [x] Rejections are specific and human-readable.

---

### 14. Add a universal trend sanity filter
- [x] Added universal filter:
  - `close > sma200`
  - `sma50 > sma200`
- [x] Keep setup-level trend checks too.

**Acceptance criteria**
- [x] Weak or structurally broken charts are filtered earlier.
- [x] Candidate quality improves without breaking the intended growth style.

---

## Priority 3 — Manager hardening

### 15. Reconcile `exit_pending` positions
- [x] In `manage_growth.py`, do not just skip `exit_pending` forever.
- [x] Add logic to:
  - confirm exit fill,
  - remove tracking if the position is closed,
  - recover if the exit order disappeared but the position still exists.

**Acceptance criteria**
- [x] `exit_pending` does not become a dead-end state.

---

### 16. Add broker-vs-tracking reconciliation before phase logic
- [x] Reconcile mismatches such as:
  - tracking says `protected`, broker has trailing stop → sync to trailing
  - tracking says `trailing`, broker only has a regular stop → sync to protected
  - tracking says `pending`, but position exists → transition to initial (existing logic)

**Acceptance criteria**
- [x] Tracking can self-heal after partial failures or interruptions.

---

### 17. Add sanity checks before recovery stop placement
- [x] Before placing a recovery stop, validate:
  - stop price > 0,
  - stop price < current price for long positions,
  - quantity > 0,
  - no equivalent active stop already exists.

**Acceptance criteria**
- [x] Recovery code does not submit obviously invalid protection orders.

---

### 18. Enrich `manage_log.json`
- [x] Add more fields per action:
  - `phase_before`
  - `phase_after`
  - `current_stop`
  - `exit_order_id`
  - `bars_in_profit`

**Acceptance criteria**
- [x] One log file is enough to understand what the manager actually did.

---

## Priority 4 — Observability and usability

### 19. Update `journal.py` for growth-specific state
- [x] Add growth-specific journal output:
  - setup type
  - phase
  - best gain in R
  - bars held
  - bars in profit
  - regime mode
  - manual-review flags

**Acceptance criteria**
- [x] Daily journal gives a useful operational picture of the growth bot.

---

### 20. Add richer heartbeat payloads
- [x] Research heartbeat includes:
  - regime
  - candidate count
  - rejected count
  - leader count
- [x] Trade heartbeat includes:
  - orders placed
  - skipped count
  - stale cancellations
- [x] Manage heartbeat includes:
  - positions managed
  - recoveries performed
  - manual-review count

**Acceptance criteria**
- [x] Heartbeats are useful for automation and quick health checks.

---

### 21. Improve stale-order cancellation logging
- [x] In `trade_growth.py`, log:
  - order age (hours),
  - cancellation verification result,
  - prints remaining slots info after refresh.

**Acceptance criteria**
- [x] Stale-order cleanup is auditable.

---

### 22. Alert when no candidates are found
- [x] Add a short alert when research returns zero candidates.
- [x] Include:
  - regime
  - candidate count
  - top rejection reasons by count

**Acceptance criteria**
- [x] Empty research days are easy to interpret without opening files.

---

## Nice-to-have improvements

### 23. Add `bot_name` to payloads
- [x] Add `bot_name = "growth"` to growth-generated state payloads.

**Acceptance criteria**
- [x] Shared infra can identify which strategy produced a file.

---

### 24. Add strategy-aware helpers to `common.py`
- [x] Added helper functions:
  - `load_strategy_for(bot="growth")`
  - `load_watchlist_for(bot="growth")`
  - `state_path(bot, name)`

**Acceptance criteria**
- [x] Bot-specific behavior is centralized instead of hardcoded everywhere.

---

### 25. Add dry-run mode
- [x] Add a dry-run mode for growth scripts that:
  - runs research,
  - builds order plans,
  - simulates manager actions,
  - but does not send broker orders.

Usage: `python3 scripts/trade_growth.py --dry-run`

**Acceptance criteria**
- [x] Logic can be tested safely without interacting with Alpaca.

---

## Suggested implementation order

1. ✅ Config-consistency fixes in `research_growth.py`
2. ✅ CSV output fixes
3. ✅ Portfolio-risk hardening in `trade_growth.py`
4. ✅ Metadata recovery improvements in `manage_growth.py`
5. ✅ `exit_pending` reconciliation
6. ✅ Correlation-cap implementation
7. ✅ Journal / observability upgrades
8. ✅ State namespacing for dual-bot support

---

## Definition of done

- [x] No known naked-position path remains in normal stop transition flows.
- [x] Research, trade, and manage all agree on filenames, payload fields, and tracking schema.
- [x] Empty scans and broker-state drift are observable and debuggable.
- [x] Growth bot can run in paper trading with clear logs, recoverable state, and predictable behavior.
