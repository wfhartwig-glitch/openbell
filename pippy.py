#!/usr/bin/env python3
"""
Pippy — terminal agent powered by claude -p (Max tokens, no API key needed).
Each message calls `claude -p` with conversation history injected as context.
MCP tools are registered in .claude/settings.json so Claude uses them automatically.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(PROJECT_DIR, "pippy_memory.json")
HISTORY_FILE = os.path.join(PROJECT_DIR, "pippy_history.json")

DEFAULT_MEMORY = {
    "interests": [],
    "mentioned_stocks": {},
    "expressed_preferences": [],
    "frequent_questions": [],
    "flagged_tickers": [],
    "last_email_sent": "",
    "last_email_summary": "",
    "session_count": 0,
    "last_session": "",
}

SYSTEM_PROMPT = """You are Pippy, the AI brain behind OpenBell — a daily pre-market briefing email. You run in a terminal.

Non-negotiable rules:
- No markdown ever. No **bold**, no headers, no asterisks. Plain text only.
- Never mention WebSearch, permissions, tools, or internet access. Live data is fetched and injected into your prompt — just use it.
- Never say you lack access to data that is already in the prompt context.
- Talk in first person. You are Pippy, not an assistant.
- Keep responses to 3-5 lines. Be direct and confident.
- If live data is in the context, lead with it. Don't caveat it."""


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                data = json.load(f)
            for k, v in DEFAULT_MEMORY.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_MEMORY)


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_history(history: list) -> None:
    # Keep last 20 turns to avoid prompt bloat
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-40:], f, indent=2)


SKIP_WORDS = {"I","A","AN","THE","AND","OR","BUT","FOR","IN","ON","AT","TO","OF",
              "IS","IT","MY","ME","DO","IF","SO","UP","AI","US","OK","AM","PM",
              "ET","PE","YOY","EPS","CEO","CFO","IPO","GDP","FED","ETF","APP",
              "WHAT","HOW","WHY","WHEN","WHO","CAN","WILL","DOES","HAS","ARE"}


def fetch_live_data(user_message: str) -> str:
    """Fetch relevant live data based on what the user asked."""
    msg   = user_message.upper()
    lower = user_message.lower()
    data_lines = []

    # Only detect tickers the user actually typed in uppercase (not words we uppercased)
    tickers = [w for w in re.findall(r'\b[A-Z]{2,5}\b', user_message) if w not in SKIP_WORDS]
    for ticker in tickers[:2]:
        try:
            t     = yf.Ticker(ticker)
            fi    = t.fast_info
            info  = t.info
            price = fi.last_price
            prev  = fi.previous_close
            if not price:
                continue
            pct = ((price - prev) / prev * 100) if prev else 0
            headline = ""
            for item in (t.news or [])[:2]:
                h = item.get("content", {}).get("title") or item.get("title", "")
                if h:
                    headline = h
                    break
            data_lines.append(
                f"{info.get('longName', ticker)} ({ticker}): ${price:.2f} ({pct:+.2f}%) | "
                f"52w: ${info.get('fiftyTwoWeekLow','?')}–${info.get('fiftyTwoWeekHigh','?')} | "
                f"P/E: {info.get('trailingPE','N/A')} | Target: ${info.get('targetMeanPrice','N/A')}"
            )
            if headline:
                data_lines.append(f"  News: {headline}")
        except Exception:
            continue

    # Fetch market snapshot for market questions
    market_words = ["market","futures","s&p","nasdaq","dow","spy","qqq","open","close","premarket","briefing"]
    if any(w in lower for w in market_words) and not tickers:
        now_m  = datetime.now()
        wday   = now_m.weekday()
        mins   = now_m.hour * 60 + now_m.minute
        # CT market hours: 8:30 AM – 3:00 PM  (ET - 1h)
        is_open = wday < 5 and (8*60+30) <= mins <= (15*60)
        # Use live index tickers during hours, futures pre/post market
        indexes = {
            "S&P 500": "^GSPC" if is_open else "ES=F",
            "Nasdaq":  "^IXIC" if is_open else "NQ=F",
            "Dow":     "^DJI"  if is_open else "YM=F",
        }
        status = "LIVE" if is_open else "futures"
        ts = now_m.strftime("%I:%M %p")
        data_lines.append(f"Market snapshot ({status}) as of {ts}:")
        for name, sym in indexes.items():
            try:
                fi    = yf.Ticker(sym).fast_info
                price = fi.last_price
                prev  = fi.previous_close
                if price and prev:
                    pct   = (price - prev) / prev * 100
                    arrow = "▲" if pct >= 0 else "▼"
                    data_lines.append(f"  {name}: {price:,.2f} {arrow} {abs(pct):.2f}%")
            except Exception:
                continue

    # Fetch headlines for news questions
    news_words = ["news","headlines","happening","briefing","today"]
    if any(w in lower for w in news_words):
        skip = ["beginner","guide","how to","what is","explainer"]
        seen, titles = set(), []
        for sym in ["SPY","QQQ","^VIX"]:
            try:
                for item in (yf.Ticker(sym).news or []):
                    t = item.get("content",{}).get("title") or item.get("title","")
                    if t and t not in seen and len(t) > 20 and not any(k in t.lower() for k in skip):
                        seen.add(t)
                        titles.append(t)
            except Exception:
                continue
            if len(titles) >= 6:
                break
        for i, t in enumerate(titles[:5], 1):
            data_lines.append(f"{i}. {t}")

    ts = datetime.now().strftime("%I:%M %p")
    return f"[Live data as of {ts}]\n" + "\n".join(data_lines) if data_lines else ""


