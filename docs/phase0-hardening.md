# Phase 0 — Production Hardening

## Objective
Make the bot operationally reliable, deterministic, auditable, and safe before adding AI layers.

## Success Criteria
After multi-week paper run:
- [ ] No duplicate or orphaned orders
- [ ] No silent state corruption
- [ ] No bot confusion between conservative and growth flows
- [ ] Every important action is reconstructable from logs and state files

## Architecture Changes

### State Directory Structure
```
state/
├── conservative/          # Conservative bot execution state
│   ├── candidates.json
│   ├── order_plan.json
│   ├── last_orders.json
│   ├── position_tracking.json
│   └── manage_log.json
├── growth/                # Growth bot execution state
│   ├── candidates.json
│   ├── order_plan.json
│   ├── last_orders.json
│   ├── position_tracking.json
│   └── manage_log.json
├── shared/                # Cross-bot aggregated state
│   ├── performance.json
│   ├── equity_curve.json
│   ├── health_summary.json
│   └── heartbeat_*.json
├── locks/                 # Job locks and receipts
│   ├── growth_trade.lock
│   ├── growth_trade_receipt.json
│   └── ...
├── logs/                  # Structured JSONL event logs
│   ├── 2026-05-05.jsonl
│   └── ...
└── (legacy flat files)    # Backward compat during migration
```

### Path Resolution
```python
from common import state_path, resolve_state

# New code uses namespaced paths:
path = state_path("growth", "candidates.json")  # → state/growth/candidates.json

# During migration, resolve_state checks new path first, falls back to legacy:
path = resolve_state("growth", "candidates.json")
```

## Data Schemas

### position_tracking.json (per position)
```json
{
  "NVDA": {
    "symbol": "NVDA",
    "bot": "growth",
    "phase": "trailing",
    "entry_date": "2026-05-01",
    "entry_price": 135.50,
    "qty": 15,
    "r_per_share": 4.20,
    "atr_at_entry": 3.80,
    "setup_type": "breakout",
    "stop_order_id": "abc123",
    "trail_order_id": "def456",
    "highest_close": 148.30,
    "bars_held": 7,
    "last_reconciled_at": "2026-05-05T16:05:00-04:00",
    "manual_review": false,
    "version": 2
  }
}
```

### Job Receipt (state/locks/*_receipt.json)
```json
{
  "job_name": "growth_trade",
  "bot": "growth",
  "stage": "trade",
  "date": "2026-05-05",
  "run_at": "2026-05-05T09:35:12-04:00",
  "input_hash": "a1b2c3d4e5f6g7h8",
  "status": "completed",
  "orders_submitted": 2,
  "dedupe_hits": 0,
  "errors": [],
  "warnings": []
}
```

### Health Summary (state/shared/health_summary.json)
```json
{
  "date": "2026-05-05",
  "generated_at": "2026-05-05T16:10:00-04:00",
  "heartbeat_status": {...},
  "job_statuses": {...},
  "manual_review_count": 0,
  "unmanaged_positions": [],
  "reconciliation_fixes_today": 0,
  "dedupe_hits_today": 0,
  "stale_files": [],
  "overall_health": "healthy",
  "issues": []
}
```

### JSONL Event Log (state/logs/YYYY-MM-DD.jsonl)
One JSON object per line:
```json
{"ts": "2026-05-05T09:35:12", "bot": "growth", "stage": "trade", "action": "order_submitted", "symbol": "NVDA", "reason": "ENTRY_ACCEPTED", "order_id": "abc123"}
{"ts": "2026-05-05T16:05:01", "bot": "growth", "stage": "manage", "action": "phase_transition", "symbol": "NVDA", "reason": "PHASE_TRANSITION", "before": {"phase": "protected"}, "after": {"phase": "trailing"}}
```

## Reason Codes
| Code | Meaning |
|------|---------|
| ENTRY_ACCEPTED | Order placed successfully |
| ENTRY_REJECTED_RELVOL | Relative volume below threshold |
| ENTRY_REJECTED_GAPUP | Gap too extended |
| ENTRY_REJECTED_PORTFOLIO_RISK | Would exceed portfolio risk budget |
| ENTRY_REJECTED_CORRELATION | Correlation cap breached |
| ENTRY_REJECTED_DUPLICATE | Dedupe hit (already placed today) |
| STOP_REPLACED | Stop order successfully replaced |
| STOP_RESTORE_FAILED | Failed to restore stop after cancel |
| BROKER_STATE_MISMATCH | Broker ≠ local tracking |
| MANUAL_REVIEW_REQUIRED | Cannot auto-heal, needs human |
| PHASE_TRANSITION | Position phase changed |
| TRAIL_UPGRADE | Trail tightened at milestone |
| TIME_STOP | Exited on time stop |
| RECONCILIATION_FIX | Auto-corrected a mismatch |
| CIRCUIT_BREAKER | Daily loss or drawdown breaker fired |
| JOB_START / JOB_END | Job lifecycle markers |
| LOCK_ACQUIRED / LOCK_STALE_CLEANED | Lock lifecycle |

## Acceptance Tests
| # | Test | Expected |
|---|------|----------|
| 1 | Double-run trade_growth.py | No duplicate order |
| 2 | Double-run manage_growth.py | No duplicate stop change |
| 3 | Missing position_tracking metadata | Reconstruction or MANUAL_REVIEW |
| 4 | Broker has position, local does not | Reconciliation rebuilds tracking |
| 5 | Local has position, broker does not | Local state closes cleanly |
| 6 | Stop cancel succeeds, replace fails | Old stop recreated |
| 7 | Stale research file | Trade run aborts safely |
| 8 | Kill switch present | No entries, management runs |
| 9 | Correlation cap breach | Rejected with reason code |
| 10 | Daily loss breaker hit | Entries blocked |
| 11 | JSONL logging | Events written correctly |
| 12 | Stale lock cleanup | Old lock removed, new acquired |

## New Files
| File | Purpose |
|------|---------|
| `scripts/reconcile.py` | Reusable broker reconciliation module |
| `scripts/healthcheck.py` | Daily health summary generator |
| `scripts/test_recovery.py` | Acceptance test suite |
| `scripts/migrate_state.py` | One-time state migration |
| `docs/phase0-hardening.md` | This document |

## Guardrails
- No AI decision-making in Phase 0
- No parameter tuning
- No universe expansion
- No change to entry/exit logic except safety fixes
- Strategy rules remain frozen

