import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
JOURNAL_DIR = ROOT / "journal"

load_dotenv(ROOT / ".env")

# Canonical market timezone — all date/time logic should use this
MARKET_TZ = ZoneInfo("America/New_York")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def get_env(name, default=None):
    return os.getenv(name, default)


def is_live_mode():
    base_url = get_env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    return "paper" not in base_url


def enforce_live_guardrails():
    if not is_live_mode():
        return
    allow_live = get_env("ALLOW_LIVE_TRADING", "false").lower() == "true"
    ack = get_env("LIVE_ACKNOWLEDGEMENT", "")
    if not allow_live or ack != "I_ACCEPT_LIVE_RISK":
        raise RuntimeError(
            "Live trading is blocked. Set ALLOW_LIVE_TRADING=true and LIVE_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_RISK only when you are intentionally ready."
        )


def alpaca_headers():
    key = get_env("ALPACA_API_KEY")
    secret = get_env("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials in .env")
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def alpaca_base_url():
    return get_env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")


def _request_with_retry(method, url, max_retries=3, **kwargs):
    """Make an HTTP request with exponential backoff retry on transient errors.
    For POST requests with a client_order_id, reconciles before retrying to
    prevent duplicate orders when the first request succeeded but the response was lost."""
    kwargs.setdefault("timeout", 30)
    is_post = (method == requests.post)
    client_order_id = None
    if is_post:
        payload = kwargs.get("json", {})
        if isinstance(payload, dict):
            client_order_id = payload.get("client_order_id")

    for attempt in range(max_retries):
        try:
            r = method(url, **kwargs)
            if r.status_code == 429:  # Rate limited
                wait = min(2 ** attempt * 2, 30)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError:
            if attempt == max_retries - 1:
                raise
            if is_post and client_order_id:
                existing = _reconcile_by_client_id(client_order_id)
                if existing:
                    return existing
            time.sleep(2 ** attempt)
        except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout):
            # Timeout does NOT guarantee the request was not received by the broker
            if is_post and client_order_id:
                existing = _reconcile_by_client_id(client_order_id)
                if existing:
                    return existing
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (500, 502, 503, 504):
                if attempt == max_retries - 1:
                    raise
                # For POSTs, check if the order was actually created before retrying
                if is_post and client_order_id:
                    existing = _reconcile_by_client_id(client_order_id)
                    if existing:
                        return existing
                time.sleep(2 ** attempt)
            else:
                raise  # Client errors (4xx except 429) fail immediately
    raise RuntimeError(f"Request failed after {max_retries} retries: {url}")


def _reconcile_by_client_id(client_order_id):
    """Check if an order with this client_order_id already exists at the broker.
    Returns the order dict if found, None otherwise."""
    try:
        url = f"{alpaca_base_url()}/v2/orders:by_client_order_id"
        r = requests.get(url, headers=alpaca_headers(),
                         params={"client_order_id": client_order_id}, timeout=15)
        if r.status_code == 200:
            order = r.json()
            if isinstance(order, dict) and order.get("id"):
                return order
    except Exception:
        pass
    return None


def alpaca_get(path, params=None):
    url = f"{alpaca_base_url()}{path}"
    return _request_with_retry(requests.get, url, headers=alpaca_headers(), params=params)


def alpaca_post(path, payload):
    url = f"{alpaca_base_url()}{path}"
    return _request_with_retry(requests.post, url, headers=alpaca_headers(), json=payload)


def get_account():
    return alpaca_get("/v2/account")


def get_clock():
    return alpaca_get("/v2/clock")


def get_positions():
    return alpaca_get("/v2/positions")


def get_orders(status="all", limit=50):
    return alpaca_get("/v2/orders", params={"status": status, "limit": limit, "direction": "desc"})


def submit_bracket_order(symbol, qty, limit_price, stop_price, take_profit_price):
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": round(limit_price, 2),
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": round(take_profit_price, 2)},
        "stop_loss": {"stop_price": round(stop_price, 2)},
    }
    return alpaca_post("/v2/orders", payload)


# Alpaca active/in-flight order statuses — canonical set for all broker-state reconciliation.
# Used by both trade.py and manage.py to avoid drift.
ACTIVE_ORDER_STATUSES = ("new", "accepted", "held", "partially_filled", "pending_new", "pending_cancel", "pending_replace")


def cancel_order(order_id):
    """Cancel an order by ID with retry on transient errors.
    Treats 404/422 as success (already canceled or filled)."""
    url = f"{alpaca_base_url()}/v2/orders/{order_id}"
    for attempt in range(3):
        try:
            r = requests.delete(url, headers=alpaca_headers(), timeout=30)
            if r.status_code in (404, 422):
                return  # Already canceled or filled — not an error
            r.raise_for_status()
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as e:
            if attempt == 2:
                raise
            if hasattr(e, 'response') and e.response is not None and e.response.status_code < 500:
                raise
            time.sleep(2 ** attempt)


