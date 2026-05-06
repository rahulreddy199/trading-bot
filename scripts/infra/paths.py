"""Path constants and state directory resolution."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
JOURNAL_DIR = ROOT / "journal"

load_dotenv(ROOT / ".env")

MARKET_TZ = ZoneInfo("America/New_York")

# Namespaced state directories
STATE_CONSERVATIVE = STATE_DIR / "conservative"
STATE_GROWTH = STATE_DIR / "growth"
STATE_SHARED = STATE_DIR / "shared"
STATE_LOCKS = STATE_DIR / "locks"
STATE_LOGS = STATE_DIR / "logs"

for _d in (STATE_CONSERVATIVE, STATE_GROWTH, STATE_SHARED, STATE_LOCKS, STATE_LOGS):
    _d.mkdir(parents=True, exist_ok=True)


def state_path(bot, name):
    """Get the namespaced state file path for a specific bot."""
    if bot == "growth":
        return STATE_GROWTH / name
    elif bot == "conservative":
        return STATE_CONSERVATIVE / name
    elif bot == "shared":
        return STATE_SHARED / name
    return STATE_DIR / name


def legacy_state_path(bot, name):
    """Return the OLD flat path for migration purposes."""
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
    return new_path

