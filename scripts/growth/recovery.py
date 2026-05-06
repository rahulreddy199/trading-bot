"""
Recovery and metadata reconstruction helpers for growth bot.
"""
import json

import sys
from pathlib import Path
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import STATE_DIR, send_alert


def try_reconstruct_metadata(symbol, track):
    """Try to reconstruct missing r_per_share/atr from multiple sources.
    Priority: last_orders → order_plan → candidates → ATR fallback.
    Returns True if any reconstruction was done.
    """
    reconstructed = False
    source = None

    # 1. last_orders_growth.json
    last_orders_path = STATE_DIR / "last_orders_growth.json"
    if last_orders_path.exists():
        try:
            orders = json.loads(last_orders_path.read_text())
            for o in orders:
                if o.get("symbol") == symbol:
                    if track.get("atr14_at_entry") is None and "atr14" in o:
                        track["atr14_at_entry"] = float(o["atr14"])
                    if track.get("r_per_share") is None and o.get("r_per_share"):
                        track["r_per_share"] = float(o["r_per_share"])
                        source = "last_orders"
                    break
        except Exception:
            pass

    # 2. order_plan_growth.json
    if track.get("r_per_share") is None:
        order_plan_path = STATE_DIR / "order_plan_growth.json"
        if order_plan_path.exists():
            try:
                plan = json.loads(order_plan_path.read_text())
                for o in plan.get("orders", []):
                    if o.get("symbol") == symbol:
                        if track.get("r_per_share") is None and o.get("r_per_share"):
                            track["r_per_share"] = float(o["r_per_share"])
                            source = "order_plan"
                        break
            except Exception:
                pass

    # 3. candidates_growth.json
    if track.get("r_per_share") is None or track.get("atr14_at_entry") is None:
        candidates_path = STATE_DIR / "candidates_growth.json"
        if candidates_path.exists():
            try:
                data = json.loads(candidates_path.read_text())
                for c in data.get("candidates", []) + data.get("rejected", []):
                    if c.get("symbol") == symbol:
                        if track.get("atr14_at_entry") is None and "atr14" in c:
                            track["atr14_at_entry"] = float(c["atr14"])
                        if track.get("r_per_share") is None and "r_per_share" in c:
                            track["r_per_share"] = float(c["r_per_share"])
                            source = "candidates"
                        break
            except Exception:
                pass

    # 4. ATR fallback estimate
    if track.get("r_per_share") is None and track.get("atr14_at_entry"):
        track["r_per_share"] = round(2.5 * track["atr14_at_entry"], 2)
        source = "atr_fallback_estimate"
        track["r_per_share_estimated"] = True
        track["MANUAL_REVIEW"] = True

    if source:
        track["r_per_share_source"] = source
        if source == "atr_fallback_estimate":
            print(f"  ⚠️ {symbol}: r_per_share estimated from ATR fallback (MANUAL_REVIEW)")
        else:
            print(f"  ℹ️ {symbol}: r_per_share reconstructed from {source}")
        reconstructed = True

    return reconstructed

