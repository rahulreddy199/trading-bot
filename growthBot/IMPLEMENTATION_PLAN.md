# Growth Bot V1 — Implementation Plan

## Existing codebase guidance

The current bot already has useful infrastructure. We do not want a full rewrite unless necessary.

### Reuse mostly as-is
- `common.py`
- `journal.py`

### Modify heavily
- `trade.py`
- `manage.py`

### Replace strategy logic
- `research.py`

## File-by-file expectations

## `common.py`
Keep:
- env loading
- config loading
- JSON helpers
- heartbeat writing
- alerting
- Alpaca HTTP wrappers
- account / orders / positions helpers
- risk position sizing helper

May add:
- helper for selecting risk config by regime
- helper for computing current open portfolio risk
- helper for correlation calculations if convenient

Do not remove:
- live trading guardrails

## `research.py`
Rewrite around growth logic.

### Required responsibilities
1. Load `strategy.json`
2. Load `watchlist.json`
3. Download daily history for all symbols and benchmarks
4. Compute indicators
5. Compute regime mode
6. Filter symbols by liquidity / price / trend eligibility
7. Compute RS scores
8. Rank leaders
9. Detect setup types:
    - breakout
    - continuation
    - shallow pullback
10. Build candidate list
11. Build rejected list with explicit reasons
12. Persist:
- `state/candidates.json`
- `state/candidates.csv`
- `state/rejected.csv`

### Important behavior
- only use prior completed daily bars for signal generation
- output deterministic fields that `trade.py` can consume directly
- include enough fields so `manage.py` can reconstruct stop / phase logic if needed

### Suggested structure
Functions such as:
- `add_indicators(df, strategy)`
- `compute_regime(raw, strategy)`
- `compute_relative_strength(df, spy_df, lookback)`
- `compute_growth_score(df, spy_df, strategy)`
- `detect_breakout(df, strategy)`
- `detect_continuation(df, strategy)`
- `detect_shallow_pullback(df, strategy)`
- `compute_candidate_stop(trigger, setup_low, atr, strategy)`
- `build_candidate_record(...)`

## `trade.py`
Keep the general order placement flow but change strategy assumptions.

### Required responsibilities
1. Load `state/candidates.json`
2. Get account, positions, open orders, clock
3. Cancel stale buy orders
4. Determine regime-specific sizing limits
5. Skip if risk-off or market closed
6. Enforce:
    - max positions
    - max total open risk
    - cash reserve
    - no duplicate symbol exposure
    - correlation cap if candidate metadata provides it
7. Submit buy stop / stop-limit orders with attached stop-loss
8. Persist:
    - `state/orderplan.json`
    - `state/lastorders.json`
    - `state/positiontracking.json`

### Candidate fields expected from research
Each candidate should at least include:
- `symbol`
- `score`
- `setup_type`
- `trigger_price`
- `limit_price`
- `setup_low`
- `atr14`
- `stop_price`
- `r_per_share`
- `regime_mode`

### Notes
- preserve stale-order cancellation behavior
- preserve pending tracking behavior
- prefer stop-limit entry similar to current bot if practical
- no take-profit order leg
- `manage.py` is responsible for exits after fill

## `manage.py`
Keep the stateful recovery/reconciliation approach, but replace conservative exits.

### Required responsibilities
1. Load current positions
2. Load tracking state
3. Reconcile pending -> filled transitions
4. Maintain phase per position:
    - `initial`
    - `protected`
    - `trailing`
    - `exited`
5. Update bars held
6. Update best price since entry
7. Manage protective stops
8. Replace fixed stops with trailing stop when rules trigger
9. Clean up tracking for closed positions
10. Persist:
- `state/positiontracking.json`
- `state/managelog.json`

### Phase behavior
#### Initial
- original stop active
- wait for 1.5R

#### Protected
- at >= 1.5R move stop to trend-friendly protected level

#### Trailing
- at >= 2.5R or after 5 profitable bars, activate trailing stop

### Exit / failure handling
- if position has no stop when it should, recreate one
- if stop replacement fails, log manual review needed
- if old stop cannot be canceled, skip placing a new one and log manual review
- if tracking data missing, try to reconstruct from candidate data or current config

## `journal.py`
Keep mostly as-is.

Optional enhancement:
- include setup type and phase for each open position
- include best gain since entry if available
- include regime mode for the day

## Config files

## `config/strategy.json`
Replace current conservative settings with growth settings.

## `config/watchlist.json`
Replace with growth universe.

## Readme
Update README to describe:
- new strategy philosophy
- universe
- ranking
- entry types
- exit logic
- growth-oriented risk profile
- paper trading first