#!/usr/bin/env python3
"""
Pippy MCP Server — exposes all tools Claude needs for the terminal conversation.
Registered in .claude/settings.json so claude -p can use them automatically.
"""

import json
import os
from datetime import datetime

import yfinance as yf
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE   = os.path.join(PROJECT_DIR, "pippy_memory.json")
PICKS_FILE    = os.path.join(PROJECT_DIR, "picks_cache.json")

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

mcp = FastMCP("Pippy")


@mcp.tool()
def fetch_stock_data(ticker: str) -> str:
    """
    Fetch live stock data for a ticker: current price, daily change, 52-week
    range, P/E ratio, analyst target price, and latest news headline.
    Call this any time the user asks about a specific stock or company.
    """
    try:
        t     = yf.Ticker(ticker.upper())
        info  = t.fast_info
        full  = t.info
        price = info.last_price
        prev  = info.previous_close
        pct   = ((price - prev) / prev * 100) if price and prev else None
        ts    = datetime.now().strftime("%I:%M %p")

        headline = ""
        for item in (t.news or [])[:3]:
            h = item.get("content", {}).get("title") or item.get("title", "")
            if h:
                headline = h
                break

        mkt_cap = full.get("marketCap")
        cap_str = f"${mkt_cap/1e9:.1f}B" if mkt_cap else "N/A"

        lines = [
            f"[{ts}] {full.get('longName', ticker)} ({ticker.upper()})",
            f"Price: ${price:.2f}  Change: {pct:+.2f}%" if pct is not None else f"Price: ${price:.2f}",
            f"52w range: ${full.get('fiftyTwoWeekLow','N/A')} – ${full.get('fiftyTwoWeekHigh','N/A')}",
            f"P/E: {full.get('trailingPE','N/A')}  Fwd P/E: {full.get('forwardPE','N/A')}  Cap: {cap_str}",
            f"Analyst target: ${full.get('targetMeanPrice','N/A')}",
            f"Sector: {full.get('sector','N/A')}",
        ]
        if headline:
            lines.append(f"Latest news: {headline}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch data for {ticker}: {e}"


@mcp.tool()
def fetch_market_snapshot() -> str:
    """
    Fetch live S&P 500, Nasdaq, and Dow futures data with % change.
    Call this when the user asks about the market, pre-market conditions,
    or how things are moving today.
    """
    futures = {"S&P 500": "ES=F", "Nasdaq": "NQ=F", "Dow": "YM=F"}
    lines   = [f"[{datetime.now().strftime('%I:%M %p')}] Market snapshot:"]
    for name, sym in futures.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct   = (price - prev) / prev * 100
                arrow = "▲" if pct >= 0 else "▼"
                lines.append(f"  {name}: {price:,.2f}  {arrow} {abs(pct):.2f}%")
            else:
                lines.append(f"  {name}: N/A")
        except Exception as e:
            lines.append(f"  {name}: error — {e}")
    return "\n".join(lines)


@mcp.tool()
def fetch_top_headlines() -> str:
    """
    Fetch today's top market-moving headlines via Yahoo Finance.
    Call this when the user asks about news, what is happening today,
    or wants a market briefing.
    """
    skip  = ["beginner", "guide", "how to", "what is", "explainer", "glossary"]
    seen, titles = set(), []
    for sym in ["SPY", "QQQ", "^VIX", "GLD"]:
        try:
            for item in (yf.Ticker(sym).news or []):
                t = item.get("content", {}).get("title") or item.get("title", "")
                if t and t not in seen and len(t) > 20 and not any(k in t.lower() for k in skip):
                    seen.add(t)
                    titles.append(t)
        except Exception:
            continue
        if len(titles) >= 8:
            break
    return "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles[:5])) or "Could not fetch headlines."


@mcp.tool()
def load_memory() -> str:
    """
    Load Pippy's full memory: user interests, mentioned stocks, preferences,
    watchlist, and email history. Call this at the start of every session
    so you know the user's full history before they say a word.
    """
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                data = json.load(f)
            for k, v in DEFAULT_MEMORY.items():
                data.setdefault(k, v)
            return json.dumps(data, indent=2)
        except Exception as e:
            return f"Could not load memory: {e}"
    return json.dumps(DEFAULT_MEMORY, indent=2)


@mcp.tool()
def save_memory(data: dict) -> str:
    """
    Save the updated memory object to pippy_memory.json.
    Call this before ending every session with everything new you learned:
    stocks mentioned, preferences expressed, watchlist updates.
    """
    data["last_session"]  = datetime.now().isoformat()
    data["session_count"] = data.get("session_count", 0) + 1
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return f"Memory saved. Session #{data['session_count']} complete."


@mcp.tool()
def get_weekly_picks() -> str:
    """
    Get the current week's stock picks with risk levels and rationale.
    Call this when the user asks about picks, recommendations, or what
    stocks to watch this week.
    """
    if not os.path.exists(PICKS_FILE):
        return "No picks cache found. Run openbell.py first to generate picks."
    try:
        with open(PICKS_FILE) as f:
            cache = json.load(f)
        if not cache:
            return "Picks cache is empty. Run openbell.py to generate picks."
        latest_key = sorted(cache.keys())[-1]
        picks = cache[latest_key]
        if not picks:
            return f"No picks for {latest_key}."
        lines = [f"Top picks ({latest_key}):"]
        for p in picks:
            lines.append(f"\n  {p['ticker']} — {p['company']} ({p['sector']}) | Risk: {p.get('risk','—')}")
            lines.append(f"  {p['rationale']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not read picks: {e}"


@mcp.tool()
def get_last_email_summary() -> str:
    """
    Get a summary of what the OpenBell morning email covered today.
    Call this when the user asks what the email said, what was in the
    briefing, or what was covered this morning.
    """
    if not os.path.exists(MEMORY_FILE):
        return "No email has been sent yet."
    try:
        with open(MEMORY_FILE) as f:
            mem = json.load(f)
        summary = mem.get("last_email_summary", "")
        sent    = mem.get("last_email_sent", "")
        if not summary:
            return "No email summary recorded yet. Run openbell.py to send a briefing."
        return f"Last email ({sent[:10]}): {summary}"
    except Exception as e:
        return f"Could not read email summary: {e}"


if __name__ == "__main__":
    mcp.run()