def build_prompt(history: list, user_message: str, mem: dict, live_data: str = "") -> str:
    """Build the full prompt for claude -p including system, memory, live data, history."""
    mem_snippet = ""
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:8]
        mem_snippet += "Stocks mentioned most: " + ", ".join(f"{t}({c}x)" for t, c in top) + ". "
    if mem.get("flagged_tickers"):
        mem_snippet += "Watchlist: " + ", ".join(mem["flagged_tickers"]) + ". "
    if mem.get("expressed_preferences"):
        mem_snippet += "Preferences: " + "; ".join(mem["expressed_preferences"][-3:]) + ". "
    if mem.get("last_email_summary"):
        mem_snippet += f"Last email: {mem['last_email_summary']} "
    if mem.get("session_count"):
        mem_snippet += f"Sessions: {mem['session_count']}."

    lines = [SYSTEM_PROMPT]
    if mem_snippet:
        lines.append(f"\nMemory: {mem_snippet}")
    if live_data:
        lines.append(f"\nLive data fetched for this message:\n{live_data}")
    if history:
        lines.append("\nConversation so far:")
        for turn in history[-10:]:
            lines.append(f"User: {turn['user']}")
            lines.append(f"Pippy: {turn['pippy']}")
    lines.append(f"\nUser: {user_message}")
    lines.append("Pippy:")
    return "\n".join(lines)


def update_memory(mem: dict, user_message: str) -> dict:
    """Update memory based on what the user said."""
    # Track tickers
    tickers = [w for w in re.findall(r'\b[A-Z]{1,5}\b', user_message.upper()) if w not in SKIP_WORDS]
    for t in tickers:
        mem["mentioned_stocks"][t] = mem["mentioned_stocks"].get(t, 0) + 1
        if mem["mentioned_stocks"][t] >= 3 and t not in mem["flagged_tickers"]:
            mem["flagged_tickers"].append(t)

    # Track preferences
    lower = user_message.lower()
    for kw in ["i like","i prefer","i love","i hate","i'm worried","worried about","i think","i believe"]:
        if kw in lower:
            snippet = user_message[:80].strip()
            if snippet not in mem["expressed_preferences"]:
                mem["expressed_preferences"].append(snippet)

    return mem


def call_claude(prompt: str) -> str:
    """Call claude -p with the given prompt and return the response."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            cwd=PROJECT_DIR,
            timeout=120,
        )
        if result.returncode != 0 and result.stderr:
            return f"Error: {result.stderr.strip()}"
        return result.stdout.strip() or "[No response]"
    except subprocess.TimeoutExpired:
        return "Timed out waiting for response. Try again."
    except FileNotFoundError:
        return "Error: `claude` CLI not found. Make sure Claude Code is installed."
    except Exception as e:
        return f"Error calling Claude: {e}"


def run():
    mem     = load_memory()
    history = load_history()

    print(f"\nPippy ready. (memory · forget · picks · briefing · exit)\n")

    opening_prompt = build_prompt(
        [],
        "Greet me in one short sentence using first person — say 'I' not 'Pippy'. No market data, no prices. Just let me know you're here and ready.",
        mem,
    )
    opening = call_claude(opening_prompt)
    print(f"Pippy: {opening}\n")
    history.append({"user": "[session start]", "pippy": opening})

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nPippy: Saved. Talk soon.")
            _save_and_exit(mem, history)

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd in ("exit", "quit", "bye"):
            mem["last_session"]  = datetime.now().isoformat()
            mem["session_count"] = mem.get("session_count", 0) + 1
            with open(MEMORY_FILE, "w") as f:
                json.dump(mem, f, indent=2)
            save_history(history)
            print("\nPippy: Saved. Talk soon.\n")
            sys.exit(0)

        if cmd == "forget":
            with open(MEMORY_FILE, "w") as f:
                json.dump(DEFAULT_MEMORY, f, indent=2)
            history = []
            save_history(history)
            print("\nPippy: Memory and history wiped. Starting fresh.\n")
            continue

        # Route shorthand commands to explicit instructions
        routed = {
            "memory":   "Call load_memory and print a clean readable summary of everything you know about me.",
            "picks":    "Call get_weekly_picks and display the results.",
            "briefing": "Call fetch_market_snapshot and fetch_top_headlines, then give me a quick briefing.",
            "watchlist": "Show me my current flagged_tickers watchlist from memory.",
        }
        effective_input = routed.get(cmd, user_input)

        # Update memory and fetch live data
        mem = update_memory(mem, user_input)
        print("  …", end="\r")
        live_data = fetch_live_data(effective_input)
        prompt = build_prompt(history, effective_input, mem, live_data)
        reply = call_claude(prompt)
        print(" " * 10, end="\r")
        print(f"\nPippy: {reply}\n")

        history.append({"user": user_input, "pippy": reply})
        save_history(history)


def _save_and_exit(mem, history):
    save_history(history)
    sys.exit(0)


if __name__ == "__main__":
    run()
