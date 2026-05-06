"""
Deterministic regime tagging.
No ML. Uses simple SMA crossovers and VIX levels.
"""
from typing import Dict
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta


def tag_regime(spy_data=None, vix_data=None) -> Dict:
    """
    Compute current regime tags from market data.

    Returns dict with:
        spy_above_50sma: bool
        spy_above_200sma: bool
        regime_label: str ("bull", "correction", "bear")
        vix_level: str ("low", "medium", "high")
        vix_value: float or None
    """
    result = {
        "spy_above_50sma": None,
        "spy_above_200sma": None,
        "regime_label": "unknown",
        "vix_level": "unknown",
        "vix_value": None,
        "tagged_at": datetime.now().isoformat(),
    }

    # SPY regime
    try:
        if spy_data is None:
            spy_data = yf.download("SPY", period="250d", interval="1d",
                                    auto_adjust=True, progress=False)
        if len(spy_data) >= 200:
            close = spy_data["Close"].iloc[-1]
            if isinstance(close, pd.Series):
                close = close.iloc[0]
            sma50 = float(spy_data["Close"].rolling(50).mean().iloc[-1])
            sma200 = float(spy_data["Close"].rolling(200).mean().iloc[-1])
            close = float(close)

            result["spy_above_50sma"] = close > sma50
            result["spy_above_200sma"] = close > sma200

            if close > sma50 and close > sma200:
                result["regime_label"] = "bull"
            elif close > sma200:
                result["regime_label"] = "correction"
            else:
                result["regime_label"] = "bear"
    except Exception:
        pass

    # VIX level
    try:
        if vix_data is None:
            vix_data = yf.download("^VIX", period="5d", interval="1d",
                                    auto_adjust=True, progress=False)
        if len(vix_data) > 0:
            vix_val = float(vix_data["Close"].iloc[-1])
            if isinstance(vix_val, pd.Series):
                vix_val = float(vix_val.iloc[0])
            result["vix_value"] = round(vix_val, 1)
            if vix_val < 15:
                result["vix_level"] = "low"
            elif vix_val < 25:
                result["vix_level"] = "medium"
            else:
                result["vix_level"] = "high"
    except Exception:
        pass

    return result


def regime_for_date(date_str: str, spy_df=None) -> str:
    """Get regime label for a historical date. Returns 'bull'/'correction'/'bear'/'unknown'."""
    try:
        if spy_df is None:
            spy_df = yf.download("SPY", period="500d", interval="1d",
                                  auto_adjust=True, progress=False)
        if len(spy_df) < 200:
            return "unknown"

        # Find the date
        target = pd.Timestamp(date_str)
        mask = spy_df.index <= target
        if mask.sum() < 200:
            return "unknown"

        subset = spy_df[mask]
        close = float(subset["Close"].iloc[-1])
        if isinstance(close, pd.Series):
            close = float(close.iloc[0])
        sma50 = float(subset["Close"].rolling(50).mean().iloc[-1])
        sma200 = float(subset["Close"].rolling(200).mean().iloc[-1])

        if close > sma50 and close > sma200:
            return "bull"
        elif close > sma200:
            return "correction"
        else:
            return "bear"
    except Exception:
        return "unknown"


