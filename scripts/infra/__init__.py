"""
infra — Infrastructure modules for the trading bot.

Split from common.py for maintainability. common.py re-exports everything
for backward compatibility.
"""
from infra.paths import (
    ROOT, CONFIG_DIR, STATE_DIR, JOURNAL_DIR, MARKET_TZ,
    STATE_CONSERVATIVE, STATE_GROWTH, STATE_SHARED, STATE_LOCKS, STATE_LOGS,
    state_path, legacy_state_path, resolve_state,
)
from infra.jsonio import load_json, save_json
from infra.logging_utils import log_event
from infra.locks import JobLock
from infra.dedupe import compute_input_hash, order_dedupe_key
from infra.env import get_env, is_live_mode, enforce_live_guardrails
from infra.time_utils import today_str, now_iso, write_heartbeat
from infra.broker import (
    ACTIVE_ORDER_STATUSES,
    alpaca_headers, alpaca_base_url, alpaca_get, alpaca_post,
    get_account, get_clock, get_positions, get_orders,
    submit_bracket_order, cancel_order, cancel_order_and_verify,
    fetch_alpaca_bars,
)
from infra.alerts import send_alert
from infra.sizing import risk_position_size
from infra.config import load_strategy, load_strategy_for, load_watchlist, load_watchlist_for, load_watchlist_with_sectors

