#!/usr/bin/env python3
"""
Pippy's Brief — autonomous email agent. Zero Anthropic API cost.
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


# ── MCP helper ────────────────────────────────────────────────────────────────

async def call(session: ClientSession, name: str, args: dict = None) -> dict | list | str:
    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except Exception:
        return text


# ── Inline-style HTML helpers (Gmail strips <style> tags) ────────────────────

GREEN  = "#16a34a"
RED    = "#dc2626"
GRAY   = "#6b7280"
BORDER = "#e5e7eb"
BG     = "#ffffff"
HEADER = "#111827"
ACCENT = "#111827"


def _pct_color(pct) -> str:
    try:
        return GREEN if float(pct) >= 0 else RED
    except Exception:
        return GRAY


def _arrow(pct) -> str:
    try:
        return "▲" if float(pct) >= 0 else "▼"
    except Exception:
        return "—"


def _fmt(pct) -> str:
    try:
        v = float(pct)
        return f"{_arrow(v)} {abs(v):.2f}%"
    except Exception:
        return str(pct) if pct else "—"


def _wrap(body: str, title: str, subtitle: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">

  <!-- HEADER -->
  <tr><td style="background:#111827;padding:28px 32px">
    <p style="margin:0 0 4px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af">{subtitle}</p>
    <h1 style="margin:0;font-size:22px;font-weight:700;color:#ffffff;line-height:1.2">{title}</h1>
  </td></tr>

  <!-- BODY -->
  {body}

  <!-- FOOTER -->
  <tr><td style="padding:20px 32px;border-top:1px solid #e5e7eb;background:#f9fafb">
    <p style="margin:0;font-size:11px;color:#9ca3af">Pippy's Brief &mdash; automated daily market briefing. Not financial advice.</p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _section(label: str, inner: str) -> str:
    return f"""<tr><td style="padding:24px 32px;border-bottom:1px solid #e5e7eb">
  <p style="margin:0 0 14px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">{label}</p>
  {inner}
