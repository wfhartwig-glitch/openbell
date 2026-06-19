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
import subprocess
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


def _picks(picks: list, week: str = "", changes: list = None) -> str:
    if not picks:
        return ""
    risk_colors = {"Low": GREEN, "Medium": "#d97706", "High": RED, "Speculative": "#7c3aed"}
    rows = ""
    for p in picks:
        risk   = p.get("risk_level") or p.get("risk", "—")
        rcolor = risk_colors.get(risk, GRAY)
        status = p.get("status", "")
        status_color = GREEN if status == "holding" else "#7c3aed" if status == "new" else GRAY
        weeks_held = p.get("weeks_held", 1)
        pct_since  = p.get("pct_change_this_week")
        pct_str    = ""
        if pct_since is not None:
            c = GREEN if pct_since >= 0 else RED
            pct_str = f'<span style="color:{c};font-weight:700">{"▲" if pct_since >= 0 else "▼"} {abs(pct_since):.1f}%</span>'
        note = p.get("note") or p.get("rationale", "")
        rows += f"""
        <tr style="border-top:1px solid #f3f4f6">
          <td style="padding:10px 10px 10px 0;font-size:13px;font-weight:700;color:#111827;width:55px">{p.get("ticker","")}</td>
          <td style="padding:10px 10px 10px 0;font-size:12px;color:#374151">{p.get("company","")}</td>
          <td style="padding:10px 10px 10px 0;font-size:11px;color:#6b7280">{p.get("sector","")}</td>
          <td style="padding:10px 10px 10px 0;font-size:10px;font-weight:700;color:{rcolor};text-transform:uppercase;white-space:nowrap">{risk}</td>
          <td style="padding:10px 10px 10px 0;font-size:11px;color:{status_color};white-space:nowrap">{status} · {weeks_held}w {pct_str}</td>
          <td style="padding:10px 0;font-size:11px;color:#6b7280">{note}</td>
        </tr>"""
    header = """<tr>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Ticker</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Company</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Sector</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Risk</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Status</td>
      <td style="padding:0 0 8px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Note</td>
    </tr>"""
    changes_html = ""
    if changes:
        items = "".join(f'<li style="margin:3px 0;font-size:12px;color:#6b7280">{c}</li>' for c in changes)
        changes_html = f'<p style="margin:12px 0 4px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Changes This Week</p><ul style="margin:0;padding-left:18px">{items}</ul>'
    week_label = f" — {week}" if week else ""
    return _section(f"Weekly Picks{week_label}",
        f'<table width="100%" cellpadding="0" cellspacing="0">{header}{rows}</table>'
        + changes_html
        + '<p style="margin:10px 0 0;font-size:11px;color:#9ca3af">Updated every Monday. Kept when thesis holds, replaced when conditions shift. Not financial advice.</p>')


def _picks_performance(perf_history: list, lessons: list) -> str:
    """Monday-only section: track record of current and recently dropped picks."""
    held    = [p for p in perf_history if p.get("still_held")]
    dropped = [p for p in perf_history if not p.get("still_held") and p.get("week_dropped")]
    # Only show picks dropped in last 4 weeks
    dropped = sorted(dropped, key=lambda x: x.get("week_dropped",""), reverse=True)[:4]

    if not held and not dropped:
        return ""

    rows = ""
    for p in sorted(held, key=lambda x: x.get("pct_change_since_pick") or 0, reverse=True):
        pct   = p.get("pct_change_since_pick")
        color = GREEN if (pct or 0) >= 0 else RED
        pct_s = f'{"▲" if (pct or 0) >= 0 else "▼"} {abs(pct):.1f}%' if pct is not None else "—"
        rows += f"""
        <tr style="border-top:1px solid #f3f4f6">
          <td style="padding:7px 10px 7px 0;font-size:13px;font-weight:700;color:#111827;width:60px">{p.get("ticker","")}</td>
          <td style="padding:7px 10px 7px 0;font-size:12px;color:#6b7280">{p.get("sector","")}</td>
          <td style="padding:7px 10px 7px 0;font-size:12px;color:#374151">since {p.get("week_picked","?")}</td>
          <td style="padding:7px 0;font-size:13px;font-weight:700;color:{color}">{pct_s}</td>
        </tr>"""

    if dropped:
        rows += f'<tr><td colspan="4" style="padding:10px 0 4px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Recently Dropped</td></tr>'
        for p in dropped:
            pct   = p.get("pct_change_since_pick")
            color = GREEN if (pct or 0) >= 0 else RED
            pct_s = f'{"▲" if (pct or 0) >= 0 else "▼"} {abs(pct):.1f}%' if pct is not None else "—"
            rows += f"""
            <tr style="border-top:1px solid #f3f4f6">
              <td style="padding:7px 10px 7px 0;font-size:13px;font-weight:700;color:#9ca3af;width:60px">{p.get("ticker","")}</td>
              <td style="padding:7px 10px 7px 0;font-size:12px;color:#9ca3af">{p.get("sector","")}</td>
              <td style="padding:7px 10px 7px 0;font-size:12px;color:#9ca3af">dropped {p.get("week_dropped","?")}</td>
              <td style="padding:7px 0;font-size:13px;font-weight:700;color:{color}">{pct_s}</td>
            </tr>"""

    lesson_html = ""
    if lessons:
        last_lesson = lessons[-1].get("note", "")
        if last_lesson:
            lesson_html = f'<p style="margin:12px 0 0;font-size:12px;color:#374151;font-style:italic">{last_lesson}</p>'

    header = """<tr>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Ticker</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Sector</td>
      <td style="padding:0 10px 8px 0;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Held Since</td>
      <td style="padding:0 0 8px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase">Return</td>
    </tr>"""
    return _section("Pick Performance",
        f'<table width="100%" cellpadding="0" cellspacing="0">{header}{rows}</table>'
        + lesson_html)


