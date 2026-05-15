"""
Variant definition — apply parameter overrides to a baseline strategy.

A variant is defined as a set of dotted-path overrides on top of the
baseline strategy_growth.json.  This module applies those overrides
to produce a complete strategy dict suitable for the backtester.

Pure functions: no I/O except loading the baseline when not provided.
"""
import copy
import json
from pathlib import Path
from typing import Dict, Any, Optional


def apply_overrides(strategy: Dict, overrides: Dict[str, Any]) -> Dict:
    """
    Return a deep copy of *strategy* with each dotted-path key in
    *overrides* set to the corresponding value.

    Example:
        overrides = {"exit.trailing_atr_multiplier": 2.5,
                     "setups.shallow_pullback.max_depth_atr": 2.0}
    """
    result = copy.deepcopy(strategy)
    for dotted_key, value in overrides.items():
        _set_nested(result, dotted_key.split("."), value)
    return result


def _set_nested(d: Dict, keys: list, value: Any):
    """Walk into *d* along *keys* and set the leaf."""
    for key in keys[:-1]:
        if key not in d:
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value


def _get_nested(d: Dict, keys: list, default=None):
    """Walk into *d* along *keys* and return the leaf or *default*."""
    for key in keys:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def get_override_value(strategy: Dict, dotted_key: str, default=None):
    """Read a single dotted-path value from a strategy dict."""
    return _get_nested(strategy, dotted_key.split("."), default)


def diff_strategies(baseline: Dict, variant: Dict, overrides: Dict[str, Any]) -> list:
    """
    Return a human-readable list of parameter diffs.

    Each entry: {"param": ..., "baseline": ..., "variant": ...}
    """
    diffs = []
    for dotted_key in sorted(overrides.keys()):
        keys = dotted_key.split(".")
        b_val = _get_nested(baseline, keys)
        v_val = _get_nested(variant, keys)
        diffs.append({
            "param": dotted_key,
            "baseline": b_val,
            "variant": v_val,
        })
    return diffs


def load_baseline_strategy(path: Optional[Path] = None) -> Dict:
    """Load the baseline strategy JSON from disk."""
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "strategy_growth.json"
    return json.loads(path.read_text())


def build_variant(overrides: Dict[str, Any], baseline: Optional[Dict] = None) -> Dict:
    """Convenience: load baseline if needed, apply overrides, return variant."""
    if baseline is None:
        baseline = load_baseline_strategy()
    return apply_overrides(baseline, overrides)

