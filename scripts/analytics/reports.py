"""
Report generation — daily and weekly markdown reports.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    STATE_DIR, STATE_SHARED, JOURNAL_DIR,
    save_json, today_str, now_iso, resolve_state,
    get_positions, get_account,
)


def _load_json(path):
    """Safely load JSON file, return {} or [] on failure."""
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data
    except Exception:
        pass
    return {}


def generate_daily_report(analytics=None, attribution=None):
    """Generate daily markdown review from analytics outputs."""
    if analytics is None:
        analytics = _load_json(STATE_SHARED / "analytics_daily.json")
    if attribution is None:
        attribution = _load_json(STATE_SHARED / "attribution_daily.json")

    date = analytics.get("date", today_str())
    regime = analytics.get("regime", {})
    metrics = analytics.get("metrics_all_time", {})
    m7d = analytics.get("metrics_7d", {})
    incidents = analytics.get("incidents", {})

    lines = []
    lines.append(f"# Daily Review — {date}")
    lines.append("")

    # ── HEADLINE METRICS ──
    lines.append("## Headline Metrics")
    lines.append("| Metric | All-Time | 7-Day |")
    lines.append("|--------|----------|-------|")
    lines.append(f"| Trades | {metrics.get('total_trades', 0)} | {m7d.get('total_trades', 0)} |")
    lines.append(f"| Net PnL | ${metrics.get('net_pnl', 0):,.2f} | ${m7d.get('net_pnl', 0):,.2f} |")
    lines.append(f"| Win Rate | {metrics.get('win_rate', 0)*100:.1f}% | {m7d.get('win_rate', 0)*100:.1f}% |")
    pf_all = metrics.get('profit_factor', 0)
    pf_7d = m7d.get('profit_factor', 0)
    pf_all_str = f"{pf_all:.2f}" if isinstance(pf_all, (int, float)) and pf_all != float('inf') else str(pf_all)
    pf_7d_str = f"{pf_7d:.2f}" if isinstance(pf_7d, (int, float)) and pf_7d != float('inf') else str(pf_7d)
    lines.append(f"| Profit Factor | {pf_all_str} | {pf_7d_str} |")
    lines.append(f"| Avg R | {metrics.get('avg_r', 0)} | {m7d.get('avg_r', 0)} |")
    lines.append(f"| Avg Hold | {metrics.get('avg_hold_time', 0)} bars | {m7d.get('avg_hold_time', 0)} bars |")
    lines.append(f"| Slippage | {metrics.get('avg_slippage_bps', 0)} bps | {m7d.get('avg_slippage_bps', 0)} bps |")
    lines.append("")

    # ── ACCOUNT ──
    lines.append("## Account")
    lines.append(f"- Equity: ${analytics.get('equity', 0):,.2f}")
    lines.append(f"- Open positions: {analytics.get('open_positions', 0)}")
    lines.append(f"- Unrealized P&L: ${analytics.get('unrealized_pnl', 0):,.2f}")
    lines.append("")

    # ── MARKET REGIME ──
    lines.append("## Market Regime")
    lines.append(f"- Label: **{regime.get('regime_label', 'unknown')}**")
    lines.append(f"- SPY > 50 SMA: {regime.get('spy_above_50sma', '?')}")
    lines.append(f"- SPY > 200 SMA: {regime.get('spy_above_200sma', '?')}")
    lines.append(f"- VIX: {regime.get('vix_value', '?')} ({regime.get('vix_level', '?')})")
    # Add research regime if available
    candidates = _load_json(resolve_state("growth", "candidates.json"))
    if candidates:
        lines.append(f"- Growth regime mode: **{candidates.get('regime_mode', '?')}**")
        breadth = candidates.get('breadth_proxy_score')
        if breadth is not None:
            lines.append(f"- Breadth proxy: {breadth}%")
    lines.append("")

    # ── OPEN POSITIONS (detailed) ──
    lines.append("## Open Positions")
    growth_tracking = _load_json(resolve_state("growth", "position_tracking.json"))
    conservative_tracking = _load_json(resolve_state("conservative", "position_tracking.json"))

    try:
        positions = get_positions()
    except Exception:
        positions = []

    if positions:
        lines.append("| Symbol | Bot | Setup | Phase | Entry | Current | P&L | R | Best R | Bars | Stop |")
        lines.append("|--------|-----|-------|-------|-------|---------|-----|---|--------|------|------|")
        for pos in positions:
            sym = pos.get("symbol", "?")
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            upl = float(pos.get("unrealized_pl", 0))
            qty = pos.get("qty", 0)

            # Find tracking data
            track = growth_tracking.get(sym) or conservative_tracking.get(sym) or {}
            bot = "growth" if sym in growth_tracking else "conservative" if sym in conservative_tracking else "?"
            setup = track.get("setup_type", "?")
            phase = track.get("phase", "?")
            current_r = 0
            r_per = track.get("r_per_share", 0)
            if r_per and r_per > 0:
                current_r = round((current - entry) / r_per, 2)
            best_r = track.get("best_gain_r", 0)
            bars = track.get("bars_held", 0)
            stop = track.get("current_stop") or track.get("initial_stop") or "?"
            stop_str = f"${stop:,.2f}" if isinstance(stop, (int, float)) else str(stop)

            lines.append(
                f"| {sym} | {bot} | {setup} | {phase} | ${entry:,.2f} | ${current:,.2f} "
                f"| ${upl:+,.2f} | {current_r}R | {best_r}R | {bars} | {stop_str} |"
            )
    else:
        lines.append("- No open positions")
    lines.append("")

    # ── TODAY'S MANAGEMENT ACTIONS ──
    lines.append("## Position Management Actions")
    manage_log = _load_json(resolve_state("growth", "manage_log.json"))
    manage_actions = manage_log.get("actions", [])
    if manage_actions:
        for a in manage_actions:
            sym = a.get("symbol", "?")
            action = a.get("action", "?")
            price = a.get("price")
            r_val = a.get("r")
            trail = a.get("trail")
            stop = a.get("stop") or a.get("current_stop")
            detail_parts = []
            if price: detail_parts.append(f"price=${price:,.2f}")
            if r_val is not None: detail_parts.append(f"R={r_val}")
            if trail: detail_parts.append(f"trail=${trail:,.2f}")
            if stop: detail_parts.append(f"stop=${stop:,.2f}" if isinstance(stop, (int, float)) else f"stop={stop}")
            if a.get("MANUAL_REVIEW"): detail_parts.append("⚠️ MANUAL REVIEW")
            detail = " | ".join(detail_parts) if detail_parts else ""
            lines.append(f"- **{sym}**: {action} — {detail}")
    else:
        lines.append("- No management actions today")
    lines.append("")

    # ── TODAY'S RESEARCH SUMMARY ──
    lines.append("## Research Summary")
    if candidates:
        n_candidates = len(candidates.get("candidates", []))
        n_rejected = len(candidates.get("rejected", []))
        lines.append(f"- Candidates found: {n_candidates}")
        lines.append(f"- Rejected: {n_rejected}")
        lines.append(f"- Regime: {candidates.get('regime_mode', '?')}")

        # Show candidates if any
        for c in candidates.get("candidates", [])[:10]:
            lines.append(f"  - **{c.get('symbol')}**: setup={c.get('setup_type', '?')}, "
                         f"score={c.get('growth_score', '?'):.3f}" if isinstance(c.get('growth_score'), (int, float)) else
                         f"  - **{c.get('symbol')}**: setup={c.get('setup_type', '?')}")

        # Top rejection reasons
        rejection_reasons = {}
        for r in candidates.get("rejected", []):
            reasons = r.get("reasons", [])
            for reason in reasons:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        if rejection_reasons:
            lines.append("- **Top rejection reasons:**")
            for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  - {reason}: {count}")
    else:
        lines.append("- No research data available")
    lines.append("")

    # ── TODAY'S ORDERS ──
    lines.append("## Orders Today")
    order_plan = _load_json(resolve_state("growth", "order_plan.json"))
    orders = order_plan.get("orders", [])
    skips = order_plan.get("skips", [])
    if orders:
        for o in orders:
            lines.append(f"- ✅ **{o.get('symbol')}**: {o.get('qty')} shares @ trigger=${o.get('trigger', '?'):.2f}, "
                         f"stop=${o.get('stop', '?'):.2f}, R/share=${o.get('r_per_share', '?'):.2f}")
    elif order_plan:
        lines.append("- No orders placed")
    else:
        lines.append("- No order plan data")
    if skips:
        lines.append(f"- Skipped: {len(skips)}")
        for s in skips[:5]:
            lines.append(f"  - {s.get('symbol', '?')}: {s.get('reason', '?')}")
    lines.append("")

    # ── CLOSED TRADES TODAY ──
    lines.append("## Trades Closed Today")
    # Check multiple possible locations for trade history
    trade_history_raw = None
    for th_path in [
        STATE_SHARED / "trade_history.json",
        resolve_state("growth", "trade_history.json"),
        STATE_DIR / "trade_history.json",
    ]:
        if th_path.exists():
            trade_history_raw = _load_json(th_path)
            break

    # Handle both list format and {trades: [...]} wrapper
    if isinstance(trade_history_raw, list):
        trade_list = trade_history_raw
    elif isinstance(trade_history_raw, dict):
        trade_list = trade_history_raw.get("trades", [])
    else:
        trade_list = []

    if trade_list:
        todays_closes = [t for t in trade_list if t.get("closed_at", "").startswith(date)]
        if todays_closes:
            for t in todays_closes:
                sym = t.get("symbol", "?")
                pnl = t.get("pnl")
                r_mult = t.get("r_multiple")
                exit_reason = t.get("exit_type") or t.get("exit_reason", "?")
                entry_p = t.get("entry_price", 0)
                exit_p = t.get("exit_price", 0)
                bars = t.get("bars_held", "?")
                setup = t.get("setup_type", "?")
                qty = t.get("qty", "?")
                pnl_str = f"${pnl:+,.2f}" if pnl is not None else "?"
                r_str = f"{r_mult:.2f}R" if r_mult is not None else "?"
                lines.append(f"- **{sym}**: {pnl_str} ({r_str}) | exit={exit_reason} | "
                             f"entry=${entry_p:,.2f} → exit=${exit_p:,.2f} | qty={qty} | {bars} bars | setup={setup}")
        else:
            lines.append("- No trades closed today")
    else:
        lines.append("- No trade history data")
    lines.append("")

    # ── BEST/WORST CONTRIBUTORS ──
    top = attribution.get("top_contributors", {})
    if top.get("best"):
        lines.append("## Best/Worst Contributors (All-Time)")
        lines.append("| Symbol | Net PnL |")
        lines.append("|--------|---------|")
        for sym, pnl in top.get("best", [])[:5]:
            lines.append(f"| {sym} | ${pnl:,.2f} |")
        lines.append("")
        if top.get("worst"):
            actual_losers = [(sym, pnl) for sym, pnl in top.get("worst", [])[:3] if pnl < 0]
            if actual_losers:
                lines.append("**Worst:**")
                for sym, pnl in actual_losers:
                    lines.append(f"- {sym}: ${pnl:,.2f}")
            lines.append("")

    # ── OPERATIONAL ISSUES ──
    lines.append("## Operational Issues")
    total_incidents = sum(incidents.values()) if incidents else 0
    if total_incidents == 0:
        lines.append("- ✅ No incidents today")
    else:
        for k, v in incidents.items():
            if v > 0:
                lines.append(f"- ⚠️ {k}: {v}")
    lines.append("")

    # ── MANUAL REVIEW ──
    lines.append("## Open Manual-Review Items")
    health_path = STATE_SHARED / "health_summary.json"
    if health_path.exists():
        health = _load_json(health_path)
        flags = health.get("manual_review_flags", [])
        if flags:
            for f in flags:
                lines.append(f"- {f['bot']}/{f['symbol']}: {f['reason']}")
        else:
            lines.append("- None")
    else:
        lines.append("- Health summary not available")
    lines.append("")

    # ── AI REVIEW ──
    lines.append("## AI Recommendations")
    ai_review = _load_json(STATE_SHARED / "ai_review.json")
    recs = ai_review.get("recommendations", [])
    if recs:
        for r in recs:
            conf = r.get("confidence", "?")
            action = r.get("next_action", "?")
            lines.append(f"- [{conf}] **{r.get('recommendation', '?')}** → {action}")
            lines.append(f"  - {r.get('reason', '')}")
    else:
        lines.append("- No recommendations")
    lines.append("")

    # ── EQUITY CURVE ──
    lines.append("## Equity Snapshot")
    equity_curve = _load_json(STATE_SHARED / "equity_curve.json")
    if isinstance(equity_curve, list) and len(equity_curve) >= 2:
        latest = equity_curve[-1]
        prev = equity_curve[-2]
        eq_now = latest.get("equity", 0)
        eq_prev = prev.get("equity", 0)
        day_change = eq_now - eq_prev
        day_pct = (day_change / eq_prev * 100) if eq_prev else 0
        total_change = eq_now - 20000  # starting capital
        total_pct = (total_change / 20000 * 100)
        lines.append(f"- Today: ${eq_now:,.2f} ({day_change:+,.2f} / {day_pct:+.2f}%)")
        lines.append(f"- Total return: ${total_change:+,.2f} ({total_pct:+.2f}%)")
        lines.append(f"- Data points: {len(equity_curve)} days")
    elif isinstance(equity_curve, list) and len(equity_curve) == 1:
        eq_now = equity_curve[-1].get("equity", 0)
        lines.append(f"- Today: ${eq_now:,.2f}")
    else:
        lines.append("- No equity curve data yet")
    lines.append("")

    # ── MARKET CONTEXT (for AI pattern learning) ──
    lines.append("## Market Context")
    try:
        import yfinance as yf
        spy_data = yf.download("SPY", period="2d", interval="1d", progress=False)
        qqq_data = yf.download("QQQ", period="2d", interval="1d", progress=False)
        if not spy_data.empty and len(spy_data) >= 1:
            spy_close = float(spy_data["Close"].iloc[-1].iloc[0]) if hasattr(spy_data["Close"].iloc[-1], 'iloc') else float(spy_data["Close"].iloc[-1])
            spy_pct = 0
            if len(spy_data) >= 2:
                spy_prev = float(spy_data["Close"].iloc[-2].iloc[0]) if hasattr(spy_data["Close"].iloc[-2], 'iloc') else float(spy_data["Close"].iloc[-2])
                spy_pct = ((spy_close - spy_prev) / spy_prev) * 100
            lines.append(f"- SPY: ${spy_close:,.2f} ({spy_pct:+.2f}%)")
        if not qqq_data.empty and len(qqq_data) >= 1:
            qqq_close = float(qqq_data["Close"].iloc[-1].iloc[0]) if hasattr(qqq_data["Close"].iloc[-1], 'iloc') else float(qqq_data["Close"].iloc[-1])
            qqq_pct = 0
            if len(qqq_data) >= 2:
                qqq_prev = float(qqq_data["Close"].iloc[-2].iloc[0]) if hasattr(qqq_data["Close"].iloc[-2], 'iloc') else float(qqq_data["Close"].iloc[-2])
                qqq_pct = ((qqq_close - qqq_prev) / qqq_prev) * 100
            lines.append(f"- QQQ: ${qqq_close:,.2f} ({qqq_pct:+.2f}%)")
    except Exception:
        lines.append("- Market data unavailable")
    lines.append("")

    # ── POSITION INTRADAY CONTEXT ──
    lines.append("## Position Price Context")
    if positions and growth_tracking:
        for pos in positions:
            sym = pos.get("symbol", "?")
            track = growth_tracking.get(sym) or conservative_tracking.get(sym) or {}
            current = float(pos.get("current_price", 0))
            entry = float(pos.get("avg_entry_price", 0))
            stop = track.get("current_stop") or track.get("initial_stop")
            best_price = track.get("best_price", 0)

            # Distance to stop (how close to getting stopped out)
            if stop and current > 0:
                stop_distance_pct = ((current - stop) / current) * 100
                lines.append(f"- **{sym}**: price=${current:,.2f} | "
                             f"stop distance={stop_distance_pct:.1f}% | "
                             f"best price=${best_price:,.2f} | "
                             f"from best={((current - best_price) / best_price * 100):+.1f}%")
            else:
                lines.append(f"- **{sym}**: price=${current:,.2f}")
    else:
        lines.append("- No position data")
    lines.append("")

    # ── NEAR-MISS CANDIDATES (almost qualified) ──
    lines.append("## Near-Miss Candidates")
    if candidates:
        rejected = candidates.get("rejected", [])
        # Find stocks that failed only 1 filter (closest to qualifying)
        near_misses = [r for r in rejected if len(r.get("reasons", [])) == 1]
        if near_misses:
            for nm in near_misses[:5]:
                sym = nm.get("symbol", "?")
                reasons = nm.get("reasons", [])
                lines.append(f"- **{sym}**: missed by → {reasons[0]}")
        else:
            # Show stocks with fewest rejections
            sorted_rej = sorted(rejected, key=lambda r: len(r.get("reasons", [])))
            for nm in sorted_rej[:3]:
                sym = nm.get("symbol", "?")
                reasons = nm.get("reasons", [])
                lines.append(f"- **{sym}**: {len(reasons)} filters failed → {', '.join(reasons[:3])}")
        lines.append("")
        lines.append(f"_({len(rejected)} total rejected out of {len(rejected) + len(candidates.get('candidates', []))} scanned)_")
    else:
        lines.append("- No research data")
    lines.append("")

    # ── CORRELATION BLOCKS ──
    lines.append("## Correlation & Diversification")
    if order_plan:
        corr_blocks = [s for s in skips if "correlation" in s.get("reason", "")]
        if corr_blocks:
            for cb in corr_blocks:
                corr_with = cb.get("correlated_with", [])
                corr_names = ", ".join([f"{c.get('symbol')}({c.get('correlation',0):.2f})" for c in corr_with]) if corr_with else "?"
                lines.append(f"- **{cb.get('symbol', '?')}** blocked: correlated with {corr_names}")
        else:
            lines.append("- No correlation blocks today")
    else:
        lines.append("- No order data")
    # Current open position sectors
    if growth_tracking:
        open_sectors = {}
        watchlist = _load_json(Path(SCRIPTS_DIR).parent / "config" / "watchlist_growth.json")
        sector_map = {}
        if watchlist:
            for s in watchlist.get("symbols", []):
                sector_map[s["ticker"]] = s.get("sector", "?")
        for sym in growth_tracking:
            if growth_tracking[sym].get("phase") not in ("pending", "exit_pending", None):
                sector = sector_map.get(sym, "?")
                open_sectors[sector] = open_sectors.get(sector, 0) + 1
        if open_sectors:
            sectors_str = ", ".join([f"{s}: {c}" for s, c in sorted(open_sectors.items())])
            lines.append(f"- Open by sector: {sectors_str}")
    lines.append("")

    # ── TRADING ACTIVITY SUMMARY ──
    lines.append("## Trading Activity Summary")
    # Days since last entry
    if trade_list:
        all_entries = sorted([t.get("closed_at", "") for t in trade_list if t.get("closed_at")])
    # Count from order plans
    last_orders = _load_json(resolve_state("growth", "last_orders.json"))
    last_order_date = last_orders.get("date") if isinstance(last_orders, dict) else None
    if last_order_date:
        lines.append(f"- Last order placed: {last_order_date}")
    total_closed = len(trade_list) if trade_list else 0
    total_wins = sum(1 for t in trade_list if (t.get("pnl") or 0) > 0) if trade_list else 0
    total_losses = sum(1 for t in trade_list if (t.get("pnl") or 0) < 0) if trade_list else 0
    lines.append(f"- Total closed trades: {total_closed} (W:{total_wins} / L:{total_losses})")
    open_count = len([t for t in (growth_tracking or {}).values()
                      if t.get("phase") not in ("pending", "exit_pending", None)])
    max_pos = 5  # from strategy
    lines.append(f"- Open positions: {open_count}/{max_pos} slots used")
    # Setups traded
    if trade_list:
        setup_counts = {}
        for t in trade_list:
            st = t.get("setup_type") or t.get("source", "unknown")
            setup_counts[st] = setup_counts.get(st, 0) + 1
        if setup_counts:
            setups_str = ", ".join([f"{s}: {c}" for s, c in sorted(setup_counts.items(), key=lambda x: -x[1])])
            lines.append(f"- Trades by setup: {setups_str}")
    lines.append("")

    # ── INSUFFICIENT EVIDENCE ──
    lines.append("## Insufficient Evidence")
    if metrics.get("total_trades", 0) < 20:
        lines.append(f"- Only {metrics.get('total_trades', 0)} closed trades. Need 20+ for reliable metrics.")
    setup_attr = attribution.get("setup_type", {})
    for setup, data in setup_attr.items():
        if data.get("total_trades", 0) < 5:
            lines.append(f"- Setup '{setup}': only {data['total_trades']} trades (need 5+ for attribution)")
    if not lines[-1].startswith("-"):
        lines.append("- All dimensions have sufficient sample sizes ✅")
    lines.append("")

    # Save
    report_path = STATE_SHARED / f"report_daily_{date}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Daily report written: {report_path}")
    return "\n".join(lines)


def generate_weekly_report(analytics=None, attribution=None, experiments=None):
    """Generate weekly markdown review from analytics + all available state."""
    if analytics is None:
        analytics = _load_json(STATE_SHARED / "analytics_rolling.json")
    if attribution is None:
        attribution = _load_json(STATE_SHARED / "attribution_daily.json")
    if experiments is None:
        experiments = _load_json(STATE_SHARED / "experiments.json")
        if not experiments:
            experiments = {"experiments": []}

    date = today_str()
    m7d = analytics.get("7d", {})
    m30d = analytics.get("30d", {})
    all_time = analytics.get("all_time", {})

    lines = []
    lines.append(f"# Weekly Review — {date}")
    lines.append("")

    # ── PERFORMANCE SUMMARY ──
    lines.append("## Performance Summary")
    lines.append("| Window | Trades | Win Rate | PF | Avg R | Net PnL |")
    lines.append("|--------|--------|----------|-----|-------|---------|")
    for label, m in [("7d", m7d), ("30d", m30d), ("All", all_time)]:
        pf = m.get('profit_factor', 0)
        pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) and pf != float('inf') else str(pf)
        lines.append(f"| {label} | {m.get('total_trades',0)} | {m.get('win_rate',0)*100:.0f}% | {pf_str} | {m.get('avg_r',0)} | ${m.get('net_pnl',0):,.0f} |")
    lines.append("")

    # ── EQUITY CURVE ──
    lines.append("## Equity Curve")
    equity_curve = _load_json(STATE_SHARED / "equity_curve.json")
    if isinstance(equity_curve, list) and len(equity_curve) >= 2:
        latest = equity_curve[-1]
        first = equity_curve[0]
        eq_now = latest.get("equity", 0)
        eq_start = first.get("equity", 20000)
        total_return = eq_now - 20000
        total_pct = (total_return / 20000 * 100)
        # Week-over-week
        week_ago_eq = eq_start
        for e in equity_curve:
            week_ago_eq = e.get("equity", week_ago_eq)  # will end up being the last entry before 7d ago
        if len(equity_curve) >= 5:
            week_ago_eq = equity_curve[-min(5, len(equity_curve))].get("equity", eq_start)
        week_change = eq_now - week_ago_eq
        week_pct = (week_change / week_ago_eq * 100) if week_ago_eq else 0

        lines.append(f"- Current equity: **${eq_now:,.2f}**")
        lines.append(f"- Total return: **${total_return:+,.2f} ({total_pct:+.2f}%)**")
        lines.append(f"- Week change: ${week_change:+,.2f} ({week_pct:+.2f}%)")
        lines.append(f"- Data points: {len(equity_curve)} days")
        lines.append("")
        # Mini equity table
        lines.append("| Date | Equity | Change |")
        lines.append("|------|--------|--------|")
        for i, e in enumerate(equity_curve[-7:]):  # last 7 data points
            eq = e.get("equity", 0)
            d = e.get("date", "?")
            if i == 0:
                lines.append(f"| {d} | ${eq:,.2f} | — |")
            else:
                prev_eq = equity_curve[-7:][i-1].get("equity", eq)
                chg = eq - prev_eq
                lines.append(f"| {d} | ${eq:,.2f} | {chg:+,.2f} |")
    else:
        lines.append("- Not enough data for equity curve yet")
    lines.append("")

    # ── ALL CLOSED TRADES ──
    lines.append("## Trade History")
    trade_history_raw = None
    for th_path in [
        STATE_SHARED / "trade_history.json",
        resolve_state("growth", "trade_history.json"),
        STATE_DIR / "trade_history.json",
    ]:
        if th_path.exists():
            trade_history_raw = _load_json(th_path)
            break
    trade_list = []
    if isinstance(trade_history_raw, list):
        trade_list = trade_history_raw
    elif isinstance(trade_history_raw, dict):
        trade_list = trade_history_raw.get("trades", [])

    if trade_list:
        lines.append("| # | Symbol | Entry | Exit | P&L | R | Exit Type | Setup | Bars |")
        lines.append("|---|--------|-------|------|-----|---|-----------|-------|------|")
        for i, t in enumerate(trade_list, 1):
            sym = t.get("symbol", "?")
            entry = t.get("entry_price", 0)
            exit_p = t.get("exit_price", 0)
            pnl = t.get("pnl")
            r_mult = t.get("r_multiple")
            exit_type = t.get("exit_type") or t.get("exit_reason", "?")
            setup = t.get("setup_type") or t.get("source", "?")
            bars = t.get("bars_held", "?")
            pnl_str = f"${pnl:+,.2f}" if pnl is not None else "?"
            r_str = f"{r_mult:.2f}R" if r_mult is not None else "?"
            lines.append(f"| {i} | {sym} | ${entry:,.2f} | ${exit_p:,.2f} | {pnl_str} | {r_str} | {exit_type} | {setup} | {bars} |")
        lines.append("")

        # Win/loss summary
        total = len(trade_list)
        wins = sum(1 for t in trade_list if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trade_list if (t.get("pnl") or 0) < 0)
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trade_list)
        avg_win = 0
        avg_loss = 0
        win_pnls = [t.get("pnl", 0) for t in trade_list if (t.get("pnl") or 0) > 0]
        loss_pnls = [t.get("pnl", 0) for t in trade_list if (t.get("pnl") or 0) < 0]
        if win_pnls:
            avg_win = sum(win_pnls) / len(win_pnls)
        if loss_pnls:
            avg_loss = sum(loss_pnls) / len(loss_pnls)
        lines.append(f"**Summary**: {total} trades | {wins}W / {losses}L | "
                     f"Net: ${total_pnl:+,.2f} | Avg win: ${avg_win:,.2f} | Avg loss: ${avg_loss:,.2f}")
    else:
        lines.append("- No closed trades yet")
    lines.append("")

    # ── OPEN POSITIONS ──
    lines.append("## Open Positions")
    growth_tracking = _load_json(resolve_state("growth", "position_tracking.json"))
    try:
        positions = get_positions()
    except Exception:
        positions = []

    if positions:
        lines.append("| Symbol | Setup | Phase | Entry | Current | P&L | R | Best R | Bars | Stop |")
        lines.append("|--------|-------|-------|-------|---------|-----|---|--------|------|------|")
        for pos in positions:
            sym = pos.get("symbol", "?")
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            upl = float(pos.get("unrealized_pl", 0))
            track = growth_tracking.get(sym, {})
            setup = track.get("setup_type", "?")
            phase = track.get("phase", "?")
            r_per = track.get("r_per_share", 0)
            current_r = round((current - entry) / r_per, 2) if r_per and r_per > 0 else 0
            best_r = track.get("best_gain_r", 0)
            bars = track.get("bars_held", 0)
            stop = track.get("current_stop") or track.get("initial_stop") or "?"
            stop_str = f"${stop:,.2f}" if isinstance(stop, (int, float)) else str(stop)
            lines.append(f"| {sym} | {setup} | {phase} | ${entry:,.2f} | ${current:,.2f} | ${upl:+,.2f} | {current_r}R | {best_r}R | {bars} | {stop_str} |")
    else:
        lines.append("- No open positions")
    lines.append("")

    # ── ATTRIBUTION HIGHLIGHTS ──
    lines.append("## Attribution Highlights")
    for dim in ["setup_type", "regime", "sector"]:
        dim_data = attribution.get(dim, {})
        if dim_data:
            lines.append(f"\n### By {dim}")
            lines.append(f"| {dim} | Trades | Win Rate | Avg R | Net PnL |")
            lines.append(f"|------|--------|----------|-------|---------|")
            for k, v in sorted(dim_data.items(), key=lambda x: x[1].get("net_pnl", 0), reverse=True):
                net = v.get("net_pnl", 0)
                lines.append(f"| {k} | {v.get('total_trades',0)} | {v.get('win_rate',0)*100:.0f}% | {v.get('avg_r',0)} | ${net:+,.2f} |")
    lines.append("")

    # ── DAILY REPORT SUMMARIES ──
    lines.append("## This Week's Daily Summaries")
    import glob
    report_files = sorted(glob.glob(str(STATE_SHARED / "report_daily_*.md")))
    # Get last 7 reports
    for rf_path in report_files[-7:]:
        rf = Path(rf_path)
        try:
            content = rf.read_text()
            # Extract key lines
            report_date = rf.stem.replace("report_daily_", "")
            # Find equity, positions, orders, closed trades
            equity_line = ""
            positions_line = ""
            orders_line = ""
            closed_line = ""
            regime_line = ""
            for line in content.split("\n"):
                if line.startswith("- Equity:"):
                    equity_line = line.replace("- Equity: ", "")
                if line.startswith("- Open positions:") and "slots" not in line:
                    positions_line = line.replace("- Open positions: ", "")
                if "orders placed" in line.lower() or line.startswith("- ✅"):
                    orders_line = line.strip("- ")
                if line.startswith("- Growth regime mode:"):
                    regime_line = line.replace("- Growth regime mode: ", "").strip("*")
            lines.append(f"- **{report_date}**: {equity_line} | regime={regime_line} | {positions_line}")
        except Exception:
            pass
    if not report_files:
        lines.append("- No daily reports found")
    lines.append("")

    # ── AI REVIEW TRENDS ──
    lines.append("## AI Review Trends")
    ai_history = _load_json(STATE_SHARED / "ai_review_history.json")
    if isinstance(ai_history, list) and ai_history:
        # Count recommendation types across history
        rec_counts = {}
        for day_review in ai_history:
            for rec in day_review.get("recommendations", []):
                key = rec.get("recommendation", "?")
                rec_counts[key] = rec_counts.get(key, 0) + 1
        lines.append(f"- Reviews tracked: {len(ai_history)} days")
        lines.append("- **Recurring recommendations:**")
        for rec, count in sorted(rec_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  - {rec}: {count}× in {len(ai_history)} days")
    else:
        lines.append("- No AI review history yet")
    lines.append("")

    # ── STRATEGY OBSERVATIONS (for AI to reason about) ──
    lines.append("## Strategy Observations")
    candidates = _load_json(resolve_state("growth", "candidates.json"))
    if candidates:
        n_rejected = len(candidates.get("rejected", []))
        n_candidates = len(candidates.get("candidates", []))
        total_scanned = n_rejected + n_candidates
        if total_scanned > 0:
            pass_rate = (n_candidates / total_scanned * 100)
            lines.append(f"- Latest scan: {n_candidates}/{total_scanned} passed ({pass_rate:.0f}% pass rate)")
        # Rejection pattern
        rejection_reasons = {}
        for r in candidates.get("rejected", []):
            for reason in r.get("reasons", []):
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        if rejection_reasons:
            lines.append("- **Top rejection reasons (latest scan):**")
            for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  - {reason}: {count}/{total_scanned}")
    # Slot utilization
    open_count = len([t for t in growth_tracking.values()
                      if t.get("phase") not in ("pending", "exit_pending", None)])
    lines.append(f"- Slot utilization: {open_count}/5 ({open_count/5*100:.0f}%)")
    if open_count <= 2:
        lines.append("- ⚠️ Low utilization — filters may be too strict or market not offering setups")
    # Sector concentration
    watchlist = _load_json(Path(SCRIPTS_DIR).parent / "config" / "watchlist_growth.json")
    sector_map = {}
    if watchlist:
        for s in watchlist.get("symbols", []):
            sector_map[s["ticker"]] = s.get("sector", "?")
    open_sectors = {}
    for sym, track in growth_tracking.items():
        if track.get("phase") not in ("pending", "exit_pending", None):
            sector = sector_map.get(sym, "?")
            open_sectors[sector] = open_sectors.get(sector, 0) + 1
    if open_sectors:
        sectors_str = ", ".join([f"{s}: {c}" for s, c in sorted(open_sectors.items())])
        lines.append(f"- Open by sector: {sectors_str}")
        if len(open_sectors) == 1 and open_count >= 2:
            lines.append("- ⚠️ All positions in one sector — concentration risk")
    lines.append("")

    # ── EXPERIMENTS ──
    lines.append("## Active Experiments")
    active = [e for e in experiments.get("experiments", []) if e.get("status") == "active"]
    if active:
        for exp in active:
            lines.append(f"- **{exp['id']}**: {exp.get('hypothesis', '?')} (window: {exp.get('evaluation_window', '?')})")
    else:
        lines.append("- No active experiments")
    lines.append("")

    # ── WHAT TO WATCH NEXT WEEK ──
    lines.append("## What to Watch Next Week")
    if open_count == 0:
        lines.append("- No open positions — watch for new setups if regime stays full_risk")
    for sym, track in growth_tracking.items():
        phase = track.get("phase")
        best_r = track.get("best_gain_r", 0)
        bars = track.get("bars_held", 0)
        if phase == "initial" and bars >= 7:
            lines.append(f"- **{sym}**: {bars} bars in initial, approaching time stop (10 bars)")
        if phase == "trailing" and best_r >= 3:
            lines.append(f"- **{sym}**: strong runner at {best_r}R — watch for trail upgrades")
        if phase == "initial" and best_r < 0.5 and bars >= 3:
            lines.append(f"- **{sym}**: slow starter ({best_r}R after {bars} bars) — monitor closely")
    lines.append("")

    # ── INSUFFICIENT DATA ──
    total_trades = all_time.get("total_trades", 0) if all_time else 0
    if total_trades < 20:
        lines.append("## Insufficient Evidence")
        lines.append(f"- Only {total_trades} closed trades. Need 20+ for reliable weekly analysis.")
        lines.append("- Strategy tuning should remain **OFF** until sample size is sufficient.")
        lines.append("")

    # Save
    report_path = STATE_SHARED / f"report_weekly_{date}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Weekly report written: {report_path}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys as _sys
    if "--weekly" in _sys.argv:
        generate_weekly_report()
    else:
        generate_daily_report()

