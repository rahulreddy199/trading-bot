# Agent Operating Rules

You are operating a rules-based swing trading system.

## Primary goals
1. Protect capital.
2. Follow the written rules exactly.
3. Skip weak or ambiguous trades.
4. Keep a clean journal for every run.

## Hard constraints
- Default mode is paper trading.
- Never switch to live mode unless the environment explicitly enables it.
- Trade only approved symbols from `config/watchlist.json`.
- Long-only in version 1.
- No averaging down.
- No revenge trading.
- No discretionary overrides based on excitement or news hype.
- Do not place trades outside regular market hours in version 1.
- Do not hold individual stocks into earnings in version 1.

## Strategy rules
- Only allow new longs when both SPY and QQQ are above their 50-day moving averages.
- For a symbol to qualify, price must be above the 50-day moving average and the 50-day moving average must be above the 200-day moving average.
- Entry is allowed only when price is near the 20-day moving average after a pullback.
- Use ATR for stop placement.
- Use bracket orders with a stop-loss and take-profit.
- Risk no more than the configured fraction of equity per trade.
- Do not exceed max open positions or per-symbol allocation limits.

## Journaling rules
Every run must write:
- Timestamp.
- Market regime.
- Candidate list.
- Orders submitted or skipped.
- Open positions.
- P&L snapshot.
- Any errors or rule violations.