# ── Email assemblers ──────────────────────────────────────────────────────────

async def morning(session: ClientSession) -> tuple[str, str]:
    today     = date.today()
    today_str = today.strftime("%A, %B %d")
    is_monday = today.weekday() == 0

    print("    fetching snapshot…")
    snapshot  = await call(session, "fetch_market_snapshot")
    print("    fetching headlines…")
    headlines = await call(session, "fetch_top_headlines")
    print("    fetching calendar…")
    econ      = await call(session, "fetch_economic_calendar")
    earnings  = await call(session, "fetch_earnings_calendar")
    print("    loading memory…")
    mem       = await call(session, "load_memory")

    # Weekly picks — regenerate every Monday
    if is_monday:
        print("    Monday: generating weekly picks…")
        picks_data = await call(session, "generate_weekly_picks")
    else:
        print("    loading weekly picks…")
        picks_data = await call(session, "get_weekly_picks")
        if not picks_data.get("picks"):
            print("    no picks cached — generating…")
            picks_data = await call(session, "generate_weekly_picks")

    flagged   = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
    watchlist = []
    for t in flagged[:5]:
        print(f"    pre-market {t}…")
        watchlist.append(await call(session, "fetch_premarket_data", {"ticker": t}))

    perf_section = ""
    if is_monday:
        perf_history = mem.get("pick_performance_history", []) if isinstance(mem, dict) else []
        lessons      = mem.get("lessons_learned", [])          if isinstance(mem, dict) else []
        perf_section = _picks_performance(perf_history, lessons)

    body = (
        _indices(snapshot.get("data", []))
        + _headlines(headlines.get("headlines", []))
        + _calendar(econ.get("events", []), earnings.get("earnings", []))
        + _watchlist(watchlist, "Your Watchlist — Pre-Market")
        + perf_section
        + _picks(picks_data.get("picks", []),
                 week=picks_data.get("week", ""),
                 changes=picks_data.get("changes_from_last_week", []))
    )
    subject = f"Pippy's Brief — {today_str} Morning Briefing"
    html    = _wrap(body, f"Morning Briefing &nbsp; {today_str}", "Pippy's Brief ☀️")
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


DEEPDIVE_CATEGORIES = [
    "private company", "sector rotation", "macro / Fed policy",
    "housing market", "emerging technology", "IPO / public markets",
    "consumer trends", "global markets",
]

DEEPDIVE_PROMPT_TEMPLATE = """You are Pippy, an autonomous investment assistant writing the weekend edition of a daily market briefing email called Pippy's Brief.

Today's date: {today}
Today's headlines:
{headlines}

Market context (last close):
{market_ctx}

Your job: write a short deep dive that connects to something REAL happening this week — a specific story from the headlines above, not a generic topic. Pick the most interesting financial thread and go deep on that specific thing.

Respond in EXACTLY this format (no extra text, no markdown, no asterisks):

TOPIC: [one specific line — the actual subject, tied to a real event this week]
CATEGORY: [one of: {categories}]
TRIGGERED_BY: [the specific headline or event that made you pick this topic, in one phrase]
HOOK: [1-2 sentences — why this matters RIGHT NOW, tied directly to the real event]
STORY: [3-4 sentences — the actual substance, real numbers if available, what's happening and why it matters to investors]
TAKE: [1 sentence — your direct opinion or call. No hedging.]

Rules:
- Total word count for HOOK + STORY + TAKE must be under 150 words
- Use specific names, numbers, and companies from the news — no vague generalities
- If headlines are thin or unrelated to markets, pick the best available thread and note why in TRIGGERED_BY
- Write like a sharp analyst, not a newsletter bot"""


def _ask_claude_sync(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=120,
        )
        return result.stdout.strip() or ""
    except Exception as e:
        return f"ERROR: {e}"


def _parse_deepdive_response(text: str) -> dict:
    """Parse Claude's structured deep dive response into a dict."""
    fields = {}
    for line in text.splitlines():
        for key in ("TOPIC", "CATEGORY", "TRIGGERED_BY", "HOOK", "STORY", "TAKE"):
            if line.startswith(f"{key}:"):
                fields[key] = line[len(key)+1:].strip()
    return fields


