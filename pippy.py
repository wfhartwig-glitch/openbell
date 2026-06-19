#!/usr/bin/env python3
"""
Pippy — terminal market data interface. Zero AI API cost.
Connects to pippy_mcp.py as an MCP client and displays live FMP data.

Commands: briefing, today, picks, memory, watchlist, forget, <TICKER>, exit
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

SKIP_WORDS = {
    # Articles / prepositions / conjunctions
    "A", "AN", "THE", "AND", "OR", "BUT", "FOR", "IN", "ON", "AT", "TO", "OF",
    "BY", "AS", "UP", "IF", "SO", "BE", "DO", "GO",
    # Pronouns / common verbs
    "I", "IT", "IS", "MY", "ME", "AM", "HE", "SHE", "WE", "HIS", "HER", "ITS",
    "ARE", "WAS", "HAS", "HAD", "CAN", "WILL", "DOES", "DID", "GET", "GOT",
    "BUY", "SELL", "HOLD", "USE", "SAY", "SEE", "NOW", "NEW",
    # Question words
    "WHAT", "HOW", "WHY", "WHEN", "WHO", "WHICH", "WHERE",
    # Finance jargon that looks like tickers
    "AI", "US", "OK", "ET", "PE", "YOY", "EPS", "CEO", "CFO", "IPO",
    "GDP", "FED", "ETF", "SEC", "ESG", "DCF", "ROI", "YTD", "ATH",
    "PM", "AM", "CT", "PT", "ET",
    # Common English words that regex catches as uppercase
    "BEST", "GOOD", "HIGH", "LOW", "TOP", "BIG", "BAD", "OLD", "NEW",
    "STOCK", "STOCKS", "PICK", "PICKS", "MARKET", "RATE", "RATES",
    "YEAR", "WEEK", "MONTH", "DAY", "TIME", "LATE", "NEXT", "LAST",
    "MOST", "MORE", "LESS", "JUST", "VERY", "ALSO", "EVEN", "ONLY",
    "INTO", "FROM", "WITH", "THAN", "THAT", "THIS", "THEY", "THEM",
    "MAKE", "TAKE", "WANT", "NEED", "LIKE", "SOME", "MANY", "MUCH",
    "LONG", "SHORT", "CALL", "PUTS", "CASH", "DEBT", "RISK", "BULL", "BEAR",
    "RIGHT", "THINK", "KNOW", "LOOK", "FEEL", "WELL", "BACK", "DOWN",
}


# ── Display helpers ───────────────────────────────────────────────────────────

def _arrow(pct) -> str:
    try:
        return "▲" if float(pct) >= 0 else "▼"
    except Exception:
        return "—"


def _color(pct, text) -> str:
    GREEN = "\033[92m"
    RED   = "\033[91m"
    RESET = "\033[0m"
    try:
        return f"{GREEN}{text}{RESET}" if float(pct) >= 0 else f"{RED}{text}{RESET}"
    except Exception:
        return text


def _fmt_pct(pct) -> str:
    try:
        v = float(pct)
        return f"{_arrow(v)} {abs(v):.2f}%"
    except Exception:
        return str(pct) if pct else "N/A"


def _bold(text) -> str:
    return f"\033[1m{text}\033[0m"


def _dim(text) -> str:
    return f"\033[2m{text}\033[0m"


# ── MCP helper ────────────────────────────────────────────────────────────────

async def call(session: ClientSession, name: str, args: dict = None) -> dict | list | str:
    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except Exception:
        return text


# ── Display functions ─────────────────────────────────────────────────────────

def show_snapshot(data: list):
    print()
    print(_bold("  Market Snapshot"))
    print("  " + "─" * 40)
    for item in data:
        name  = item.get("name", "").ljust(10)
        price = item.get("price")
        pct   = item.get("pct")
        p_str = f"${float(price):>10,.2f}" if price else "        —"
        pct_s = _fmt_pct(pct)
        print(f"  {name}  {p_str}   {_color(pct, pct_s)}")
    print()


def show_headlines(headlines: list):
    print(_bold("  Top Headlines"))
    print("  " + "─" * 40)
    for i, h in enumerate(headlines, 1):
        title = h.get("title", h) if isinstance(h, dict) else str(h)
        site  = h.get("site", "") if isinstance(h, dict) else ""
        print(f"  {i}. {title}")
        if site:
            print(f"     {_dim(site)}")
    print()


def show_stock(d: dict):
    sym   = d.get("ticker", "")
    name  = d.get("name", sym)
    price = d.get("price")
    pct   = d.get("pct")
    p_str = f"${float(price):,.2f}" if price else "—"
    print()
    print(f"  {_bold(sym)}  {name}")
    print("  " + "─" * 40)
    print(f"  Price:     {p_str}   {_color(pct, _fmt_pct(pct))}")
    if d.get("52w_low") and d.get("52w_high"):
        print(f"  52-week:   ${d['52w_low']} – ${d['52w_high']}")
    if d.get("pe"):
        print(f"  P/E:       {d['pe']}")
    if d.get("market_cap"):
        print(f"  Mkt Cap:   {d['market_cap']}")
    if d.get("headline"):
        print(f"  News:      {_dim(d['headline'][:80])}")
    print()


def show_picks(picks: list, month: str):
    print()
    print(_bold(f"  Monthly Picks — {month}"))
    print("  " + "─" * 40)
    for p in picks:
        ticker  = p.get("ticker", "").ljust(6)
        company = p.get("company", "")[:30].ljust(30)
        risk    = p.get("risk", "—").ljust(12)
        rat     = p.get("rationale", "")[:60]
        print(f"  {_bold(ticker)}  {company}  {_dim(risk)}")
        print(f"         {_dim(rat)}")
    print()


def show_memory(mem: dict):
    print()
    print(_bold("  Pippy Memory"))
    print("  " + "─" * 40)
    if mem.get("flagged_tickers"):
        print(f"  Watchlist:    {', '.join(mem['flagged_tickers'])}")
    if mem.get("interests"):
        print(f"  Interests:    {', '.join(mem['interests'][:5])}")
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:8]
        print(f"  Top mentions: {', '.join(f'{t}({c}x)' for t, c in top)}")
    if mem.get("last_email_summary"):
        print(f"  Last email:   {mem['last_email_summary']}")
    print(f"  Sessions:     {mem.get('session_count', 0)}")
    print(f"  Emails sent:  {mem.get('email_count', 0)}")
    print()


def show_movers(gainers: list, losers: list):
    print()
    print(_bold("  Today's Movers"))
    print("  " + "─" * 40)
    print(f"  {_color(1, 'GAINERS')}")
    for m in gainers:
        sym  = m.get("symbol", "").ljust(6)
        pct  = m.get("pct")
        print(f"    {sym}  {_color(pct, _fmt_pct(pct))}")
    print(f"\n  {_color(-1, 'LOSERS')}")
    for m in losers:
        sym  = m.get("symbol", "").ljust(6)
        pct  = m.get("pct")
        print(f"    {sym}  {_color(pct, _fmt_pct(pct))}")
    print()


# ── Git helpers ───────────────────────────────────────────────────────────────

def git_pull():
    try:
        r = subprocess.run(["git", "pull", "--rebase", "--quiet"],
                           capture_output=True, text=True, cwd=PROJECT_DIR, timeout=15)
        if r.returncode == 0:
            print(_dim("  (synced memory from cloud)"))
    except Exception:
        pass


def git_push():
    try:
        subprocess.run(["git", "add", "pippy_memory.json"],
                       cwd=PROJECT_DIR, capture_output=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=PROJECT_DIR)
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m",
                 f"Pippy memory — terminal {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
                cwd=PROJECT_DIR, capture_output=True,
            )
            subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True)
            print(_dim("  (memory pushed to cloud)"))
    except Exception:
        pass


# ── Main REPL ─────────────────────────────────────────────────────────────────

async def repl(session: ClientSession):
    git_pull()
    mem = await call(session, "load_memory")

    print()
    print(_bold("  Pippy — market data terminal"))
    print(_dim("  briefing · picks · memory · watchlist · movers · <TICKER> · exit"))
    print()

    while True:
        try:
            user = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Bye.")
            await call(session, "save_memory", {"data": mem}) if isinstance(mem, dict) else None
            git_push()
            break

        if not user:
            continue

        cmd = user.lower()

        if cmd in ("exit", "quit", "bye"):
            if isinstance(mem, dict):
                await call(session, "save_memory", {"data": mem})
            git_push()
            print("  Bye.")
            break

        if cmd == "forget":
            await call(session, "save_memory", {"data": {}})
            mem = await call(session, "load_memory")
            print("  Memory wiped.")
            continue

        if cmd in ("briefing", "today", "market"):
            snapshot  = await call(session, "fetch_market_snapshot")
            headlines = await call(session, "fetch_top_headlines")
            show_snapshot(snapshot.get("data", []))
            show_headlines(headlines.get("headlines", []))
            continue

        if cmd == "movers":
            movers = await call(session, "fetch_top_movers")
            show_movers(movers.get("gainers", []), movers.get("losers", []))
            continue

        if cmd == "picks":
            picks = await call(session, "get_monthly_picks")
            if not picks.get("picks"):
                print("  Generating picks…")
                picks = await call(session, "generate_monthly_picks")
            show_picks(picks.get("picks", []), picks.get("month", ""))
            continue

        if cmd in ("memory", "me", "profile"):
            mem = await call(session, "load_memory")
            if isinstance(mem, dict):
                show_memory(mem)
            continue

        if cmd in ("watchlist", "watch"):
            mem = await call(session, "load_memory")
            flagged = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
            if not flagged:
                print("  Watchlist is empty. Mention a ticker 3+ times to add it.\n")
                continue
            for t in flagged:
                d = await call(session, "fetch_stock_data", {"ticker": t})
                show_stock(d)
            continue

        # Ticker lookup — only match words the user typed in ALL-CAPS themselves
        tokens  = re.findall(r'\b[A-Z]{2,5}\b', user)  # original case, not uppercased
        tickers = [t for t in tokens if t not in SKIP_WORDS]
        if tickers:
            for sym in tickers[:2]:
                d = await call(session, "fetch_stock_data", {"ticker": sym})
                show_stock(d)
                # Track mentions
                if isinstance(mem, dict):
                    mem.setdefault("mentioned_stocks", {})
                    mem["mentioned_stocks"][sym] = mem["mentioned_stocks"].get(sym, 0) + 1
                    if (mem["mentioned_stocks"][sym] >= 3
                            and sym not in mem.get("flagged_tickers", [])):
                        mem.setdefault("flagged_tickers", []).append(sym)
                        print(f"  {_dim(sym + ' added to your watchlist.')}")
            continue

        print(f"  I'm a data terminal — I don't answer open-ended questions.")
        print(f"  Try: briefing · picks · memory · movers · watchlist · AAPL · exit\n")


async def main():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")],
        env=dict(os.environ),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await repl(session)


if __name__ == "__main__":
    asyncio.run(main())
