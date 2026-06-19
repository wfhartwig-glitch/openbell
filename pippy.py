#!/usr/bin/env python3
"""
Pippy — terminal investment assistant.
Fast data commands (briefing, picks, movers, watchlist, <TICKER>).
Everything else answered by Claude via `claude -p` with live data injected.
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

SYSTEM_PROMPT = """You are Pippy, an autonomous investment assistant and the brain behind a daily market briefing email called Pippy's Brief.

Rules:
- Plain text only. No markdown, no asterisks, no bullet symbols, no headers.
- 3-5 lines max. Be direct and confident.
- You are Pippy, not an assistant. Speak in first person.
- If live market data is provided, lead with it. Never say you lack access to data that is already shown.
- Never mention tools, MCP, Claude, or how you work internally."""

TICKER_SKIP = {
    "A","AN","THE","AND","OR","BUT","FOR","IN","ON","AT","TO","OF","BY","AS",
    "UP","IF","SO","BE","DO","GO","I","IT","IS","MY","ME","AM","HE","SHE","WE",
    "AI","US","OK","PE","EPS","CEO","CFO","IPO","GDP","FED","ETF","SEC","PM","AM",
}


# ── Colors ────────────────────────────────────────────────────────────────────

def _g(t): return f"\033[92m{t}\033[0m"
def _r(t): return f"\033[91m{t}\033[0m"
def _b(t): return f"\033[1m{t}\033[0m"
def _d(t): return f"\033[2m{t}\033[0m"

def _pct_str(pct):
    try:
        v = float(pct)
        s = f'{"▲" if v>=0 else "▼"} {abs(v):.2f}%'
        return _g(s) if v >= 0 else _r(s)
    except Exception:
        return "—"


# ── MCP helper ────────────────────────────────────────────────────────────────

async def call(session: ClientSession, name: str, args: dict = None):
    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except Exception:
        return text


# ── Display helpers ───────────────────────────────────────────────────────────

def show_snapshot(data: list):
    print()
    print(_b("  Market Snapshot"))
    print("  " + "─" * 38)
    for item in data:
        name  = item.get("name", "").ljust(10)
        price = item.get("price")
        pct   = item.get("pct")
        p_str = f"${float(price):>10,.2f}" if price else "          —"
        print(f"  {name}  {p_str}   {_pct_str(pct)}")
    print()


def show_headlines(headlines: list):
    print(_b("  Top Headlines"))
    print("  " + "─" * 38)
    for i, h in enumerate(headlines, 1):
        title = h.get("title", h) if isinstance(h, dict) else str(h)
        site  = h.get("site", "")  if isinstance(h, dict) else ""
        print(f"  {i}. {title}")
        if site:
            print(f"     {_d(site)}")
    print()


def show_stock(d: dict):
    sym   = d.get("ticker", "")
    name  = d.get("name", sym)
    price = d.get("price")
    pct   = d.get("pct")
    p_str = f"${float(price):,.2f}" if price else "—"
    print()
    print(f"  {_b(sym)}  {name}")
    print("  " + "─" * 38)
    print(f"  Price:    {p_str}   {_pct_str(pct)}")
    if d.get("52w_low") and d.get("52w_high"):
        print(f"  52-week:  ${d['52w_low']} – ${d['52w_high']}")
    if d.get("pe"):
        print(f"  P/E:      {d['pe']}")
    if d.get("market_cap"):
        print(f"  Mkt Cap:  {d['market_cap']}")
    if d.get("headline"):
        print(f"  News:     {_d(d['headline'][:80])}")
    print()


def show_picks(picks: list, week: str = ""):
    print()
    label = f"Weekly Picks — {week}" if week else "Weekly Picks"
    print(_b(f"  {label}"))
    print("  " + "─" * 38)
    for p in picks:
        ticker = p.get("ticker", "").ljust(6)
        company = p.get("company", "")[:28].ljust(28)
        risk    = (p.get("risk_level") or p.get("risk", "—")).ljust(12)
        note    = p.get("note") or p.get("rationale", "")
        weeks   = p.get("weeks_held", 1)
        pct     = p.get("pct_change_this_week")
        pct_s   = f"  {_pct_str(pct)}" if pct is not None else ""
        print(f"  {_b(ticker)}  {company}  {_d(risk)}  {weeks}w{pct_s}")
        if note:
            print(f"          {_d(note[:70])}")
    print()


def show_memory(mem: dict):
    print()
    print(_b("  Pippy Memory"))
    print("  " + "─" * 38)
    if mem.get("flagged_tickers"):
        print(f"  Watchlist:   {', '.join(mem['flagged_tickers'])}")
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:6]
        print(f"  Mentions:    {', '.join(f'{t}({c}x)' for t, c in top)}")
    if mem.get("last_email_summary"):
        print(f"  Last email:  {mem['last_email_summary']}")
    lessons = mem.get("lessons_learned", [])
    if lessons:
        print(f"  Last lesson: {_d(lessons[-1].get('note','')[:70])}")
    print(f"  Sessions:    {mem.get('session_count', 0)}   Emails: {mem.get('email_count', 0)}")
    print()


def show_movers(gainers: list, losers: list):
    print()
    print(_b("  Today's Movers"))
    print("  " + "─" * 38)
    print(f"  {_g('GAINERS')}")
    for m in gainers:
        sym = m.get("symbol", "").ljust(6)
        print(f"    {sym}  {_pct_str(m.get('pct'))}")
    print(f"\n  {_r('LOSERS')}")
    for m in losers:
        sym = m.get("symbol", "").ljust(6)
        print(f"    {sym}  {_pct_str(m.get('pct'))}")
    print()


# ── Claude via subprocess ─────────────────────────────────────────────────────

def ask_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=120,
        )
        if result.returncode != 0 and result.stderr:
            return f"Error: {result.stderr.strip()[:120]}"
        return result.stdout.strip() or "[no response]"
    except FileNotFoundError:
        return "Error: claude CLI not found. Make sure Claude Code is installed."
    except subprocess.TimeoutExpired:
        return "Timed out."
    except Exception as e:
        return f"Error: {e}"


async def claude_with_data(session: ClientSession, user_input: str, mem: dict) -> str:
    """Fetch relevant live data then pass everything to claude -p."""
    lower = user_input.lower()
    data_lines = []

    # Always inject memory context
    mem_parts = []
    if mem.get("flagged_tickers"):
        mem_parts.append(f"Watchlist: {', '.join(mem['flagged_tickers'])}")
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:5]
        mem_parts.append(f"Most discussed: {', '.join(f'{t}({c}x)' for t, c in top)}")
    if mem.get("last_email_summary"):
        mem_parts.append(f"Last email: {mem['last_email_summary']}")
    lessons = mem.get("lessons_learned", [])
    if lessons:
        mem_parts.append(f"Recent insight: {lessons[-1].get('note','')}")

    # Fetch market data if question is market-related
    market_words = ["market","stock","invest","portfolio","buy","sell","sector",
                    "index","sp500","nasdaq","dow","futures","economy","fed","rate",
                    "brief","today","going on","happening","news","headline"]
    if any(w in lower for w in market_words):
        print(_d("  fetching live data…"))
        snap = await call(session, "fetch_market_snapshot")
        for item in snap.get("data", []):
            pct = item.get("pct", 0) or 0
            arr = "▲" if float(pct) >= 0 else "▼"
            data_lines.append(f"{item.get('name')}: ${item.get('price'):,.2f} {arr} {abs(float(pct)):.2f}%")
        heads = await call(session, "fetch_top_headlines")
        for h in heads.get("headlines", [])[:3]:
            data_lines.append(f"News: {h.get('title','')}")

    # Fetch specific ticker data if any ALL-CAPS tickers mentioned
    tokens  = re.findall(r'\b[A-Z]{2,5}\b', user_input)
    tickers = [t for t in tokens if t not in TICKER_SKIP]
    for sym in tickers[:2]:
        print(_d(f"  fetching {sym}…"))
        d = await call(session, "fetch_stock_data", {"ticker": sym})
        if d.get("price"):
            pct = d.get("pct", 0) or 0
            arr = "▲" if float(pct) >= 0 else "▼"
            data_lines.append(
                f"{sym} ({d.get('name','')}) ${d.get('price'):,.2f} {arr} {abs(float(pct)):.2f}% "
                f"| 52w ${d.get('52w_low')}–${d.get('52w_high')} | P/E {d.get('pe','N/A')} | {d.get('market_cap','')}"
            )
            if d.get("headline"):
                data_lines.append(f"{sym} news: {d['headline']}")

    # Build prompt
    now = datetime.now().strftime("%A %B %d, %Y %I:%M %p")
    parts = [SYSTEM_PROMPT]
    if mem_parts:
        parts.append(f"\nUser context: {' | '.join(mem_parts)}")
    if data_lines:
        parts.append(f"\nLive market data as of {now}:\n" + "\n".join(data_lines))
    parts.append(f"\nUser: {user_input}\nPippy:")
    return ask_claude("\n".join(parts))


# ── Git ───────────────────────────────────────────────────────────────────────

def git_pull():
    try:
        r = subprocess.run(["git", "pull", "--rebase", "--quiet"],
                           capture_output=True, text=True, cwd=PROJECT_DIR, timeout=15)
        if r.returncode == 0:
            print(_d("  (synced from cloud)"))
    except Exception:
        pass


def git_push():
    try:
        subprocess.run(["git", "add", "pippy_memory.json"], cwd=PROJECT_DIR, capture_output=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=PROJECT_DIR)
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m",
                 f"Pippy memory — terminal {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
                cwd=PROJECT_DIR, capture_output=True,
            )
            subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True)
            print(_d("  (memory pushed to cloud)"))
    except Exception:
        pass


# ── REPL ──────────────────────────────────────────────────────────────────────

async def repl(session: ClientSession):
    git_pull()
    mem = await call(session, "load_memory")

    print()
    print(_b("  Pippy — investment assistant"))
    print(_d("  briefing · picks · movers · watchlist · memory · <TICKER> · exit"))
    print(_d("  or just ask anything"))
    print()

    while True:
        try:
            user = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Saved. Talk soon.")
            if isinstance(mem, dict):
                await call(session, "save_memory", {"data": mem})
            git_push()
            break

        if not user:
            continue

        cmd = user.lower().strip()

        if cmd in ("exit", "quit", "bye"):
            if isinstance(mem, dict):
                await call(session, "save_memory", {"data": mem})
            git_push()
            print("  Saved. Talk soon.")
            break

        if cmd == "forget":
            await call(session, "save_memory", {"data": {}})
            mem = await call(session, "load_memory")
            print("  Memory wiped.\n")
            continue

        if cmd in ("briefing", "today", "market"):
            snap  = await call(session, "fetch_market_snapshot")
            heads = await call(session, "fetch_top_headlines")
            show_snapshot(snap.get("data", []))
            show_headlines(heads.get("headlines", []))
            continue

        if cmd == "movers":
            movers = await call(session, "fetch_top_movers")
            show_movers(movers.get("gainers", []), movers.get("losers", []))
            continue

        if cmd == "picks":
            picks = await call(session, "get_weekly_picks")
            if not picks.get("picks"):
                print("  Generating picks…")
                picks = await call(session, "generate_weekly_picks")
            show_picks(picks.get("picks", []), picks.get("week", ""))
            continue

        if cmd in ("memory", "me", "profile"):
            mem = await call(session, "load_memory")
            if isinstance(mem, dict):
                show_memory(mem)
            continue

        if cmd in ("watchlist", "watch"):
            mem     = await call(session, "load_memory")
            flagged = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
            if not flagged:
                print("  Watchlist is empty — mention a ticker 3+ times to add it.\n")
                continue
            for t in flagged:
                show_stock(await call(session, "fetch_stock_data", {"ticker": t}))
            continue

        # Pure ALL-CAPS ticker shortcut (e.g. user types just "NVDA")
        tokens  = re.findall(r'\b[A-Z]{2,5}\b', user)
        tickers = [t for t in tokens if t not in TICKER_SKIP]
        # Only treat as pure ticker lookup if the ENTIRE input is tickers
        if tickers and all(t in tickers for t in re.findall(r'\b\w+\b', user) if len(t) >= 2):
            for sym in tickers[:2]:
                d = await call(session, "fetch_stock_data", {"ticker": sym})
                show_stock(d)
                if isinstance(mem, dict):
                    mem.setdefault("mentioned_stocks", {})
                    mem["mentioned_stocks"][sym] = mem["mentioned_stocks"].get(sym, 0) + 1
                    if (mem["mentioned_stocks"][sym] >= 3
                            and sym not in mem.get("flagged_tickers", [])):
                        mem.setdefault("flagged_tickers", []).append(sym)
                        print(_d(f"  {sym} added to your watchlist."))
            continue

        # Everything else → Claude with live data injected
        print(_d("  thinking…"))
        reply = await claude_with_data(session, user, mem if isinstance(mem, dict) else {})
        print(f"\n  Pippy: {reply}\n")

        # Track any tickers mentioned
        if isinstance(mem, dict):
            for sym in tickers[:3]:
                mem.setdefault("mentioned_stocks", {})
                mem["mentioned_stocks"][sym] = mem["mentioned_stocks"].get(sym, 0) + 1
                if (mem["mentioned_stocks"][sym] >= 3
                        and sym not in mem.get("flagged_tickers", [])):
                    mem.setdefault("flagged_tickers", []).append(sym)


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
