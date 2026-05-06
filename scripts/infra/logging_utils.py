"""Structured JSONL event logging."""
import json
from datetime import datetime
from infra.paths import MARKET_TZ, STATE_LOGS
from infra.time_utils import today_str


def log_event(bot, stage, action, symbol=None, reason_code=None,
              before_state=None, after_state=None, order_id=None, extra=None):
    """Append a structured event to today's JSONL log file."""
    event = {
        "ts": datetime.now(MARKET_TZ).isoformat(),
        "bot": bot,
        "stage": stage,
        "action": action,
    }
    if symbol:
        event["symbol"] = symbol
    if reason_code:
        event["reason"] = reason_code
    if before_state is not None:
        event["before"] = before_state
    if after_state is not None:
        event["after"] = after_state
    if order_id:
        event["order_id"] = order_id
    if extra:
        event.update(extra)

    log_path = STATE_LOGS / f"{today_str()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")

