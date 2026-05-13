"""
Performance tracking: analyzes closed trades and generates stats.

Run after journal.py or on demand to see:
- Win rate, profit factor, avg R
- Largest winner/loser
- Equity curve data
- YTD and 30-day P&L

Stores results in state/performance.json and state/trade_history.json.
"""
import json
from datetime import datetime, timedelta

from common import (
    STATE_DIR,
    STATE_SHARED,
    get_account,
    get_orders,
    get_positions,
    load_strategy,
    now_iso,
    resolve_state,
    save_json,
    today_str,
    write_heartbeat,
)


def load_trade_history():
    path = STATE_DIR / "trade_history.json"
    if path.exists():
        data = json.loads(path.read_text())
        # Handle both {"trades": [...]} and [...] formats
        if isinstance(data, list):
            return {"trades": data, "last_checked_order_id": None}
        return data
    return {"trades": [], "last_checked_order_id": None}


def save_trade_history(history):
    save_json(STATE_DIR / "trade_history.json", history)


def load_tracking():
    """Load growth position tracking."""
    path = resolve_state("growth", "position_tracking.json")
    if path.exists():
        return json.loads(path.read_text())
    return {}


def detect_closed_trades(history):
    """Scan recent filled sell orders to catch any trades NOT already recorded by manage.py.
    Primary source: manage.py's _record_closed_trade (writes directly to trade_history.json).
    This function is a secondary safety net only."""
    orders = get_orders(status="closed", limit=100)
    tracking = load_tracking()

    existing_ids = {t.get("sell_order_id") for t in history["trades"] if t.get("sell_order_id")}
    existing_client_ids = {t.get("client_order_id") for t in history["trades"] if t.get("client_order_id")}

    new_trades = []
    for order in orders:
        if order.get("status") != "filled" or order.get("side") != "sell":
            continue
        if order.get("id") in existing_ids:
            continue
        if order.get("client_order_id") in existing_client_ids:
            continue

        symbol = order.get("symbol")
        filled_at = order.get("filled_at", "")


        filled_price = float(order.get("filled_avg_price", 0))
        filled_qty = int(float(order.get("filled_qty", 0)))

        # Find entry price from tracking or buy orders
        entry_price = None
        r_per_share = None
        if symbol in tracking:
            entry_price = tracking[symbol].get("entry_price") or tracking[symbol].get("planned_entry")
            r_per_share = tracking[symbol].get("r_per_share")

        if entry_price is None:
            for buy_order in orders:
                if (buy_order.get("symbol") == symbol
                        and buy_order.get("side") == "buy"
                        and buy_order.get("status") == "filled"):
                    entry_price = float(buy_order.get("filled_avg_price", 0))
                    break

        if not entry_price:
            continue

        pnl = (filled_price - entry_price) * filled_qty
        pnl_pct = (filled_price / entry_price - 1) * 100
        r_multiple = pnl / (r_per_share * filled_qty) if r_per_share and r_per_share > 0 else None

        new_trades.append({
            "symbol": symbol,
            "entry_price": round(entry_price, 2),
            "exit_price": round(filled_price, 2),
            "qty": filled_qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "r_multiple": round(r_multiple, 2) if r_multiple else None,
            "exit_type": order.get("type", "unknown"),
            "sell_order_id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "closed_at": filled_at,
            "source": "order_scan",
        })

    return new_trades


