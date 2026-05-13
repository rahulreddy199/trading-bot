"""
Autonomous Trading Bot Orchestrator

Uses Claude as the decision-making brain to:
1. Run daily trading workflow (research → trade → manage → journal)
2. Send alerts on all activity
3. Weekly: analyze performance and auto-tune strategy within guardrails

Runs as a persistent process with scheduled tasks.
"""
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import schedule
import time
import atexit
import signal

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from common import (
    STATE_DIR, CONFIG_DIR, JOURNAL_DIR,
    load_json, save_json, send_alert, get_env, now_iso,
)

# --- Configuration ---
ANTHROPIC_API_KEY = get_env("ANTHROPIC_API_KEY", "")
if ANTHROPIC_API_KEY in ("", "your_anthropic_key_here", "your_key_here", "sk-placeholder"):
    ANTHROPIC_API_KEY = ""
VENV_PYTHON = str(ROOT / "venv" / "bin" / "python")
API_USAGE_PATH = STATE_DIR / "api_usage.json"
BOT_LOG_PATH = STATE_DIR / "bot_log.json"
TUNING_WEEKLY_PATH = STATE_DIR / "tuning_weekly_counter.json"
LAST_RUN_PATH = STATE_DIR / "last_run_times.json"
LOCKFILE_PATH = STATE_DIR / "orchestrator.pid"
MAX_TURNS_PER_RUN = 8
MAX_WEEKLY_PARAM_CHANGES = 2

# Valid time windows for order placement (hour, minute) in local time
MORNING_TRADE_WINDOW = (9, 30, 15, 30)   # 9:30 AM - 3:30 PM
# No afternoon trades — afternoon is manage-only


# --- Single-instance PID lock ---

def acquire_lock():
    """Acquire a PID lockfile. Raises RuntimeError if another instance is running."""
    if LOCKFILE_PATH.exists():
        try:
            old_pid = int(LOCKFILE_PATH.read_text().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            raise RuntimeError(
                f"Another orchestrator is already running (PID {old_pid}). "
                f"Remove {LOCKFILE_PATH} if this is stale."
            )
        except ProcessLookupError:
            # Stale lockfile — previous process died
            pass
        except ValueError:
            pass  # Corrupt lockfile
    LOCKFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCKFILE_PATH.write_text(str(os.getpid()))


def release_lock():
    """Release the PID lockfile."""
    try:
        if LOCKFILE_PATH.exists():
            stored_pid = int(LOCKFILE_PATH.read_text().strip())
            if stored_pid == os.getpid():
                LOCKFILE_PATH.unlink()
    except Exception:
        pass


# --- Weekly tuning counter (hard code enforcement) ---

def get_weekly_tuning_count():
    """Return how many parameter changes have been applied this ISO week."""
    now = datetime.now()
    current_week = now.strftime("%G-W%V")  # ISO year-week
    if TUNING_WEEKLY_PATH.exists():
        try:
            data = load_json(TUNING_WEEKLY_PATH)
            if data.get("week") == current_week:
                return data.get("count", 0)
        except Exception:
            pass
    return 0


def increment_weekly_tuning_count(n=1):
    """Increment the weekly parameter change counter."""
    now = datetime.now()
    current_week = now.strftime("%G-W%V")
    data = {"week": current_week, "count": 0}
    if TUNING_WEEKLY_PATH.exists():
        try:
            data = load_json(TUNING_WEEKLY_PATH)
            if data.get("week") != current_week:
                data = {"week": current_week, "count": 0}
        except Exception:
            data = {"week": current_week, "count": 0}
    data["count"] = data.get("count", 0) + n
    save_json(TUNING_WEEKLY_PATH, data)


# --- Last run tracking for missed-job detection ---

def record_run(run_type, status="success"):
    """Record that a run completed with its status (success, partial, failed)."""
    data = {}
    if LAST_RUN_PATH.exists():
        try:
            data = load_json(LAST_RUN_PATH)
        except Exception:
            data = {}
    data[run_type] = {
        "timestamp": now_iso(),
        "status": status,
    }
    save_json(LAST_RUN_PATH, data)


def check_missed_jobs():
    """Check if any scheduled jobs were missed (e.g., after restart) and run them.
    Only catches up on same-day missed jobs. Failed runs are eligible for retry."""
    now = datetime.now()
    weekday = now.weekday()  # 0=Mon ... 6=Sun

    data = {}
    if LAST_RUN_PATH.exists():
        try:
            data = load_json(LAST_RUN_PATH)
        except Exception:
            data = {}

    today_str = now.strftime("%Y-%m-%d")

    def ran_successfully_today(run_type):
        entry = data.get(run_type, {})
        # Support both old format (string) and new format (dict)
        if isinstance(entry, dict):
            ts = entry.get("timestamp", "")
            status = entry.get("status", "unknown")
        elif isinstance(entry, str):
            ts = entry
            status = "success"  # backward compatibility
        else:
            return False
        return ts.startswith(today_str) and status == "success"

    missed = []

    # Weekday jobs
    if weekday < 5:  # Mon-Fri
        morning_time = now.replace(hour=9, minute=35, second=0, microsecond=0)
        afternoon_time = now.replace(hour=16, minute=5, second=0, microsecond=0)

        if now > morning_time and not ran_successfully_today("morning"):
            missed.append("morning")
        if now > afternoon_time and not ran_successfully_today("afternoon"):
            missed.append("afternoon")

    # Saturday weekly review
    if weekday == 5:  # Saturday
        review_time = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now > review_time and not ran_successfully_today("weekly_review"):
            missed.append("weekly_review")

    return missed


def is_within_trade_window():
    """Check if current time is within the valid window for placing new orders."""
    now = datetime.now()
    start_h, start_m, end_h, end_m = MORNING_TRADE_WINDOW
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now <= end


def log_event(event_type, data):
    """Append to bot log."""
    log = []
    if BOT_LOG_PATH.exists():
        try:
            log = load_json(BOT_LOG_PATH)
        except Exception:
            log = []
    log.append({
        "timestamp": now_iso(),
        "type": event_type,
        **data,
    })
    # Keep last 500 entries
    log = log[-500:]
    save_json(BOT_LOG_PATH, log)


def run_script(script_name, max_retries=2):
    """Run a trading script and capture output. Retries on failure."""
    script_path = SCRIPTS_DIR / f"{script_name}.py"
    if not script_path.exists():
        return {"success": False, "error": f"Script not found: {script_name}.py"}

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                [VENV_PYTHON, str(script_path)],
                capture_output=True, text=True, timeout=300,
                cwd=str(ROOT),
            )
            output = result.stdout.strip()
            errors = result.stderr.strip()
            success = result.returncode == 0

            if success or attempt == max_retries:
                return {
                    "success": success,
                    "output": output[-2000:] if output else "",
                    "errors": errors[-1000:] if errors else "",
                    "return_code": result.returncode,
                    "attempts": attempt + 1,
                }
            # Retry on failure
            log_event("script_retry", {"script": script_name, "attempt": attempt + 1, "error": errors[:500]})
            time.sleep(5 * (attempt + 1))
        except subprocess.TimeoutExpired:
            if attempt == max_retries:
                return {"success": False, "error": f"Script timed out (300s) after {attempt + 1} attempts"}
            time.sleep(5)
        except Exception as e:
            if attempt == max_retries:
                return {"success": False, "error": str(e)}
            time.sleep(5)


