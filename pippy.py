#!/usr/bin/env python3
"""
Pippy — sharp terminal investment assistant.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = """You are Pippy — a sharp, opinionated investment analyst. Think like a hedge fund PM who has seen every market cycle and always has a view.

Rules:
- Lead with the answer in the first sentence. Never restate the question, never open with filler like "Great question" or "Let's dive in."
- Plain text only. No markdown, no asterisks, no bullet symbol characters.
- Match length to the question. Quick question = 2-3 lines. Deep question = as long as it needs to be, but no padding.
- Only elaborate if the user asks a follow-up or says "more" or "explain."
- You are Pippy. Not Claude. Not an assistant. Never break character or explain how you work.
- When asked for your best pick or call, make one. Don't hedge.
- If something in the market data is notable and the user didn't ask about it, flag it anyway.
- You remember everything discussed earlier in this session. Build on it."""


# ── Colors ────────────────────────────────────────────────────────────────────

def _g(t): return f"\033[92m{t}\033[0m"
def _r(t): return f"\033[91m{t}\033[0m"
def _b(t): return f"\033[1m{t}\033[0m"
def _d(t): return f"\033[2m{t}\033[0m"
def _c(t): return f"\033[96m{t}\033[0m"

def _pct(pct):
    try:
        v = float(pct)
        s = f'{"▲" if v >= 0 else "▼"} {abs(v):.2f}%'
        return _g(s) if v >= 0 else _r(s)
    except Exception:
        return "—"


# ── Session-level data cache (60s TTL) ────────────────────────────────────────

_CACHE: dict = {}
_CACHE_TTL   = 60  # seconds


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _CACHE[key] = {"ts": time.time(), "data": data}


# ── MCP helper ────────────────────────────────────────────────────────────────

async def call(session: ClientSession, name: str, args: dict = None):
    """Call an MCP tool. Caches read-only data tools for 60s."""
    cacheable = {
        "fetch_market_snapshot", "fetch_top_headlines", "fetch_top_movers",
        "fetch_sector_performance", "fetch_earnings_calendar",
        "fetch_economic_calendar", "get_weekly_picks",
    }
    cache_key = f"{name}:{json.dumps(args or {}, sort_keys=True)}"
    if name in cacheable:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        data = json.loads(text)
    except Exception:
        data = text

    if name in cacheable:
        _cache_set(cache_key, data)
    return data


# ── Display helpers ───────────────────────────────────────────────────────────

def show_snapshot(data: list, source: str = ""):
    print()
    src_label = _d(f"  [{source}]") if source else ""
    print(_b("  Market Snapshot") + (f"  {_d(source)}" if source else ""))
    print("  " + "─" * 42)
    for item in data:
        name  = item.get("name", "").ljust(10)
        price = item.get("price")
        p_str = f"${float(price):>10,.2f}" if price else "          —"
        print(f"  {name}  {p_str}   {_pct(item.get('pct'))}")
    print()


def show_headlines(headlines: list):
    print(_b("  Top Headlines"))
    print("  " + "─" * 42)
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
    p_str = f"${float(price):,.2f}" if price else "—"
    print()
    print(f"  {_b(sym)}  {name}")
    print("  " + "─" * 42)
    print(f"  Price:    {p_str}   {_pct(d.get('pct'))}")
    if d.get("52w_low") and d.get("52w_high"):
        print(f"  52-week:  ${d['52w_low']} – ${d['52w_high']}")
    if d.get("pe"):
        print(f"  P/E:      {d['pe']}")
    if d.get("market_cap"):
        print(f"  Mkt Cap:  {d['market_cap']}")
    if d.get("headline"):
        print(f"  News:     {_d(d['headline'][:85])}")
    print()


def show_movers(gainers: list, losers: list):
    print()
    print(_b("  Today's Movers"))
    print("  " + "─" * 42)
    print(f"  {_g('GAINERS')}")
    for m in gainers:
        name = (m.get("name") or "")[:24].ljust(24)
        print(f"    {m.get('symbol','').ljust(6)}  {name}  {_pct(m.get('pct'))}")
    print(f"\n  {_r('LOSERS')}")
    for m in losers:
        name = (m.get("name") or "")[:24].ljust(24)
        print(f"    {m.get('symbol','').ljust(6)}  {name}  {_pct(m.get('pct'))}")
    print()


def show_sectors(sectors: list):
    print()
    print(_b("  Sector Performance"))
    print("  " + "─" * 42)
    for s in sectors:
        name = s.get("sector", "").ljust(22)
        print(f"  {name}  {_pct(s.get('pct'))}")
    print()


def show_picks(picks: list, week: str = ""):
    print()
    label = f"Weekly Picks — {week}" if week else "Weekly Picks"
    print(_b(f"  {label}"))
    print("  " + "─" * 42)
    for p in picks:
        ticker  = p.get("ticker", "").ljust(6)
        company = p.get("company", "")[:26].ljust(26)
        risk    = (p.get("risk_level") or p.get("risk", "—")).ljust(12)
        note    = p.get("note") or p.get("rationale", "")
        weeks   = p.get("weeks_held", 1)
        wk_pct  = p.get("pct_change_this_week")
        pct_s   = f"  {_pct(wk_pct)}" if wk_pct is not None else ""
        print(f"  {_b(ticker)}  {company}  {_d(risk)}  {weeks}w{pct_s}")
        if note:
            print(f"          {_d(note[:72])}")
    print()


def show_memory(mem: dict):
    print()
    print(_b("  Pippy Memory"))
    print("  " + "─" * 42)
    if mem.get("flagged_tickers"):
        print(f"  Watchlist:   {', '.join(mem['flagged_tickers'])}")
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:6]
        print(f"  Mentions:    {', '.join(f'{t}({c}x)' for t, c in top)}")
    if mem.get("last_email_summary"):
        print(f"  Last email:  {mem['last_email_summary']}")
    lessons = mem.get("lessons_learned", [])
    if lessons:
        print(f"  Insight:     {_d(lessons[-1].get('note', '')[:72])}")
    print(f"  Sessions:    {mem.get('session_count', 0)}   Emails: {mem.get('email_count', 0)}")
    print()


# ── Startup greeting ──────────────────────────────────────────────────────────

async def print_greeting(session: ClientSession):
    """Fetch live market status and print a warm, conversational greeting."""
    import pytz
    import random
    try:
        market_status, snap = await asyncio.gather(
            call(session, "is_market_open_today"),
            call(session, "fetch_market_snapshot"),
        )
        indices = snap.get("data", [])
        sp  = next((i for i in indices if i.get("name") == "S&P 500"), None)
        ndx = next((i for i in indices if i.get("name") == "Nasdaq"),  None)

        is_open = False
        if isinstance(market_status, dict):
            is_open = bool(market_status.get("open", False))

        def fmt_pct(item):
            if not item:
                return None
            try:
                v = float(item.get("pct", 0) or 0)
                direction = "up" if v >= 0 else "down"
                return f"{direction} {abs(v):.2f}%"
            except Exception:
                return None

        sp_s  = fmt_pct(sp)
        ndx_s = fmt_pct(ndx)

        print()
        print(_b("  Pippy"))

        if is_open:
            sp_part  = f"S&P's {sp_s}"  if sp_s  else None
            ndx_part = f"Nasdaq {ndx_s}" if ndx_s else None
            nums = " and ".join(p for p in [sp_part, ndx_part] if p)
            openers = [
                f"Hey — markets are live.{(' ' + nums + '.') if nums else ''} What's on your mind?",
                f"Morning. We're open.{(' ' + nums + '.') if nums else ''} What are you watching?",
                f"Markets are running.{(' ' + nums + '.') if nums else ''} What do you want to dig into?",
            ]
            print(f"  {random.choice(openers)}")

        else:
            et     = pytz.timezone("America/New_York")
            now_et = datetime.now(et)

            if now_et.weekday() >= 5:
                day = now_et.strftime("%A")
                sp_part  = f"S&P closed {sp_s}"  if sp_s  else None
                ndx_part = f"Nasdaq {ndx_s}" if ndx_s else None
                nums = ", ".join(p for p in [sp_part, ndx_part] if p)
                openers = [
                    f"It's {day} — markets are dark.{(' ' + nums + ' on the week.' ) if nums else ''} Good time to zoom out.",
                    f"{day}. No trading today.{(' ' + nums + '.' ) if nums else ''} Want to look at picks or talk through something?",
                    f"Weekend. Markets closed.{(' ' + nums + '.' ) if nums else ''} Want the weekly recap or just to think out loud?",
                ]
                print(f"  {random.choice(openers)}")

            else:
                now_hour = now_et.hour + now_et.minute / 60
                sp_part  = f"S&P closed {sp_s}"  if sp_s  else None
                ndx_part = f"Nasdaq {ndx_s}" if ndx_s else None
                nums = ", ".join(p for p in [sp_part, ndx_part] if p)

                if now_hour < 9.5:
                    openers = [
                        f"Pre-market. Bell's not rung yet.{(' ' + nums + ' at the close yesterday.') if nums else ''} What are you thinking about?",
                        f"Early. Markets open at 9:30.{(' ' + nums + ' yesterday.') if nums else ''} What's on your radar?",
                    ]
                else:
                    tod = "Evening" if now_hour >= 17 else "Afternoon"
                    openers = [
                        f"{tod}. Markets wrapped up — {nums + '.' if nums else 'day is done.'} Want the full rundown or something specific?",
                        f"Day's done.{(' ' + nums + '.') if nums else ''} What do you want to look at?",
                        f"After-hours now.{(' ' + nums + ' today.') if nums else ''} What's on your mind?",
                    ]
                print(f"  {random.choice(openers)}")

        print(_d("  briefing · picks · movers · sectors · watchlist · memory · exit"))
        print()
    except Exception:
        print()
        print(_b("  Pippy"))
        print(_d("  briefing · picks · movers · sectors · watchlist · memory · exit"))
        print()


# ── Market context builder ────────────────────────────────────────────────────

SKIP_WORDS = {
    "A","AN","THE","AND","OR","I","IN","ON","AT","TO","OF","IS","IT","MY",
    "ME","AM","BE","DO","AI","US","OK","PE","EPS","CEO","CFO","IPO","GDP",
    "FED","ETF","SEC","PM","CT","SP","IF","SO","UP","BY","AS","FOR","NO",
    "YES","NOT","ALL","BUT","HAS","HAD","CAN","GET","ITS","ARE","WAS","HOW",
    "WHY","WHO","WHAT","WHEN","WILL","DID","ANY","NOW","OUT","ONE",
}


async def build_market_context(session: ClientSession, user_input: str, mem: dict) -> str:
    lower = user_input.lower()
    lines = [f"Current date/time: {datetime.now().strftime('%A %B %d, %Y — %I:%M %p CT')}"]

    # Parallel fetch of always-needed data — single round trip
    snap, heads, movers, sectors = await asyncio.gather(
        call(session, "fetch_market_snapshot"),
        call(session, "fetch_top_headlines"),
        call(session, "fetch_top_movers"),
        call(session, "fetch_sector_performance"),
    )

    # Indices
    lines.append("\nMarket indices:")
    for item in snap.get("data", []):
        pct = item.get("pct", 0) or 0
        arr = "up" if float(pct) >= 0 else "down"
        lines.append(f"  {item.get('name')}: ${item.get('price'):,.2f} ({arr} {abs(float(pct)):.2f}%)")

    # Movers
    lines.append("\nTop gainers:")
    for m in movers.get("gainers", []):
        lines.append(f"  {m.get('symbol')} {m.get('name', '')} +{m.get('pct', 0):.2f}%")
    lines.append("Top losers:")
    for m in movers.get("losers", []):
        lines.append(f"  {m.get('symbol')} {m.get('name', '')} {m.get('pct', 0):.2f}%")

    # Headlines
    lines.append("\nTop headlines:")
    for h in heads.get("headlines", [])[:5]:
        lines.append(f"  - {h.get('title', '')}")

    # Sectors
    lines.append("\nSector performance:")
    for s in sectors.get("sectors", []):
        pct = s.get("pct", 0) or 0
        arr = "up" if float(pct) >= 0 else "down"
        lines.append(f"  {s.get('sector')}: {arr} {abs(float(pct)):.2f}%")

    # Conditional: earnings calendar
    earn_words = {"earn","report","eps","quarter","q1","q2","q3","q4","guidance","beat","miss"}
    if any(w in lower for w in earn_words):
        earn = await call(session, "fetch_earnings_calendar")
        if earn.get("earnings"):
            lines.append("\nUpcoming earnings:")
            for e in earn.get("earnings", [])[:8]:
                eps = f" EPS est. ${e['eps_estimated']:.2f}" if e.get("eps_estimated") else ""
                lines.append(f"  {e.get('symbol')} — {e.get('date', '')}{eps}")

    # Conditional: economic calendar
    macro_words = {"fed","rate","cpi","inflation","gdp","jobs","unemployment","macro","economy","fomc","powell","rates"}
    if any(w in lower for w in macro_words):
        econ = await call(session, "fetch_economic_calendar")
        if econ.get("events"):
            lines.append("\nEconomic calendar:")
            for e in econ.get("events", [])[:8]:
                lines.append(f"  {e.get('date', '')} {e.get('event', '')} [{e.get('impact', '')}]")

    # Specific tickers mentioned in all-caps
    tokens  = re.findall(r'\b[A-Z]{2,5}\b', user_input)
    tickers = [t for t in tokens if t not in SKIP_WORDS]
    for sym in tickers[:3]:
        d = await call(session, "fetch_stock_data", {"ticker": sym})
        if d.get("price"):
            pct = d.get("pct", 0) or 0
            arr = "up" if float(pct) >= 0 else "down"
            lines.append(f"\n{sym} ({d.get('name', '')}):")
            lines.append(f"  Price: ${d.get('price'):,.2f} ({arr} {abs(float(pct)):.2f}%)")
            lines.append(f"  52-week: ${d.get('52w_low')} – ${d.get('52w_high')}")
            if d.get("pe"):         lines.append(f"  P/E: {d['pe']}")
            if d.get("market_cap"): lines.append(f"  Market cap: {d['market_cap']}")
            if d.get("headline"):   lines.append(f"  Latest news: {d['headline']}")

    # Memory context
    lines.append("\nUser context:")
    if mem.get("flagged_tickers"):
        lines.append(f"  Watchlist: {', '.join(mem['flagged_tickers'])}")
    if mem.get("mentioned_stocks"):
        top = sorted(mem["mentioned_stocks"].items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append(f"  Most discussed: {', '.join(f'{t}({c}x)' for t, c in top)}")
    picks_data = await call(session, "get_weekly_picks")
    if picks_data.get("picks"):
        lines.append(f"  Current picks: {', '.join(p['ticker'] for p in picks_data['picks'])}")
    lessons = mem.get("lessons_learned", [])
    if lessons:
        lines.append(f"  Recent insight: {lessons[-1].get('note', '')}")

    return "\n".join(lines)


# ── Claude via subprocess ─────────────────────────────────────────────────────

def ask_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=120,
        )
        if result.returncode != 0 and result.stderr:
            return f"(error: {result.stderr.strip()[:100]})"
        return result.stdout.strip() or "(no response)"
    except FileNotFoundError:
        return "Error: claude CLI not found."
    except subprocess.TimeoutExpired:
        return "Timed out."
    except Exception as e:
        return f"Error: {e}"


# ── Git ───────────────────────────────────────────────────────────────────────

def git_pull():
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=15,
        )
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
    except Exception:
        pass


# ── REPL ──────────────────────────────────────────────────────────────────────

async def repl(session: ClientSession):
    git_pull()
    mem, _ = await asyncio.gather(
        call(session, "load_memory"),
        print_greeting(session),
    )
    history: list[dict] = []

    while True:
        try:
            user = input("  You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Saved.")
            if isinstance(mem, dict):
                await call(session, "save_memory", {"data": mem})
            git_push()
            break

        if not user:
            continue

        cmd = user.lower().strip()

        # ── Hard commands ──────────────────────────────────────────────────────

        if cmd in ("exit", "quit", "bye"):
            if isinstance(mem, dict):
                await call(session, "save_memory", {"data": mem})
            git_push()
            print("  Saved.")
            break

        if cmd == "forget":
            await call(session, "save_memory", {"data": {}})
            mem = await call(session, "load_memory")
            history = []
            _CACHE.clear()
            print("  Memory cleared.\n")
            continue

        if cmd in ("briefing", "today", "market"):
            snap, heads = await asyncio.gather(
                call(session, "fetch_market_snapshot"),
                call(session, "fetch_top_headlines"),
            )
            show_snapshot(snap.get("data", []), source=snap.get("source", ""))
            show_headlines(heads.get("headlines", []))
            continue

        if cmd == "movers":
            m = await call(session, "fetch_top_movers")
            show_movers(m.get("gainers", []), m.get("losers", []))
            continue

        if cmd == "sectors":
            s = await call(session, "fetch_sector_performance")
            show_sectors(s.get("sectors", []))
            continue

        if cmd == "picks":
            picks = await call(session, "get_weekly_picks")
            if not picks.get("picks"):
                print(_d("  generating picks…"))
                picks = await call(session, "generate_weekly_picks")
            show_picks(picks.get("picks", []), picks.get("week", ""))
            continue

        if cmd in ("memory", "me", "profile"):
            mem = await call(session, "load_memory")
            if isinstance(mem, dict):
                show_memory(mem)
            continue

        if cmd == "learning":
            mem = await call(session, "load_memory")
            if not isinstance(mem, dict):
                print("  No memory found.\n")
                continue
            history  = mem.get("briefing_history", [])
            accuracy = mem.get("prediction_accuracy", [])
            freq     = mem.get("theme_frequency", {})
            cal_note = mem.get("calibration_note", "")

            if history:
                dates = [e["date"] for e in history if "date" in e]
                first, last = (dates[0], dates[-1]) if dates else ("n/a", "n/a")
                print(f"\n  Tracked {len(history)} briefings ({first} → {last}).")
            else:
                print("\n  No briefings tracked yet.")

            if accuracy:
                correct = sum(1 for r in accuracy if r.get("accurate"))
                total   = len(accuracy)
                pct     = int(100 * correct / total)
                mixed_total   = sum(1 for r in accuracy if r.get("called") == "mixed")
                mixed_correct = sum(1 for r in accuracy if r.get("called") == "mixed" and r.get("accurate"))
                mixed_note = ""
                if mixed_total > 0 and int(100 * mixed_correct / mixed_total) < 50:
                    mixed_note = f" — mixed-direction calls have been least reliable ({int(100 * mixed_correct / mixed_total)}%)"
                print(f"  Prediction accuracy: {correct}/{total} ({pct}%){mixed_note}.")
            else:
                print("  No prediction accuracy data yet.")

            if freq:
                top = max(freq, key=lambda k: freq[k])
                window = history[-10:] if len(history) >= 10 else history
                recent_count = sum(
                    1 for e in window
                    if (e.get("headline_theme") or e.get("theme")) == top
                )
                print(f"  Top recurring theme: {top} (mentioned {freq[top]}x total, {recent_count}x in last {len(window)} briefings).")
            else:
                print("  No theme data yet.")

            print(f"  Calibration note: {cal_note if cal_note else 'none flagged.'}")
            print()
            continue

        if cmd in ("watchlist", "watch"):
            mem     = await call(session, "load_memory")
            flagged = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
            if not flagged:
                print("  Watchlist empty — mention a ticker 3+ times to auto-add it.\n")
                continue
            for t in flagged:
                show_stock(await call(session, "fetch_stock_data", {"ticker": t}))
            continue

        # Pure ticker shortcut — entire input is ticker symbols
        tokens  = re.findall(r'\b[A-Z]{2,5}\b', user)
        tickers = [t for t in tokens if t not in SKIP_WORDS]
        words   = [w for w in user.split() if len(w) >= 2]
        all_upper = words and all(w.upper() in tickers for w in words)
        if tickers and all_upper:
            for sym in tickers[:2]:
                show_stock(await call(session, "fetch_stock_data", {"ticker": sym}))
                if isinstance(mem, dict):
                    mem.setdefault("mentioned_stocks", {})
                    mem["mentioned_stocks"][sym] = mem["mentioned_stocks"].get(sym, 0) + 1
                    if (mem["mentioned_stocks"][sym] >= 3
                            and sym not in mem.get("flagged_tickers", [])):
                        mem.setdefault("flagged_tickers", []).append(sym)
                        print(_d(f"  {sym} added to your watchlist."))
            continue

        # ── Everything else → Claude with full market context ──────────────────

        print(_d("  thinking…"), end="\r", flush=True)
        market_ctx = await build_market_context(
            session, user, mem if isinstance(mem, dict) else {}
        )

        history_block = ""
        if history:
            history_block = "\nConversation so far this session:\n"
            for turn in history[-6:]:
                history_block += f"You: {turn['user']}\nPippy: {turn['pippy']}\n"

        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Live market data:\n{market_ctx}\n"
            f"{history_block}\n"
            f"You: {user}\nPippy:"
        )

        reply = ask_claude(prompt)
        print(" " * 24, end="\r")
        print(f"\n  {_c('Pippy:')} {reply}\n")

        history.append({"user": user, "pippy": reply})

        # Track tickers in memory
        if isinstance(mem, dict):
            for sym in tickers[:3]:
                mem.setdefault("mentioned_stocks", {})
                mem["mentioned_stocks"][sym] = mem["mentioned_stocks"].get(sym, 0) + 1
                if (mem["mentioned_stocks"][sym] >= 3
                        and sym not in mem.get("flagged_tickers", [])):
                    mem.setdefault("flagged_tickers", []).append(sym)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")],
        env=dict(os.environ),
    )
    # Redirect MCP server's stderr to /dev/null so INFO logs never hit the terminal
    devnull = open(os.devnull, "w")
    async with stdio_client(server_params, errlog=devnull) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await repl(session)


if __name__ == "__main__":
    asyncio.run(main())
