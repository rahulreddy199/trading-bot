"""
Slack Bot — Interactive trading assistant.

Runs alongside orchestrator.py via Socket Mode (no public URL needed).
Commands:
  /positions  — Show all open positions with P&L
  /sell SYMBOL — Sell a position (requires confirmation passcode)
  /status     — Bot health check (heartbeats, account equity)
  /orders     — Show open/pending orders
  /kill       — Activate kill switch (halt all new entries)
  /resume     — Deactivate kill switch

Setup:
  1. Create a Slack App at https://api.slack.com/apps
  2. Enable Socket Mode (Settings → Socket Mode → Enable)
  3. Add Bot Token Scopes: chat:write, commands
  4. Add Slash Commands: /positions, /sell, /status, /orders, /kill, /resume
  5. Install to workspace
  6. Add tokens to .env:
     SLACK_BOT_TOKEN=xoxb-...
     SLACK_APP_TOKEN=xapp-...
     SELL_PASSCODE=your_secret_code
"""
import json
import os
import sys
import hashlib
from datetime import datetime
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    MARKET_TZ,
    STATE_DIR,
    get_account,
    get_clock,
    get_positions,
    alpaca_get,
    alpaca_post,
    get_env,
    now_iso,
    save_json,
    send_alert,
)

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


# --- Config ---
SLACK_BOT_TOKEN = get_env("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = get_env("SLACK_APP_TOKEN", "")
SELL_PASSCODE = get_env("SELL_PASSCODE", "SELL123")  # Change this!

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    print("❌ Missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN in .env")
    print("   See docstring in this file for setup instructions.")
    sys.exit(1)

app = App(token=SLACK_BOT_TOKEN)


# --- /positions command ---
@app.command("/positions")
def handle_positions(ack, respond):
    ack()
    try:
        positions = get_positions()
        account = get_account()
        equity = float(account["equity"])

        if not positions:
            respond("📭 No open positions.")
            return

        lines = [f"📊 *Open Positions* ({len(positions)}) | Equity: ${equity:,.2f}\n"]
        total_unrealized = 0.0

        for p in positions:
            sym = p["symbol"]
            qty = p["qty"]
            avg_entry = float(p["avg_entry_price"])
            current = float(p["current_price"])
            unrealized = float(p.get("unrealized_pl", 0))
            unrealized_pct = float(p.get("unrealized_plpc", 0)) * 100
            market_val = float(p.get("market_value", 0))
            total_unrealized += unrealized

            emoji = "🟢" if unrealized >= 0 else "🔴"
            lines.append(
                f"{emoji} *{sym}* — {qty} shares @ ${avg_entry:.2f}\n"
                f"      Now: ${current:.2f} | P&L: ${unrealized:+,.2f} ({unrealized_pct:+.1f}%) | Value: ${market_val:,.2f}"
            )

        lines.append(f"\n💰 *Total unrealized P&L: ${total_unrealized:+,.2f}*")
        respond("\n".join(lines))

    except Exception as e:
        respond(f"❌ Error fetching positions: {e}")


# --- /sell command ---
@app.command("/sell")
def handle_sell(ack, respond, command):
    ack()
    text = command.get("text", "").strip()
    parts = text.split()

    if len(parts) < 2:
        respond(
            "⚠️ Usage: `/sell SYMBOL PASSCODE`\n"
            "Example: `/sell AAPL SELL123`\n"
            "This will market-sell your entire position in that symbol."
        )
        return

    symbol = parts[0].upper()
    passcode = parts[1] if len(parts) > 1 else ""

    # Verify passcode
    if passcode != SELL_PASSCODE:
        respond("🔒 Incorrect passcode. Sale blocked.")
        return

    # Verify position exists
    try:
        positions = get_positions()
        position = next((p for p in positions if p["symbol"] == symbol), None)

        if not position:
            respond(f"❌ No open position in *{symbol}*.")
            return

        qty = int(float(position["qty"]))
        avg_entry = float(position["avg_entry_price"])
        current = float(position["current_price"])
        unrealized = float(position.get("unrealized_pl", 0))

        # Submit market sell
        date_str = datetime.now(MARKET_TZ).strftime("%Y%m%d")
        key = f"{date_str}_manual_sell_{symbol}"
        hash_suffix = hashlib.sha256(key.encode()).hexdigest()[:8]
        client_id = f"bot_manual_{symbol}_{date_str}_{hash_suffix}"

        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_id,
        }
        resp = alpaca_post("/v2/orders", payload)
        order_id = resp.get("id", "unknown")

        respond(
            f"✅ *SOLD* {symbol} — {qty} shares at market\n"
            f"Entry: ${avg_entry:.2f} | Last: ${current:.2f} | P&L: ${unrealized:+,.2f}\n"
            f"Order ID: `{order_id}`"
        )
        send_alert(f"🔴 MANUAL SELL via Slack: {symbol} x{qty} | P&L: ${unrealized:+,.2f}", level="trade")

    except Exception as e:
        respond(f"❌ Sell failed: {e}")


