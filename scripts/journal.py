import json
from pathlib import Path

from common import JOURNAL_DIR, STATE_DIR, get_account, get_orders, get_positions, today_str, write_heartbeat, resolve_state


def main():
    date = today_str()
    account = get_account()
    positions = get_positions()
    orders = get_orders(status="all", limit=20)

    # Growth bot state files (primary)
    candidates_growth_path = resolve_state("growth", "candidates.json")
    order_plan_growth_path = resolve_state("growth", "order_plan.json")
    manage_log_growth_path = resolve_state("growth", "manage_log.json")
    tracking_growth_path = resolve_state("growth", "position_tracking.json")

    # Legacy fallback paths
    if not candidates_growth_path.exists():
        candidates_growth_path = STATE_DIR / "candidates_growth.json"
    if not order_plan_growth_path.exists():
        order_plan_growth_path = STATE_DIR / "order_plan_growth.json"
    if not manage_log_growth_path.exists():
        manage_log_growth_path = STATE_DIR / "manage_log_growth.json"
    if not tracking_growth_path.exists():
        tracking_growth_path = STATE_DIR / "position_tracking_growth.json"

    # Read per-job heartbeats
    hb_parts = []
    for job in ("research_growth", "trade_growth", "manage_growth", "journal", "performance"):
        hb_path = STATE_DIR / f"heartbeat_{job}.json"
        if hb_path.exists():
            hb_data = json.loads(hb_path.read_text())
            hb_parts.append(f"{job}={hb_data.get('status', '?')}")

    lines = []
    lines.append(f"# Trading Journal - {date}")
    lines.append("")
    lines.append("## Account")
    lines.append(f"- Equity: ${float(account.get('equity', 0)):,.2f}")
    lines.append(f"- Cash: ${float(account.get('cash', 0)):,.2f}")
    lines.append(f"- Portfolio value: ${float(account.get('portfolio_value', 0)):,.2f}")
    lines.append(f"- Buying power: ${float(account.get('buying_power', 0)):,.2f}")
    lines.append("")

    # P&L summary
    lines.append("## P&L")
    total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    lines.append(f"- Open P&L: ${total_unrealized:,.2f}")
    lines.append(f"- Positions: {len(positions)}")
    # Check for today's closed trades
    todays_fills = [o for o in orders if o.get("status") == "filled" and o.get("filled_at", "").startswith(date)]
    sells_today = [o for o in todays_fills if o.get("side") == "sell"]
    if sells_today:
        lines.append(f"- Sells filled today: {len(sells_today)}")
        for s in sells_today:
            lines.append(f"  - {s.get('symbol')} {s.get('qty')} shares @ ${float(s.get('filled_avg_price', 0)):,.2f}")
    lines.append("")
    lines.append("## Open positions")
    if positions:
        for p in positions:
            lines.append(f"- {p.get('symbol')}: qty={p.get('qty')} unrealized_pl={p.get('unrealized_pl')} market_value={p.get('market_value')}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Recent orders")
    if orders:
        for o in orders[:10]:
            lines.append(f"- {o.get('submitted_at')} {o.get('symbol')} {o.get('side')} {o.get('qty')} status={o.get('status')} type={o.get('type')}")
    else:
        lines.append("- None")
    lines.append("")

    # Growth position management actions
    lines.append("## Position management")
    if manage_log_growth_path.exists():
        try:
            mlog = json.loads(manage_log_growth_path.read_text())
            actions = mlog.get("actions", [])
            if actions:
                failures = [a for a in actions if "failed" in a.get("action", "") or a.get("MANUAL_REVIEW")]
                holds = [a for a in actions if a.get("action", "").startswith("hold")]
                exits = [a for a in actions if any(k in a.get("action", "") for k in ("trailing", "protected", "exit", "time_stop"))]

                if failures:
                    lines.append("### ⚠️ FAILURES REQUIRING REVIEW")
                    for f in failures:
                        lines.append(f"- **{f.get('symbol', 'N/A')}**: {f.get('action')} — {f.get('error', '')}")
                    lines.append("")

                if exits:
                    lines.append("### Phase transitions & exits")
                    for e in exits:
                        lines.append(f"- {e.get('symbol', 'N/A')}: {e.get('action')}")
                    lines.append("")

                if holds:
                    lines.append("### Holding")
                    for h in holds:
                        lines.append(f"- {h.get('symbol')}: {h.get('action')} | price={h.get('price')} R={h.get('r')} bars={h.get('bars')} stop={h.get('current_stop')}")
                    lines.append("")
            else:
                lines.append("- No positions to manage")
                lines.append("")
        except Exception:
            lines.append("- Error reading manage log")
            lines.append("")
    else:
        lines.append("- manage_log not found (manage_growth.py may not have run)")
        lines.append("")

    lines.append("## Files")
    lines.append(f"- Candidates file exists: {candidates_growth_path.exists()}")
    lines.append(f"- Order plan file exists: {order_plan_growth_path.exists()}")
    lines.append(f"- Manage log exists: {manage_log_growth_path.exists()}")
    lines.append(f"- Heartbeats: {', '.join(hb_parts) if hb_parts else 'none found'}")
    lines.append("")

    # Performance stats (if available)
    perf_path = STATE_DIR / "performance.json"
    if perf_path.exists():
        perf = json.loads(perf_path.read_text())
        all_time = perf.get("all_time", {})
        last_30 = perf.get("last_30_days", {})
        if all_time.get("total_trades", 0) > 0:
            lines.append("## Performance")
            lines.append(f"- Total closed trades: {all_time['total_trades']}")
            lines.append(f"- Win rate: {all_time.get('win_rate', 0)}%")
            lines.append(f"- Profit factor: {all_time.get('profit_factor', 0)}")
            lines.append(f"- Total P&L: ${all_time.get('total_pnl', 0):,.2f}")
            lines.append(f"- Avg R: {all_time.get('avg_r', 'N/A')}")
            lines.append(f"- Largest winner: ${all_time.get('largest_winner', 0):,.2f}")
            lines.append(f"- Largest loser: ${all_time.get('largest_loser', 0):,.2f}")
            if last_30.get("total_trades", 0) > 0:
                lines.append(f"- Last 30d trades: {last_30['total_trades']} | P&L: ${last_30.get('total_pnl', 0):,.2f}")
            lines.append("")

    # ── GROWTH BOT POSITIONS ──
    lines.append("---")
    lines.append("")
    lines.append("## Growth Bot Positions")

    # Growth tracking state
    if tracking_growth_path.exists():
        try:
            gt = json.loads(tracking_growth_path.read_text())
            if gt:
                for sym, tr in gt.items():
                    phase = tr.get("phase", "?")
                    best_r = tr.get("best_gain_r", 0)
                    bars = tr.get("bars_held", 0)
                    bars_profit = tr.get("bars_in_profit", 0)
                    setup = tr.get("setup_type", "?")
                    manual = " ⚠️REVIEW" if tr.get("MANUAL_REVIEW") else ""
                    lines.append(f"- **{sym}**: phase={phase} | setup={setup} | best_R={best_r:.1f} | bars={bars} | bars_in_profit={bars_profit}{manual}")
            else:
                lines.append("- No growth positions tracked")
        except Exception:
            lines.append("- Error reading growth tracking")
    else:
        lines.append("- No growth tracking file")

    # Growth candidates summary
    if candidates_growth_path.exists():
        try:
            cg = json.loads(candidates_growth_path.read_text())
            regime = cg.get("regime_mode", "?")
            cands = cg.get("candidates", [])
            lines.append("")
            lines.append(f"### Research: regime={regime}, candidates={len(cands)}")
            for c in cands[:5]:
                lines.append(f"- {c['symbol']}: {c.get('setup_type')} score={c.get('score')}")
        except Exception:
            pass

    lines.append("")
    lines.append("## Notes")
    lines.append("- Review any skipped trades and confirm they were blocked for a good reason.")
    lines.append("- Check position management failures above — these need manual intervention.")
    lines.append("- Compare paper fills with market prices before switching to live.")
    lines.append("")

    path = JOURNAL_DIR / f"{date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    write_heartbeat("journal", "ok", {"journal_file": str(path)})
    print(f"Journal written: {path}")


if __name__ == "__main__":
    main()
