"""Alpaca broker API client with retry logic."""
import time
import requests
import pandas as pd
from infra.env import get_env


ACTIVE_ORDER_STATUSES = (
    "new", "accepted", "held", "partially_filled",
    "pending_new", "pending_cancel", "pending_replace",
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


def _reconcile_by_client_id(client_order_id):
    """Check if an order with this client_order_id already exists at the broker."""
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


def _request_with_retry(method, url, max_retries=3, **kwargs):
    """Make an HTTP request with exponential backoff retry on transient errors."""
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
            if r.status_code == 429:
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
                if is_post and client_order_id:
                    existing = _reconcile_by_client_id(client_order_id)
                    if existing:
                        return existing
                time.sleep(2 ** attempt)
            else:
                raise
    raise RuntimeError(f"Request failed after {max_retries} retries: {url}")


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


def cancel_order(order_id):
    """Cancel an order by ID with retry on transient errors."""
    url = f"{alpaca_base_url()}/v2/orders/{order_id}"
    for attempt in range(3):
        try:
            r = requests.delete(url, headers=alpaca_headers(), timeout=30)
            if r.status_code in (404, 422):
                return
            r.raise_for_status()
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as e:
            if attempt == 2:
                raise
            if hasattr(e, 'response') and e.response is not None and e.response.status_code < 500:
                raise
            time.sleep(2 ** attempt)


def cancel_order_and_verify(order_id, max_wait=2.0):
    """Cancel an order and verify it's no longer active."""
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
            if e.response is not None and e.response.status_code == 404:
                return True
        except Exception:
            pass
    return False


def fetch_alpaca_bars(symbol, timeframe="1Day", limit=500):
    """Fetch historical bars from Alpaca market data API."""
    data_url = "https://data.alpaca.markets/v2"
    url = f"{data_url}/stocks/{symbol}/bars"
    params = {"timeframe": timeframe, "limit": limit, "adjustment": "split",
              "feed": get_env("ALPACA_DATA_FEED", "iex")}
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