def _deepdive_html(fields: dict, today: str, snapshot_data: list) -> tuple[str, str]:
    """Build the short-form deep dive HTML email."""
    topic        = fields.get("TOPIC", "This Week's Deep Dive")
    hook         = fields.get("HOOK", "")
    story        = fields.get("STORY", "")
    take         = fields.get("TAKE", "")
    triggered_by = fields.get("TRIGGERED_BY", "")

    # Word count check
    body_text  = f"{hook} {story} {take}"
    word_count = len(body_text.split())
    print(f"    word count: {word_count}")

    # Market snapshot strip (small, above the piece)
    index_items = ""
    for item in snapshot_data:
        pct   = item.get("pct") or 0
        color = _pct_color(pct)
        index_items += (
            f'<span style="margin-right:20px;font-size:12px;color:#6b7280">'
            f'<strong style="color:#374151">{item.get("name","")}</strong> '
            f'<span style="color:{color};font-weight:700">{_fmt(pct)}</span>'
            f'</span>'
        )
    snapshot_strip = (
        f'<tr><td style="padding:12px 32px 0;background:#f9fafb;border-bottom:1px solid #e5e7eb">'
        f'{index_items}</td></tr>'
        if index_items else ""
    )

    # Deep dive body
    dive_html = f"""
    <tr><td style="padding:28px 32px;border-bottom:1px solid #e5e7eb">

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Hook</p>
      <p style="margin:0 0 20px;font-size:15px;color:#111827;line-height:1.6">{hook}</p>

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Story</p>
      <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.7">{story}</p>

      <div style="border-left:3px solid #111827;padding:10px 0 10px 14px;margin:0">
        <p style="margin:0 0 3px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">Pippy's Take</p>
        <p style="margin:0;font-size:14px;font-weight:600;color:#111827;line-height:1.5">{take}</p>
      </div>

      {"" if not triggered_by else f'<p style="margin:16px 0 0;font-size:11px;color:#9ca3af;font-style:italic">Triggered by: {triggered_by}</p>'}

    </td></tr>"""

    body    = snapshot_strip + dive_html
    subject = f"Pippy's Brief 📚 — {today}: {topic}"
    html    = _wrap(body, topic, f"Pippy's Brief 📚 &nbsp;·&nbsp; {today}")
    return subject, html


async def deepdive(session: ClientSession) -> tuple[str, str]:
    today     = date.today().strftime("%A, %B %d")
    today_iso = date.today().isoformat()

    print("    fetching headlines and snapshot…")
    snapshot, headlines, mem = await asyncio.gather(
        call(session, "fetch_market_snapshot"),
        call(session, "fetch_top_headlines"),
        call(session, "load_memory"),
    )

    # Build headline list for the prompt
    head_list = headlines.get("headlines", [])
    headlines_str = "\n".join(
        f"- {h.get('title','')}" + (f" ({h.get('site','')})" if h.get("site") else "")
        for h in head_list[:8]
    ) or "No headlines available — use recent market knowledge."

    if not head_list:
        print("    WARNING: no headlines returned — using fallback rotation")

    # Build market context string
    index_data = snapshot.get("data", [])
    market_ctx = "\n".join(
        f"  {i.get('name')}: last close {_fmt(i.get('pct'))}"
        for i in index_data
    ) or "Market data unavailable."

    # Ask Claude to pick a topic and write the piece
    print("    asking Claude for deep dive topic and write-up…")
    prompt = DEEPDIVE_PROMPT_TEMPLATE.format(
        today=today,
        headlines=headlines_str,
        market_ctx=market_ctx,
        categories=", ".join(DEEPDIVE_CATEGORIES),
    )
    raw_response = _ask_claude_sync(prompt)
    print(f"    Claude response preview: {raw_response[:120]}…")

    fields = _parse_deepdive_response(raw_response)

    # Fallback if parsing failed
    if not fields.get("TOPIC"):
        print("    WARNING: Claude response parsing failed — using data-only fallback")
        fields = {
            "TOPIC": "Market Snapshot — This Week",
            "CATEGORY": "macro / Fed policy",
            "TRIGGERED_BY": "fallback — no parseable Claude response",
            "HOOK": "Markets were closed today. Here's where things stood at the end of the week.",
            "STORY": " ".join(
                f"{i.get('name')} closed {_fmt(i.get('pct'))}."
                for i in index_data
            ) or "Market data unavailable.",
            "TAKE": "Watch next week's open for follow-through.",
        }

    # Log to memory
    if isinstance(mem, dict):
        history = mem.get("deep_dive_history", [])
        history.append({
            "date":         today_iso,
            "category":     fields.get("CATEGORY", "unknown"),
            "topic":        fields.get("TOPIC", ""),
            "triggered_by": fields.get("TRIGGERED_BY", ""),
        })
        mem["deep_dive_history"] = history[-52:]  # keep ~1 year
        await call(session, "save_memory", {"data": mem})

    subject, html = _deepdive_html(fields, today, index_data)
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