def read_state_file(filename):
    """Read a state or config file."""
    for base_dir in [STATE_DIR, CONFIG_DIR, JOURNAL_DIR]:
        path = base_dir / filename
        if path.exists():
            try:
                if filename.endswith(".json"):
                    return load_json(path)
                else:
                    return path.read_text()[:5000]
            except Exception as e:
                return {"error": str(e)}
    return {"error": f"File not found: {filename}"}


def track_api_usage(input_tokens, output_tokens):
    """Track API usage and costs. Returns True if under budget, False if budget exceeded."""
    usage = {"total_calls": 0, "total_input_tokens": 0, "total_output_tokens": 0,
             "estimated_cost_usd": 0, "month": datetime.now().strftime("%Y-%m")}
    if API_USAGE_PATH.exists():
        try:
            usage = load_json(API_USAGE_PATH)
        except Exception:
            pass
    # Reset monthly
    current_month = datetime.now().strftime("%Y-%m")
    if usage.get("month") != current_month:
        usage = {"total_calls": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                 "estimated_cost_usd": 0, "month": current_month}
    usage["total_calls"] += 1
    usage["total_input_tokens"] += input_tokens
    usage["total_output_tokens"] += output_tokens
    # Claude Sonnet pricing estimate
    cost = (input_tokens / 1_000_000 * 3) + (output_tokens / 1_000_000 * 15)
    usage["estimated_cost_usd"] = round(usage.get("estimated_cost_usd", 0) + cost, 4)
    save_json(API_USAGE_PATH, usage)
    return usage


def check_api_budget():
    """Check if we're within the monthly API budget. Returns (ok, spent, limit)."""
    try:
        guardrails = load_json(CONFIG_DIR / "guardrails.json")
        max_spend = guardrails.get("max_monthly_api_spend_usd", 10.0)
    except Exception:
        max_spend = 10.0

    if API_USAGE_PATH.exists():
        usage = load_json(API_USAGE_PATH)
        current_month = datetime.now().strftime("%Y-%m")
        if usage.get("month") == current_month:
            spent = usage.get("estimated_cost_usd", 0)
            return spent < max_spend, spent, max_spend
    return True, 0, max_spend



