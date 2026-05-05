# Growth Bot V1 — Strategy Specification

## Strategy intent

This bot is an aggressive **growth / momentum swing trading bot**.

It is designed to outperform a passive benchmark by:
- concentrating into stronger leaders,
- entering breakouts and trend continuations,
- rotating faster,
- staying more invested in rising markets,
- using looser exits so strong winners are not cut too early.

This is not an income bot and not a covered-call bot.

## Benchmarks

- Primary benchmark: `SPY`
- Secondary benchmark: `QQQ`

## Timeframe

- Signal generation: daily bars
- Orders: next-day / current-day market session using daily-bar-derived signals
- No intraday decision engine in V1

## Direction

- Long only

## Universe

### ETFs
- SPY
- QQQ
- IWM
- SMH

### Stocks
Start with liquid momentum / growth / leadership names:

- NVDA
- AMD
- AVGO
- ANET
- META
- AMZN
- MSFT
- AAPL
- GOOGL
- NFLX
- PLTR
- TSLA
- MU
- CRM
- NOW
- PANW
- CRWD
- SNOW
- TTD
- UBER
- SHOP
- FCX
- NUE

## Base filters

A symbol is eligible only if:

- price > $20
- 20-day average dollar volume > $25M
- ATR(14) is valid
- sufficient daily history exists for indicators
- no obviously broken / missing data

## Indicator set

Use these indicators:

- EMA 10
- SMA 20
- SMA 50
- SMA 200
- ATR 14
- 20-day high
- 55-day high
- 20-day average volume
- 20-day average dollar volume
- 3-month return
- 6-month return
- relative strength vs SPY over 3 months
- relative strength vs SPY over 6 months

## Ranking model

Each symbol gets a composite growth score:

- 50% weight: 3-month relative strength vs SPY
- 30% weight: 6-month relative strength vs SPY
- 20% weight: normalized trend strength, such as distance above 50-day SMA, capped to avoid overrewarding extreme extension

Then:
- rank all eligible names by score descending
- keep only top 25% by rank
- all entry setups must come from this ranked leader pool

## Regime model

Use a lighter regime filter than the conservative bot.

### Full risk
- both SPY and QQQ above 50-day SMA

### Reduced risk
- only one of SPY or QQQ above 50-day SMA

### Risk off
- both SPY and QQQ below 50-day SMA

Rules:
- New longs allowed in full-risk mode
- New longs allowed in reduced-risk mode with smaller risk and fewer positions
- No new longs in risk-off mode

Note:
- Keep this simple in V1
- Do not require SPY/QQQ above both 50 and 200 SMA like the old conservative bot

## Entry setup types

The bot should support 3 setup types.

### 1. Breakout

Definition:
- close at or near 20-day high or 55-day high
- price above SMA 20, SMA 50, SMA 200
- relative strength rank in top tier
- optional confirmation: volume >= 1.2x average volume

Entry:
- buy stop slightly above breakout bar high

Candidate fields needed:
- setup_type = `breakout`
- setup_high
- setup_low
- trigger_price
- initial_stop_reference

### 2. Continuation

Definition:
- strong trend already in place
- recent breakout occurred
- pullback is shallow, around 1 to 3 bars
- price remains above SMA 20
- last bar closes green or strong

Entry:
- buy stop above prior bar high or setup bar high

Candidate fields needed:
- setup_type = `continuation`
- pullback_bars
- setup_high
- setup_low
- trigger_price

### 3. Shallow pullback

Definition:
- price above SMA 20, SMA 50, SMA 200
- distance from recent swing high not more than about 1 to 1.5 ATR
- symbol still in top-ranked RS group
- pullback remains orderly

Entry:
- trigger above prior bar high or reclaim of EMA 10 / local pivot high

Candidate fields needed:
- setup_type = `shallow_pullback`
- pullback_depth_atr
- setup_high
- setup_low
- trigger_price

## Entry exclusions

Reject new entries if:

- regime is risk-off
- symbol already has an open position
- symbol already has a pending buy order
- ATR invalid
- stop would be above or equal to entry
- position size would be zero
- correlation cap would be violated
- max positions reached
- max total risk reached
- cash reserve would be violated

## Sizing and portfolio rules

### Full risk mode
- risk per trade: 0.75% of equity
- max open positions: 5
- max total open risk: 3.0% of equity
- max allocation per symbol: 25%
- cash reserve: 5%

### Reduced risk mode
- risk per trade: 0.40% of equity
- max open positions: 3
- max total open risk: 1.5%
- max allocation per symbol: 20%
- cash reserve: 10%

### Concentration intent
The bot should normally hold 3 to 5 names, not a wide portfolio.

## Correlation control

Keep a simple correlation guardrail.

- enabled: true
- lookback: 40 trading days
- threshold: 0.85
- max correlated positions: 2

Meaning:
- if a new candidate is highly correlated with multiple existing positions, skip it
- keep this simple and practical

## Initial stop logic

Initial stop should be the wider / looser of:

- setup low minus 0.2 ATR
- entry minus 2.5 ATR

This is intentionally wider than the conservative bot.

## Exit management

The new bot should not cut winners too quickly.

### Phase 1 — Initial
- hold original stop
- do not move to breakeven at 1R
- only consider protection once trade reaches 1.5R

### Phase 2 — Protected
At 1.5R:
- move stop to near entry, for example:
    - entry - 0.1 ATR, or
    - just below EMA 10
- use the looser / more trend-friendly option

### Phase 3 — Trend hold
At 2.5R or after 5 bars in profit:
- replace fixed stop with trailing logic
- use either:
    - trailing stop at 3 ATR
    - or stop below EMA 10 / SMA 20 trend structure
- implementation can choose one consistent approach in V1

### Exit on failure
Exit if any of these happen:
- trailing stop hit
- decisive close below SMA 20 after trend deterioration
- position stagnates for too long

### Time stop
- optional but preferred in V1
- if no meaningful progress after 10 bars, exit and recycle capital

## Logging and state

Persist enough information for later scripts:

For each candidate, persist:
- symbol
- score
- setup_type
- regime_mode
- trigger_price
- limit_price
- setup_high
- setup_low
- atr14
- stop_price
- r_per_share
- relative strength values
- correlation notes if applicable
- rejection reasons if rejected

For each tracked position, persist:
- entry date
- entry price
- stop price
- ATR at entry
- setup type
- phase
- bars held
- best price since entry
- current trailing mode
- initial R

## Safety requirements

- Paper trading by default
- Preserve live-trading acknowledgment gate
- Never remove stop logic
- No averaging down
- No martingale sizing
- No extended hours orders in V1