# --- /status command ---
@app.command("/status")
def handle_status(ack, respond):
    ack()
    try:
        account = get_account()
        clock = get_clock()
        positions = get_positions()

        equity = float(account["equity"])
        cash = float(account["cash"])
        market_open = clock.get("is_open", False)

        # Read heartbeats
        hb_lines = []
        for job in ("research", "trade", "manage"):
            hb_path = STATE_DIR / f"heartbeat_{job}.json"
            if hb_path.exists():
                hb = json.loads(hb_path.read_text())
                ts = hb.get("timestamp", "?")[:16]
                status = hb.get("status", "?")
                hb_lines.append(f"  • {job}: {status} @ {ts}")
            else:
                hb_lines.append(f"  • {job}: no heartbeat")

        # Kill switch
        kill_active = (STATE_DIR / "KILL_SWITCH").exists()

        respond(
            f"🤖 *Bot Status*\n"
            f"Market: {'🟢 Open' if market_open else '🔴 Closed'}\n"
            f"Equity: ${equity:,.2f} | Cash: ${cash:,.2f}\n"
            f"Positions: {len(positions)} open\n"
            f"Kill switch: {'🛑 ACTIVE' if kill_active else '✅ Off'}\n"
            f"\n*Last heartbeats:*\n" + "\n".join(hb_lines)
        )

    except Exception as e:
        respond(f"❌ Error: {e}")


# --- /orders command ---
@app.command("/orders")
def handle_orders(ack, respond):
    ack()
    try:
        orders = alpaca_get("/v2/orders", params={"status": "open", "limit": 20})

        if not orders:
            respond("📭 No open orders.")
            return

        lines = [f"📋 *Open Orders* ({len(orders)})\n"]
        for o in orders:
            sym = o.get("symbol")
            side = o.get("side")
            qty = o.get("qty")
            order_type = o.get("type")
            status = o.get("status")
            stop_price = o.get("stop_price", "—")
            limit_price = o.get("limit_price", "—")

            lines.append(
                f"• *{sym}* {side} {qty} | type={order_type} | "
                f"stop={stop_price} limit={limit_price} | status={status}"
            )

        respond("\n".join(lines))

    except Exception as e:
        respond(f"❌ Error: {e}")


# --- /kill command ---
@app.command("/kill")
def handle_kill(ack, respond):
    ack()
    kill_path = STATE_DIR / "KILL_SWITCH"
    kill_path.touch()
    respond("🛑 *Kill switch ACTIVATED.* No new entries will be placed until removed.")
    send_alert("🛑 Kill switch activated via Slack command", level="error")


# --- /resume command ---
@app.command("/resume")
def handle_resume(ack, respond, command):
    ack()
    text = command.get("text", "").strip()

    if text != SELL_PASSCODE:
        respond("🔒 Usage: `/resume PASSCODE` — passcode required to deactivate kill switch.")
        return

    kill_path = STATE_DIR / "KILL_SWITCH"
    if kill_path.exists():
        kill_path.unlink()
        respond("✅ Kill switch *deactivated*. New entries are allowed.")
        send_alert("✅ Kill switch deactivated via Slack command", level="info")
    else:
        respond("ℹ️ Kill switch was not active.")


# --- Main ---
if __name__ == "__main__":
    print("🤖 Slack bot starting (Socket Mode)...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()