# --- Tool definitions for Claude ---
TOOLS = [
    {
        "name": "run_research",
        "description": "Scan the watchlist for trade candidates. Checks market regime, breadth, trend filters, pullback patterns, and confirmation candles.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_trade",
        "description": "Place stop-limit buy orders for qualified candidates. Requires research to have run first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_manage",
        "description": "Manage open positions: move stops to breakeven at 1R, activate trailing stops at 2R, early invalidation exits.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_journal",
        "description": "Write the daily trading journal with account status, positions, P&L, and management actions.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_performance",
        "description": "Calculate and save performance metrics (win rate, profit factor, avg R, equity curve).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_learning",
        "description": "Analyze recent trade performance and generate strategy tuning proposals. Run weekly.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_research_growth",
        "description": "Growth bot: scan the momentum universe for growth/breakout candidates. Checks regime, RS ranking, setup detection.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_trade_growth",
        "description": "Growth bot: place stop-limit buy orders for momentum setups. Requires growth research to have run first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_manage_growth",
        "description": "Growth bot: manage open growth positions — phase transitions (initial→protected→trailing), time stops, recovery.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_state",
        "description": "Read a state, config, or journal file. Examples: candidates.json, strategy.json, performance.json, order_plan.json, manage_log.json, learning_analysis.json, guardrails.json",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Name of the file to read"}
            },
            "required": ["filename"],
        },
    },
    {
        "name": "apply_strategy_change",
        "description": "Apply a strategy parameter change. Automatically validates against guardrails, snapshots current config, and logs the change. Use only after reviewing learning analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "param_name": {"type": "string", "description": "Parameter name from guardrails.json (e.g., trailing_atr_multiplier)"},
                "new_value": {"type": "number", "description": "New value for the parameter"},
                "reason": {"type": "string", "description": "Why this change is being made"},
            },
            "required": ["param_name", "new_value", "reason"],
        },
    },
    {
        "name": "send_notification",
        "description": "Send an alert/notification via the configured webhook (Slack/Discord).",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Alert message to send"},
                "level": {"type": "string", "enum": ["info", "trade", "warning", "error"], "description": "Alert severity level"},
            },
            "required": ["message", "level"],
        },
    },
]


def check_drawdown_circuit_breaker():
    """Check if portfolio drawdown exceeds circuit breaker threshold."""
    try:
        guardrails = read_state_file("guardrails.json")
        cb = guardrails.get("drawdown_circuit_breaker", {})
        max_dd = cb.get("max_drawdown_pct", 15.0)

        eq_curve = read_state_file("equity_curve.json")
        if isinstance(eq_curve, list) and len(eq_curve) > 1:
            equities = [e.get("equity", 0) for e in eq_curve if e.get("equity")]
            if equities:
                peak = max(equities)
                current = equities[-1]
                dd_pct = (peak - current) / peak * 100 if peak > 0 else 0
                if dd_pct >= max_dd:
                    send_alert(
                        f"🚨 CIRCUIT BREAKER: Drawdown {dd_pct:.1f}% exceeds {max_dd}% limit. "
                        f"All new entries HALTED. Peak: ${peak:,.0f}, Current: ${current:,.0f}",
                        level="error"
                    )
                    log_event("circuit_breaker", {"drawdown_pct": dd_pct, "peak": peak, "current": current})
                    return True, dd_pct
        return False, 0
    except Exception:
        return False, 0


def handle_tool_call(tool_name, tool_input):
    """Execute a tool call and return the result."""
    if tool_name in ("run_research", "run_research_growth"):
        return run_script("research_growth")
    elif tool_name in ("run_trade", "run_trade_growth"):
        # Check circuit breaker before trading
        tripped, dd = check_drawdown_circuit_breaker()
        if tripped:
            return {"success": False, "error": f"Circuit breaker tripped: {dd:.1f}% drawdown. No new trades."}
        # Hard time-window guard: block order placement outside valid hours
        if not is_within_trade_window():
            now = datetime.now()
            msg = (f"Order placement blocked: current time {now.strftime('%H:%M')} is outside "
                   f"the valid trade window ({MORNING_TRADE_WINDOW[0]}:{MORNING_TRADE_WINDOW[1]:02d}-"
                   f"{MORNING_TRADE_WINDOW[2]}:{MORNING_TRADE_WINDOW[3]:02d}). "
                   f"Research can run late, but orders are time-restricted.")
            log_event("trade_window_blocked", {"time": now.strftime("%H:%M")})
            return {"success": False, "error": msg}
        return run_script("trade_growth")
    elif tool_name in ("run_manage", "run_manage_growth"):
        return run_script("manage_growth")
    elif tool_name == "run_journal":
        return run_script("journal")
    elif tool_name == "run_performance":
        return run_script("performance")
    elif tool_name == "run_learning":
        return run_script("learning")
    elif tool_name == "read_state":
        return read_state_file(tool_input.get("filename", ""))
    elif tool_name == "apply_strategy_change":
        # --- Hard enforcement: max N parameter changes per week ---
        current_count = get_weekly_tuning_count()
        if current_count >= MAX_WEEKLY_PARAM_CHANGES:
            msg = (f"Weekly tuning limit reached: {current_count}/{MAX_WEEKLY_PARAM_CHANGES} "
                   f"changes already applied this week. No more changes allowed until next week.")
            log_event("tuning_blocked", {"reason": "weekly_limit", "count": current_count})
            return {"applied": [], "rejected": [{"error": msg}]}

        from strategy_manager import apply_changes
        changes = {tool_input["param_name"]: tool_input["new_value"]}
        applied, rejected = apply_changes(changes, reason=tool_input.get("reason", "agent_tune"))

        # Track successful changes in weekly counter
        if applied:
            increment_weekly_tuning_count(len(applied))
            log_event("tuning_applied", {
                "params": [a["param"] for a in applied],
                "weekly_count": get_weekly_tuning_count(),
            })

        return {"applied": applied, "rejected": rejected}
    elif tool_name == "send_notification":
        send_alert(tool_input["message"], level=tool_input.get("level", "info"))
        return {"sent": True}
    else:
        return {"error": f"Unknown tool: {tool_name}"}


