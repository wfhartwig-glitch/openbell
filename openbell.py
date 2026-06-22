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

async def call(session: ClientSession, name: str, args: dict = None) -> dict | list:
    result = await session.call_tool(name, args or {})
    text   = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except Exception:
        print(f"  [warn] tool '{name}' returned non-JSON: {text[:120]}", flush=True)
        return {}


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


# ── New data section builders ─────────────────────────────────────────────────

def _global_indices(indices: list) -> str:
    if not indices:
        return ""
    asia    = [i for i in indices if i.get("session") == "Asia (overnight)"]
    europe  = [i for i in indices if i.get("session") == "Europe"]

    def _rows(items):
        out = ""
        for i in items:
            pct   = i.get("pct", 0)
            color = _pct_color(pct)
            out += f"""
            <tr>
              <td style="padding:6px 0;font-size:13px;color:#374151;width:130px">{i.get("name","")}</td>
              <td style="padding:6px 0;font-size:13px;font-weight:700;color:{color};text-align:right">{_fmt(pct)}</td>
            </tr>"""
        return out

    inner = ""
    if asia:
        inner += f'<p style="margin:0 0 6px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Asia — Overnight</p>'
        inner += f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:14px">{_rows(asia)}</table>'
    if europe:
        inner += f'<p style="margin:0 0 6px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Europe — This Morning</p>'
        inner += f'<table width="100%" cellpadding="0" cellspacing="0">{_rows(europe)}</table>'
    return _section("Global Markets", inner) if inner else ""


