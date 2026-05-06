import json
import math
import os
import time
import hashlib
import fcntl
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

# --- Namespaced State Directories ---
STATE_CONSERVATIVE = STATE_DIR / "conservative"
STATE_GROWTH = STATE_DIR / "growth"
STATE_SHARED = STATE_DIR / "shared"
STATE_LOCKS = STATE_DIR / "locks"
STATE_LOGS = STATE_DIR / "logs"

# Ensure dirs exist on import
for _d in (STATE_CONSERVATIVE, STATE_GROWTH, STATE_SHARED, STATE_LOCKS, STATE_LOGS):
    _d.mkdir(parents=True, exist_ok=True)


def state_path(bot, name):
    """Get the namespaced state file path for a specific bot.

    bot="growth"       → state/growth/<name>
    bot="conservative" → state/conservative/<name>
    bot="shared"       → state/shared/<name>

    Falls back to legacy flat path for backward compat during migration.
    """
    if bot == "growth":
        return STATE_GROWTH / name
    elif bot == "conservative":
        return STATE_CONSERVATIVE / name
    elif bot == "shared":
        return STATE_SHARED / name
    # Legacy fallback
    return STATE_DIR / name


def legacy_state_path(bot, name):
    """Return the OLD flat path for migration purposes.
    E.g. legacy_state_path("growth", "candidates.json") → state/candidates_growth.json
    """
    if bot == "growth":
        stem = Path(name).stem
        ext = Path(name).suffix
        return STATE_DIR / f"{stem}_growth{ext}"
    return STATE_DIR / name


def resolve_state(bot, name):
    """Resolve state file: prefer new namespaced path, fallback to legacy if exists."""
    new_path = state_path(bot, name)
    if new_path.exists():
        return new_path
    old_path = legacy_state_path(bot, name)
    if old_path.exists():
        return old_path
    return new_path  # Return new path for writing


# --- JSON helpers ---

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# --- Structured Event Logging (JSONL) ---

def log_event(bot, stage, action, symbol=None, reason_code=None,
              before_state=None, after_state=None, order_id=None, extra=None):
    """Append a structured event to today's JSONL log file.

    Reason codes:
        ENTRY_ACCEPTED, ENTRY_REJECTED_RELVOL, ENTRY_REJECTED_GAPUP,
        ENTRY_REJECTED_PORTFOLIO_RISK, ENTRY_REJECTED_CORRELATION,
        ENTRY_REJECTED_DUPLICATE, STOP_REPLACED, STOP_RESTORE_FAILED,
        BROKER_STATE_MISMATCH, MANUAL_REVIEW_REQUIRED, PHASE_TRANSITION,
        TRAIL_UPGRADE, TIME_STOP, RECONCILIATION_FIX, JOB_START, JOB_END,
        LOCK_ACQUIRED, LOCK_STALE_CLEANED, CIRCUIT_BREAKER
    """
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


# --- Job Locking ---