def run_agent_loop(task_description, system_prompt, allowed_tools=None):
    """Run Claude in an agentic tool-use loop until it's done. Returns structured result."""
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY not set — running scripts directly without AI orchestration")
        return run_direct_mode(task_description)

    # Filter tools if whitelist provided
    tools = TOOLS if allowed_tools is None else [t for t in TOOLS if t["name"] in allowed_tools]

    # Hard budget enforcement
    ok, spent, limit = check_api_budget()
    if not ok:
        print(f"⚠️  API budget exceeded (${spent:.2f} / ${limit:.2f} this month) — falling back to direct mode")
        send_alert(f"⚠️ API budget exceeded (${spent:.2f}/${limit:.2f}). Running in direct mode.", level="warning")
        log_event("budget_exceeded", {"spent": spent, "limit": limit})
        result = run_direct_mode(task_description)
        if isinstance(result, dict):
            result["budget_fallback"] = True
        else:
            result = {"status": "partial", "budget_fallback": True}
        return result

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = [{"role": "user", "content": task_description}]
    turns = 0
    completed = False
    tool_failures = []

    while turns < MAX_TURNS_PER_RUN:
        turns += 1
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )
            track_api_usage(response.usage.input_tokens, response.usage.output_tokens)
        except Exception as e:
            log_event("api_error", {"error": str(e)})
            send_alert(f"🚨 Claude API error: {e}", level="error")
            return {"status": "failed", "turns": turns, "error": str(e), "tool_failures": tool_failures}

        # Process response
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Check if done
        if response.stop_reason == "end_turn":
            completed = True
            for block in assistant_content:
                if hasattr(block, "text"):
                    log_event("agent_response", {"text": block.text[:500]})
                    print(f"  Agent: {block.text[:200]}")
            break

        # Handle tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    print(f"  → Running: {block.name}")
                    result = handle_tool_call(block.name, block.input)

                    failed = (
                        isinstance(result, dict)
                        and (result.get("success") is False or "error" in result)
                    )
                    if failed:
                        tool_failures.append({
                            "tool": block.name,
                            "input": block.input,
                            "result": result,
                        })

                    log_event("tool_call", {
                        "tool": block.name,
                        "input": block.input,
                        "success": not failed,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str)[:4000],
                    })
            messages.append({"role": "user", "content": tool_results})

    if not completed:
        return {
            "status": "partial",
            "turns": turns,
            "error": "Agent loop ended without clean completion",
            "tool_failures": tool_failures,
        }

    return {
        "status": "success" if not tool_failures else "partial",
        "turns": turns,
        "tool_failures": tool_failures,
    }


def run_direct_mode(task, bot="growth"):
    """Fallback: run scripts directly without Claude (when no API key)."""
    print(f"  Direct mode: {task}")
    if "morning" in task.lower() or "research" in task.lower():
        rg1 = run_script("research_growth")
        print(f"  Research: {rg1.get('output', rg1.get('error', ''))[:200]}")

        rg2 = None
        if rg1.get("success"):
            if is_within_trade_window():
                rg2 = run_script("trade_growth")
                print(f"  Trade: {rg2.get('output', rg2.get('error', ''))[:200]}")
            else:
                rg2 = {"success": False, "output": "Skipped — outside trade window"}
                print("  Trade: SKIPPED (outside trade window)")
                log_event("trade_window_blocked", {"mode": "direct", "time": datetime.now().strftime("%H:%M")})
                send_alert("⚠️ Morning trade skipped in direct mode: outside trade window", level="warning")
        else:
            rg2 = {"success": False, "output": "Skipped — research failed"}
            print("  Trade: SKIPPED (research failed)")
            send_alert(f"⚠️ Morning research failed, trade skipped: {rg1.get('error', rg1.get('errors', ''))[:300]}", level="warning")

        # Alert
        n_cands = 0
        regime = "?"
        candidates = read_state_file("candidates_growth.json")
        if isinstance(candidates, dict):
            n_cands = len(candidates.get("candidates", []))
            regime = candidates.get("regime_mode", "?")

        msg_parts = [f"🌅 Morning scan complete", f"Regime: {regime}", f"Candidates: {n_cands}"]
        send_alert("\n".join(msg_parts), level="info")

        results = {"research": rg1}
        if rg2: results["trade"] = rg2
        all_results = [v for v in [rg1, rg2] if v is not None]
        all_ok = all(r.get("success") for r in all_results)
        any_ok = any(r.get("success") for r in all_results)
        results["status"] = "success" if all_ok else ("partial" if any_ok else "failed")
        return results

    elif "afternoon" in task.lower() or "manage" in task.lower():
        results = {}

        # Manage positions
        rg1 = run_script("manage_growth")
        print(f"  Manage: {rg1.get('output', rg1.get('error', ''))[:200]}")
        results["manage"] = rg1
        if not rg1.get("success"):
            send_alert(f"⚠️ Manage failed: {rg1.get('error', rg1.get('errors', ''))[:300]}", level="warning")

        r2 = run_script("performance")
        print(f"  Performance: {r2.get('output', r2.get('error', ''))[:200]}")
        results["performance"] = r2

        r3 = run_script("journal")
        print(f"  Journal: {r3.get('output', r3.get('error', ''))[:200]}")
        results["journal"] = r3

        # Daily analytics pipeline + report
        try:
            from analytics.pipeline import run_daily_pipeline
            from analytics.reports import generate_daily_report
            from analytics.ai_review import generate_recommendations
            run_daily_pipeline()
            generate_daily_report()
            generate_recommendations()
            print("  Analytics: daily report generated ✅")
        except Exception as e:
            print(f"  Analytics: failed ({e})")

        send_alert("🌆 Afternoon management complete", level="info")

        all_results = [v for v in results.values() if isinstance(v, dict)]
        all_ok = all(r.get("success") for r in all_results)
        any_ok = any(r.get("success") for r in all_results)
        results["status"] = "success" if all_ok else ("partial" if any_ok else "failed")
        return results

    elif "learn" in task.lower() or "weekly" in task.lower():
        r1 = run_script("learning")
        print(f"  Learning: {r1.get('output', r1.get('error', ''))[:200]}")
        send_alert(f"📊 Weekly learning review\n{r1.get('output', '')}", level="info")
        return {"status": "success" if r1.get("success") else "failed", "learning": r1}

    return {"status": "failed", "error": "Unknown task"}