def cancel_order_and_verify(order_id, max_wait=2.0):
    """Cancel an order and verify it's no longer active.
    Returns True only if broker confirms non-active status or order is 404 (not found).
    Returns False on ambiguous/transient errors — callers should abort replacement."""
    try:
        cancel_order(order_id)
    except Exception:
        pass
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(0.3)
        elapsed += 0.3
        try:
            order = alpaca_get(f"/v2/orders/{order_id}")
            if order and order.get("status") not in ACTIVE_ORDER_STATUSES:
                return True
        except requests.exceptions.HTTPError as e:
            # 404 = order not found at broker = confirmed gone
            if e.response is not None and e.response.status_code == 404:
                return True
            # Any other HTTP error (5xx, auth, etc.) = ambiguous, keep polling
        except Exception:
            # Network error, timeout, etc. = ambiguous, do NOT assume canceled
            pass
    return False


def today_str():
    return datetime.now(MARKET_TZ).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(MARKET_TZ).isoformat()


def write_heartbeat(name, status, extra=None):
    payload = {"name": name, "status": status, "timestamp": now_iso()}
    if extra:
        payload.update(extra)
    save_json(STATE_DIR / f"heartbeat_{name}.json", payload)


# --- Alerting ---

def send_alert(message, level="info"):
    """Send alert via webhook (Slack, Discord, etc).
    Set ALERT_WEBHOOK_URL in .env to enable.
    Fails silently if not configured — alerting should never crash the bot."""
    webhook_url = get_env("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        emoji = {"info": "ℹ️", "trade": "📈", "warning": "⚠️", "error": "🚨"}.get(level, "📋")
        payload = {"text": f"{emoji} *Trading Bot* [{level.upper()}]\n{message}"}
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass  # Never let alerting crash the bot


def risk_position_size(equity, risk_fraction, entry_price, stop_price, max_alloc_fraction):
    risk_dollars = equity * risk_fraction
    per_share_risk = max(entry_price - stop_price, 0.01)
    raw_qty = math.floor(risk_dollars / per_share_risk)
    max_alloc_qty = math.floor((equity * max_alloc_fraction) / entry_price)
    return max(min(raw_qty, max_alloc_qty), 0)


def load_strategy():
    return load_json(CONFIG_DIR / "strategy.json")


def load_strategy_for(bot="default"):
    """Load strategy config for a specific bot."""
    if bot == "growth":
        return load_json(CONFIG_DIR / "strategy_growth.json")
    return load_json(CONFIG_DIR / "strategy.json")


def load_watchlist():
    data = load_json(CONFIG_DIR / "watchlist.json")
    return [x["ticker"] for x in data["symbols"] if x.get("enabled", True)]


def load_watchlist_for(bot="default"):
    """Load watchlist for a specific bot."""
    if bot == "growth":
        data = load_json(CONFIG_DIR / "watchlist_growth.json")
        return [x["ticker"] for x in data["symbols"] if x.get("enabled", True)]
    return load_watchlist()


def state_path(bot, name):
    """Get the state file path for a specific bot.
    E.g. state_path("growth", "candidates.json") → STATE_DIR / "candidates_growth.json"
    """
    if bot == "growth":
        stem = Path(name).stem
        ext = Path(name).suffix
        return STATE_DIR / f"{stem}_growth{ext}"
    return STATE_DIR / name


def load_watchlist():
    data = load_json(CONFIG_DIR / "watchlist.json")
    return [x["ticker"] for x in data["symbols"] if x.get("enabled", True)]


def load_watchlist_with_sectors():
    data = load_json(CONFIG_DIR / "watchlist.json")
    return {x["ticker"]: x.get("sector", "Unknown") for x in data["symbols"] if x.get("enabled", True)}


def fetch_alpaca_bars(symbol, timeframe="1Day", limit=500):
    """Fetch historical bars from Alpaca market data API.
    Returns a DataFrame with OHLCV data, or empty DataFrame on failure."""
    data_url = "https://data.alpaca.markets/v2"
    url = f"{data_url}/stocks/{symbol}/bars"
    params = {"timeframe": timeframe, "limit": limit, "adjustment": "split", "feed": get_env("ALPACA_DATA_FEED", "iex")}
    try:
        r = requests.get(url, headers=alpaca_headers(), params=params, timeout=30)
        r.raise_for_status()
        bars = r.json().get("bars", [])
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df = df.rename(columns={"t": "Date", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return pd.DataFrame()


