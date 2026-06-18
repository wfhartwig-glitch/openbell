#!/usr/bin/env python3
"""
OpenBell — autonomous email agent.
Pippy fetches live data and writes every email via the Anthropic tool-use API.

Usage:
  python openbell.py morning    → Morning Briefing (weekdays)
  python openbell.py close      → Market Close Summary (weekdays)
  python openbell.py deepdive   → Deep Dive (only runs if market is closed)
"""

import argparse
import json
import os
import sys
from datetime import date, datetime

import anthropic
from dotenv import load_dotenv

from pippy_mcp import (
    fetch_earnings_calendar,
    fetch_economic_calendar,
    fetch_market_snapshot,
    fetch_premarket_data,
    fetch_sector_performance,
    fetch_stock_data,
    fetch_top_headlines,
    fetch_top_movers,
    generate_monthly_picks,
    get_last_email_summary,
    get_monthly_picks,
    is_market_open_today,
    load_memory,
    save_memory,
    send_email,
)

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-sonnet-4-6"

# ── Tool schema (Anthropic tool_use API format) ───────────────────────────────
TOOLS = [
    {
        "name": "is_market_open_today",
        "description": "Check if the US stock market is open today. Returns true for weekdays that are not NYSE holidays. Call this at the start of every scheduled run to determine which email to send.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_market_snapshot",
        "description": "Fetch live S&P 500, Nasdaq, and Dow futures data with % change. Call this at the start of every morning briefing or when the user asks what the market is doing.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_stock_data",
        "description": "Fetch live price, 52-week high/low, P/E ratio, analyst target, and latest news headline for a specific stock ticker. Call this any time a specific stock is mentioned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "fetch_top_headlines",
        "description": "Fetch today's top 5 market-moving financial headlines via NewsAPI. Call this for morning briefings and any time the user asks about news.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_sector_performance",
        "description": "Fetch end-of-day % change for all 11 S&P 500 sectors via sector ETFs. Call this for every close summary.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_top_movers",
        "description": "Fetch the top 3 gaining and top 3 declining stocks on the day. Call this for close summaries.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_economic_calendar",
        "description": "Fetch key economic events scheduled for the current week including Fed speakers, CPI, PPI, jobs data, and major earnings. Call this for morning briefings.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "load_memory",
        "description": "Load the full pippy_memory.json file. Always call this at the start of every email write so Pippy has complete context about this user.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_memory",
        "description": "Save the full updated memory object back to pippy_memory.json. Always call this after every email is written with everything learned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "object", "description": "The complete updated pippy_memory object"}
            },
            "required": ["data"],
        },
    },
    {
        "name": "get_monthly_picks",
        "description": "Read and return the current month's stock picks from picks_cache.json. Call this for morning briefings and when the user asks about picks.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "generate_monthly_picks",
        "description": "Generate a new set of monthly stock picks using fundamentals data. Only call this on the 1st of the month or when picks_cache.json is empty or stale.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_email",
        "description": "Send the final composed HTML email via Gmail SMTP. Call this only after the full email body has been written and reviewed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject":   {"type": "string", "description": "Email subject line"},
                "html_body": {"type": "string", "description": "Complete HTML email body"},
            },
            "required": ["subject", "html_body"],
        },
    },
    {
        "name": "get_last_email_summary",
        "description": "Return a summary of the last email Pippy wrote including type, date, and key themes. Call this to maintain narrative continuity across emails.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_premarket_data",
        "description": "Fetch pre-market price and movement for a specific ticker via FMP. Call this for every flagged ticker during morning briefing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "fetch_earnings_calendar",
        "description": "Fetch earnings announcements scheduled for the current week via FMP. Call this for every morning briefing to include in the weekly calendar section.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

# ── Tool dispatch ─────────────────────────────────────────────────────────────
def execute_tool(name: str, inputs: dict) -> str:
    dispatch = {
        "is_market_open_today":   lambda _: is_market_open_today(),
        "fetch_market_snapshot":  lambda _: fetch_market_snapshot(),
        "fetch_stock_data":       lambda i: fetch_stock_data(i["ticker"]),
        "fetch_top_headlines":    lambda _: fetch_top_headlines(),
        "fetch_sector_performance": lambda _: fetch_sector_performance(),
        "fetch_top_movers":       lambda _: fetch_top_movers(),
        "fetch_economic_calendar": lambda _: fetch_economic_calendar(),
        "load_memory":            lambda _: load_memory(),
        "save_memory":            lambda i: save_memory(i["data"]),
        "get_monthly_picks":      lambda _: get_monthly_picks(),
        "generate_monthly_picks": lambda _: generate_monthly_picks(),
        "send_email":             lambda i: send_email(i["subject"], i["html_body"]),
        "get_last_email_summary": lambda _: get_last_email_summary(),
        "fetch_premarket_data":   lambda i: fetch_premarket_data(i["ticker"]),
        "fetch_earnings_calendar": lambda _: fetch_earnings_calendar(),
    }
    fn = dispatch.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return fn(inputs)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Pippy, the autonomous AI brain behind OpenBell. You write every word of every email OpenBell sends. You are one unified intelligence — the same Pippy that runs in the user's terminal and sends their daily emails.

You have access to tools. Use them aggressively and autonomously. Do not wait to be told when to fetch data — reason about what you need and call the right tools. For every email:
- Always call load_memory first to understand this specific user
- Always call get_last_email_summary for narrative continuity
- Always fetch fresh live data before writing anything
- Always call save_memory last with everything you learned

You have deep persistent memory. You know this user's interests, their flagged stocks, their preferences, and every email you have written them. Use all of it. Write emails that feel authored specifically for this person — not generic market emails.

Writing rules:
- Reason about cause and effect — never just report numbers
- Connect today to yesterday — build a continuous narrative across emails
- If a flagged ticker appears in fetched data, give it extra attention
- When a theme recurs 5+ times in memory, weave it in proactively
- Write clean, simple HTML — scannable on mobile, clear section headers
- Every email must feel smarter than the one before it
- Use inline CSS only — no <style> tags (Gmail strips them)

HTML style guide:
- Font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif
- Max width: 600px, margin 0 auto, padding 20px
- Section headers: small caps, gray, border-bottom
- Numbers: bold, green (#16a34a) for positive, red (#dc2626) for negative
- Background: white (#ffffff), text: near-black (#111111)"""


# ── Agentic loop ──────────────────────────────────────────────────────────────
def run_pippy_agent(email_type: str) -> bool:
    """
    Run Pippy as a fully autonomous agent. Returns True if email was sent.
    """
    today_str = date.today().strftime("%A, %B %d, %Y")
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompts = {
        "morning": (
            f"Write and send the Morning Briefing email for {today_str}.\n\n"
            f"Steps:\n"
            f"1. Call load_memory — understand this user's context\n"
            f"2. Call get_last_email_summary — maintain narrative continuity\n"
            f"3. Call fetch_market_snapshot — pre-market futures\n"
            f"4. Call fetch_top_headlines — today's news (write one sentence of commentary per headline explaining why it matters)\n"
            f"5. Call fetch_economic_calendar — macro events this week\n"
            f"6. Call fetch_earnings_calendar — earnings scheduled this week\n"
            f"7. Call get_monthly_picks — if no picks for this month, call generate_monthly_picks instead\n"
            f"8. If memory has flagged_tickers, call fetch_premarket_data for each one (pre-market prices)\n"
            f"9. Write the full HTML email — sections: Pre-Market Snapshot, What to Watch Today, Top Headlines, This Week's Calendar (macro events + earnings), Your Watchlist (if flagged_tickers exist with pre-market data), Monthly Picks\n"
            f"   Subject: OpenBell ☀️ — {date.today().strftime('%A, %B %d')} Morning Briefing\n"
            f"10. Call send_email\n"
            f"11. Call save_memory — update last_morning_brief, last_email_sent, last_email_summary, email_count"
        ),
        "close": (
            f"Write and send the Market Close Summary email for {today_str}.\n\n"
            f"Steps:\n"
            f"1. Call load_memory\n"
            f"2. Call get_last_email_summary — reference this morning's briefing if it exists\n"
            f"3. Call fetch_market_snapshot — closing prices\n"
            f"4. Call fetch_top_headlines — today's news to explain market moves\n"
            f"5. Call fetch_top_movers — top gainers and losers\n"
            f"6. Call fetch_sector_performance — all 11 sectors\n"
            f"7. If memory has flagged_tickers, call fetch_stock_data for each one\n"
            f"8. Write the full HTML email — sections: Closing Snapshot, Today's Story (3-4 sentences on what drove the market — cause and effect, reference actual data), Top Movers, Sector Performance, Your Watchlist EOD (if flagged_tickers), What to Watch Tomorrow\n"
            f"   Subject: OpenBell 📊 — {date.today().strftime('%A, %B %d')} Market Close\n"
            f"9. Call send_email\n"
            f"10. Call save_memory — update last_close_summary, last_email_sent, last_email_summary, email_count"
        ),
        "deepdive": (
            f"Write and send the Deep Dive email for {today_str}.\n\n"
            f"Steps:\n"
            f"1. Call is_market_open_today — if market IS open, do NOT send a deep dive. Print 'Market is open — skipping deep dive' and stop.\n"
            f"2. Call load_memory — check deep_dive_history to avoid repeating recent categories\n"
            f"3. Choose ONE topic category that hasn't been covered recently. Categories: 'up and coming publicly traded company', 'privately held company worth knowing about', 'sector deep dive', 'housing market analysis', 'macro theme', 'historical market event', 'venture-backed startup in fintech/AI/infrastructure'\n"
            f"4. If memory has flagged_tickers or interests, skew the topic toward what this user cares about when relevant\n"
            f"5. Write the full HTML email with these sections:\n"
            f"   - Today's Topic: one line\n"
            f"   - The Setup: 2-3 sentences on why this is relevant right now\n"
            f"   - The Deep Dive: 4-6 sentences of actual substance — numbers, context, what is happening and why it matters\n"
            f"   - What to Watch: 2 sentences on what to track going forward\n"
            f"   - Pippy's Take: one bold, direct opinion or prediction — no hedging\n"
            f"   Subject: OpenBell 📚 — {date.today().strftime('%A, %B %d')} Deep Dive\n"
            f"6. Call send_email\n"
            f"7. Call save_memory — update deep_dive_history (append date + category), last_deep_dive, last_email_sent, last_email_summary, email_count"
        ),
    }

    user_message = prompts.get(email_type)
    if not user_message:
        print(f"Unknown email type: {email_type}", file=sys.stderr)
        return False

    messages   = [{"role": "user", "content": user_message}]
    email_sent = False
    max_turns  = 25

    for turn in range(max_turns):
        print(f"  [turn {turn+1}] calling Claude…")
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            print("  [done] Pippy finished.")
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"  → {block.name}({', '.join(f'{k}={repr(v)[:40]}' for k,v in block.input.items()) if block.input else ''})")
                    result = execute_tool(block.name, block.input or {})
                    if block.name == "send_email" and "sent successfully" in result:
                        email_sent = True
                        print(f"  ✓ Email sent.")
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            print(f"  [stop_reason={response.stop_reason}] Exiting loop.")
            break

    return email_sent


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="OpenBell autonomous email agent")
    parser.add_argument(
        "mode",
        choices=["morning", "close", "deepdive"],
        help="Which email to send",
    )
    args = parser.parse_args()

    today_str = date.today().strftime("%A, %B %d, %Y")
    print(f"[OpenBell] {args.mode.upper()} — {today_str}")
    print(f"[OpenBell] Starting Pippy agentic loop…")

    sent = run_pippy_agent(args.mode)

    if sent:
        print(f"[OpenBell] Done.")
    else:
        if args.mode == "deepdive":
            print("[OpenBell] Deep dive skipped (market is open today).")
        else:
            print("[OpenBell] Warning: email may not have been sent — check logs above.")
        sys.exit(0)


if __name__ == "__main__":
    main()