def _commodities_and_yields(commodities: list, treasury: dict) -> str:
    rows = ""
    for c in commodities:
        pct   = c.get("pct", 0)
        price = c.get("price")
        unit  = "bbl" if "Crude" in c.get("name","") else "oz"
        p_str = f"${float(price):,.2f}/{unit}" if price else "—"
        color = _pct_color(pct)
        rows += f"""
        <tr>
          <td style="padding:7px 0;font-size:13px;color:#374151;width:130px">{c.get("name","")}</td>
          <td style="padding:7px 0;font-size:13px;color:#374151">{p_str}</td>
          <td style="padding:7px 0;font-size:13px;font-weight:700;color:{color};text-align:right">{_fmt(pct)}</td>
        </tr>"""
    if treasury and treasury.get("yield"):
        yld    = treasury.get("yield", 0)
        change = treasury.get("change", 0)
        color  = _pct_color(change)
        sign   = "+" if change >= 0 else ""
        rows += f"""
        <tr>
          <td style="padding:7px 0;font-size:13px;color:#374151;width:130px">10-Yr Treasury</td>
          <td style="padding:7px 0;font-size:13px;color:#374151">{yld:.2f}%</td>
          <td style="padding:7px 0;font-size:13px;font-weight:700;color:{color};text-align:right">{sign}{change:.3f}</td>
        </tr>"""
    if not rows:
        return ""
    return _section("Commodities &amp; Yields", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


# ── Pippy narrative summaries ─────────────────────────────────────────────────

# Keywords whose presence in a headline suggests a macro driver worth citing
_MACRO_KEYWORDS = {
    "Fed": "Fed policy",
    "Federal Reserve": "Fed policy",
    "rate cut": "rate expectations",
    "rate hike": "rate expectations",
    "interest rate": "rate expectations",
    "inflation": "inflation data",
    "CPI": "inflation data",
    "jobs": "jobs data",
    "unemployment": "jobs data",
    "payroll": "jobs data",
    "Iran": "geopolitical tensions",
    "tariff": "trade policy",
    "trade war": "trade policy",
    "earnings": "earnings season",
    "GDP": "growth data",
    "recession": "recession fears",
}

_OPEN_PHRASES = [
    "Stocks look set to open {tone} this morning",
    "U.S. markets are opening {tone}",
    "Markets are pointing {tone} at the open",
    "The opening bell sets up {tone}",
]


def _build_morning_summary(
    snapshot_data: list,
    headlines: list,
    picks_data: dict,
    commodities: list = None,
    treasury: dict = None,
) -> str:
    """3-4 sentence plain-English pre-market read, 100% deterministic, zero AI."""
    import hashlib

    idx = {}
    for item in snapshot_data:
        name = item.get("name", "")
        try:
            idx[name] = float(item.get("pct") or item.get("changesPercentage") or 0)
        except Exception:
            idx[name] = 0.0

    sp  = idx.get("S&P 500", 0.0)
    ndx = idx.get("Nasdaq",  0.0)
    dow = idx.get("Dow",     0.0)

    all_vals = [v for v in [sp, ndx, dow] if v != 0.0]
    max_move = max((abs(v) for v in all_vals), default=0.0)
    greens   = sum(1 for v in all_vals if v >= 0)
    reds     = sum(1 for v in all_vals if v < 0)

    if greens == len(all_vals):
        tone_word = "broadly higher" if max_move > 0.5 else "modestly higher"
    elif reds == len(all_vals):
        tone_word = "under pressure" if max_move > 0.5 else "slightly lower"
    else:
        tone_word = "mixed"

    # Rotate open phrase deterministically by date so it varies day-to-day
    from datetime import date as _date
    day_hash = int(hashlib.md5(_date.today().isoformat().encode()).hexdigest(), 16)
    open_tmpl = _OPEN_PHRASES[day_hash % len(_OPEN_PHRASES)]
    s1_base   = open_tmpl.format(tone=tone_word)

    # Market-relevance check: only cite a headline if it touches a market-moving topic.
    # Scan top 3 headlines; use the first that passes. Skip cleanly if none qualify.
    _MARKET_RELEVANT = {
        "fed", "federal reserve", "rate cut", "rate hike", "interest rate",
        "inflation", "cpi", "ppi", "jobs", "payroll", "unemployment",
        "iran", "war", "tariff", "trade", "oil", "opec",
        "rally", "selloff", "sell-off", "stocks", "market", "dow", "nasdaq",
        "s&p", "treasury", "yield", "recession", "gdp", "earnings",
        "gdp", "growth", "debt", "deficit", "sanctions", "bank",
    }

    top_title  = ""
    macro_ctx  = ""
    hl_age_hrs = None
    hl_list    = headlines if isinstance(headlines, list) else []

    # Pass 1: find first headline that has a macro keyword (for s1 context weaving)
    for h in hl_list[:3]:
        title = h.get("title", "") if isinstance(h, dict) else str(h)
        if not title or len(title) <= 15:
            continue
        for kw, ctx in _MACRO_KEYWORDS.items():
            if kw.lower() in title.lower():
                top_title  = title
                hl_age_hrs = h.get("age_hrs")
                macro_ctx  = ctx
                break
        if macro_ctx:
            break

    # Pass 2: if no macro keyword, find first headline that's market-relevant (for s2 quote)
    relevant_title    = ""
    relevant_age_hrs  = None
    if not macro_ctx:
        for h in hl_list[:3]:
            title = h.get("title", "") if isinstance(h, dict) else str(h)
            if not title or len(title) <= 15:
                continue
            tl = title.lower()
            if any(kw in tl for kw in _MARKET_RELEVANT):
                relevant_title   = title
                relevant_age_hrs = h.get("age_hrs")
                break

    if macro_ctx:
        s1 = f"{s1_base} as investors weigh {macro_ctx}."
    else:
        named     = {"S&P 500": sp, "Nasdaq": ndx, "Dow": dow}
        ldr_name  = max(named, key=lambda k: abs(named[k]))
        ldr_val   = named[ldr_name]
        dir_word  = "leading" if ldr_val > 0 else "lagging" if ldr_val < 0 else "flat"
        s1 = f"{s1_base}, with {ldr_name} {dir_word} at {_fmt(ldr_val)}."

    # Sentence 2: cite a market-relevant headline only — omit cleanly if none qualifies
    s2 = ""
    if macro_ctx and top_title:
        age_note = f" ({int(float(hl_age_hrs)):.0f}h ago)" if hl_age_hrs and float(hl_age_hrs) < 12 else ""
        s2 = f"Headline driving that narrative{age_note}: \"{top_title.rstrip('.')}.\""
    elif relevant_title:
        age_note = f" ({int(float(relevant_age_hrs)):.0f}h ago)" if relevant_age_hrs and float(relevant_age_hrs) < 12 else ""
        s2 = f"On the tape{age_note}: {relevant_title.rstrip('.')}."
    # else: no relevant headline — s2 stays empty, paragraph reads cleanly without it

    # Sentence 3: notable commodity or yield move
    s3 = ""
    if commodities:
        for c in commodities:
            try:
                cpct = float(c.get("pct", 0))
                if abs(cpct) >= 1.0:
                    cname = c.get("name", "")
                    dir_c = "up" if cpct > 0 else "down"
                    s3 = f"{cname} is {dir_c} {abs(cpct):.1f}% overnight — worth watching for downstream sector impact."
                    break
            except Exception:
                pass
    if not s3 and treasury and treasury.get("yield"):
        try:
            chg = float(treasury.get("change", 0))
            if abs(chg) >= 0.05:
                yld  = treasury.get("yield", 0)
                dir_y = "rising" if chg > 0 else "falling"
                s3 = f"The 10-year yield is {dir_y} to {yld:.2f}%, which typically adds pressure to rate-sensitive sectors."
        except Exception:
            pass

    # Sentence 4: picks status
    picks = picks_data.get("picks", []) if isinstance(picks_data, dict) else []
    s4 = ""
    if picks:
        n       = len(picks)
        changes = picks_data.get("changes_from_last_week", [])
        if changes:
            s4 = f"{len(changes)} of your {n} picks rotated this week — details below."
        else:
            s4 = f"All {n} of your weekly picks are holding — no changes this week."

    parts = [s for s in [s1, s2, s3, s4] if s]
    return " ".join(parts)


def _morning_summary_html(
    snapshot_data: list,
    headlines: list,
    picks_data: dict,
    commodities: list = None,
    treasury: dict = None,
) -> str:
    text = _build_morning_summary(snapshot_data, headlines, picks_data, commodities, treasury)
    return _section("What's Going On",
        f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.6">{text}</p>')


def _build_close_summary(
    snapshot_data: list,
    movers: dict,
    sectors: list,
    commodities: list = None,
    treasury: dict = None,
) -> str:
    """3-sentence plain-English close-of-day read, 100% deterministic, zero AI."""
    idx = {}
    for item in snapshot_data:
        name = item.get("name", "")
        try:
            idx[name] = float(item.get("pct") or item.get("changesPercentage") or 0)
        except Exception:
            idx[name] = 0.0

    sp  = idx.get("S&P 500", 0.0)
    ndx = idx.get("Nasdaq",  0.0)
    dow = idx.get("Dow",     0.0)

    all_vals = [v for v in [sp, ndx, dow] if v != 0.0]
    max_abs  = max((abs(v) for v in all_vals), default=0.0)
    greens   = sum(1 for v in all_vals if v >= 0)
    reds     = sum(1 for v in all_vals if v < 0)

    if not all_vals:
        s1 = "Markets wrapped up the session today."
    else:
        if greens == len(all_vals):
            tone = "rallied" if max_abs > 0.5 else "edged higher"
        elif reds == len(all_vals):
            tone = "sold off" if max_abs > 0.5 else "slipped"
        else:
            tone = "finished mixed"
        named    = {"S&P 500": sp, "Nasdaq": ndx, "Dow": dow}
        ldr_name = max(named, key=lambda k: abs(named[k]))
        ldr_val  = named[ldr_name]
        sp_str   = f" (S&P {_fmt(sp)})" if sp != 0.0 else ""
        if tone in ("rallied", "sold off", "finished mixed"):
            s1 = f"Markets {tone} today{sp_str}, with {ldr_name} the biggest mover at {_fmt(ldr_val)}."
        else:
            s1 = f"Markets {tone} today{sp_str}."

    # Sentence 2: sector story + optional commodity/yield note
    s2 = ""
    if sectors:
        try:
            best      = max(sectors, key=lambda x: float(x.get("pct") or x.get("changesPercentage") or 0))
            worst     = min(sectors, key=lambda x: float(x.get("pct") or x.get("changesPercentage") or 0))
            best_pct  = float(best.get("pct") or best.get("changesPercentage") or 0)
            worst_pct = float(worst.get("pct") or worst.get("changesPercentage") or 0)
            best_name = best.get("sector", "")
            worst_name= worst.get("sector", "")
            if best_name and abs(best_pct) > 0.1:
                s2 = f"{best_name} led the tape at {_fmt(best_pct)}"
                if worst_name and worst_name != best_name and worst_pct < -0.1:
                    s2 += f"; {worst_name} was the drag at {_fmt(worst_pct)}."
                else:
                    s2 += "."
        except Exception:
            pass

    # Sentence 3: biggest single mover, or commodity note if notable
    s3 = ""
    gainers = movers.get("gainers", [])
    losers  = movers.get("losers", [])
    biggest = None
    best_abs = 0.0
    for m in gainers + losers:
        try:
            p = abs(float(m.get("pct") or m.get("changesPercentage") or 0))
            if p > best_abs:
                best_abs = p
                biggest  = m
        except Exception:
            pass
    if biggest and best_abs >= 2.0:
        sym  = biggest.get("symbol", "")
        pct  = float(biggest.get("pct") or biggest.get("changesPercentage") or 0)
        dir_ = "surged" if pct > 3 else "gained" if pct > 0 else "dropped"
        s3   = f"On the mover board, {sym} {dir_} {_fmt(abs(pct))}."
    elif commodities:
        for c in commodities:
            try:
                cpct = float(c.get("pct", 0))
                if abs(cpct) >= 1.0:
                    cname = c.get("name", "")
                    dir_c = "moved up" if cpct > 0 else "moved lower"
                    s3 = f"{cname} {dir_c} {abs(cpct):.1f}% on the day."
                    break
            except Exception:
                pass

    parts = [s for s in [s1, s2, s3] if s]
    return " ".join(parts)


def _close_summary_html(
    snapshot_data: list,
    movers: dict,
    sectors: list,
    commodities: list = None,
    treasury: dict = None,
) -> str:
    text = _build_close_summary(snapshot_data, movers, sectors, commodities, treasury)
    return _section("What Happened Today",
        f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.6">{text}</p>')


# ── Email assemblers ──────────────────────────────────────────────────────────

async def morning(session: ClientSession) -> tuple[str, str]:
    today     = date.today()
    today_str = today.strftime("%A, %B %d")
    is_monday = today.weekday() == 0

    print("    fetching snapshot + global + commodities…")
    snapshot, global_idx, commodities_raw, treasury_raw, headlines, econ, earnings, mem = \
        await asyncio.gather(
            call(session, "fetch_market_snapshot"),
            call(session, "fetch_global_indices"),
            call(session, "fetch_commodities"),
            call(session, "fetch_treasury_yield"),
            call(session, "fetch_top_headlines"),
            call(session, "fetch_economic_calendar"),
            call(session, "fetch_earnings_calendar"),
            call(session, "load_memory"),
        )

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

    if not isinstance(picks_data, dict):
        picks_data = {}

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

    snap_list   = snapshot.get("data", [])
    hl_list     = headlines.get("headlines", [])
    global_list = global_idx.get("indices", [])
    comm_list   = commodities_raw.get("commodities", [])
    tsy         = treasury_raw if treasury_raw.get("yield") else {}

    body = (
        _morning_summary_html(snap_list, hl_list, picks_data, comm_list, tsy)
        + _indices(snap_list)
        + _global_indices(global_list)
        + _commodities_and_yields(comm_list, tsy)
        + _headlines(hl_list)
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
    print("    fetching snapshot + sectors + movers + commodities…")
    snapshot, sectors, movers, commodities_raw, headlines, mem = \
        await asyncio.gather(
            call(session, "fetch_market_snapshot"),
            call(session, "fetch_sector_performance"),
            call(session, "fetch_top_movers"),
            call(session, "fetch_commodities"),
            call(session, "fetch_top_headlines"),
            call(session, "load_memory"),
        )

    flagged   = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
    watchlist = []
    for t in flagged[:5]:
        print(f"    EOD {t}…")
        watchlist.append(await call(session, "fetch_stock_data", {"ticker": t}))

    snap_list    = snapshot.get("data", [])
    sectors_list = sectors.get("sectors", [])
    comm_list    = commodities_raw.get("commodities", [])

    body = (
        _close_summary_html(snap_list, movers, sectors_list, comm_list)
        + _indices(snap_list)
        + _movers(movers.get("gainers", []), movers.get("losers", []))
        + _sectors(sectors_list)
        + _commodities_and_yields(comm_list, {})
        + _watchlist(watchlist, "Your Watchlist — End of Day")
        + _headlines(headlines.get("headlines", []))
    )
    subject = f"Pippy's Brief — {today} Market Close"
    html    = _wrap(body, f"Market Close &nbsp; {today}", "Pippy's Brief 📊")
    return subject, html


# Priority keywords for headline-based topic selection
_HEADLINE_KEYWORDS = [
    ("Fed", "macro / Fed policy"),
    ("Federal Reserve", "macro / Fed policy"),
    ("rate cut", "macro / Fed policy"),
    ("rate hike", "macro / Fed policy"),
    ("interest rate", "macro / Fed policy"),
    ("inflation", "macro / Fed policy"),
    ("CPI", "macro / Fed policy"),
    ("Powell", "macro / Fed policy"),
    ("IPO", "IPO / public markets"),
    ("goes public", "IPO / public markets"),
    ("merger", "private company"),
    ("acquisition", "private company"),
    ("acquired", "private company"),
    ("takeover", "private company"),
    ("earnings", "earnings"),
    ("beats", "earnings"),
    ("misses", "earnings"),
    ("guidance", "earnings"),
    ("recession", "macro / Fed policy"),
    ("GDP", "macro / Fed policy"),
    ("jobs", "macro / Fed policy"),
    ("unemployment", "macro / Fed policy"),
    ("housing", "housing market"),
    ("mortgage", "housing market"),
    ("AI", "emerging technology"),
    ("artificial intelligence", "emerging technology"),
    ("chip", "emerging technology"),
    ("semiconductor", "emerging technology"),
]


def _build_deep_dive(
    snapshot_data: list,
    headline_list: list,
    movers: dict,
    sectors: list,
) -> dict:
    """
    Deterministic deep dive builder. Picks the most notable event/signal
    from real data and writes hook/story/take using template strings.
    No AI, no external text generation — just conditional logic on real numbers.
    """

    # ── index numbers ─────────────────────────────────────────────────────────
    idx = {i.get("name"): i for i in snapshot_data}
    sp  = idx.get("S&P 500", {})
    ndx = idx.get("Nasdaq",  {})
    dow = idx.get("Dow",     {})

    def pct(item): return float(item.get("pct") or 0)
    def price(item): return float(item.get("price") or 0)

    sp_pct  = pct(sp)
    ndx_pct = pct(ndx)
    dow_pct = pct(dow)

    sp_dir  = "up" if sp_pct  >= 0 else "down"
    ndx_dir = "up" if ndx_pct >= 0 else "down"
    dow_dir = "up" if dow_pct >= 0 else "down"

    spread = abs(ndx_pct - dow_pct)

    # ── top sector divergence ─────────────────────────────────────────────────
    sector_vals = [(s.get("sector",""), float(s.get("pct") or 0)) for s in sectors if s.get("pct") is not None]
    sector_vals.sort(key=lambda x: x[1], reverse=True)
    top_sector    = sector_vals[0]  if sector_vals else ("—", 0)
    bottom_sector = sector_vals[-1] if sector_vals else ("—", 0)
    sector_spread = top_sector[1] - bottom_sector[1]

    # ── top movers ────────────────────────────────────────────────────────────
    gainers = movers.get("gainers", [])
    losers  = movers.get("losers",  [])
    top_gainer = gainers[0] if gainers else {}
    top_loser  = losers[0]  if losers  else {}

    # ── priority: real headline with keyword match ────────────────────────────
    matched_headline = None
    matched_category = "market overview"
    for h in headline_list[:8]:
        title = h.get("title", "")
        for kw, cat in _HEADLINE_KEYWORDS:
            if kw.lower() in title.lower():
                matched_headline = h
                matched_category = cat
                break
        if matched_headline:
            break

    # If no keyword match, use first headline anyway if we have one
    if not matched_headline and headline_list:
        matched_headline = headline_list[0]
        matched_category = "market overview"

    # ── build the piece ───────────────────────────────────────────────────────

    if matched_headline:
        # Headline-led piece
        title = matched_headline.get("title", "")
        site  = matched_headline.get("site", "")
        snippet = matched_headline.get("snippet", "")

        topic        = title
        triggered_by = f'"{title}"' + (f" via {site}" if site else "")
        category     = matched_category

        hook = (
            f'This week\'s standout story: "{title}." '
            f'With the S&P {sp_dir} {abs(sp_pct):.2f}% on the week, the timing matters.'
        )
        story_parts = []
        if snippet:
            story_parts.append(snippet[:180].rstrip(".") + ".")
        story_parts.append(
            f"Meanwhile the broader market closed with S&P {sp_dir} {abs(sp_pct):.2f}%, "
            f"Nasdaq {ndx_dir} {abs(ndx_pct):.2f}%, and Dow {dow_dir} {abs(dow_pct):.2f}%."
        )
        if sector_spread > 1.5:
            story_parts.append(
                f"{top_sector[0]} led sectors ({'+' if top_sector[1]>=0 else ''}{top_sector[1]:.2f}%) "
                f"while {bottom_sector[0]} lagged ({'+' if bottom_sector[1]>=0 else ''}{bottom_sector[1]:.2f}%)."
            )
        story = " ".join(story_parts[:3])

        if matched_category in ("macro / Fed policy", "earnings"):
            take = (
                f"Watch how the market digests this heading into Monday — "
                f"{'rate-sensitive names could see volatility' if 'rate' in title.lower() or 'Fed' in title else 'positioning shifts often follow weekend headlines like this'}."
            )
        else:
            take = (
                f"This is the story to track next week — "
                f"whether the initial reaction holds or fades will signal how serious the market considers it."
            )

    elif spread >= 1.0:
        # Rotation story — meaningful Nasdaq/Dow divergence, no headline
        if ndx_pct > dow_pct:
            leader, laggard, leader_pct, laggard_pct = "Nasdaq", "Dow", ndx_pct, dow_pct
            rotation_type = "growth over value"
        else:
            leader, laggard, leader_pct, laggard_pct = "Dow", "Nasdaq", dow_pct, ndx_pct
            rotation_type = "value over growth"

        topic        = f"{leader} vs. {laggard} — {spread:.2f}% spread points to {rotation_type} rotation"
        triggered_by = f"Index divergence at Thursday's close: {leader} {leader_pct:+.2f}%, {laggard} {laggard_pct:+.2f}%"
        category     = "sector rotation"

        hook = (
            f"A {spread:.2f}% gap between the {leader} and {laggard} at the weekly close "
            f"isn't noise — that kind of spread usually signals active rotation, not a broad move."
        )
        story = (
            f"Thursday closed with S&P {sp_dir} {abs(sp_pct):.2f}%, {leader} {'+' if leader_pct>=0 else ''}{leader_pct:.2f}%, "
            f"and {laggard} {'+' if laggard_pct>=0 else ''}{laggard_pct:.2f}%. "
            f"Nearly {spread:.0f}% of divergence between {rotation_type.split(' over ')[0]} and "
            f"{rotation_type.split(' over ')[1]} names in a single session typically reflects institutional repositioning. "
        )
        if sector_spread > 1.5:
            story += (
                f"Sector data backs it up: {top_sector[0]} led ({'+' if top_sector[1]>=0 else ''}{top_sector[1]:.2f}%) "
                f"while {bottom_sector[0]} lagged ({'+' if bottom_sector[1]>=0 else ''}{bottom_sector[1]:.2f}%)."
            )
        take = (
            f"Watch equal-weight S&P vs. cap-weight spread Monday morning — "
            f"if the gap holds, the {rotation_type} trade has legs."
        )

    elif sector_spread >= 2.0:
        # Sector divergence story
        topic        = f"{top_sector[0]} leads, {bottom_sector[0]} lags — {sector_spread:.1f}% sector spread this week"
        triggered_by = f"Sector divergence: {top_sector[0]} {top_sector[1]:+.2f}% vs {bottom_sector[0]} {bottom_sector[1]:+.2f}%"
        category     = "sector rotation"

        hook = (
            f"This week's sector spread hit {sector_spread:.1f}% between {top_sector[0]} and {bottom_sector[0]}. "
            f"That's wide enough to matter for anyone positioned across sectors."
        )
        story = (
            f"{top_sector[0]} finished the week {'+' if top_sector[1]>=0 else ''}{top_sector[1]:.2f}%, "
            f"while {bottom_sector[0]} ended at {'+' if bottom_sector[1]>=0 else ''}{bottom_sector[1]:.2f}%. "
            f"The S&P overall went {sp_dir} {abs(sp_pct):.2f}%, "
            f"but a {sector_spread:.1f}% range between sectors means the index number hides more than it reveals. "
            f"Narrow-sector ETF positioning would have made a significant difference in performance this week."
        )
        take = (
            f"If you hold a broad index fund, you got the average — "
            f"the {top_sector[0]} overweight was this week's real trade."
        )

    elif top_gainer.get("symbol") and abs(float(top_gainer.get("pct") or 0)) >= 5:
        # Big single mover story
        sym      = top_gainer.get("symbol", "")
        gainer_pct = float(top_gainer.get("pct") or 0)
        name     = (top_gainer.get("name") or sym)[:40]

        topic        = f"{sym}'s {gainer_pct:.1f}% move — what's behind it"
        triggered_by = f"Top mover: {sym} up {gainer_pct:.2f}% on Friday's close"
        category     = "private company"

        hook = (
            f"{sym} was Friday's standout mover, closing up {gainer_pct:.1f}%. "
            f"A move that size on a {date.today().strftime('%A')} is worth understanding."
        )
        story = (
            f"{name} posted a {gainer_pct:.1f}% gain in the session. "
            f"The broader market was less dramatic — S&P {sp_dir} {abs(sp_pct):.2f}%, "
            f"Nasdaq {ndx_dir} {abs(ndx_pct):.2f}%. "
            f"Single-stock moves this sharp relative to the index typically trace back to "
            f"earnings surprise, analyst action, M&A speculation, or a macro read specific to the sector. "
            f"The move is worth tracking into Monday for follow-through or fade."
        )
        take = (
            f"A {gainer_pct:.0f}% pop with no obvious catalyst is the kind of setup "
            f"that can cut both ways Monday — confirm the reason before chasing."
        )

    else:
        # Quiet week — plain summary
        direction = "positive" if sp_pct >= 0 else "negative"
        topic        = f"A {direction} but quiet week — S&P {sp_pct:+.2f}%, Nasdaq {ndx_pct:+.2f}%"
        triggered_by = "No notable divergence or headlines — plain market summary"
        category     = "market overview"

        hook = (
            f"No dramatic divergences this week — the S&P finished {sp_dir} {abs(sp_pct):.2f}%, "
            f"and the other major indices moved in the same direction."
        )
        story = (
            f"S&P closed {sp_dir} {abs(sp_pct):.2f}%, Nasdaq {ndx_dir} {abs(ndx_pct):.2f}%, "
            f"Dow {dow_dir} {abs(dow_pct):.2f}%. "
        )
        if sector_spread > 0:
            story += (
                f"Sector dispersion stayed contained — {top_sector[0]} led at {top_sector[1]:+.2f}% "
                f"with {bottom_sector[0]} at the bottom at {bottom_sector[1]:+.2f}%. "
            )
        story += "Quiet weeks like this are often consolidation before the next directional move."
        take = "Nothing to chase here — wait for Monday's open to see if the trend resumes or reverses."

    word_count = len(f"{hook} {story} {take}".split())
    print(f"    topic: {topic[:80]}")
    print(f"    category: {category}  |  triggered by: {triggered_by[:60]}")
    print(f"    word count: {word_count}")

    return {
        "TOPIC":        topic,
        "CATEGORY":     category,
        "TRIGGERED_BY": triggered_by,
        "HOOK":         hook,
        "STORY":        story,
        "TAKE":         take,
    }


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

    print("    fetching snapshot, headlines, movers, sectors…")
    snapshot, headlines, movers, sectors, mem = await asyncio.gather(
        call(session, "fetch_market_snapshot"),
        call(session, "fetch_top_headlines"),
        call(session, "fetch_top_movers"),
        call(session, "fetch_sector_performance"),
        call(session, "load_memory"),
    )

    head_list  = headlines.get("headlines", [])
    index_data = snapshot.get("data", [])

    if not head_list:
        print("    no headlines returned — will use market data for topic selection")

    # Build the deep dive deterministically from real data — zero AI calls
    print("    building deep dive from real data…")
    fields = _build_deep_dive(index_data, head_list, movers, sectors.get("sectors", []))

    # Log to memory — include actual publish date of the source headline
    source_published = ""
    if head_list and fields.get("TRIGGERED_BY", "").startswith('"'):
        source_published = head_list[0].get("published", "")
    if isinstance(mem, dict):
        history = mem.get("deep_dive_history", [])
        history.append({
            "date":                     today_iso,
            "category":                 fields.get("CATEGORY", "unknown"),
            "topic":                    fields.get("TOPIC", ""),
            "triggered_by":             fields.get("TRIGGERED_BY", ""),
            "source_headline_published": source_published,
        })
        mem["deep_dive_history"] = history[-52:]  # keep ~1 year
        await call(session, "save_memory", {"data": mem})

    subject, html = _deepdive_html(fields, today, index_data)
    return subject, html


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(mode: str, dry_run: bool = False):
    today_str = date.today().strftime("%A, %B %d, %Y")
    start_ts  = datetime.now().strftime("%H:%M:%S UTC")
    if dry_run:
        print("=== DRY RUN — no email will be sent, no memory will be saved ===")
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

            market     = await call(session, "is_market_open_today")
            is_open    = bool(market.get("open", False))
            mkt_reason = market.get("reason", "unknown")

            if mode == "deepdive":
                if is_open:
                    print("[Pippy's Brief] Market is open — skipping deep dive.")
                    return
                subject, html = await deepdive(session)

            elif mode == "morning":
                subject, html = await morning(session)

            elif mode == "close":
                if is_open:
                    # Triggered while market is still open — too early for close summary
                    print("[Pippy's Brief] Skipped close summary — market still open, run again after 4 PM ET.")
                    return
                non_trading = "weekend" in mkt_reason or "holiday" in mkt_reason
                if non_trading:
                    print(f"[Pippy's Brief] Skipped close summary — {mkt_reason}, no session today.")
                    return
                subject, html = await close(session)

            else:
                print(f"[Pippy's Brief] Unknown mode: {mode}")
                return

            if dry_run:
                print(f"\n--- SUBJECT ---\n{subject}\n")
                print(f"--- HTML BODY ({len(html)} chars) ---")
                print(html[:6000])
                if len(html) > 6000:
                    print(f"  … (truncated, {len(html) - 6000} more chars)")
                print("\n=== DRY RUN COMPLETE — no email sent, no memory saved ===")
            else:
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full pipeline but skip send_email and save_memory")
    args = parser.parse_args()
    asyncio.run(run(args.mode, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
