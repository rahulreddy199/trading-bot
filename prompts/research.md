Run the research workflow for the swing trading agent.

Steps:
1. Load `config/strategy.json` and `config/watchlist.json`.
2. Run `python scripts/research.py`.
3. Open `state/candidates.json` and summarize:
   - market regime
   - qualified setups
   - rejected symbols and why
4. Append a short operational note to today's journal file if needed.