# --- System Prompt ---
SYSTEM_PROMPT = """You are an autonomous trading bot operator managing a rules-based swing trading system.

YOUR ROLE:
- Execute the daily trading workflow by calling tools in the right order
- Monitor results and send alerts about important events
- Weekly: review performance and consider small strategy adjustments within guardrails

DAILY WORKFLOW:
Morning (market open):
1. run_research — scan for candidates
2. read_state candidates.json — review what was found
3. run_trade — place orders (only if candidates exist and market is open)
4. send_notification — alert on results

Afternoon (after close):
1. run_manage — manage open positions
2. run_performance — update metrics
3. run_journal — write daily journal
4. send_notification — alert on position changes

WEEKLY (Saturday morning):
1. run_learning — analyze recent performance
2. read_state learning_analysis.json — review proposals
3. read_state guardrails.json — check bounds
4. If proposals are sound AND backed by sufficient data → apply_strategy_change
5. send_notification — alert on any changes

RULES:
- This is PAPER TRADING only. Never mention "live" or suggest switching to live.
- Do NOT apply strategy changes unless learning analysis has clear evidence.
- Never change more than 2 parameters per week.
- Always send a notification summarizing what happened.
- If something fails, send an error alert and continue with remaining tasks.
- Be concise in notifications — include key numbers only."""


def morning_run(allow_trade=True):
    """Execute morning workflow."""
    print(f"\n{'='*50}")
    print(f"  MORNING RUN - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")
    log_event("run_start", {"type": "morning", "allow_trade": allow_trade})

    task = (
        "Execute the morning trading workflow: scan for candidates, review results, place orders if appropriate, and send a summary alert."
        if allow_trade else
        "Execute morning research only: scan for candidates, review results, and send a summary alert. Do NOT place any orders — this is a late catch-up run outside the trade window."
    )

    try:
        if allow_trade:
            result = run_agent_loop(task, SYSTEM_PROMPT)
        else:
            result = run_agent_loop(
                task, SYSTEM_PROMPT,
                allowed_tools={"run_research", "read_state", "send_notification"},
            )
        status = result.get("status", "failed") if isinstance(result, dict) else "failed"
        if status != "success":
            log_event("run_warning", {"type": "morning", "result": result})
    except Exception as e:
        status = "failed"
        result = {"status": "failed", "error": str(e)}
        log_event("run_error", {"type": "morning", "error": str(e)})
        send_alert(f"🚨 Morning run failed: {e}", level="error")
        traceback.print_exc()

    # Post-action reconciliation after morning trade
    recon = None
    if allow_trade and status != "failed":
        recon = reconcile_after_action("morning_trade")
        if isinstance(recon, dict):
            if recon.get("match") is False and status == "success":
                status = "partial"
                log_event("run_downgraded", {"type": "morning", "reason": "reconciliation_mismatch"})
            elif recon.get("match") is None and status == "success":
                status = "partial"
                log_event("run_downgraded", {"type": "morning", "reason": "reconciliation_error", "error": recon.get("error")})

    log_event("run_complete", {"type": "morning", "status": status, "summary": summarize_result(result),
                                "reconciliation": recon if recon and recon.get("match") is not True else None})
    record_run("morning", status=status)


