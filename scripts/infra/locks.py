"""File-based job locking with timeout and receipts."""
import json
import os
from datetime import datetime
from infra.paths import MARKET_TZ, STATE_LOCKS
from infra.jsonio import save_json
from infra.logging_utils import log_event
from infra.time_utils import today_str


class JobLock:
    """File-based job lock with timeout to prevent concurrent/repeated runs."""

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
        if self.lock_path.exists():
            try:
                lock_data = json.loads(self.lock_path.read_text())
                lock_time = datetime.fromisoformat(lock_data.get("acquired_at", ""))
                elapsed = (datetime.now(MARKET_TZ) - lock_time).total_seconds() / 60
                if elapsed > self.timeout_minutes:
                    self.lock_path.unlink(missing_ok=True)
                    log_event(self.bot, self.stage, "stale_lock_cleaned",
                              reason_code="LOCK_STALE_CLEANED",
                              extra={"elapsed_minutes": round(elapsed, 1)})
                else:
                    self.acquired = False
                    return self
            except (json.JSONDecodeError, ValueError, KeyError):
                self.lock_path.unlink(missing_ok=True)

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

