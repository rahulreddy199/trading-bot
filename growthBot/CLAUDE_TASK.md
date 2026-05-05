# Growth Bot V1 — Implementation Task

## Goal

Convert the current conservative swing trading bot into an aggressive **growth-first momentum swing bot**.

This is a new strategy family. We are **not** trying to slightly tune the old pullback bot. We want a bot that is more likely to capture strong upside in market leaders, with higher concentration, more trend participation, and more willingness to accept volatility.

The current conservative bot folder has already been copied as a backup. We now want to modify this bot **in place** inside the `growthBot/` folder.

## High-level objective

The growth bot should:

- Stay long-only.
- Use daily bars only in V1.
- Reuse the existing operational infrastructure where possible.
- Replace the conservative pullback-only strategy logic with a momentum growth strategy.
- Prioritize total return over smoothness.
- Accept higher drawdown and volatility than the conservative bot.
- Be paper-trading safe by default.

## What should be reused

Keep and reuse as much of this as possible:

- `common.py` core helpers.
- Alpaca auth / retry / account / positions / order helpers.
- Live-trading safety guardrails.
- State file pattern under `state/`.
- Markdown journaling pattern in `journal.py`.
- Script pipeline structure:
    - `research.py`
    - `trade.py`
    - `manage.py`
    - `journal.py`

## What should change

The current bot is too conservative because it waits for:
- strict trend filters,
- narrow pullback conditions,
- bullish candle confirmation,
- lower-frequency entries.

The new bot should instead:
- rank momentum leaders,
- support breakout entries,
- support continuation entries,
- support shallow pullback entries,
- hold fewer but stronger names,
- use wider stops and looser exits,
- avoid cutting winners too early.

## Deliverables

Please implement:

1. Updated `research.py`
2. Updated `trade.py`
3. Updated `manage.py`
4. New `config/strategy.json` for growth bot defaults
5. New `config/watchlist.json` for growth universe
6. Updated `README.md` describing the growth bot
7. Preserve paper-trading safety defaults
8. Preserve compatibility with existing folder structure and daily run flow

## Constraints

- Use daily candles only.
- No websocket or live intraday stream logic in V1.
- No options logic.
- No covered calls logic.
- No short selling.
- No leverage in code logic for V1.
- Paper-trading safe by default.
- Do not remove live guardrails.
- Keep implementation simple and deterministic.

## Success criteria

The new bot should:
- produce a ranked candidate list daily,
- place orders for top valid growth setups,
- manage open positions with growth-oriented exits,
- log enough information for debugging and review,
- preserve current reliability patterns.

Refer to the other markdown files in this folder for strategy rules and implementation details.