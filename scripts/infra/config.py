"""Strategy and watchlist config loaders."""
from infra.paths import CONFIG_DIR
from infra.jsonio import load_json


def load_strategy():
    return load_json(CONFIG_DIR / "strategy_growth.json")


def load_strategy_for(bot="growth"):
    return load_json(CONFIG_DIR / "strategy_growth.json")


def load_watchlist():
    data = load_json(CONFIG_DIR / "watchlist_growth.json")
    return [x["ticker"] for x in data["symbols"] if x.get("enabled", True)]


def load_watchlist_for(bot="growth"):
    data = load_json(CONFIG_DIR / "watchlist_growth.json")
    return [x["ticker"] for x in data["symbols"] if x.get("enabled", True)]


def load_watchlist_with_sectors():
    data = load_json(CONFIG_DIR / "watchlist_growth.json")
    return {x["ticker"]: x.get("sector", "Unknown") for x in data["symbols"] if x.get("enabled", True)}
