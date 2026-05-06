"""Time utilities."""
from datetime import datetime
from infra.paths import MARKET_TZ, STATE_SHARED, STATE_DIR
from infra.jsonio import save_json


def today_str():
    return datetime.now(MARKET_TZ).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(MARKET_TZ).isoformat()


def write_heartbeat(name, status, extra=None):
    payload = {"name": name, "status": status, "timestamp": now_iso()}
    if extra:
        payload.update(extra)
    save_json(STATE_SHARED / f"heartbeat_{name}.json", payload)
    save_json(STATE_DIR / f"heartbeat_{name}.json", payload)

