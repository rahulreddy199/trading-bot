"""Environment variable helpers and live-mode guardrails."""
import os
from infra.paths import ROOT
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


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
            "Live trading is blocked. Set ALLOW_LIVE_TRADING=true and "
            "LIVE_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_RISK only when you are intentionally ready."
        )