def afternoon_run():
    """Execute afternoon workflow."""
    print(f"\n{'='*50}")
    print(f"  AFTERNOON RUN - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")
    log_event("run_start", {"type": "afternoon"})

    try:
        result = run_agent_loop(
            "Execute the afternoon workflow: manage open positions, update performance metrics, write the journal, and send a summary alert with any position changes.",
            SYSTEM_PROMPT,
        )
        status = result.get("status", "failed") if isinstance(result, dict) else "failed"
        if status != "success":
            log_event("run_warning", {"type": "afternoon", "result": result})
    except Exception as e:
        status = "failed"
        result = {"status": "failed", "error": str(e)}
        log_event("run_error", {"type": "afternoon", "error": str(e)})
        send_alert(f"🚨 Afternoon run failed: {e}", level="error")
        traceback.print_exc()

    # Post-action reconciliation after position management
    recon = None
    if status != "failed":
        recon = reconcile_after_action("afternoon_manage")
        if isinstance(recon, dict):
            if recon.get("match") is False and status == "success":
                status = "partial"
                log_event("run_downgraded", {"type": "afternoon", "reason": "reconciliation_mismatch"})
            elif recon.get("match") is None and status == "success":
                status = "partial"
                log_event("run_downgraded", {"type": "afternoon", "reason": "reconciliation_error", "error": recon.get("error")})

    log_event("run_complete", {"type": "afternoon", "status": status, "summary": summarize_result(result),
                                "reconciliation": recon if recon and recon.get("match") is not True else None})
    record_run("afternoon", status=status)


def weekly_review():
    """Execute weekly learning and tuning review."""
    print(f"\n{'='*50}")
    print(f"  WEEKLY REVIEW - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")
    log_event("run_start", {"type": "weekly_review"})

    try:
        result = run_agent_loop(
            "Execute the weekly performance review: run the learning analysis, review the proposals and guardrails, decide whether to apply any strategy changes, and send a detailed summary alert.",
            SYSTEM_PROMPT,
        )
        status = result.get("status", "failed") if isinstance(result, dict) else "failed"
        if status != "success":
            log_event("run_warning", {"type": "weekly_review", "result": result})
    except Exception as e:
        status = "failed"
        result = {"status": "failed", "error": str(e)}
        log_event("run_error", {"type": "weekly_review", "error": str(e)})
        send_alert(f"🚨 Weekly review failed: {e}", level="error")
        traceback.print_exc()

    log_event("run_complete", {"type": "weekly_review", "status": status, "summary": summarize_result(result)})
    record_run("weekly_review", status=status)


def daily_backup():
    """Run daily backup of critical state files."""
    try:
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "backup.sh")],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
        )
        if result.returncode == 0:
            log_event("backup", {"status": "ok", "output": result.stdout.strip()})
        else:
            log_event("backup_failed", {"error": result.stderr.strip()})
            send_alert("⚠️ Daily backup failed", level="warning")
    except subprocess.TimeoutExpired:
        log_event("backup_failed", {"error": "Backup timed out after 30s"})
        send_alert("⚠️ Daily backup timed out", level="warning")
    except Exception as e:
        log_event("backup_failed", {"error": str(e)})
        send_alert(f"⚠️ Daily backup error: {e}", level="warning")


def heartbeat():
    """Send a daily heartbeat to confirm the bot is alive."""
    usage = {}
    if API_USAGE_PATH.exists():
        usage = load_json(API_USAGE_PATH)
    send_alert(
        f"💓 Bot heartbeat — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"API calls this month: {usage.get('total_calls', 0)} | "
        f"Est. cost: ${usage.get('estimated_cost_usd', 0):.2f}",
        level="info"
    )


def setup_instance_lock():
    """Acquire PID lock and register cleanup. Used by all entry points."""
    try:
        acquire_lock()
    except RuntimeError as e:
        print(f"🛑 {e}")
        send_alert(f"🛑 Orchestrator refused to start: {e}", level="error")
        sys.exit(1)

    atexit.register(release_lock)

    def _shutdown(sig, frame):
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


def startup_self_check():
    """Verify required files, env vars, writable dirs, and API availability before entering schedule loop."""
    issues = []

    # Required directories must be writable
    for d in [STATE_DIR, CONFIG_DIR, JOURNAL_DIR]:
        if not d.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                issues.append(f"Cannot create directory {d}: {e}")
        elif not os.access(str(d), os.W_OK):
            issues.append(f"Directory not writable: {d}")

    # Required config files
    for fname in ["strategy_growth.json", "watchlist_growth.json", "guardrails.json"]:
        path = CONFIG_DIR / fname
        if not path.exists():
            issues.append(f"Missing config: {path}")
        else:
            try:
                load_json(path)
            except Exception as e:
                issues.append(f"Invalid JSON in {fname}: {e}")

    # Required env vars for trading
    if not get_env("ALPACA_API_KEY"):
        issues.append("Missing ALPACA_API_KEY in .env")
    if not get_env("ALPACA_SECRET_KEY"):
        issues.append("Missing ALPACA_SECRET_KEY in .env")

    # Python venv
    if not Path(VENV_PYTHON).exists():
        issues.append(f"Python venv not found: {VENV_PYTHON}")

    # Alpaca API connectivity (non-blocking — warn only)
    try:
        from common import alpaca_get
        clock = alpaca_get("/v2/clock")
        if not isinstance(clock, dict):
            issues.append("Alpaca API returned unexpected response")
    except Exception as e:
        issues.append(f"Alpaca API unreachable: {e}")

    if issues:
        msg = "Startup self-check warnings:\n" + "\n".join(f"  • {i}" for i in issues)
        print(f"⚠️ {msg}")
        log_event("startup_check", {"issues": issues})
        send_alert(f"⚠️ {msg}", level="warning")
        # Only block on truly fatal issues
        fatal = [i for i in issues if any(k in i for k in ("Missing config", "not writable", "Missing ALPACA_", "venv not found"))]
        if fatal:
            print("🛑 Fatal startup issues — cannot continue")
            sys.exit(1)
    else:
        print("✅ Startup self-check passed")
        log_event("startup_check", {"status": "ok"})


