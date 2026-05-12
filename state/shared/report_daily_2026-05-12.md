# Daily Review — 2026-05-12

## Headline Metrics
| Metric | All-Time | 7-Day |
|--------|----------|-------|
| Trades | 1 | 1 |
| Net PnL | $323.36 | $323.36 |
| Win Rate | 100.0% | 100.0% |
| Profit Factor | inf | inf |
| Avg R | 2.79 | 2.79 |
| Avg Hold | 0.0 bars | 0.0 bars |
| Slippage | 0.0 bps | 0.0 bps |

## Account
- Equity: $20,513.45
- Open positions: 2
- Unrealized P&L: $190.09

## Market Regime
- Label: **unknown**
- SPY > 50 SMA: None
- SPY > 200 SMA: None
- VIX: None (unknown)
- Growth regime mode: **full_risk**

## Open Positions
| Symbol | Bot | Setup | Phase | Entry | Current | P&L | R | Best R | Bars | Stop |
|--------|-----|-------|-------|-------|---------|-----|---|--------|------|------|
| AMD | growth | continuation | initial | $431.57 | $447.61 | $+16.04 | 0.27R | 0.55R | 3 | $372.75 |
| SMH | growth | breakout | trailing | $517.22 | $560.50 | $+173.12 | 1.39R | 1.9R | 6 | $540.83 |

## Position Management Actions
- **AMD**: hold_initial — price=$447.30 | R=0.27 | stop=$372.75
- **SMH**: hold_trailing — price=$560.81 | R=1.4 | stop=$540.83

## Research Summary
- Candidates found: 0
- Rejected: 25
- Regime: full_risk
- **Top rejection reasons:**
  - below_sma200: 10
  - below_rank_cutoff: 9
  - sma50_below_sma200: 3
  - low_rel_volume_shallow_pullback: 2
  - low_rel_volume_breakout: 1

## Orders Today
- No orders placed

## Trades Closed Today
- **MU**: $+323.36 (2.79R) | exit=trailing_stop | entry=$572.91 → exit=$734.60 | qty=2 | ? bars | setup=?

## Best/Worst Contributors (All-Time)
| Symbol | Net PnL |
|--------|---------|
| MU | $323.36 |


## Operational Issues
- ✅ No incidents today

## Open Manual-Review Items
- None

## AI Recommendations
- [high] **Continue paper trading to accumulate data** → monitor
  - Only 1 closed trades. Need 20+ for reliable attribution.

## Equity Snapshot
- Today: $20,192.55 (+188.98 / +0.94%)
- Total return: $+192.55 (+0.96%)
- Data points: 2 days

## Market Context
- SPY: $738.17 (-0.15%)
- QQQ: $707.24 (-0.85%)

## Position Price Context
- **AMD**: price=$447.61 | stop distance=16.7% | best price=$464.10 | from best=-3.6%
- **SMH**: price=$560.50 | stop distance=3.5% | best price=$576.17 | from best=-2.7%

## Near-Miss Candidates
- **META**: missed by → below_sma200
- **MSFT**: missed by → below_sma200
- **NFLX**: missed by → below_sma200
- **PLTR**: missed by → below_sma200
- **TSLA**: missed by → sma50_below_sma200

_(25 total rejected out of 25 scanned)_

## Correlation & Diversification
- No correlation blocks today
- Open by sector: Technology: 2

## Trading Activity Summary
- Total closed trades: 1 (W:1 / L:0)
- Open positions: 2/5 slots used
- Trades by setup: order_scan: 1

## Insufficient Evidence
- Only 1 closed trades. Need 20+ for reliable metrics.
- Setup 'unknown': only 1 trades (need 5+ for attribution)