class JobLock:
    """File-based job lock with timeout to prevent concurrent/repeated runs.

    Usage:
        with JobLock("growth", "morning") as lock:
            if not lock.acquired:
                return  # already running or already ran today
            # do work
    """
    def __init__(self, bot, stage, timeout_minutes=30):
        self.lock_path = STATE_LOCKS / f"{bot}_{stage}.lock"
        self.receipt_path = STATE_LOCKS / f"{bot}_{stage}_receipt.json"
        self.bot = bot
        self.stage = stage
        self.timeout_minutes = timeout_minutes
        self.acquired = False
        self._fd = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Check for stale lock
        if self.lock_path.exists():
            try:
                lock_data = json.loads(self.lock_path.read_text())
                lock_time = datetime.fromisoformat(lock_data.get("acquired_at", ""))
                elapsed = (datetime.now(MARKET_TZ) - lock_time).total_seconds() / 60
                if elapsed > self.timeout_minutes:
                    # Stale lock — clean up
                    self.lock_path.unlink(missing_ok=True)
                    log_event(self.bot, self.stage, "stale_lock_cleaned",
                              reason_code="LOCK_STALE_CLEANED",
                              extra={"elapsed_minutes": round(elapsed, 1)})
                else:
                    # Active lock — bail
                    self.acquired = False
                    return self
            except (json.JSONDecodeError, ValueError, KeyError):
                self.lock_path.unlink(missing_ok=True)

        # Try to acquire
        try:
            self._fd = open(self.lock_path, "x")
            lock_data = {
                "bot": self.bot,
                "stage": self.stage,
                "pid": os.getpid(),
                "acquired_at": datetime.now(MARKET_TZ).isoformat(),
            }
            self._fd.write(json.dumps(lock_data))
            self._fd.flush()
            self.acquired = True
            log_event(self.bot, self.stage, "lock_acquired", reason_code="LOCK_ACQUIRED")
        except FileExistsError:
            self.acquired = False
        return self

    def __exit__(self, *args):
        if self._fd:
            self._fd.close()
        if self.acquired:
            self.lock_path.unlink(missing_ok=True)

    def already_ran_today(self, input_hash=None):
        """Check if this job already produced a receipt today with the same inputs."""
        if not self.receipt_path.exists():
            return False
        try:
            receipt = json.loads(self.receipt_path.read_text())
            if receipt.get("date") != today_str():
                return False
            if input_hash and receipt.get("input_hash") != input_hash:
                return False
            return receipt.get("status") == "completed"
        except Exception:
            return False

    def write_receipt(self, status="completed", orders_submitted=0,
                      dedupe_hits=0, errors=None, warnings=None, input_hash=None):
        """Write a job receipt for audit trail."""
        receipt = {
            "job_name": f"{self.bot}_{self.stage}",
            "bot": self.bot,
            "stage": self.stage,
            "date": today_str(),
            "run_at": datetime.now(MARKET_TZ).isoformat(),
            "input_hash": input_hash,
            "status": status,
            "orders_submitted": orders_submitted,
            "dedupe_hits": dedupe_hits,
            "errors": errors or [],
            "warnings": warnings or [],
        }
        save_json(self.receipt_path, receipt)
        log_event(self.bot, self.stage, "job_receipt",
                  reason_code="JOB_END", extra={"status": status})


# --- Dedupe Helpers ---

def compute_input_hash(data):
    """Compute a stable hash of input data for dedupe checks."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def order_dedupe_key(bot, symbol, setup_type, trigger_price):
    """Generate a dedupe key for an order: date+bot+symbol+setup+trigger."""
    date_str = today_str()
    raw = f"{date_str}_{bot}_{symbol}_{setup_type}_{trigger_price:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


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


# Alpaca active/in-flight order statuses
ACTIVE_ORDER_STATUSES = ("new", "accepted", "held", "partially_filled", "pending_new", "pending_cancel", "pending_replace")


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


def today_str():
    return datetime.now(MARKET_TZ).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(MARKET_TZ).isoformat()


def write_heartbeat(name, status, extra=None):
    payload = {"name": name, "status": status, "timestamp": now_iso()}
    if extra:
        payload.update(extra)
    save_json(STATE_SHARED / f"heartbeat_{name}.json", payload)
    # Also write to legacy location for backward compat
    save_json(STATE_DIR / f"heartbeat_{name}.json", payload)


# --- Alerting ---

def send_alert(message, level="info"):
    """Send alert via webhook (Slack, Discord, etc)."""
    webhook_url = get_env("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        emoji = {"info": "ℹ️", "trade": "📈", "warning": "⚠️", "error": "🚨"}.get(level, "📋")
        payload = {"text": f"{emoji} *Trading Bot* [{level.upper()}]\n{message}"}
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


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


def load_watchlist_with_sectors():
    data = load_json(CONFIG_DIR / "watchlist.json")
    return {x["ticker"]: x.get("sector", "Unknown") for x in data["symbols"] if x.get("enabled", True)}


def fetch_alpaca_bars(symbol, timeframe="1Day", limit=500):
    """Fetch historical bars from Alpaca market data API."""
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