def reconcile_after_action(action_name):
    """Post-action reconciliation: 3-way compare of local tracking vs broker positions vs broker open orders."""
    try:
        from common import alpaca_get

        # Fetch broker state
        broker_positions = alpaca_get("/v2/positions")
        broker_position_symbols = {p["symbol"] for p in broker_positions}

        broker_orders = alpaca_get("/v2/orders", params={"status": "open", "limit": 100})
        broker_open_buy_symbols = {o["symbol"] for o in broker_orders if o.get("side") == "buy"}
        broker_open_sell_symbols = {o["symbol"] for o in broker_orders if o.get("side") == "sell"}

        # Load local tracking
        tracking_path = STATE_DIR / "position_tracking_growth.json"
        if not tracking_path.exists():
            # No tracking file but broker has positions — flag it
            if broker_position_symbols:
                msg = f"🔍 Reconciliation: no local tracking file but broker has positions: {sorted(broker_position_symbols)}"
                log_event("reconciliation_mismatch", {"action": action_name, "issue": "no_tracking_file", "broker_positions": sorted(broker_position_symbols)})
                send_alert(msg, level="warning")
                return {"match": False, "issue": "no_tracking_file"}
            return {"match": True}

        tracking = load_json(tracking_path)
        mismatches = []

        # Categorize local tracking by phase
        # Known phases: pending, initial, protected, trailing, exit_pending, closed
        OPEN_PHASES = {"initial", "protected", "trailing"}
        local_pending = {sym for sym, d in tracking.items() if isinstance(d, dict) and d.get("phase") == "pending"}
        local_open = {sym for sym, d in tracking.items() if isinstance(d, dict) and d.get("phase") in OPEN_PHASES}
        local_exit_pending = {sym for sym, d in tracking.items() if isinstance(d, dict) and d.get("phase") == "exit_pending"}

        # Check 1: local open positions should exist at broker
        orphaned_positions = local_open - broker_position_symbols
        if orphaned_positions:
            mismatches.append(f"Tracked as open but not at broker: {sorted(orphaned_positions)}")

        # Check 2: broker positions not tracked locally (excluding pending entries that just filled)
        all_known_local = local_open | local_pending | local_exit_pending
        untracked_positions = broker_position_symbols - all_known_local
        if untracked_positions:
            mismatches.append(f"At broker but not tracked: {sorted(untracked_positions)}")

        # Check 3: local pending entries should have open buy orders at broker
        pending_without_order = local_pending - broker_open_buy_symbols - broker_position_symbols
        if pending_without_order:
            mismatches.append(f"Pending entry but no open buy order or position: {sorted(pending_without_order)}")

        # Check 4: open positions should have protective sell orders (stop/trailing)
        positions_without_exit = local_open - broker_open_sell_symbols
        # Only flag if the position is also still at broker (not just exited and uncleared)
        positions_without_exit = positions_without_exit & broker_position_symbols
        if positions_without_exit:
            mismatches.append(f"Open position but no protective sell order: {sorted(positions_without_exit)}")

        if mismatches:
            msg = f"🔍 Reconciliation mismatch after {action_name}:\n" + "\n".join(f"  • {m}" for m in mismatches)
            log_event("reconciliation_mismatch", {
                "action": action_name,
                "mismatches": mismatches,
                "local_open": sorted(local_open),
                "local_pending": sorted(local_pending),
                "broker_positions": sorted(broker_position_symbols),
                "broker_open_buys": sorted(broker_open_buy_symbols),
                "broker_open_sells": sorted(broker_open_sell_symbols),
            })
            send_alert(msg, level="warning")
            return {"match": False, "mismatches": mismatches}

        return {"match": True}
    except Exception as e:
        log_event("reconciliation_error", {"action": action_name, "error": str(e)})
        return {"match": None, "error": str(e)}


def summarize_result(result):
    """Extract a compact summary from a workflow result for logging."""
    if not isinstance(result, dict):
        return {"status": "unknown"}
    summary = {"status": result.get("status", "unknown")}
    if result.get("turns"):
        summary["turns"] = result["turns"]
    if result.get("tool_failures"):
        summary["failed_tools"] = [f["tool"] for f in result["tool_failures"]]
    if result.get("error"):
        summary["error"] = str(result["error"])[:200]
    if result.get("budget_fallback"):
        summary["budget_fallback"] = True
    return summary


