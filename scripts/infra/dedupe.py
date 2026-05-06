"""Deduplication helpers."""
import json
import hashlib
from infra.time_utils import today_str


def compute_input_hash(data):
    """Compute a stable hash of input data for dedupe checks."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def order_dedupe_key(bot, symbol, setup_type, trigger_price):
    """Generate a dedupe key for an order: date+bot+symbol+setup+trigger."""
    date_str = today_str()
    raw = f"{date_str}_{bot}_{symbol}_{setup_type}_{trigger_price:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