def compute_stats(trades, label="all"):
    """Compute performance stats for a list of trades."""
    if not trades:
        return {
            "label": label,
            "total_trades": 0,
            "win_rate": 0,
            "profit_factor": 0,
            "avg_pnl": 0,
            "avg_r": 0,
            "total_pnl": 0,
            "largest_winner": 0,
            "largest_loser": 0,
        }

    # Filter to trades with valid numeric pnl
    valid = [t for t in trades if t.get("pnl") is not None and isinstance(t.get("pnl"), (int, float))]
    if not valid:
        return {
            "label": label,
            "total_trades": len(trades),
            "trades_missing_pnl": len(trades),
            "win_rate": 0, "profit_factor": 0, "avg_pnl": 0, "avg_r": 0,
            "total_pnl": 0, "largest_winner": 0, "largest_loser": 0,
        }

    winners = [t for t in valid if t["pnl"] > 0]
    losers = [t for t in valid if t["pnl"] <= 0]

    total_wins = sum(t["pnl"] for t in winners)
    total_losses = abs(sum(t["pnl"] for t in losers))

    r_multiples = [t["r_multiple"] for t in valid if t.get("r_multiple") is not None]

    return {
        "label": label,
        "total_trades": len(trades),
        "trades_with_pnl": len(valid),
        "trades_missing_pnl": len(trades) - len(valid),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(len(winners) / len(valid) * 100, 1) if valid else 0,
        "profit_factor": round(total_wins / total_losses, 2) if total_losses > 0 else float('inf') if total_wins > 0 else 0,
        "avg_pnl": round(sum(t["pnl"] for t in valid) / len(valid), 2),
        "avg_r": round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else None,
        "total_pnl": round(sum(t["pnl"] for t in valid), 2),
        "largest_winner": round(max((t["pnl"] for t in winners), default=0), 2),
        "largest_loser": round(min((t["pnl"] for t in losers), default=0), 2),
    }


def main():
    history = load_trade_history()

    # Detect newly closed trades
    new_trades = detect_closed_trades(history)
    if new_trades:
        history["trades"].extend(new_trades)
        save_trade_history(history)
        print(f"Added {len(new_trades)} new closed trades to history")

    all_trades = history["trades"]

    # All-time stats
    all_time = compute_stats(all_trades, "all_time")

    # Last 30 days
    cutoff_30d = (datetime.now() - timedelta(days=30)).isoformat()
    recent_trades = [t for t in all_trades if t.get("closed_at", "") >= cutoff_30d]
    last_30d = compute_stats(recent_trades, "last_30_days")

    # Account info
    account = get_account()
    equity = float(account.get("equity", 0))

    performance = {
        "timestamp": now_iso(),
        "equity": equity,
        "total_closed_trades": len(all_trades),
        "all_time": all_time,
        "last_30_days": last_30d,
        "recent_trades": all_trades[-10:],  # Last 10 trades
    }

    save_json(STATE_DIR / "performance.json", performance)

    # Append to equity curve (daily snapshot) — in shared/ for reports
    curve_path = STATE_SHARED / "equity_curve.json"
    if curve_path.exists():
        curve = json.loads(curve_path.read_text())
    else:
        curve = []
    # Only add one entry per day
    today = today_str()
    if not curve or curve[-1].get("date") != today:
        curve.append({
            "date": today,
            "equity": equity,
            "total_pnl": all_time["total_pnl"],
            "open_positions": len(get_positions()),
            "total_trades": all_time["total_trades"],
        })
        save_json(curve_path, curve)

    write_heartbeat("performance", "ok", {"trades": len(all_trades)})

    # Print summary
    print(f"\n{'='*50}")
    print(f"PERFORMANCE SUMMARY")
    print(f"{'='*50}")
    print(f"Equity: ${equity:,.2f}")
    print(f"Total closed trades: {all_time['total_trades']}")
    if all_time['total_trades'] > 0:
        print(f"Win rate: {all_time['win_rate']}%")
        print(f"Profit factor: {all_time['profit_factor']}")
        print(f"Avg P&L per trade: ${all_time['avg_pnl']}")
        print(f"Avg R-multiple: {all_time['avg_r']}")
        print(f"Total P&L: ${all_time['total_pnl']}")
        print(f"Largest winner: ${all_time['largest_winner']}")
        if all_time['largest_loser'] < 0:
            print(f"Largest loser: ${all_time['largest_loser']}")
        else:
            print(f"Largest loser: —")
    else:
        print("No closed trades yet.")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()

