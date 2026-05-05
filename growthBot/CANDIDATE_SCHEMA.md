# Growth Bot V1 — Candidate and Tracking Schema

## `state/candidates.json`

Suggested top-level structure:

```json
{
  "date": "YYYY-MM-DD",
  "regime_mode": "full_risk",
  "allow_new_entries": true,
  "risk_per_trade": 0.0075,
  "max_positions": 5,
  "market_regime": {
    "SPY": {
      "close": 610.25,
      "sma50": 598.10,
      "risk_on": true
    },
    "QQQ": {
      "close": 522.11,
      "sma50": 510.42,
      "risk_on": true
    }
  },
  "candidates": [],
  "rejected": []
}
```

## Candidate record

Each candidate object should contain fields like:

```json
{
  "symbol": "NVDA",
  "score": 92.4,
  "setup_type": "breakout",
  "close": 123.45,
  "ema10": 119.80,
  "sma20": 117.30,
  "sma50": 108.20,
  "sma200": 82.90,
  "atr14": 4.25,
  "avg_dollar_volume": 125000000,
  "rs_3m": 0.184,
  "rs_6m": 0.261,
  "trend_strength": 0.141,
  "trigger_price": 124.10,
  "limit_price": 124.45,
  "setup_high": 123.91,
  "setup_low": 119.20,
  "stop_price": 113.48,
  "r_per_share": 10.62,
  "pullback_bars": 0,
  "pullback_depth_atr": 0.0,
  "volume_ratio": 1.34,
  "regime_mode": "full_risk",
  "correlation_blocked": false,
  "notes": [
    "20d breakout",
    "top quartile RS",
    "volume confirmation"
  ]
}
```

## Rejected record

Rejected candidates should explain why.

```json
{
  "symbol": "SHOP",
  "score": 81.2,
  "reasons": [
    "regime_reduced_risk_and_too_extended",
    "correlation_cap_violation"
  ]
}
```

## `state/positiontracking.json`

Each symbol should have tracking data like:

```json
{
  "NVDA": {
    "entry_date": "YYYY-MM-DD",
    "setup_type": "breakout",
    "phase": "initial",
    "planned_entry": 124.10,
    "actual_entry": 124.22,
    "initial_stop": 113.48,
    "current_stop": 113.48,
    "atr14_at_entry": 4.25,
    "r_per_share": 10.74,
    "bars_held": 1,
    "best_price": 126.00,
    "best_gain_r": 0.17,
    "regime_mode_at_entry": "full_risk",
    "order_id": "broker-order-id"
  }
}
```

## `state/managelog.json`

Suggested shape:

```json
{
  "timestamp": "ISO_TIMESTAMP",
  "actions": [
    {
      "symbol": "NVDA",
      "action": "hold_initial",
      "phase": "initial",
      "current_price": 126.0,
      "target_r": 1.5,
      "best_gain_r": 0.17
    },
    {
      "symbol": "META",
      "action": "move_to_protected",
      "phase": "protected",
      "new_stop": 498.25,
      "reason": "reached_1.5R"
    },
    {
      "symbol": "AVGO",
      "action": "activate_trailing_stop",
      "phase": "trailing",
      "trail_amount": 8.5,
      "reason": "reached_2.5R"
    }
  ]
}
```

## Notes for implementation

- Keep schema stable and explicit.
- Prefer adding fields rather than encoding logic implicitly.
- Journaling and debugging are easier when every decision is inspectable.
- Rejection reasons should always be human-readable.