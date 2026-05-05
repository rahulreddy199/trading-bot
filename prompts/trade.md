Run the trade workflow for the swing trading agent.

Steps:
1. Confirm the environment is paper trading unless live mode was explicitly approved.
2. Run `python scripts/trade.py`.
3. Review `state/order_plan.json` and `state/last_orders.json`.
4. Summarize which trades were submitted, skipped, or blocked by risk checks.
