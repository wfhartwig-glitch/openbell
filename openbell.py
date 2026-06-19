#!/usr/bin/env python3
"""
OpenBell — autonomous email agent. Zero Anthropic API cost.
Runs as an MCP client, calls pippy_mcp.py tools for data, builds HTML, sends email.

Usage:
  python openbell.py morning    → Morning Briefing (weekdays only)
  python openbell.py close      → Market Close Summary (weekdays only)
  python openbell.py deepdive   → Weekend/Holiday Summary (skips if market open)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

load_dotenv()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

STYLE = """
body{font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#0a0a0a;color:#e0e0e0}
.header{background:#1a1a2e;padding:20px;border-bottom:2px solid #00d4aa}
.header h1{color:#00d4aa;margin:0;font-size:22px;letter-spacing:.5px}
.header p{color:#888;margin:4px 0 0;font-size:13px}
.section{padding:16px 20px;border-bottom:1px solid #1e1e1e}
.section h2{color:#00d4aa;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin:0 0 12px}
table{border-collapse:collapse;width:100%}
td{padding:5px 14px 5px 0;font-size:14px;vertical-align:top}
.green{color:#00d4aa;font-weight:700}
.red{color:#ff4757;font-weight:700}
.neutral{color:#888}
.ticker{background:#1a1a2e;padding:3px 7px;border-radius:4px;font-family:monospace;font-size:13px}
.tag-high{color:#ff4757;font-size:11px;font-weight:700;text-transform:uppercase}
.tag-med{color:#ffa502;font-size:11px;font-weight:700;text-transform:uppercase}
.tag-earn{color:#7c3aed;font-size:11px;font-weight:700;text-transform:uppercase}
.tag-risk-low{color:#00d4aa;font-size:11px;font-weight:700}
.tag-risk-med{color:#ffa502;font-size:11px;font-weight:700}
.tag-risk-high{color:#ff4757;font-size:11px;font-weight:700}
.tag-risk-spec{color:#7c3aed;font-size:11px;font-weight:700}
.hl-item{padding:7px 0;border-bottom:1px solid #1e1e1e;font-size:14px}
.hl-item:last-child{border-bottom:none}
.hl-snippet{color:#888;font-size:12px;margin-top:2px}
.hl-meta{color:#555;font-size:11px;margin-top:1px}
.footer{padding:14px 20px;color:#444;font-size:11px}
"""


# ── MCP helpers ───────────────────────────────────────────────────────────────

async def call(session: ClientSession, name: str, args: dict = None) -> dict | list | str:
    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except Exception:
        return text


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _cls(pct) -> str:
    try:
        return "green" if float(pct) >= 0 else "red"
    except Exception:
        return "neutral"


def _arrow(pct) -> str:
    try:
        return "▲" if float(pct) >= 0 else "▼"
    except Exception:
        return "—"


def _fmt(pct) -> str:
    try:
        v = float(pct)
        return f'{_arrow(v)} {abs(v):.2f}%'
    except Exception:
        return str(pct) if pct else "N/A"


def _base(title: str, subtitle: str, body: str) -> str:
    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>{STYLE}</style></head>'
        f'<body>'
        f'<div class="header"><h1>{title}</h1><p>{subtitle}</p></div>'
        f'{body}'
        f'<div class="footer">OpenBell &mdash; automated market briefing &nbsp;&nbsp;Not financial advice.</div>'
        f'</body></html>'
    )


def _sec(title: str, inner: str) -> str:
    return f'<div class="section"><h2>{title}</h2>{inner}</div>'


# ── Section builders ──────────────────────────────────────────────────────────

def _indices(data: list) -> str:
    rows = ""
    for item in data:
        name  = item.get("name", "")
        price = item.get("price")
        pct   = item.get("pct") or item.get("changesPercentage")
        p_str = f"${float(price):,.2f}" if price else "—"
        rows += (
            f'<tr>'
            f'<td style="font-weight:600;width:90px">{name}</td>'
            f'<td>{p_str}</td>'
            f'<td class="{_cls(pct)}">{_fmt(pct)}</td>'
            f'</tr>'
        )
    return _sec("Market Snapshot", f'<table>{rows}</table>')


def _headlines(headlines: list) -> str:
    items = ""
    for h in headlines:
        title   = h.get("title", h) if isinstance(h, dict) else str(h)
        snippet = h.get("snippet", "") if isinstance(h, dict) else ""
        site    = h.get("site", "") if isinstance(h, dict) else ""
        items += (
            f'<div class="hl-item"><div>{title}</div>'
            + (f'<div class="hl-snippet">{snippet}</div>' if snippet else "")
            + (f'<div class="hl-meta">{site}</div>'       if site    else "")
            + '</div>'
        )
    return _sec("Top Headlines", items)


def _calendar(events: list, earnings: list) -> str:
    rows = ""
    for e in events[:8]:
        evt    = e.get("event", "")
        dt     = (e.get("date", "") or "")[-5:]
        impact = e.get("impact", "")
        tag    = (f'<span class="tag-high">{impact}</span>' if impact == "High"
                  else f'<span class="tag-med">{impact}</span>' if impact else "")
        rows += f'<tr><td class="neutral" style="width:55px">{dt}</td><td>{evt}</td><td style="width:70px">{tag}</td></tr>'
    for e in earnings[:6]:
        sym  = e.get("symbol", "")
        dt   = (e.get("date", "") or "")[-5:]
        eps  = e.get("eps_estimated")
        note = f"EPS est. ${eps:.2f}" if eps else "earnings"
        rows += (
            f'<tr><td class="neutral" style="width:55px">{dt}</td>'
            f'<td><span class="ticker">{sym}</span> {note}</td>'
            f'<td style="width:70px"><span class="tag-earn">EARN</span></td></tr>'
        )
    if not rows:
        return _sec("This Week's Calendar",
                    '<p class="neutral" style="font-size:13px">No major events found.</p>')
    return _sec("This Week's Calendar", f'<table>{rows}</table>')


def _sectors(sectors: list) -> str:
    rows = ""
    for s in sectors:
        name = s.get("sector", "")
        pct  = s.get("pct") or s.get("changesPercentage")
        rows += f'<tr><td style="font-size:13px">{name}</td><td class="{_cls(pct)}">{_fmt(pct)}</td></tr>'
    return _sec("Sector Performance", f'<table>{rows}</table>')


def _movers(gainers: list, losers: list) -> str:
    def _block(items, label, css_cls):
        rows = ""
        for m in items:
            sym   = m.get("symbol", "")
            name  = (m.get("name") or "")[:22]
            price = m.get("price")
            pct   = m.get("pct") or m.get("changesPercentage")
            p_str = f"${float(price):,.2f}" if price else ""
            rows += (
                f'<tr>'
                f'<td><span class="ticker">{sym}</span></td>'
                f'<td class="neutral" style="font-size:12px">{name}</td>'
                f'<td>{p_str}</td>'
                f'<td class="{css_cls}">{_fmt(pct)}</td>'
                f'</tr>'
            )
        return (
            f'<div style="margin-bottom:12px">'
            f'<div class="{css_cls}" style="font-size:11px;font-weight:700;text-transform:uppercase;margin-bottom:6px">{label}</div>'
            f'<table>{rows}</table></div>'
        )
    return _sec("Top Movers", _block(gainers, "Gainers", "green") + _block(losers, "Losers", "red"))


def _watchlist(tickers_data: list, label: str) -> str:
    if not tickers_data:
        return ""
    rows = ""
    for w in tickers_data:
        sym   = w.get("ticker", "")
        price = w.get("price")
        pct   = w.get("pct") or w.get("changesPercentage")
        head  = w.get("headline", "")
        p_str = f"${float(price):,.2f}" if price else "—"
        rows += (
            f'<tr>'
            f'<td style="width:70px"><span class="ticker">{sym}</span></td>'
            f'<td>{p_str}</td>'
            f'<td class="{_cls(pct)}" style="width:80px">{_fmt(pct)}</td>'
            + (f'<td class="neutral" style="font-size:12px">{head[:55]}…</td>' if head else '<td></td>')
            + '</tr>'
        )
    return _sec(label, f'<table>{rows}</table>')


def _picks(picks: list) -> str:
    if not picks:
        return ""
    risk_cls = {"Low": "tag-risk-low", "Medium": "tag-risk-med",
                "High": "tag-risk-high", "Speculative": "tag-risk-spec"}
    rows = (
        '<tr style="border-bottom:1px solid #222;font-size:11px;color:#555">'
        '<td>TICKER</td><td>COMPANY</td><td>SECTOR</td><td>RISK</td><td>RATIONALE</td></tr>'
    )
    for p in picks:
        risk = p.get("risk", "—")
        cls  = risk_cls.get(risk, "neutral")
        rows += (
            f'<tr style="border-bottom:1px solid #1a1a1a">'
            f'<td><span class="ticker">{p["ticker"]}</span></td>'
            f'<td style="font-size:13px">{p.get("company","")}</td>'
            f'<td class="neutral" style="font-size:12px">{p.get("sector","")}</td>'
            f'<td><span class="{cls}">{risk}</span></td>'
            f'<td style="font-size:12px;color:#aaa">{p.get("rationale","")}</td>'
            f'</tr>'
        )
    return _sec("Monthly Picks",
        f'<table>{rows}</table>'
        f'<div style="font-size:11px;color:#444;margin-top:8px">Refreshed monthly. Informational only.</div>')


# ── Email assemblers ──────────────────────────────────────────────────────────

async def morning(session: ClientSession) -> tuple[str, str]:
    today = date.today().strftime("%A, %B %d")
    print("    fetching snapshot…")
    snapshot  = await call(session, "fetch_market_snapshot")
    print("    fetching headlines…")
    headlines = await call(session, "fetch_top_headlines")
    print("    fetching calendar…")
    econ      = await call(session, "fetch_economic_calendar")
    earnings  = await call(session, "fetch_earnings_calendar")
    print("    loading memory…")
    mem       = await call(session, "load_memory")
    print("    loading picks…")
    picks     = await call(session, "get_monthly_picks")
    if not picks.get("picks"):
        print("    generating picks…")
        picks = await call(session, "generate_monthly_picks")

    flagged   = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
    watchlist = []
    for t in flagged[:5]:
        print(f"    pre-market {t}…")
        watchlist.append(await call(session, "fetch_premarket_data", {"ticker": t}))

    body = (
        _indices(snapshot.get("data", []))
        + _headlines(headlines.get("headlines", []))
        + _calendar(econ.get("events", []), earnings.get("earnings", []))
        + _watchlist(watchlist, "Your Watchlist — Pre-Market")
        + _picks(picks.get("picks", []))
    )
    return (f"OpenBell ☀️ — {today} Morning Briefing",
            _base(f"OpenBell ☀️  {today}", "Morning Briefing", body))


async def close(session: ClientSession) -> tuple[str, str]:
    today = date.today().strftime("%A, %B %d")
    print("    fetching snapshot…")
    snapshot  = await call(session, "fetch_market_snapshot")
    print("    fetching sectors…")
    sectors   = await call(session, "fetch_sector_performance")
    print("    fetching movers…")
    movers    = await call(session, "fetch_top_movers")
    print("    fetching headlines…")
    headlines = await call(session, "fetch_top_headlines")
    print("    loading memory…")
    mem       = await call(session, "load_memory")

    flagged   = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
    watchlist = []
    for t in flagged[:5]:
        print(f"    EOD {t}…")
        watchlist.append(await call(session, "fetch_stock_data", {"ticker": t}))

    body = (
        _indices(snapshot.get("data", []))
        + _movers(movers.get("gainers", []), movers.get("losers", []))
        + _sectors(sectors.get("sectors", []))
        + _watchlist(watchlist, "Your Watchlist — EOD")
        + _headlines(headlines.get("headlines", []))
    )
    return (f"OpenBell 📊 — {today} Market Close",
            _base(f"OpenBell 📊  {today}", "Market Close Summary", body))


async def deepdive(session: ClientSession) -> tuple[str, str]:
    today = date.today().strftime("%A, %B %d")
    print("    fetching data…")
    snapshot  = await call(session, "fetch_market_snapshot")
    headlines = await call(session, "fetch_top_headlines")
    movers    = await call(session, "fetch_top_movers")
    sectors   = await call(session, "fetch_sector_performance")
    earnings  = await call(session, "fetch_earnings_calendar")

    upcoming = earnings.get("earnings", [])
    earn_html = ""
    if upcoming:
        rows = "".join(
            f'<tr>'
            f'<td><span class="ticker">{e.get("symbol","")}</span></td>'
            f'<td class="neutral" style="font-size:13px">{e.get("date","")}</td>'
            f'<td class="neutral" style="font-size:12px">'
            + (f'EPS est. ${e["eps_estimated"]:.2f}' if e.get("eps_estimated") else "")
            + '</td></tr>'
            for e in upcoming[:8]
        )
        earn_html = _sec("Earnings This Week", f'<table>{rows}</table>')

    body = (
        _indices(snapshot.get("data", []))
        + _headlines(headlines.get("headlines", []))
        + _movers(movers.get("gainers", []), movers.get("losers", []))
        + _sectors(sectors.get("sectors", []))
        + earn_html
    )
    return (f"OpenBell 📚 — {today} Weekend Summary",
            _base(f"OpenBell 📚  {today}", "Weekend Market Summary", body))


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(mode: str):
    today_str = date.today().strftime("%A, %B %d, %Y")
    start_ts  = datetime.now().strftime("%H:%M:%S UTC+0")
    print(f"[OpenBell] {mode.upper()} — {today_str}")
    print(f"[OpenBell] started at {start_ts}")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")],
        env=dict(os.environ),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            market = await call(session, "is_market_open_today")

            if mode == "deepdive":
                if market.get("open"):
                    print("[OpenBell] Market is open — skipping deep dive.")
                    return
                subject, html = await deepdive(session)

            elif mode == "morning":
                subject, html = await morning(session)

            elif mode == "close":
                subject, html = await close(session)

            else:
                print(f"[OpenBell] Unknown mode: {mode}")
                return

            send_ts = datetime.now().strftime("%H:%M:%S UTC+0")
            print(f"  → Sending… (pre-send time: {send_ts})")
            result = await call(session, "send_email",
                                {"subject": subject, "html_body": html})
            print(f"  {result}")

            mem = await call(session, "load_memory")
            if isinstance(mem, dict):
                mem["last_email_sent"]    = datetime.now().isoformat()
                mem["last_email_summary"] = f"{mode} sent {today_str}"
                mem["email_count"]        = mem.get("email_count", 0) + 1
                await call(session, "save_memory", {"data": mem})

            print("[OpenBell] Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["morning", "close", "deepdive"])
    args = parser.parse_args()
    asyncio.run(run(args.mode))


if __name__ == "__main__":
    main()
