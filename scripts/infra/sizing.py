"""Position sizing helpers."""
import math


def risk_position_size(equity, risk_fraction, entry_price, stop_price, max_alloc_fraction):
    risk_dollars = equity * risk_fraction
    per_share_risk = max(entry_price - stop_price, 0.01)
    raw_qty = math.floor(risk_dollars / per_share_risk)
    max_alloc_qty = math.floor((equity * max_alloc_fraction) / entry_price)
    return max(min(raw_qty, max_alloc_qty), 0)