</td></tr>"""


# ── Section builders — all inline styles ──────────────────────────────────────

def _indices(data: list) -> str:
    rows = ""
    for item in data:
        name  = item.get("name", "")
        price = item.get("price")
        pct   = item.get("pct") or item.get("changesPercentage")
        p_str = f"${float(price):,.2f}" if price else "—"
        color = _pct_color(pct)
        rows += f"""
        <tr>
          <td style="padding:8px 0;font-size:15px;font-weight:600;color:#111827;width:100px">{name}</td>
          <td style="padding:8px 0;font-size:15px;color:#374151">{p_str}</td>
          <td style="padding:8px 0;font-size:15px;font-weight:700;color:{color};text-align:right">{_fmt(pct)}</td>
        </tr>"""
    return _section("Market Snapshot", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _headlines(headlines: list) -> str:
    items = ""
    for i, h in enumerate(headlines):
        title   = h.get("title", h) if isinstance(h, dict) else str(h)
        snippet = h.get("snippet", "") if isinstance(h, dict) else ""
        site    = h.get("site", "") if isinstance(h, dict) else ""
        border  = "border-top:1px solid #f3f4f6;" if i > 0 else ""
        items += f"""
        <div style="{border}padding:10px 0">
          <p style="margin:0 0 3px;font-size:14px;font-weight:500;color:#111827;line-height:1.4">{title}</p>
          {"" if not snippet else f'<p style="margin:0 0 2px;font-size:12px;color:#6b7280;line-height:1.4">{snippet}</p>'}
          {"" if not site    else f'<p style="margin:0;font-size:11px;color:#9ca3af">{site}</p>'}
        </div>"""
    return _section("Top Headlines", items)


def _calendar(events: list, earnings: list) -> str:
    rows = ""
    for e in events[:8]:
        evt    = e.get("event", "")
        dt     = (e.get("date", "") or "")[-5:]
        impact = e.get("impact", "")
        impact_color = RED if impact == "High" else "#d97706" if impact == "Medium" else GRAY
        badge = f'<span style="font-size:10px;font-weight:700;color:{impact_color};text-transform:uppercase">{impact}</span>' if impact else ""
        rows += f"""
        <tr>
          <td style="padding:7px 12px 7px 0;font-size:12px;color:#6b7280;white-space:nowrap;width:50px">{dt}</td>
          <td style="padding:7px 12px 7px 0;font-size:13px;color:#374151">{evt}</td>
          <td style="padding:7px 0;text-align:right">{badge}</td>
        </tr>"""
    for e in earnings[:6]:
        sym  = e.get("symbol", "")
        dt   = (e.get("date", "") or "")[-5:]
        eps  = e.get("eps_estimated")
        note = f"EPS est. ${eps:.2f}" if eps else "reports earnings"
        rows += f"""
        <tr>
          <td style="padding:7px 12px 7px 0;font-size:12px;color:#6b7280;white-space:nowrap;width:50px">{dt}</td>
          <td style="padding:7px 12px 7px 0;font-size:13px;color:#374151"><strong style="color:#111827">{sym}</strong> — {note}</td>
          <td style="padding:7px 0;text-align:right"><span style="font-size:10px;font-weight:700;color:#7c3aed;text-transform:uppercase">Earnings</span></td>
        </tr>"""
    if not rows:
        return _section("This Week's Calendar", '<p style="margin:0;font-size:13px;color:#9ca3af">No major events found.</p>')
    return _section("This Week's Calendar", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _sectors(sectors: list) -> str:
    rows = ""
    for s in sectors:
        name = s.get("sector", "")
        pct  = s.get("pct") or s.get("changesPercentage")
        color = _pct_color(pct)
        rows += f"""
        <tr>
          <td style="padding:6px 0;font-size:13px;color:#374151">{name}</td>
          <td style="padding:6px 0;font-size:13px;font-weight:700;color:{color};text-align:right">{_fmt(pct)}</td>
        </tr>"""
    return _section("Sector Performance", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _movers(gainers: list, losers: list) -> str:
    def _block(items, label, color):
        rows = ""
        for m in items:
            sym   = m.get("symbol", "")
            name  = (m.get("name") or "")[:28]
            price = m.get("price")
            pct   = m.get("pct") or m.get("changesPercentage")
            p_str = f"${float(price):,.2f}" if price else "—"
            rows += f"""
            <tr>
              <td style="padding:7px 12px 7px 0;font-size:13px;font-weight:700;color:#111827;width:60px">{sym}</td>
              <td style="padding:7px 12px 7px 0;font-size:12px;color:#6b7280">{name}</td>
              <td style="padding:7px 12px 7px 0;font-size:13px;color:#374151">{p_str}</td>
              <td style="padding:7px 0;font-size:13px;font-weight:700;color:{color};text-align:right">{_fmt(pct)}</td>
            </tr>"""
        return f"""
        <div style="margin-bottom:16px">
          <p style="margin:0 0 8px;font-size:11px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:.06em">{label}</p>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </div>"""
    return _section("Top Movers", _block(gainers, "Gainers", GREEN) + _block(losers, "Losers", RED))


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
        color = _pct_color(pct)
        rows += f"""
        <tr>
          <td style="padding:8px 12px 8px 0;font-size:13px;font-weight:700;color:#111827;width:65px">{sym}</td>
          <td style="padding:8px 12px 8px 0;font-size:13px;color:#374151">{p_str}</td>
          <td style="padding:8px 12px 8px 0;font-size:13px;font-weight:700;color:{color};width:85px">{_fmt(pct)}</td>
          <td style="padding:8px 0;font-size:12px;color:#6b7280">{"" if not head else head[:60] + "…"}</td>
        </tr>"""
    return _section(label, f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _picks(picks: list) -> str:
    if not picks:
        return ""
    risk_colors = {"Low": GREEN, "Medium": "#d97706", "High": RED, "Speculative": "#7c3aed"}
    rows = ""
    for p in picks:
        risk  = p.get("risk", "—")
        rcolor = risk_colors.get(risk, GRAY)
        rows += f"""
        <tr style="border-top:1px solid #f3f4f6">
          <td style="padding:10px 12px 10px 0;font-size:13px;font-weight:700;color:#111827;width:60px">{p.get("ticker","")}</td>
          <td style="padding:10px 12px 10px 0;font-size:13px;color:#374151">{p.get("company","")}</td>
          <td style="padding:10px 12px 10px 0;font-size:12px;color:#6b7280">{p.get("sector","")}</td>
          <td style="padding:10px 12px 10px 0;font-size:11px;font-weight:700;color:{rcolor};text-transform:uppercase;white-space:nowrap">{risk}</td>
          <td style="padding:10px 0;font-size:12px;color:#6b7280">{p.get("rationale","")}</td>
        </tr>"""
    header = """<tr>
      <td style="padding:0 12px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Ticker</td>
      <td style="padding:0 12px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Company</td>
      <td style="padding:0 12px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Sector</td>
      <td style="padding:0 12px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Risk</td>
      <td style="padding:0 0 8px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Rationale</td>
    </tr>"""
    return _section("Monthly Picks",
        f'<table width="100%" cellpadding="0" cellspacing="0">{header}{rows}</table>'
        f'<p style="margin:10px 0 0;font-size:11px;color:#9ca3af">Refreshed monthly. Informational only.</p>')


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
    subject = f"Pippy's Brief — {today} Morning Briefing"
    html    = _wrap(body, f"Morning Briefing &nbsp; {today}", "Pippy's Brief ☀️")
    return subject, html


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
        + _watchlist(watchlist, "Your Watchlist — End of Day")
        + _headlines(headlines.get("headlines", []))
    )
    subject = f"Pippy's Brief — {today} Market Close"
    html    = _wrap(body, f"Market Close &nbsp; {today}", "Pippy's Brief 📊")
    return subject, html


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
            f"""<tr>
              <td style="padding:7px 12px 7px 0;font-size:13px;font-weight:700;color:#111827;width:65px">{e.get("symbol","")}</td>
              <td style="padding:7px 12px 7px 0;font-size:13px;color:#374151">{e.get("date","")}</td>
              <td style="padding:7px 0;font-size:12px;color:#6b7280">{"EPS est. $" + f'{e["eps_estimated"]:.2f}' if e.get("eps_estimated") else ""}</td>
            </tr>"""
            for e in upcoming[:8]
        )
        earn_html = _section("Earnings This Week",
            f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')

    body = (
        _indices(snapshot.get("data", []))
        + _headlines(headlines.get("headlines", []))
        + _movers(movers.get("gainers", []), movers.get("losers", []))
        + _sectors(sectors.get("sectors", []))
        + earn_html
    )
    subject = f"Pippy's Brief — {today} Weekend Summary"
    html    = _wrap(body, f"Weekend Summary &nbsp; {today}", "Pippy's Brief 📚")
    return subject, html


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(mode: str):
    today_str = date.today().strftime("%A, %B %d, %Y")
    start_ts  = datetime.now().strftime("%H:%M:%S UTC")
    print(f"[Pippy's Brief] {mode.upper()} — {today_str}")
    print(f"[Pippy's Brief] started at {start_ts}")

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
                    print("[Pippy's Brief] Market is open — skipping deep dive.")
                    return
                subject, html = await deepdive(session)

            elif mode == "morning":
                subject, html = await morning(session)

            elif mode == "close":
                subject, html = await close(session)

            else:
                print(f"[Pippy's Brief] Unknown mode: {mode}")
                return

            send_ts = datetime.now().strftime("%H:%M:%S UTC")
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

            print("[Pippy's Brief] Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["morning", "close", "deepdive"])
    args = parser.parse_args()
    asyncio.run(run(args.mode))


if __name__ == "__main__":
    main()