def main():

    # --- Startup self-check ---
    startup_self_check()

    print("=" * 60)
    print("  AUTONOMOUS TRADING BOT")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  PID: {os.getpid()}")
    print(f"  Mode: {'AI-Orchestrated (Claude)' if ANTHROPIC_API_KEY else 'Direct (no API key)'}")
    print("  Paper Trading Only")
    print("=" * 60)

    send_alert("🤖 Trading bot started", level="info")
    log_event("bot_start", {"mode": "ai" if ANTHROPIC_API_KEY else "direct", "pid": os.getpid()})

    # --- Check for missed jobs after restart ---
    missed = check_missed_jobs()
    if missed:
        print(f"\n⚠️  Detected missed jobs: {missed}")
        send_alert(f"⚠️ Bot restarted — catching up missed jobs: {', '.join(missed)}", level="warning")
        log_event("missed_jobs_detected", {"missed": missed})
        for job in missed:
            print(f"  ▶ Running missed: {job}")
            if job == "morning":
                if not is_within_trade_window():
                    print("  ⚠️ Outside trade window — running research only (orders blocked)")
                    log_event("catch_up_restricted", {"job": "morning", "reason": "outside_trade_window"})
                    send_alert("⚠️ Missed morning catch-up: research only (outside trade window)", level="warning")
                    morning_run(allow_trade=False)
                else:
                    morning_run()
            elif job == "afternoon":
                afternoon_run()
            elif job == "weekly_review":
                weekly_review()

    # Schedule daily tasks (times are in local timezone)
    schedule.every().monday.at("09:35").do(morning_run)
    schedule.every().tuesday.at("09:35").do(morning_run)
    schedule.every().wednesday.at("09:35").do(morning_run)
    schedule.every().thursday.at("09:35").do(morning_run)
    schedule.every().friday.at("09:35").do(morning_run)

    schedule.every().monday.at("16:05").do(afternoon_run)
    schedule.every().tuesday.at("16:05").do(afternoon_run)
    schedule.every().wednesday.at("16:05").do(afternoon_run)
    schedule.every().thursday.at("16:05").do(afternoon_run)
    schedule.every().friday.at("16:05").do(afternoon_run)

    # Intraday growth management (catch phase transitions faster)
    def intraday_growth_manage():
        """Run growth position management mid-day for faster phase transitions."""
        print(f"\n  INTRADAY GROWTH MANAGE - {datetime.now().strftime('%H:%M')}")
        log_event("run_start", {"type": "intraday_manage_growth"})
        try:
            result = run_script("manage_growth")
            log_event("intraday_manage", {"success": result.get("success"), "output": result.get("output", "")[:200]})
        except Exception as e:
            log_event("intraday_manage_error", {"error": str(e)})

    schedule.every().monday.at("10:30").do(intraday_growth_manage)
    schedule.every().tuesday.at("10:30").do(intraday_growth_manage)
    schedule.every().wednesday.at("10:30").do(intraday_growth_manage)
    schedule.every().thursday.at("10:30").do(intraday_growth_manage)
    schedule.every().friday.at("10:30").do(intraday_growth_manage)

    schedule.every().monday.at("13:00").do(intraday_growth_manage)
    schedule.every().tuesday.at("13:00").do(intraday_growth_manage)
    schedule.every().wednesday.at("13:00").do(intraday_growth_manage)
    schedule.every().thursday.at("13:00").do(intraday_growth_manage)
    schedule.every().friday.at("13:00").do(intraday_growth_manage)

    # Weekly review on Saturday morning
    schedule.every().saturday.at("10:00").do(weekly_review)

    # Daily heartbeat and backup
    schedule.every().day.at("08:00").do(heartbeat)
    schedule.every().day.at("17:00").do(daily_backup)

    print("\nScheduled tasks:")
    print("  Mon-Fri 9:35 AM  → Morning scan + trade")
    print("  Mon-Fri 10:30 AM → Intraday growth manage")
    print("  Mon-Fri 1:00 PM  → Intraday growth manage")
    print("  Mon-Fri 4:05 PM  → Manage + journal")
    print("  Saturday 10:00 AM → Weekly learning review")
    print("  Daily 8:00 AM    → Heartbeat")
    print("  Daily 5:00 PM    → Backup")
    print(f"\nTuning limit: max {MAX_WEEKLY_PARAM_CHANGES} parameter changes per week (enforced in code)")
    print(f"Trade window: {MORNING_TRADE_WINDOW[0]}:{MORNING_TRADE_WINDOW[1]:02d}-{MORNING_TRADE_WINDOW[2]}:{MORNING_TRADE_WINDOW[3]:02d} (enforced in code)")
    print(f"PID lock: {LOCKFILE_PATH}")
    print("\nWaiting for next scheduled run...\n")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log_event("scheduler_error", {"error": str(e)})
            send_alert(f"🚨 Scheduler error: {e}", level="error")
            traceback.print_exc()
        time.sleep(30)


if __name__ == "__main__":
    setup_instance_lock()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "morning":
            morning_run()
        elif cmd == "afternoon":
            afternoon_run()
        elif cmd == "weekly":
            weekly_review()
        elif cmd == "test":
            print("Running test cycle...")
            morning_run()
            afternoon_run()
        else:
            print(f"Usage: {sys.argv[0]} [morning|afternoon|weekly|test]")
            print("  No argument = run as persistent scheduled bot")
            print("  Examples:")
            print("    python orchestrator.py morning    # Morning research + trade")
            print("    python orchestrator.py afternoon  # EOD manage + performance + journal")
            print("    python orchestrator.py weekly     # Weekly review")
    else:
        main()

