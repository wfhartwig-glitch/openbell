#!/usr/bin/env python3
"""
Pippy's Brief — autonomous email agent. Zero Anthropic API cost.
Runs as an MCP client, calls pippy_mcp.py tools for data, builds HTML, sends email.

Usage:
  python openbell.py morning     → Morning Briefing (weekdays only; skips on non-trading days)
  python openbell.py close       → Market Close Summary (weekdays only)
  python openbell.py casestudy   → Standalone business-history Case Study (fires on its own schedule,
                                    unconditional on market status — weekday noon CT + weekend 8:30am CT)
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

load_dotenv()

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
SEND_LOG_FILE = os.path.join(PROJECT_DIR, "send_log.json")


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


# Reused across every section's <td> (via _section()) and by the two secondary
# table columns hidden on very narrow screens (watchlist headline snippet,
# movers' company-name column) — progressive disclosure instead of letting a
# 4-column row squeeze its percentage value off the edge at 320px.
_RESPONSIVE_STYLE_BLOCK = """<style>
  @media screen and (max-width: 480px) {
    .pippy-section-pad { padding: 20px 16px !important; }
    .pippy-header-pad  { padding: 22px 16px !important; }
    .pippy-footer-pad  { padding: 16px 16px !important; }
    .pippy-hide-narrow { display: none !important; mso-hide: all; }
  }
  /* UNVERIFIED — this dark-mode rule has never been checked in a real dark
     inbox on a real phone, only in the browser pane's prefers-color-scheme
     emulation. That emulation can only prove the CSS itself does what it
     says; it can't prove a light card reads as intentional (vs. broken)
     inside an actual dark Gmail/Mail app on a device. Do not treat this as
     confirmed until that check happens.
     Explicit light-mode opt-in for the card itself (color-scheme meta tells
     well-behaved clients we handle both modes; this pins the white card so
     its existing text-color contrast, and the red/green arrows, hold up
     even when the surrounding client chrome is dark). Gmail's app-level
     dark mode uses its own image/color heuristic rather than
     prefers-color-scheme, so this belt-and-suspenders rule is aimed at
     Apple Mail / Outlook.com / other standards-following dark modes —
     it will not reliably reach Gmail's own inversion. */
  @media (prefers-color-scheme: dark) {
    .pippy-outer-bg  { background: #0b0f19 !important; }
    .pippy-card      { background: #ffffff !important; }
  }
</style>"""


def _wrap(body: str, title: str, subtitle: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supports-color-scheme" content="light dark">
{_RESPONSIVE_STYLE_BLOCK}
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table class="pippy-outer-bg" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0">
<tr><td align="center">
<table class="pippy-card" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">

  <!-- HEADER -->
  <tr><td class="pippy-header-pad" style="background:#111827;padding:28px 32px">
    <p style="margin:0 0 4px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af">{subtitle}</p>
    <h1 style="margin:0;font-size:22px;font-weight:700;color:#ffffff;line-height:1.2">{title}</h1>
  </td></tr>

  <!-- BODY -->
  {body}

  <!-- FOOTER -->
  <tr><td class="pippy-footer-pad" style="padding:20px 32px;border-top:1px solid #e5e7eb;background:#f9fafb">
    <p style="margin:0;font-size:11px;color:#9ca3af">Pippy's Brief &mdash; automated daily market briefing. Not financial advice.</p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _section(label: str, inner: str, bg: str = "") -> str:
    bg_style = f"background:{bg};" if bg else ""
    return f"""<tr><td class="pippy-section-pad" style="{bg_style}padding:24px 32px;border-bottom:1px solid #e5e7eb">
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
          <td style="padding:8px 0;font-size:16px;font-weight:600;color:#111827;width:100px">{name}</td>
          <td style="padding:8px 12px 8px 0;font-size:16px;color:#374151;white-space:nowrap">{p_str}</td>
          <td style="padding:8px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{_fmt(pct)}</td>
        </tr>"""
    return _section("Market Snapshot", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _headlines(headlines: list) -> str:
    if not headlines:
        return ""   # no section at all rather than a heading over nothing
    items = ""
    for i, h in enumerate(headlines):
        title   = h.get("title", h) if isinstance(h, dict) else str(h)
        snippet = h.get("snippet", "") if isinstance(h, dict) else ""
        site    = h.get("site", "") if isinstance(h, dict) else ""
        border  = "border-top:1px solid #f3f4f6;" if i > 0 else ""
        items += f"""
        <div style="{border}padding:10px 0">
          <p style="margin:0 0 3px;font-size:16px;font-weight:500;color:#111827;line-height:1.4">{title}</p>
          {"" if not snippet else f'<p style="margin:0 0 2px;font-size:16px;color:#6b7280;line-height:1.4">{snippet}</p>'}
          {"" if not site    else f'<p style="margin:0;font-size:11px;color:#9ca3af">{site}</p>'}
        </div>"""
    return _section("Top Headlines", items)


def _calendar(events: list, earnings: list, econ_failed: bool = False) -> str:
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
          <td style="padding:7px 12px 7px 0;font-size:16px;color:#374151">{evt}</td>
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
          <td style="padding:7px 12px 7px 0;font-size:16px;color:#374151"><strong style="color:#111827">{sym}</strong> — {note}</td>
          <td style="padding:7px 0;text-align:right"><span style="font-size:10px;font-weight:700;color:#7c3aed;text-transform:uppercase">Earnings</span></td>
        </tr>"""
    if not rows:
        if econ_failed:
            msg = '<p style="margin:0;font-size:16px;color:#9ca3af">Macro event data unavailable (source error). No tracked earnings in the next two weeks.</p>'
        else:
            msg = '<p style="margin:0;font-size:16px;color:#9ca3af">No major events or tracked earnings in the next two weeks.</p>'
        return _section("This Week's Calendar", msg)
    return _section("This Week's Calendar", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _daily_scan(candidates: list, scanned: int = 0, elapsed: float = 0) -> str:
    if not candidates:
        return _section("Today's Top Scored Candidates",
                        '<p style="margin:0;font-size:16px;color:#9ca3af">Scan unavailable — no data returned.</p>')
    # Stacked cards, not a wide multi-column table — long rationale text wraps
    # naturally at any screen width instead of forcing a cramped/overflowing table.
    cards = ""
    for i, c in enumerate(candidates[:5], 1):
        ticker   = c.get("ticker", "")
        company  = c.get("company", ticker)
        score    = c.get("score", 0)
        rationale= c.get("rationale", "")
        sector   = c.get("sector", "")
        risk     = c.get("risk_level", "")
        momentum = c.get("momentum", 0)
        mom_color = GREEN if momentum >= 0 else RED
        mom_str   = f'{"▲" if momentum >= 0 else "▼"} {abs(momentum):.1f}% (3mo)'
        score_color = GREEN if score >= 30 else "#d97706" if score >= 15 else GRAY
        border = "" if i == 1 else "border-top:1px solid #f3f4f6;"
        cards += f"""
        <div style="{border}padding:12px 0">
          <p style="margin:0 0 3px;font-size:16px;line-height:1.4">
            <span style="font-weight:700;color:#9ca3af">{i}.</span>
            <span style="font-weight:700;color:#111827">{ticker}</span>
            <span style="color:#6b7280;font-size:13px">{company}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280;line-height:1.5">
            {sector}{" · " + risk if risk else ""} ·
            <span style="font-weight:700;color:{score_color}">Score {score:.0f}</span> ·
            <span style="font-weight:700;color:{mom_color}">{mom_str}</span>
          </p>
          <p style="margin:0;font-size:16px;color:#374151;line-height:1.5">{rationale}</p>
        </div>"""
    footer = ""
    if scanned:
        footer = f'<p style="margin:10px 0 0;font-size:11px;color:#9ca3af">Daily mechanical scan · {scanned} tickers scored · {elapsed:.0f}s runtime · separate from your held Weekly Picks</p>'
    return _section("Today's Top Scored Candidates", cards + footer)


def _sectors(sectors: list) -> str:
    rows = ""
    for s in sectors:
        name = s.get("sector", "")
        pct  = s.get("pct") or s.get("changesPercentage")
        color = _pct_color(pct)
        rows += f"""
        <tr>
          <td style="padding:6px 0;font-size:16px;color:#374151">{name}</td>
          <td style="padding:6px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{_fmt(pct)}</td>
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
              <td style="padding:7px 12px 7px 0;font-size:16px;font-weight:700;color:#111827;width:60px;white-space:nowrap">{sym}</td>
              <td style="padding:7px 12px 7px 0;font-size:13px;color:#6b7280">{name}</td>
              <td style="padding:7px 12px 7px 0;font-size:16px;color:#374151;white-space:nowrap">{p_str}</td>
              <td style="padding:7px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{_fmt(pct)}</td>
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
          <td style="padding:8px 12px 8px 0;font-size:16px;font-weight:700;color:#111827;width:65px;white-space:nowrap">{sym}</td>
          <td style="padding:8px 12px 8px 0;font-size:16px;color:#374151;white-space:nowrap">{p_str}</td>
          <td style="padding:8px 12px 8px 0;font-size:16px;font-weight:700;color:{color};width:85px;white-space:nowrap">{_fmt(pct)}</td>
          <td class="pippy-hide-narrow" style="padding:8px 0;font-size:13px;color:#6b7280">{"" if not head else head[:60] + "…"}</td>
        </tr>"""
    return _section(label, f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _enrich_picks_with_perf(picks: list, mem: dict) -> list:
    """Merge pct_change_since_pick from perf_history into each pick dict for display."""
    perf_map = {ph["ticker"]: ph for ph in mem.get("pick_performance_history", [])}
    enriched = []
    for p in picks:
        sym = p.get("ticker", "")
        enriched_pick = dict(p)
        if sym in perf_map:
            enriched_pick["pct_change_since_pick"] = perf_map[sym].get("pct_change_since_pick")
        enriched.append(enriched_pick)
    return enriched


def _unified_picks(picks: list, scan_candidates: list, week: str = "", changes: list = None,
                   scanned: int = 0, elapsed: float = 0) -> str:
    """
    Stacked cards, not a wide multi-column table — held Weekly Picks first, then
    new daily-scan candidates not already held. A ticker that's both held and
    top-scored today gets a single card (held) with an inline flag — never two
    cards with two numbers. Cards (not table columns) so the long free-text
    Detail line wraps naturally at any screen width instead of forcing a
    cramped/overflowing table on mobile.
    """
    if not picks and not scan_candidates:
        return ""

    held_tickers = {p.get("ticker", "") for p in picks}
    top_scan       = scan_candidates[:5]
    scan_by_ticker = {c.get("ticker", ""): c for c in top_scan}

    cards = ""
    is_first = True

    def _border():
        nonlocal is_first
        b = "" if is_first else "border-top:1px solid #f3f4f6;"
        is_first = False
        return b

    for p in picks:
        ticker     = p.get("ticker", "")
        weeks_held = p.get("weeks_held", 1)
        pct_since  = p.get("pct_change_since_pick")
        pct_str    = ""
        if pct_since is not None:
            c = GREEN if pct_since >= 0 else RED
            pct_str = f'<span style="color:{c};font-weight:700">{"▲" if pct_since >= 0 else "▼"} {abs(pct_since):.1f}%</span> since entry — '
        note = p.get("note") or p.get("rationale", "")

        flag = ""
        if ticker in scan_by_ticker:
            sc = scan_by_ticker[ticker]
            flag = f' — <span style="color:#7c3aed;font-weight:600">also top-scored today (Score {sc.get("score", 0):.0f})</span>'

        cards += f"""
        <div style="{_border()}padding:12px 0">
          <p style="margin:0 0 3px;font-size:16px;font-weight:700;color:#111827">
            {ticker} <span style="font-weight:500;color:#6b7280;font-size:13px">{p.get("company","")}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280">
            {p.get("sector","")} · <span style="font-weight:700;color:{GREEN}">Holding · {weeks_held}w</span>
          </p>
          <p style="margin:0;font-size:16px;color:#374151;line-height:1.5">{pct_str}{note}{flag}</p>
        </div>"""

    new_candidates = [c for c in top_scan if c.get("ticker", "") not in held_tickers][:4]
    for c in new_candidates:
        score    = c.get("score", 0)
        momentum = c.get("momentum", 0)
        mom_str  = f'{"▲" if momentum >= 0 else "▼"} {abs(momentum):.1f}% (3mo)'
        detail   = f"Score {score:.0f} — {c.get('rationale','')} — {mom_str}"
        cards += f"""
        <div style="{_border()}padding:12px 0">
          <p style="margin:0 0 3px;font-size:16px;font-weight:700;color:#111827">
            {c.get("ticker","")} <span style="font-weight:500;color:#6b7280;font-size:13px">{c.get("company","")}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280">
            {c.get("sector","")} · <span style="font-weight:700;color:#7c3aed">New candidate</span>
          </p>
          <p style="margin:0;font-size:16px;color:#374151;line-height:1.5">{detail}</p>
        </div>"""

    changes_html = ""
    if changes:
        items = "".join(f'<li style="margin:3px 0;font-size:16px;color:#6b7280">{c}</li>' for c in changes)
        changes_html = f'<p style="margin:12px 0 4px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Changes This Week</p><ul style="margin:0;padding-left:18px">{items}</ul>'

    explainer = ('<p style="margin:0 0 12px;font-size:16px;color:#6b7280">'
                 'Holding = current positions, updated weekly. '
                 'New candidate = fresh signal from today\'s scan.</p>')

    footer_bits = ["Updated every Monday."]
    if scanned:
        footer_bits.append(f"Daily mechanical scan · {scanned} tickers scored · {elapsed:.0f}s runtime.")
    footer = f'<p style="margin:10px 0 0;font-size:11px;color:#9ca3af">{" ".join(footer_bits)}</p>'

    week_label = f" — {week}" if week else ""
    return _section(f"Stock Picks{week_label}",
        explainer + cards + changes_html + footer)


# ── Learning loop helpers ─────────────────────────────────────────────────────

def _today_ct(now: datetime = None) -> date:
    """
    Canonical "what day is it, for briefing purposes" — always America/Chicago
    (matches the "already sent today" send-guard in run()), never bare
    date.today() / datetime.now(), which return the machine's LOCAL system
    time. On a GitHub Actions runner (system TZ defaults to UTC) that's a real
    bug near midnight UTC: date.today() rolls over to "tomorrow" ~5-6 hours
    BEFORE Chicago does, which could mis-stamp a briefing_history entry,
    mis-filter "today's" earnings, mis-date an email subject line, or disagree
    with the send-guard's own date key on a late-night manual/catch-up run.
    `now` (tz-aware or naive) is injectable for frozen-timestamp tests instead
    of only ever reading the live clock.
    """
    import pytz
    ct = pytz.timezone("America/Chicago")
    if now is None:
        now = datetime.now(ct)
    elif now.tzinfo is None:
        now = ct.localize(now)
    else:
        now = now.astimezone(ct)
    return now.date()


def _today_ct_iso(now: datetime = None) -> str:
    """String form of _today_ct() — see its docstring."""
    return _today_ct(now).isoformat()


def _classify_direction(snapshot_data: list) -> str:
    """Classify market direction from snapshot into 'higher' / 'lower' / 'mixed'."""
    vals = []
    for item in snapshot_data:
        try:
            vals.append(float(item.get("pct") or item.get("changesPercentage") or 0))
        except Exception:
            pass
    if not vals:
        return "unknown"
    greens = sum(1 for v in vals if v >= 0)
    reds   = sum(1 for v in vals if v < 0)
    if greens == len(vals):
        return "higher"
    if reds == len(vals):
        return "lower"
    return "mixed"


def _classify_group_direction(values: list, laggard_flat: float = 0.25, majority_min: float = 0.4) -> dict:
    """
    Classifies a group of index/asset percentage moves as one of:
      - "directional"              — every value shares a sign (or is zero).
      - "directional_with_laggard" — all but one share a sign, and that one
        dissenter sits within `laggard_flat` of flat while the majority
        averages at least `majority_min` — a real move with one name sitting
        it out, not a genuine split.
      - "mixed"                    — real magnitude on both sides.
      - "unknown"                  — no usable values at all.

    Fixes a real bug: a group's own direction was never classified
    independently — Europe was labeled "mixed" purely by copying the US
    tape's own label, even on a day both European indices were positive.
    Also fixes magnitude-blindness: "any sign disagreement = mixed" called a
    rally with one index a hair negative "mixed" even when that dissenter was
    within a quarter point of flat and the rest of the group was up close to
    a percent.

    Returns {"kind", "direction" ("higher"/"lower"/None), "laggard_idx"
    (index into `values`, or None)}. Laggard-vs-majority thresholds are a
    judgment call, stated here rather than buried: 0.25% is "close enough to
    flat that it reads as sitting out, not disagreeing"; 0.4% average is
    "the majority actually moved, not just technically shares a sign."
    """
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return {"kind": "unknown", "direction": None, "laggard_idx": None}

    signs = [1 if v >= 0 else -1 for v in vals]
    if all(s == signs[0] for s in signs):
        return {"kind": "directional", "direction": "higher" if signs[0] >= 0 else "lower", "laggard_idx": None}

    pos_idxs = [i for i, s in enumerate(signs) if s > 0]
    neg_idxs = [i for i, s in enumerate(signs) if s < 0]
    for majority_idxs, minority_idxs, maj_dir in (
        (pos_idxs, neg_idxs, "higher"), (neg_idxs, pos_idxs, "lower"),
    ):
        if len(minority_idxs) == 1 and majority_idxs:
            laggard_val  = vals[minority_idxs[0]]
            majority_avg = sum(abs(vals[i]) for i in majority_idxs) / len(majority_idxs)
            if abs(laggard_val) <= laggard_flat and majority_avg >= majority_min:
                return {"kind": "directional_with_laggard", "direction": maj_dir, "laggard_idx": minority_idxs[0]}

    return {"kind": "mixed", "direction": None, "laggard_idx": None}


def _log_briefing_history_health(mem, today_s: str) -> None:
    """
    Prints the date of the most recent briefing_history entry on every run, so a
    persistence gap (the write silently failing, being skipped, or overwritten)
    is visible in the run's own log output instead of requiring someone to go
    looking for it. Read-only — never mutates or saves mem.
    """
    if not isinstance(mem, dict):
        print(f"[MEMORY-HEALTH] load_memory returned a non-dict ({type(mem).__name__}) — "
              f"cannot check briefing_history. This is itself worth investigating.")
        return
    history = mem.get("briefing_history", [])
    if not history:
        print(f"[MEMORY-HEALTH] briefing_history is empty as of {today_s}. "
              f"If prior runs should have populated it, the write path may be failing.")
        return
    dates = [e.get("date") for e in history if e.get("date")]
    if not dates:
        print(f"[MEMORY-HEALTH] briefing_history has {len(history)} entries but none carry a 'date' field.")
        return
    latest = max(dates)
    try:
        gap_days = (date.fromisoformat(today_s) - date.fromisoformat(latest)).days
    except Exception:
        gap_days = None
    if gap_days is not None and gap_days > 3:
        print(f"[MEMORY-HEALTH] ⚠ briefing_history's most recent entry is {latest} "
              f"({gap_days} days before today, {today_s}) — {len(history)} entries total. "
              f"A gap this wide means the write/commit path likely stopped working; check "
              f"recent workflow runs' 'Commit updated memory' steps.")
    else:
        print(f"[MEMORY-HEALTH] briefing_history OK — most recent entry {latest}, "
              f"{len(history)} entries total.")


def _update_learning_memory(mem: dict, log_entry: dict) -> dict:
    """
    Append a briefing log entry, update theme_frequency, check prediction accuracy.
    Returns the modified mem dict. Caller is responsible for saving it.
    """
    today_str = _today_ct_iso()

    # ── briefing_history (cap 60) ─────────────────────────────────────────────
    history = mem.setdefault("briefing_history", [])
    log_entry["date"] = today_str
    history.append(log_entry)
    if len(history) > 60:
        mem["briefing_history"] = history[-60:]

    # ── theme_frequency ───────────────────────────────────────────────────────
    theme = log_entry.get("headline_theme") or log_entry.get("theme")
    if theme:
        freq = mem.setdefault("theme_frequency", {})
        freq[theme] = freq.get(theme, 0) + 1

    # ── prediction_accuracy (close only) ─────────────────────────────────────
    if log_entry.get("type") == "close":
        actual     = log_entry.get("actual_direction", "unknown")
        # Find today's morning entry to compare against
        morning_entry = next(
            (e for e in reversed(mem.get("briefing_history", []))
             if e.get("date") == today_str and e.get("type") == "morning"),
            None,
        )
        if morning_entry:
            called   = morning_entry.get("direction_called", "unknown")
            accurate = called == actual
            acc_list = mem.setdefault("prediction_accuracy", [])
            acc_list.append({
                "date":     today_str,
                "called":   called,
                "actual":   actual,
                "accurate": accurate,
            })
            if len(acc_list) > 60:
                mem["prediction_accuracy"] = acc_list[-60:]

            # Calibration flag: if last 10 accuracy entries are <50% accurate, note it
            recent = mem["prediction_accuracy"][-10:]
            if len(recent) >= 10:
                acc_rate = sum(1 for r in recent if r.get("accurate")) / len(recent)
                if acc_rate < 0.5:
                    mem["calibration_note"] = (
                        f"Direction-calling accuracy has been {acc_rate:.0%} over the last "
                        f"{len(recent)} sessions — consider reviewing classification thresholds."
                    )
                else:
                    mem.pop("calibration_note", None)

    return mem


def _get_recurring_theme(mem: dict, window: int = 5, threshold: int = 3) -> str:
    """
    Return the theme name if any theme appears threshold+ times in the last window briefings,
    else return empty string.
    """
    recent = [
        e.get("headline_theme") or e.get("theme")
        for e in mem.get("briefing_history", [])[-window:]
        if e.get("headline_theme") or e.get("theme")
    ]
    from collections import Counter
    counts = Counter(recent)
    for theme, n in counts.most_common(1):
        if n >= threshold and theme:
            return theme
    return ""


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
              <td style="padding:6px 0;font-size:16px;color:#374151;width:130px">{i.get("name","")}</td>
              <td style="padding:6px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{_fmt(pct)}</td>
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
          <td style="padding:7px 0;font-size:16px;color:#374151;width:130px">{c.get("name","")}</td>
          <td style="padding:7px 12px 7px 0;font-size:16px;color:#374151;white-space:nowrap">{p_str}</td>
          <td style="padding:7px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{_fmt(pct)}</td>
        </tr>"""
    if treasury and treasury.get("yield"):
        yld    = treasury.get("yield", 0)
        change = treasury.get("change", 0)
        color  = _pct_color(change)
        sign   = "+" if change >= 0 else ""
        rows += f"""
        <tr>
          <td style="padding:7px 0;font-size:16px;color:#374151;width:130px">10-Yr Treasury</td>
          <td style="padding:7px 12px 7px 0;font-size:16px;color:#374151;white-space:nowrap">{yld:.2f}%</td>
          <td style="padding:7px 0;font-size:16px;font-weight:700;color:{color};text-align:right;white-space:nowrap">{sign}{change:.3f}</td>
        </tr>"""
    if not rows:
        return ""
    return _section("Commodities &amp; Yields", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


# ── Pippy narrative summaries ─────────────────────────────────────────────────

# Keywords whose presence in a headline suggests a macro theme — used ONLY by the
# fallback path below (when zero ladder drivers trip and we need something to say).
# IMPORTANT: any new keyword added here must use word-boundary matching if <=6 chars.
# Run test_keyword_safety.py after adding new keywords to catch substring collisions
# (e.g. "Fed"→"FedEx", "iran"→"Iranian") before they ship in a real email.
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

# Market-relevance check for the fallback path — two tiers, strong (any single
# match qualifies) and weak (requires 2+ matches). Word-boundary matching for
# short/ambiguous terms to avoid false substrings (the same bug class as
# "Fed"→"FedEx" — see test_keyword_safety.py).
_STRONG_MARKET_KWS = [
    "federal reserve", "rate cut", "rate hike", "interest rate",
    "inflation", "cpi", "ppi", "payroll", "unemployment",
    "iran", "tariff", "trade war", "opec",
    "selloff", "sell-off", "s&p 500", "nasdaq composite",
    "treasury yield", "10-year yield", "recession", "gdp",
]
_WEAK_MARKET_KWS = [
    "fed", "jobs", "war", "oil", "trade", "stocks", "market", "dow", "nasdaq",
    "treasury", "yield", "earnings", "growth", "debt", "deficit", "sanctions", "bank",
    "rally", "rates",
]


def _headline_is_market_relevant(title: str) -> bool:
    tl = title.lower()
    def _strong_hit(kw: str) -> bool:
        if len(kw) <= 6:
            return bool(re.search(r'\b' + re.escape(kw) + r'\b', tl))
        return kw in tl
    if any(_strong_hit(kw) for kw in _STRONG_MARKET_KWS):
        return True
    weak_hits = sum(1 for kw in _WEAK_MARKET_KWS if re.search(r'\b' + re.escape(kw) + r'\b', tl))
    return weak_hits >= 2


# ── Headline sentiment gate ────────────────────────────────────────────────────
# Cheap keyword-based tagging — not real NLP, just enough to catch the exact bug
# class this rewrite fixes: a headline whose tone reads as bearish, or as
# uncertain/"mixed", being cited under a confidently directional tape (the
# reported incident: "Equity Futures Mixed Pre-Bell Thursday" cited as the
# driver under a "broadly higher" tape).
_BULLISH_HL_WORDS = [
    "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies",
    "gain", "gains", "climb", "climbs", "higher", "beats", "record high",
    "advance", "advances", "rebound", "rebounds",
]
_BEARISH_HL_WORDS = [
    "plunge", "plunges", "tumble", "tumbles", "selloff", "sell-off", "sink",
    "sinks", "slump", "slumps", "drop", "drops", "falls", "fall", "lower",
    "misses", "slide", "slides", "crash", "crashes", "slumps",
]
_MIXED_HL_WORDS = ["mixed", "flat", "choppy", "directionless", "little changed", "muted"]


def _classify_headline_sentiment(title: str) -> str:
    """Returns 'bullish' / 'bearish' / 'mixed' / 'neutral'."""
    tl = title.lower()
    def _hit(words):
        return any(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in words)
    if _hit(_MIXED_HL_WORDS):
        return "mixed"
    bullish, bearish = _hit(_BULLISH_HL_WORDS), _hit(_BEARISH_HL_WORDS)
    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "neutral"


def _sentiment_gate_ok(headline_sentiment: str, tape_tone: str) -> bool:
    """
    tape_tone is 'higher' / 'lower' / 'mixed' / 'unknown'. Reject a headline
    whose sentiment contradicts a confidently directional tape, OR that reads
    as uncertain/"mixed" when the tape itself has a confident direction —
    that second case is exactly the reported bug (a "mixed" headline cited
    under a "broadly higher" tape). A headline the classifier can't read
    (neutral) is always allowed through, and anything is allowed when the
    tape itself has no confident direction to contradict.
    """
    if headline_sentiment == "neutral":
        return True
    if tape_tone in ("mixed", "unknown"):
        return True
    if tape_tone == "higher":
        return headline_sentiment not in ("bearish", "mixed")
    if tape_tone == "lower":
        return headline_sentiment not in ("bullish", "mixed")
    return True


def _rate_extreme_note(current, six_mo_high, six_mo_high_day, six_mo_low, six_mo_low_day) -> str:
    """
    If current 10y is within ~10bp of its trailing 6-month high/low, names
    that — with a day-of-week ("Tuesday's high") if the extreme was recent
    enough for that to actually mean something, else a generic "6-month"
    framing. If the extreme occurred TODAY specifically, phrases it as
    setting a new high/low right now ("its highest level in six months")
    rather than "off Wednesday's high" — that phrasing would misleadingly
    imply it already receded from a past peak when it's actually sitting AT
    one right now.
    """
    if current is None:
        return ""
    today_name = _today_ct().strftime("%A")
    for kind, extreme_val, day_name in (("high", six_mo_high, six_mo_high_day),
                                        ("low",  six_mo_low,  six_mo_low_day)):
        if extreme_val is None:
            continue
        if abs(float(current) - float(extreme_val)) <= 0.10:
            if day_name and day_name == today_name:
                return f"its {'highest' if kind == 'high' else 'lowest'} level in six months"
            lead = f"{day_name}'s" if day_name else "the recent 6-month"
            return f"off {lead} {extreme_val:.2f}% {kind}"
    return ""


def _crossed_round_10(price, change) -> bool:
    """True if price crossed an integer multiple of $10 vs. its previous level."""
    try:
        price, change = float(price), float(change)
    except Exception:
        return False
    prev = price - change
    return int(price // 10) != int(prev // 10)


def _rot_phrase(pool: list, day_hash: int, salt_key: str) -> str:
    """Deterministic per-day rotation through a phrase pool, salted per pool so
    different pools don't all lock-step to the same index on the same day."""
    import hashlib
    salt = int(hashlib.md5(salt_key.encode()).hexdigest(), 16)
    return pool[(day_hash + salt) % len(pool)]



# ── Index-divergence explanation (Step 4a) ────────────────────────────────────



def _sector_pct_map(sectors: list) -> dict:
    out = {}
    for s in sectors or []:
        name = (s.get("sector") or "").strip()
        if not name:
            continue
        try:
            out[name] = float(s.get("pct") if s.get("pct") is not None else s.get("changesPercentage", 0))
        except Exception:
            continue
    return out


def _sector_lookup(pct_map: dict, *name_fragments):
    """Case-insensitive substring lookup — sector-name spelling varies by
    source ('Consumer Discret.' vs 'Consumer Discretionary')."""
    for name, pct in pct_map.items():
        nl = name.lower()
        if any(frag in nl for frag in name_fragments):
            return pct
    return None


# ── Rotation interpretation (Step 5, close mode only) ─────────────────────────

# ── Outsized mover (Step 6, close mode) ────────────────────────────────────────

# ── Event/catalyst extraction (Step 2) ────────────────────────────────────────
# Mines the headline feed already fetched elsewhere — no new API, no new fetch.
# Word-boundary matched for short/ambiguous terms, same false-substring-match
# safety this file already applies everywhere else (see test_keyword_safety.py).

_EVENT_EXCLUDE_PATTERNS = [
    r"\bopening bell\b", r"\bclosing bell\b", r"\brings? the bell\b",
    r"\bfloor visit\b", r"\bvisits? the (?:nyse|nasdaq|trading floor)\b",
    r"\banniversary\b", r"\byears? ago today\b", r"\blooks? back at\b",
    r"\bwhat (?:warren )?buffett would\b", r"\bbuffett'?s advice\b",
    r"\bbeginner'?s guide\b", r"\bhow to invest\b", r"\bwhat is an? etf\b",
    r"\betf explained\b", r"\bexplainer:\b",
]

_EVENT_MONETARY_TERMS = [
    "federal reserve", "fed chair", "fomc", "rate decision", "rate cut", "rate hike",
    "cpi", "ppi", "jobs report", "payrolls", "unemployment claims", "treasury auction",
    "european central bank", "ecb", "bank of japan", "boj", "bank of england",
    "central bank", "fed minutes", "policy meeting", "inflation report", "core inflation",
]
_EVENT_MACRO_POLITICAL_TERMS = [
    "tariff", "trade deal", "trade war", "trade policy", "summit", "state visit",
    "election", "geopolitical", "sanctions", "government shutdown",
    "opec", "oil supply", "supply chain", "foreign leader",
]
_EVENT_CORPORATE_TERMS = [
    "earnings report", "quarterly results", "keynote", "product launch", "unveils",
    "merger", "acquisition", "regulatory approval", "antitrust", "ftc probe",
    "sec investigation", "lawsuit",
]
_EVENT_FUTURE_CUES = [
    "expected", "will ", "due ", "ahead of", "later today", "scheduled",
    "awaits", "set to", "upcoming", "later this week", "tomorrow",
]
_EVENT_PAST_CUES = [
    "said", "announced", "reported", "signaled", "delivered", "held",
    "warned", "cut rates", "raised rates", "kept rates", "released", "posted",
]


# Terms that are also ordinary names ("Summit Materials", "Election Systems & Software") only count
# as an event when the headline carries supporting context.
_EVENT_CONTEXT_REQUIRED = {
    "summit": ["g7", "g20", "leaders", "president", "talks", "trade", "peace", "climate", "nato", "eu ",
               "china", "white house", "prime minister"],
}


def _event_word_boundary_hit(term: str, text_l: str) -> bool:
    pat = r'\b' + re.escape(term) + r'\b' if len(term) <= 6 else re.escape(term)
    if not re.search(pat, text_l):
        return False
    need = _EVENT_CONTEXT_REQUIRED.get(term)
    return True if not need else any(c in text_l for c in need)


def _event_is_scheduled(text_l: str):
    """True=forward-looking, False=already happened, None=ambiguous either way."""
    if any(c in text_l for c in _EVENT_FUTURE_CUES):
        return True
    if any(c in text_l for c in _EVENT_PAST_CUES):
        return False
    return None


def _extract_market_events(headlines: list, mode: str, max_events: int = 2) -> list:
    """
    Classifies headlines already fetched into MONETARY / MACRO_POLITICAL /
    CORPORATE, distinguishing scheduled-today from already-happened. Applies
    a signal test FIRST, before any category match: ceremonial and evergreen
    content (opening-bell pieces, floor-visit photo-ops, milestone
    anniversaries, "what Buffett would do" columns, generic ETF explainers)
    is excluded outright, even if it happens to also contain a matching
    keyword — a headline that can't plausibly move a price or signal a
    forward risk doesn't qualify no matter what words it contains.

    Capped at `max_events` (2, per spec), preferring whichever tense matches
    the brief's own lean (morning = forward-looking, close = backward-looking)
    without ever dropping a qualifying event just because its tense doesn't
    match — the mode preference only breaks ties in ordering.
    """
    results = []
    seen_titles = set()
    for h in headlines or []:
        title   = (h.get("title") or "").strip()
        snippet = (h.get("snippet") or "").strip()
        if not title or title in seen_titles:
            continue
        text_l = f"{title} {snippet}".lower()

        if any(re.search(pat, text_l) for pat in _EVENT_EXCLUDE_PATTERNS):
            continue

        category, term = None, ""
        for terms, cat in (
            (_EVENT_MONETARY_TERMS, "MONETARY"),
            (_EVENT_MACRO_POLITICAL_TERMS, "MACRO_POLITICAL"),
            (_EVENT_CORPORATE_TERMS, "CORPORATE"),
        ):
            hit = next((kw for kw in terms if _event_word_boundary_hit(kw, text_l)), None)
            if hit:
                category, term = cat, hit
                break
        if not category:
            continue

        seen_titles.add(title)
        results.append({
            "category": category,
            "term": term,
            "scheduled": _event_is_scheduled(text_l),
            "headline": h,
        })

    prefer_scheduled = mode == "morning"
    results.sort(key=lambda e: 0 if e["scheduled"] is prefer_scheduled else 1)
    return results[:max_events]


_WORD_COUNT_TARGETS = {
    ("morning", "P1"): (120, 180),
    ("morning", "P2"): (50, 80),
    ("close", "P1"): (150, 220),
    ("close", "P2"): (60, 100),
}


def _log_word_count(mode: str, part: str, text: str, diagnostics: dict = None) -> None:
    """
    Step 10: word-count floors are a floor on explanation, not permission to
    pad. Logs (never raises/blocks) when a part falls short, naming which
    mode/part AND which specific layers did or didn't fire (`diagnostics`),
    so a shortfall points at a concrete reason ("no qualifying event found",
    "no divergence >= 0.3pp") instead of a generic reminder to go check.
    """
    lo, hi = _WORD_COUNT_TARGETS.get((mode, part), (0, 10**9))
    count = len((text or "").split())
    if count < lo:
        reasons = []
        if diagnostics:
            for label, fired in diagnostics.items():
                if not fired:
                    reasons.append(f"no {label}")
        reason_str = f" Likely reason(s): {', '.join(reasons)}." if reasons else ""
        print(f"[LENGTH] {mode.upper()} {part} is {count} words, under the {lo}-{hi} target.{reason_str}")
    elif count > hi:
        print(f"[LENGTH] {mode.upper()} {part} is {count} words, over the {lo}-{hi} target.")




def _pct_words(v) -> str:
    """'up 0.62%' / 'down 0.15%' / 'unchanged' — plain words instead of arrow glyphs
    for prose (the data tables keep the glyphs)."""
    try:
        v = float(v)
    except Exception:
        return "unchanged"
    if abs(v) < 0.005:
        return "unchanged"
    return f"{'up' if v >= 0 else 'down'} {abs(v):.2f}%"


def _pct_signed(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "n/a"


def _index_trio_words(sp: float, ndx: float, dow: float, verb: str = "") -> str:
    v = f"{verb} " if verb else ""
    return (f"the S&P 500 {v}{_pct_words(sp)}, the Nasdaq {v}{_pct_words(ndx)} "
            f"and the Dow {v}{_pct_words(dow)}")


# An index within this many percent of unchanged counts as "flat". 0.25 is a
# judgment call: small enough that a session inside it genuinely reads as a
# non-event, large enough that ordinary noise around zero doesn't register as a
# "move" for a reader with no market background.
_FLAT_BAND = 0.25

# Mega-cap names large enough to move the S&P 500 / Nasdaq / Dow on their own
# (approximate list by index weight — used only to decide whether a big single-
# stock move can plausibly explain an index move, never to claim that it did).
_INDEX_HEAVY = {
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA",
    "BRK-B", "BRK.B", "LLY", "JPM", "V", "WMT", "UNH", "XOM", "ORCL", "NFLX",
}

_FUT = "Stock futures (early trading before the market opens)"
_OPEN_STRONG_HIGHER = ["{fut} are pointing clearly higher — {trio}.",
                       "{fut} are firm — {trio}.",
                       "{fut} show a solid advance — {trio}."]
_OPEN_MODEST_HIGHER = ["{fut} are edging higher — {trio}.",
                       "{fut} are modestly higher — {trio}.",
                       "{fut} lean higher — {trio}."]
_OPEN_STRONG_LOWER = ["{fut} are pointing clearly lower — {trio}.",
                      "{fut} are under pressure — {trio}.",
                      "{fut} show a notable decline — {trio}."]
_OPEN_MODEST_LOWER = ["{fut} are edging lower — {trio}.",
                      "{fut} are modestly lower — {trio}.",
                      "{fut} lean lower — {trio}."]
_OPEN_SPLIT = ["{fut} are split — {trio}.",
               "{fut} are pointing in different directions — {trio}.",
               "{fut} are mixed — {trio}."]


def _morning_opener(sp: float, ndx: float, dow: float, group: dict, day_hash: int) -> str:
    """Tape sentence for the morning brief. A flat tape is described as flat (not
    "directionless... with tech leading", which contradicts itself); a rally with
    one index barely negative names it as the laggard instead of calling the
    whole tape mixed."""
    vals = (sp, ndx, dow)
    trio = _index_trio_words(sp, ndx, dow, "is")
    if all(abs(v) <= _FLAT_BAND for v in vals):
        trio = _index_trio_words(sp, ndx, dow)
        return (f"{_FUT} are close to flat — {trio}. A day like this usually means "
                f"opposing forces are cancelling out, or traders are waiting for news.")
    if group["kind"] == "directional_with_laggard":
        lag = ["S&P 500", "Nasdaq", "Dow"][group["laggard_idx"]]
        word = "higher" if group["direction"] == "higher" else "lower"
        return f"{_FUT} are pointing {word}, though not evenly — {trio}. The {lag} is the one holding back."
    max_move = max(abs(v) for v in vals)
    if group["kind"] == "directional":
        up = group["direction"] == "higher"
        strong = max_move > 0.5
        pool = (_OPEN_STRONG_HIGHER if strong else _OPEN_MODEST_HIGHER) if up else \
               (_OPEN_STRONG_LOWER if strong else _OPEN_MODEST_LOWER)
        return _rot_phrase(pool, day_hash, "tape_open").format(fut=_FUT, trio=trio)
    return _rot_phrase(_OPEN_SPLIT, day_hash, "tape_open").format(fut=_FUT, trio=trio)


def _rates_sentence(treasury: dict, mode: str, rotation_context: dict = None,
                    skip_growth_link: bool = False, tape_dir: str = None) -> dict:
    """
    ALWAYS names the 10-year yield's level plus trailing context (inclusion is
    unconditional, Step 4b), and defines the term for a reader with no market
    background. The >=4 basis point trip only gates the "what it means for
    stocks" clause.

    Hedging rule: the pipeline sees co-movement, not causes, so the yield-to-
    stocks link is always stated as a general tendency ("can pressure") plus how
    today's tape lines up with it — never as "because yields rose, stocks fell".
    When yields and stocks moved the "wrong" way for the usual link, it says so
    and says which side the tape sided with (conflicting drivers).

    `rotation_context` (close mode): a rotation note already made a yields claim —
    this sentence confirms or complicates it. `skip_growth_link`: the divergence
    note already addressed yields, so this sentence doesn't repeat it.
    Returns {"text", "has_chain"}.
    """
    empty = {"text": "", "has_chain": False}
    if not treasury or treasury.get("yield") is None:
        return empty
    try:
        yld = float(treasury.get("yield"))
    except Exception:
        return empty
    try:
        chg = float(treasury.get("change", 0) or 0)
    except Exception:
        chg = 0.0
    bp = abs(chg) * 100
    extreme = _rate_extreme_note(
        yld, treasury.get("six_mo_high"), treasury.get("six_mo_high_day"),
        treasury.get("six_mo_low"), treasury.get("six_mo_low_day"),
    )
    meaningful = bp >= 4

    if bp < 1:
        move_clause = "little changed on the session"
    else:
        unit = "basis point" if round(bp) == 1 else "basis points"
        move_clause = (f"{'down' if chg < 0 else 'up'} {bp:.0f} {unit} "
                       f"({abs(chg):.2f} percentage points)")
    extreme_clause = f", {extreme}" if extreme else ""
    sentence = (f"The 10-year Treasury yield — what the U.S. government pays to borrow for ten "
                f"years, a benchmark for loans everywhere — is {yld:.2f}%, {move_clause}{extreme_clause}.")
    has_chain = False

    if rotation_context and rotation_context.get("wants_rates_crosscheck"):
        implies_falling = rotation_context.get("implies_falling_yields", False)
        has_chain = True
        if implies_falling and chg < -0.005:
            sentence += (" That fall fits the pattern: lower yields tend to favor growth "
                         "companies over income-paying sectors.")
        elif implies_falling and chg > 0.005:
            sentence += (" That complicates the picture: yields rose today, which doesn't fit "
                         "a simple falling-rates story, so this is an unusual pairing we can't "
                         "fully explain from the data.")
        elif implies_falling:
            sentence += (" Yields barely moved, so this looks more like money shifting between "
                         "sectors than a story about interest rates.")
    elif meaningful and not skip_growth_link:
        up = chg > 0
        mech = ("Higher yields can pressure stocks, especially fast-growing technology companies, "
                "because their profits are expected years from now and are worth less when "
                "interest rates are higher."
                if up else
                "Lower yields can help stocks, especially fast-growing technology companies, "
                "because their profits are expected years from now and are worth more when "
                "interest rates are lower.")
        what = "futures" if mode == "morning" else "stocks"
        link = ""
        if tape_dir == "lower" and up:
            link = f" That lines up with {what} falling."
        elif tape_dir == "higher" and up:
            link = f" {what.capitalize()} rose anyway, so buyers outweighed that pressure."
        elif tape_dir == "higher" and not up:
            link = f" That lines up with {what} rising."
        elif tape_dir == "lower" and not up:
            link = f" {what.capitalize()} fell anyway, so other worries outweighed that help."
        sentence += f" {mech}{link}"
        has_chain = True
    elif not meaningful and not skip_growth_link:
        sentence += " Yields barely moved, so interest rates aren't pushing stocks either way."
    return {"text": sentence, "has_chain": has_chain}


def _commodity_sentence(commodities: list, tape_dir: str = None, energy_pct=None) -> dict:
    """
    AT LEAST ONE commodity (oil or gold), always (Step 4c) — whichever moved more,
    ties to oil. A meaningful move (>=1%) gets what it implies for stocks and the
    economy; a tiny one is just described as little changed — it doesn't get an
    inflation story it hasn't earned. Implications are general tendencies ("can
    feed inflation"), never claims about today's tape except via "consistent with".
    Returns {"text", "has_chain"}.
    """
    def _num(c, key):
        try:
            return float(c.get(key, 0) or 0)
        except Exception:
            return 0.0

    oil  = next((c for c in commodities if "crude" in c.get("name", "").lower()
                or "oil" in c.get("name", "").lower()), None)
    gold = next((c for c in commodities if "gold" in c.get("name", "").lower()), None)
    if oil is None and gold is None:
        return {"text": "", "has_chain": False}

    oil_pct  = _num(oil, "pct") if oil else 0.0
    gold_pct = _num(gold, "pct") if gold else 0.0
    pick_oil = oil is not None and (gold is None or abs(oil_pct) >= abs(gold_pct))

    if pick_oil:
        price = _num(oil, "price")
        if abs(oil_pct) < 1.0:
            return {"text": (f"Crude oil was little changed at ${price:,.2f} a barrel, so energy "
                             f"costs aren't adding pressure either way."), "has_chain": False}
        if oil_pct > 0:
            text = (f"Crude oil rose {oil_pct:.2f}% to ${price:,.2f} a barrel. Pricier energy raises "
                    f"costs for airlines and shippers and can feed inflation (rising prices overall), "
                    f"while helping oil producers.")
            if energy_pct is not None:
                text += (" Energy stocks rising is consistent with that." if energy_pct > 0 else
                         " Energy stocks fell anyway, a disconnect we can't explain from the data.")
        else:
            text = (f"Crude oil fell {abs(oil_pct):.2f}% to ${price:,.2f} a barrel. Cheaper energy "
                    f"lowers costs for airlines and shippers and can ease inflation, while squeezing "
                    f"oil producers.")
            if energy_pct is not None:
                text += (" Energy stocks falling is consistent with that." if energy_pct < 0 else
                         " Energy stocks rose anyway, a disconnect we can't explain from the data.")
        return {"text": text, "has_chain": True}

    price = _num(gold, "price")
    if abs(gold_pct) < 1.0:
        return {"text": f"Gold was little changed at ${price:,.0f} an ounce.", "has_chain": False}
    if gold_pct > 0:
        tail = (" — consistent with stocks falling as investors moved toward safer holdings."
                if tape_dir == "lower" else ".")
        text = (f"Gold rose {gold_pct:.2f}% to ${price:,.0f} an ounce. Investors often buy gold as "
                f"a safe place to park money, so a rise can signal caution{tail}")
    else:
        tail = (", consistent with investors feeling comfortable taking more risk in stocks."
                if tape_dir == "higher" else ".")
        text = (f"Gold fell {abs(gold_pct):.2f}% to ${price:,.0f} an ounce. A drop can mean less "
                f"demand for safe places to park money{tail}")
    return {"text": text, "has_chain": True}


# ── Index-divergence explanation (Step 4a) ────────────────────────────────────

_DOW_30_COMPONENTS = {
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
}


def _dow_beat_nasdaq_base(gap: str) -> str:
    return (f"The Dow beat the Nasdaq by {gap}, which suggests the gains were in more traditional, "
            f"economically sensitive companies rather than in technology.")


def _divergence_note(sp: float, ndx: float, dow: float, treasury_chg, sectors: list = None,
                     movers: dict = None, flat: bool = False, rotation_covered: bool = False,
                     skip_yields: bool = False) -> dict:
    """
    Whenever the best-to-worst spread of S&P/Nasdaq/Dow is >=0.3 percentage
    points, explain it — hedged. Mapped only from numbers already in the data
    (yield move, sector map, a Dow member's move); if nothing maps, it says the
    data points to no clear cause instead of inventing one.

    `flat`: on a flat tape a 0.3pp gap is a "slight tilt", not a headline.
    `skip_yields`: a rotation note already made the yields-vs-growth point, so
    this note states only the gap itself instead of repeating it a third time.
    Returns {"text", "addressed_yields", "has_chain"} — addressed_yields tells the
    rates sentence not to repeat the yields point; has_chain marks a real
    observation -> driver -> meaning chain (vs. an honest "no clear cause").
    """
    named = {"S&P 500": sp, "Nasdaq": ndx, "Dow": dow}
    best_name  = max(named, key=lambda k: named[k])
    worst_name = min(named, key=lambda k: named[k])
    spread = named[best_name] - named[worst_name]
    empty = {"text": "", "addressed_yields": False, "has_chain": False}
    if spread < 0.3:
        return empty

    chg = treasury_chg if isinstance(treasury_chg, (int, float)) else 0.0
    yields_up   = chg >= 0.04
    yields_down = chg <= -0.04
    gap = "more than a full percentage point" if spread >= 1.0 else f"{spread:.2f} percentage points"

    def out(text, addressed=False, chain=False):
        return {"text": text, "addressed_yields": addressed, "has_chain": chain}

    growth_down = ("lower rates tend to help fast-growing tech companies most, because their profits "
                   "lie years away and are worth more when rates fall")
    growth_up   = ("higher rates tend to hit fast-growing tech companies hardest, because their profits "
                   "lie years away and are worth less when rates rise")

    # A named Dow member falling sharply is the one concrete, checkable explanation.
    if worst_name == "Dow" and movers:
        for m in (movers.get("losers", []) or []):
            sym = m.get("symbol", "")
            if sym in _DOW_30_COMPONENTS:
                try:
                    mpct = float(m.get("pct") or m.get("changesPercentage") or 0)
                except Exception:
                    continue
                if mpct <= -3.0:
                    return out(f"The Dow trailed by {gap}; {sym}, a Dow member, fell {abs(mpct):.2f}%, "
                               f"which may account for part of that.", chain=True)

    if skip_yields and best_name == "Nasdaq" and worst_name == "Dow" and not flat:
        return out(f"The Nasdaq beat the Dow by {gap}, which suggests the gains were concentrated in "
                   f"large technology companies rather than spread across the whole market.", True, True)
    if skip_yields and best_name == "Dow" and worst_name == "Nasdaq" and not flat:
        return out(_dow_beat_nasdaq_base(gap), True, True)
    if skip_yields and worst_name == "Nasdaq" and not flat:
        return out(f"The Nasdaq trailed by {gap}.", True, False)

    # Dow lagging, Nasdaq/S&P ahead — the most common shape.
    if worst_name == "Dow" and best_name in ("Nasdaq", "S&P 500"):
        if best_name == "Nasdaq":
            if flat:
                lead = "The small tilt toward technology — the Nasdaq is slightly ahead of the Dow —"
                if yields_down:
                    return out(f"{lead} is consistent with falling yields: {growth_down}.", True, True)
                if yields_up:
                    return out(f"{lead} came even though yields rose, which usually pressures tech, "
                               f"so buyers had a slight edge on balance.", True, True)
                return out(f"{lead} isn't explained by interest rates, which were little changed.", True, False)
            base = (f"The Nasdaq beat the Dow by {gap}, which suggests the gains were concentrated in "
                    f"large technology companies rather than spread across the whole market.")
            if yields_down:
                return out(f"{base} That is consistent with the fall in yields: {growth_down}.", True, True)
            if yields_up:
                return out(f"{base} That happened even though yields rose, which usually pressures tech, "
                           f"so buyers sided with technology despite the headwind.", True, True)
            return out(f"{base} Yields were little changed, so interest rates don't explain the gap.", True, True)
        return out(f"The S&P 500 {'was slightly ahead of' if flat else 'beat'} the Dow by {gap}, "
                   f"though nothing in today's data points to a clear reason.")

    # Nasdaq lagging, yields rising — the direct, checkable mapping.
    if worst_name == "Nasdaq" and yields_up:
        lead = ("The small tilt away from technology — the Nasdaq is slightly behind —" if flat
                else f"The Nasdaq trailed by {gap}.")
        joiner = " lines up with the rise in yields:" if flat else " That lines up with the rise in yields:"
        return out(f"{lead}{joiner} {growth_up}.", True, True)

    # Dow ahead alongside cyclical sector leadership (close mode only).
    if best_name == "Dow" and sectors and not rotation_covered:
        pct_map = _sector_pct_map(sectors)
        cyc = [v for v in (_sector_lookup(pct_map, "energy"), _sector_lookup(pct_map, "financial"),
                           _sector_lookup(pct_map, "industrial")) if v is not None]
        if cyc and sum(cyc) / len(cyc) > 0:
            return out("The pattern suggests money moved toward older, cheaper, economically sensitive "
                       "companies — energy, financials, industrials — and away from the richly priced "
                       "technology names that dominate the Nasdaq.", chain=True)

    # Mirror of the Nasdaq-beats-Dow reading above (yields rising is handled earlier).
    if best_name == "Dow" and worst_name == "Nasdaq" and not flat:
        base = _dow_beat_nasdaq_base(gap)
        if yields_down:
            return out(f"{base} Falling yields usually help tech, so rates don't explain it.", True, True)
        return out(f"{base} Yields were little changed, so interest rates don't explain the gap.", True, True)

    if worst_name == "Nasdaq":
        lead = ("The small tilt away from technology — the Nasdaq is slightly behind —" if flat
                else f"The Nasdaq trailed by {gap}.")
        if yields_down:
            return out(f"{lead} Falling yields usually help tech, so rates don't explain it, and "
                       f"nothing else in today's data points to a clear cause." if not flat else
                       f"{lead} comes even though falling yields usually help tech, so rates don't explain it.",
                       True, False)
        return out(f"{lead} With yields little changed, nothing in today's data points to a clear cause."
                   if not flat else f"{lead} isn't explained by interest rates, which were little changed.",
                   True, False)

    return out(f"The {best_name} and {worst_name} split by {gap} today, though nothing in today's "
               f"data points to a clear single cause.")


def _flat_day_why(treasury_chg, oil_pct, gold_pct, events: list, yields_addressed: bool,
                  used_headlines: set, other_chain: bool = False) -> dict:
    """
    A flat or directionless tape still needs a WHY: flat days happen because
    forces offset or because traders are waiting. Names the offsetting forces
    from the data (yields vs oil vs gold), or a qualifying scheduled event they
    may be waiting on. If neither exists it says plainly that this was a quiet,
    low-conviction session — it never invents a driver.
    `other_chain`: the day is already explained by another sentence (a tilt tied
    to yields, a commodity implication), so a one-sided "could have helped but
    wasn't enough" would only repeat it; only a genuinely OFFSETTING pair, a
    waiting-for event, or the plain quiet-session statement is added.
    Returns {"text", "has_chain", "event_used"}.
    """
    chg = treasury_chg if isinstance(treasury_chg, (int, float)) else 0.0
    down, up = [], []   # forces that normally push stocks down / up
    if not yields_addressed:
        if chg >= 0.04:
            down.append("rising yields")
        elif chg <= -0.04:
            up.append("falling yields")
    if isinstance(oil_pct, (int, float)):
        if oil_pct >= 1.5:
            down.append("pricier oil")
        elif oil_pct <= -1.5:
            up.append("cheaper oil")
    if isinstance(gold_pct, (int, float)) and gold_pct >= 1.0:
        down.append("a bid for safe-haven gold")

    parts = []
    if down and up:
        parts.append(f"{' and '.join(down).capitalize()} could weigh on stocks while "
                     f"{' and '.join(up)} could support them, which may help explain why the tape "
                     f"stayed close to flat.")
    elif down and not other_chain:
        parts.append(f"{' and '.join(down).capitalize()} could have weighed on stocks, but it "
                     f"wasn't enough to move the tape.")
    elif up and not other_chain:
        parts.append(f"{' and '.join(up).capitalize()} could have helped stocks, but it wasn't "
                     f"enough to move the tape.")

    event_used = None
    for ev in events or []:
        if ev["scheduled"] is True and ev["headline"].get("title", "") not in used_headlines:
            event_used = ev
            parts.append(f"Traders may be waiting for {_event_phrase(ev)}, flagged in today's headlines.")
            used_headlines.add(ev["headline"].get("title", ""))
            break

    if not parts:
        if other_chain:
            return {"text": "", "has_chain": True, "event_used": None}
        return {"text": ("Nothing in today's data points to a clear driver, so this looks like a "
                         "quiet, low-conviction session."), "has_chain": False, "event_used": None}
    return {"text": " ".join(parts), "has_chain": True, "event_used": event_used}


def _overseas_sentence(global_indices: list, tape_dir: str) -> str:
    """Europe AND Asia both get real numbers, each classified from its own data
    (never by copying the US tape's label), with no "too"/"also" wording that
    assumes an earlier classification."""
    gl = global_indices if isinstance(global_indices, list) else []
    regions = (("Europe", [g for g in gl if g.get("session") == "Europe"]),
               ("Asia", [g for g in gl if g.get("session") == "Asia (overnight)"]))
    parts, dirs, mags = [], [], []
    for name, lst in regions:
        if not lst:
            continue
        try:
            vals = [float(g.get("pct", 0) or 0) for g in lst]
        except Exception:
            continue
        desc = ", ".join(f"{g.get('name', '')} {_pct_words(g.get('pct', 0))}" for g in lst[:2])
        mags.extend(abs(v) for v in vals)
        if all(abs(v) < 0.15 for v in vals):
            parts.append(f"{name} was flat ({desc})"); dirs.append("flat")
            continue
        grp = _classify_group_direction(vals)
        if grp["kind"] in ("directional", "directional_with_laggard"):
            if name == "Europe":
                parts.append(f"Europe is {grp['direction']} ({desc})")
            else:
                parts.append(f"Asia finished {grp['direction']} overnight ({desc})")
            dirs.append(grp["direction"])
        else:
            parts.append(f"{name} was split ({desc})"); dirs.append("split")
    if not parts:
        return ""
    avg = sum(mags) / len(mags) if mags else 0.0
    if all(d == "higher" for d in dirs):
        lead, overseas = ("Overseas was mildly positive" if avg < 0.75 else "Overseas was positive"), "higher"
    elif all(d == "lower" for d in dirs):
        lead, overseas = ("Overseas was mildly negative" if avg < 0.75 else "Overseas was weak"), "lower"
    elif all(d == "flat" for d in dirs):
        lead, overseas = "Overseas was quiet", "flat"
    else:
        lead, overseas = "Overseas was split", "split"
    tail = ""
    if overseas in ("higher", "lower") and tape_dir == overseas:
        tail = ", which lines up with the US open"
    elif overseas in ("higher", "lower") and tape_dir in ("higher", "lower"):
        tail = ", pulling the other way from US futures"
    elif overseas in ("higher", "lower") and tape_dir == "flat" and avg < 0.75:
        tail = ", which is consistent with a calm open"
    return f"{lead} — {' and '.join(parts)}{tail}."

def _rotation_interpretation_note(sector_pct: dict, treasury_chg, oil_pct, oil_price) -> dict:
    """
    Names what a sector pairing MEANS, hedged ("consistent with", "the pattern
    suggests") because sector moves show co-movement, not intent. Checked in
    priority order (most specific pattern first):
      1. Utilities + Real Estate down, Technology up -> rate-sensitive rotation
         into growth (cross-checked against the actual yield move by the rates
         sentence, so a day that contradicts the story says so).
      2. Financials + Industrials + Energy up, Technology down -> cyclical rotation.
      3. Defensives up, everything else down -> investors turning cautious.
      4. Energy alone, tied to (or flagged against) crude.
    Returns {"text", "mentions_rates", "implies_falling_yields", "mentions_commodity"}.
    """
    util   = _sector_lookup(sector_pct, "utilities")
    re_    = _sector_lookup(sector_pct, "real estate")
    tech   = _sector_lookup(sector_pct, "technology")
    fin    = _sector_lookup(sector_pct, "financial")
    ind    = _sector_lookup(sector_pct, "industrial")
    nrg    = _sector_lookup(sector_pct, "energy")
    stap   = _sector_lookup(sector_pct, "staples")
    health = _sector_lookup(sector_pct, "health")

    empty = {"text": "", "mentions_rates": False, "implies_falling_yields": False, "mentions_commodity": False}

    # A rotation claim needs a real gap (>=1 percentage point): a broad rally in which
    # two sectors merely lag is not "money moving out of" them.
    if None not in (util, re_, tech) and util < -_FLAT_BAND and re_ < -_FLAT_BAND and tech > 0 \
       and tech - (util + re_) / 2 >= 1.0:
        text = (f"Utilities ({_pct_words(util)}) and real estate ({_pct_words(re_)}) lagged while "
                f"technology rose {tech:.2f}% — a pairing consistent with investors moving out of "
                f"income-paying, interest-rate-sensitive sectors and into growth. Utilities and real "
                f"estate typically carry heavy debt and pay steady income, which makes them among the "
                f"first things investors sell when the outlook for interest rates shifts.")
        return {"text": text, "mentions_rates": True, "implies_falling_yields": True, "mentions_commodity": False}

    if None not in (fin, ind, nrg, tech) and fin > 0 and ind > 0 and nrg > 0 and tech < 0 \
       and (fin + ind + nrg) / 3 - tech >= 1.0:
        text = (f"Financials ({_pct_words(fin)}), industrials ({_pct_words(ind)}) and energy "
                f"({_pct_words(nrg)}) rose while technology was {_pct_words(tech)} — a combination "
                f"consistent with money moving toward cheaper, older, economically sensitive companies "
                f"and away from richly priced technology names.")
        mentions_commodity = False
        if isinstance(oil_pct, (int, float)) and isinstance(oil_price, (int, float)):
            if oil_pct >= 0:
                text += f" Energy's gain fits crude oil's rise to ${oil_price:,.2f} a barrel ({_pct_words(oil_pct)})."
            else:
                text += (f" Energy rose even though crude oil fell to ${oil_price:,.2f} a barrel "
                         f"({_pct_words(oil_pct)}), a disconnect we can't explain from the data.")
            mentions_commodity = True
        return {"text": text, "mentions_rates": False, "implies_falling_yields": False,
                "mentions_commodity": mentions_commodity}

    defensive_vals = [v for v in (stap, util, health) if v is not None]
    non_defensive_vals = [v for n, v in sector_pct.items()
                          if not any(f in n.lower() for f in ("staples", "utilities", "health"))]
    if defensive_vals and non_defensive_vals and all(v > 0 for v in defensive_vals) \
       and all(v < 0 for v in non_defensive_vals):
        text = ("With defensive sectors — staples, utilities, health care — the only ones higher, "
                "the pattern suggests investors turning cautious rather than chasing risk.")
        return {"text": text, "mentions_rates": False, "implies_falling_yields": False, "mentions_commodity": False}

    if nrg is not None and isinstance(oil_pct, (int, float)) and isinstance(oil_price, (int, float)):
        same = (nrg < 0 and oil_pct < 0) or (nrg > 0 and oil_pct > 0)
        opposite = (nrg < 0 and oil_pct > 0) or (nrg > 0 and oil_pct < 0)
        if same:
            text = (f"Energy stocks {'rose' if nrg > 0 else 'fell'} {abs(nrg):.2f}%, in step with crude oil, "
                    f"which {'rose' if oil_pct > 0 else 'fell'} {abs(oil_pct):.2f}% to ${oil_price:,.2f} a barrel.")
            return {"text": text, "mentions_rates": False, "implies_falling_yields": False, "mentions_commodity": True}
        if opposite:
            text = (f"Energy stocks {'rose' if nrg > 0 else 'fell'} {abs(nrg):.2f}% even though crude oil "
                    f"moved the other way ({_pct_words(oil_pct)} to ${oil_price:,.2f} a barrel), a "
                    f"disconnect we can't explain from the data.")
            return {"text": text, "mentions_rates": False, "implies_falling_yields": False, "mentions_commodity": True}

    return empty


# Headlines are never pasted or quoted. Zero-AI means we can't paraphrase freely, so a
# headline is reduced to the TOPIC it covers, in the brief's own words — what it is
# about ("an analyst price-target change"), never what it concludes or why a stock moved.
# First matching pattern wins; word-boundary matched like every other keyword list here.
_TICKER_HEADLINE_TOPICS = [
    (r"\b(earnings|quarterly results|revenue|profit|eps)\b", "its latest earnings or results"),
    (r"\bupgrade[sd]?\b", "an analyst upgrade"),
    (r"\bdowngrade[sd]?\b", "an analyst downgrade"),
    (r"\bprice targets?\b", "an analyst price-target change"),
    (r"\b(guidance|outlook|forecast)\b", "its financial outlook"),
    (r"\b(acquir\w+|acquisition|merger|takeover|buyout)\b", "an acquisition or merger"),
    (r"\b(lawsuit|sues?|sued|settlement|probe|investigation|antitrust)\b", "legal or regulatory action"),
    (r"\b(fda|approval|approved|recall)\b", "a regulatory decision"),
    (r"\b(launch\w*|unveil\w*|introduc\w+|new product)\b", "a product announcement"),
    (r"\b(layoffs?|job cuts|restructuring)\b", "job cuts or restructuring"),
    (r"\b(buybacks?|dividends?|stock split)\b", "a buyback, dividend or stock split"),
    (r"\b(ceo|cfo|steps? down|resigns?|appoint\w*)\b", "a leadership change"),
    (r"\b(contracts?|partnerships?|deals?|agreements?)\b", "a business deal"),
    (r"\b(slips?|slid\w*|falls?|fell|drops?|dropp\w+|plunge\w*|surges?|surged|jumps?|jumped|soars?|soared|rall\w+)\b",
     "the stock's recent move"),
]

_EVENT_PHRASES = {
    "federal reserve": "a Federal Reserve decision or comment", "fed chair": "a Federal Reserve decision or comment",
    "fomc": "a Fed policy meeting", "rate decision": "an interest-rate decision",
    "rate cut": "a possible interest-rate cut", "rate hike": "a possible interest-rate hike",
    "cpi": "an inflation report", "ppi": "a wholesale-inflation report", "core inflation": "an inflation report",
    "inflation report": "an inflation report", "jobs report": "the jobs report", "payrolls": "the jobs report",
    "unemployment claims": "weekly jobless claims", "treasury auction": "a Treasury bond auction",
    "european central bank": "a European Central Bank decision", "ecb": "a European Central Bank decision",
    "bank of japan": "a Bank of Japan decision", "boj": "a Bank of Japan decision",
    "bank of england": "a Bank of England decision", "central bank": "a central bank decision",
    "fed minutes": "minutes from the last Fed meeting", "policy meeting": "a central bank policy meeting",
    "tariff": "a tariff announcement", "trade deal": "trade-deal news", "trade war": "trade-policy news",
    "trade policy": "trade-policy news", "summit": "a leaders' summit", "state visit": "a foreign leader's visit",
    "foreign leader": "a foreign leader's visit", "election": "an election", "geopolitical": "geopolitical tension",
    "sanctions": "new sanctions", "government shutdown": "a possible government shutdown",
    "opec": "an OPEC decision on oil supply", "oil supply": "oil-supply news", "supply chain": "supply-chain disruption",
    "earnings report": "a major company's earnings report", "quarterly results": "a major company's results",
    "keynote": "a company keynote", "product launch": "a product launch", "unveils": "a product announcement",
    "merger": "a major merger", "acquisition": "a major acquisition", "regulatory approval": "a regulatory decision",
    "antitrust": "an antitrust action", "ftc probe": "a regulatory probe", "sec investigation": "a regulatory probe",
    "lawsuit": "a lawsuit",
}
_EVENT_CATEGORY_FALLBACK = {"MONETARY": "central-bank or economic-data news",
                            "MACRO_POLITICAL": "a policy or geopolitical development",
                            "CORPORATE": "a major company announcement"}


_TOPIC_WINDOW_WORDS = 5   # a topic keyword counts only within this many words of the stock's own name


def _headline_topic(h: dict, field: str = None, symbol: str = "", company: str = "") -> str:
    """
    The topic a ticker-specific headline covers, in the brief's own words, or "" if
    none can be assigned confidently (the caller then says it can't tell what it covers).

    A keyword only counts when it sits within a few words of the stock's own ticker or
    company name — "Walmart earnings beat estimates; MELI also higher" mentions earnings,
    but they're Walmart's, so attributing "its latest earnings" to MELI would be a guess
    presented as fact. When symbol/company are given and no topic keyword is near a
    mention, the answer is "" rather than the nearest keyword anywhere in the headline.
    """
    from_name = _company_short_name(company).lower()
    sym_l = (symbol or "").lower()
    parts = [h.get("title", "") or "", h.get("snippet", "") or ""]
    if field == "snippet":
        parts.reverse()
    for text in parts:
        tl = text.lower()
        if not tl:
            continue
        tokens = [(m.start(), m.group()) for m in re.finditer(r"[a-z0-9&'.-]+", tl)]
        mention_idx = []
        if sym_l or from_name:
            for k, (_, tok) in enumerate(tokens):
                if sym_l and tok.strip(".'-") == sym_l:
                    mention_idx.append(k)
            if from_name:
                first = from_name.split()[0]
                for k, (_, tok) in enumerate(tokens):
                    if tok.strip(".'-") == first:
                        mention_idx.append(k)
            if not mention_idx:
                continue
        for pat, phrase in _TICKER_HEADLINE_TOPICS:
            for m in re.finditer(pat, tl):
                if not mention_idx:
                    return phrase
                kw_idx = sum(1 for st, _ in tokens if st < m.start())
                if any(abs(kw_idx - mi) <= _TOPIC_WINDOW_WORDS for mi in mention_idx):
                    return phrase
    return ""


def _event_phrase(event: dict) -> str:
    return _EVENT_PHRASES.get(event.get("term", ""), _EVENT_CATEGORY_FALLBACK.get(event.get("category", ""), "a market-moving development"))


def _outsized_mover_note(movers: dict, headlines: list, used_headlines: set,
                         earnings_today: set = None, tape_dir: str = None) -> tuple:
    """
    Any mover beyond +/-8% is the most interesting thing in the brief and gets its
    own sentence, ranked by ABSOLUTE magnitude (a +14.56% gainer outranks a -3.26%
    loser). A matching headline is summarized by topic in the brief's own words
    (never quoted, never asserted as the cause); with no ticker-specific headline
    it says the move happened without news we could find. It also says whether the stock is big enough to move an
    index or too small to explain the index move.
    Returns (sentence, symbol_or_None).
    """
    earnings_today = earnings_today or set()
    candidates = []
    for lst in (movers.get("gainers", []) or [], movers.get("losers", []) or []):
        for m in lst:
            try:
                pct = float(m.get("pct") or m.get("changesPercentage") or 0)
            except Exception:
                continue
            if abs(pct) >= 8.0:
                candidates.append((abs(pct), pct, m))
    if not candidates:
        return "", None

    candidates.sort(key=lambda t: t[0], reverse=True)
    _, pct, m = candidates[0]
    sym   = m.get("symbol", "")
    name  = _company_short_name(m.get("name", "")) or sym
    label = f"{name} ({sym})" if name != sym else sym

    if sym in _INDEX_HEAVY:
        move = {"higher": "gain", "lower": "decline"}.get(tape_dir, "move")
        scale = f" It is large enough to move the major indexes, so it may explain part of the market's {move}."
    else:
        scale = " It is one smaller company, so it doesn't explain the index move."

    if sym in earnings_today:
        return (f"{label} was the day's biggest mover, {_pct_words(pct)}, after reporting earnings "
                f"today.{scale}"), sym

    if not headlines:
        return (f"{label} was the day's biggest mover, {_pct_words(pct)}; we couldn't pull "
                f"headlines today, so we can't say whether there was company news.{scale}"), sym

    h, is_specific, field = _find_headline_for_symbol(headlines, sym, m.get("name", ""), sector="",
                                                      exclude=used_headlines)
    if h and is_specific:
        used_headlines.add(h.get("title", ""))
        topic = _headline_topic(h, field, sym, m.get("name", ""))
        what = (f"a same-day headline covers {topic}" if topic else
                "a same-day headline mentions it, though we can't tell what it covers")
        return f"{label} was the day's biggest mover, {_pct_words(pct)}; {what}.{scale}", sym
    return (f"{label} was the day's biggest mover, {_pct_words(pct)}, with no company news we could "
            f"find, which can mean positioning or an analyst call we can't see.{scale}"), sym


def _event_clause(event: dict, mode: str, used_headlines: set) -> str:
    """
    One hedged sentence per qualifying event, naming what the headline is ABOUT in the
    brief's own words (never the headline itself). Positioning is stated as a
    possibility ("may be holding back"), never as a fact — the pipeline can see that
    an event is in the news, not what traders were thinking.
    """
    used_headlines.add(event["headline"].get("title", ""))
    phrase, category = _event_phrase(event), event["category"]
    if event["scheduled"] is True:
        if category == "MONETARY":
            return f"Traders may be holding back ahead of {phrase}, which headlines flag for today."
        if category == "MACRO_POLITICAL":
            return f"Markets may be watching {phrase}, flagged in today's headlines."
        return f"On the calendar today, per the headlines: {phrase}."
    return f"Headlines today point to {phrase}."


def _candidate_cross_reference_note(movers: dict, scan_candidates: list, exclude_syms: set = None) -> str:
    """A big mover that is also on today's fundamentals-scan candidate list.
    Direction-aware: a gainer "ranks #N among today's candidates"; a decliner
    "fell... but still scored N" — the old single template ("...and still scored
    into the candidate list") only made sense for a decliner."""
    exclude_syms = exclude_syms or set()
    ranks = {c.get("ticker"): (i + 1, c.get("score"))
             for i, c in enumerate(scan_candidates or []) if c.get("ticker")}
    for m in (movers.get("gainers", []) or []) + (movers.get("losers", []) or []):
        sym = m.get("symbol", "")
        if not sym or sym in exclude_syms or sym not in ranks:
            continue
        try:
            pct = float(m.get("pct") or m.get("changesPercentage") or 0)
        except Exception:
            continue
        if abs(pct) < 2.0:
            continue
        rank, score = ranks[sym]
        try:
            score_str = f"{float(score):.0f}"
        except Exception:
            score_str = str(score)
        name = _company_short_name(m.get("name", "")) or sym
        label = f"{name} ({sym})" if name != sym else sym
        if pct >= 0:
            return (f"{label} gained {abs(pct):.2f}% and ranks #{rank} among today's "
                    f"candidates (score {score_str}).")
        return (f"{label} fell {abs(pct):.2f}% but still scored {score_str} (ranked #{rank}) among "
                f"today's candidates.")
    return ""


def _day_verb(p: float) -> str:
    if p >= 0:
        return "rose"
    return "slipped" if p > -1 else "fell"


def _holdings_close_sentences(valid_day: list, perf_since: dict, sector_by_ticker: dict,
                              sp: float, ndx: float, headlines: list, used_headlines: set) -> list:
    """Close-mode portfolio: day performance and since-entry performance both
    stated explicitly, plus one hedged chain tying the biggest-moving holding to
    the market (or flagging that it moved against it)."""
    if not valid_day:
        return []
    best_t, best_p   = max(valid_day, key=lambda x: x[1])
    worst_t, worst_p = min(valid_day, key=lambda x: x[1])
    out = []
    if best_t == worst_t:
        since = perf_since.get(best_t)
        tail = f"; since entry it stands at {_pct_signed(since)}" if isinstance(since, (int, float)) else ""
        out.append(f"Your one holding, {best_t}, {_day_verb(best_p)} {abs(best_p):.2f}% on the day{tail}.")
    else:
        bits = [perf_since.get(t) for t in (best_t, worst_t)]
        tail = (f"; since entry they stand at {_pct_signed(bits[0])} and {_pct_signed(bits[1])}"
                if all(isinstance(b, (int, float)) for b in bits) else "")
        out.append(f"Among your holdings, {best_t} {_day_verb(best_p)} {abs(best_p):.2f}% on the day and "
                   f"{worst_t} {_day_verb(worst_p)} {abs(worst_p):.2f}%{tail}.")

    sym, p = max(valid_day, key=lambda x: abs(x[1]))
    if abs(p) >= 0.3:
        h, is_specific, _ = _find_headline_for_symbol(headlines, sym, "", sector="", exclude=used_headlines)
        if not (h and is_specific):
            no_news = "we found no headline explaining it" if headlines else "we couldn't pull headlines today to check"
            sector = (sector_by_ticker.get(sym) or "").lower()
            tech = "tech" in sector
            bench_name, bench = ("Nasdaq", ndx) if tech else ("S&P 500", sp)
            kind = "a technology stock" if tech else "a stock"
            if (p >= 0) == (bench >= 0):
                out.append(f"{sym} is {kind}, so its move is consistent with the {bench_name}'s "
                           f"{'rise' if bench >= 0 else 'drop'} of {abs(bench):.2f}%.")
            else:
                out.append(f"{sym} moved against the {bench_name} ({_pct_words(bench)}), which points to "
                           f"something specific to the company; {no_news}.")
    return out


def _loop_close_note(snapshot_data: list, mem: dict, divergence_spread: float = None,
                     best_index_name: str = None, worst_index_name: str = None) -> str:
    """
    Closes the loop against this morning's call, graded against the S&P 500 only.
      - A FLAT call (S&P within +/-0.15% at the morning call) followed by an S&P
        move beyond +/-0.5% is a miss, reported plainly ("We expected a quiet
        open; stocks rallied instead"), not softened. Within +/-0.5% the quiet
        call held.
      - A directional call is "wrong" only when the S&P closed the opposite way
        by more than 0.15%; a flat finish is "didn't pan out", a same-direction
        finish is held / mostly held / partly held (breadth + conviction).
    Older morning entries without `sp_pct_called` are mapped from the composite
    label (mixed -> a flat call) rather than skipped. Thresholds (0.15 flat call,
    0.5 miss) are the stated spec; the 0.15% "thin move" cutoff for partly held is
    a judgment call.
    A missing morning record is logged loudly, not silently skipped.
    """
    today_iso = _today_ct_iso()
    history = (mem or {}).get("briefing_history", [])
    morning_entry = next(
        (e for e in reversed(history) if e.get("date") == today_iso and e.get("type") == "morning"),
        None,
    )
    if not morning_entry:
        print(f"[LOOP-CLOSE] Skipped — no morning record found for {today_iso}; "
              f"cannot close the loop on this morning's call. If this persists, "
              f"check whether the morning workflow's memory commit is silently failing.")
        return ""

    sp_called = morning_entry.get("sp_pct_called")
    if not isinstance(sp_called, (int, float)):
        sp_called = {"higher": 1.0, "lower": -1.0, "mixed": 0.0}.get(morning_entry.get("direction_called"))
        if sp_called is None:
            return ""

    sp_close = None
    for item in snapshot_data:
        if item.get("name") == "S&P 500":
            try:
                sp_close = float(item.get("pct") or item.get("changesPercentage") or 0)
            except Exception:
                sp_close = None
            break
    if sp_close is None:
        return ""

    breadth_differed = isinstance(divergence_spread, (int, float)) and divergence_spread >= 0.3
    has_names = best_index_name and worst_index_name and best_index_name != worst_index_name

    if abs(sp_called) <= 0.15:
        if abs(sp_close) > 0.5:
            rally = sp_close > 0
            tail = ""
            if breadth_differed and has_names:
                if rally and best_index_name == "Nasdaq":
                    tail = ", led by tech"
                elif rally:
                    tail = f", led by the {best_index_name}"
                elif worst_index_name == "Nasdaq":
                    tail = ", with tech falling hardest"
                else:
                    tail = f", with the {worst_index_name} falling hardest"
            return (f"We expected a quiet open; stocks {'rallied' if rally else 'fell'} instead{tail} — "
                    f"a miss on this morning's call.")
        return (f"We expected a quiet open, and the session stayed fairly calm (the S&P 500 "
                f"{_pct_words(sp_close)}), so that call held.")

    called_sign = "higher" if sp_called >= 0 else "lower"
    actual_sign = "higher" if sp_close >= 0 else "lower"

    if called_sign != actual_sign:
        if abs(sp_close) <= 0.15:
            return (f"We called it {called_sign} this morning, but the S&P 500 finished essentially "
                    f"unchanged, so the call didn't pan out.")
        return (f"We called the tape {called_sign} this morning; the S&P closed "
                f"{actual_sign} instead — that one didn't hold.")

    if not breadth_differed:
        return f"We called the tape {called_sign} this morning, and the S&P held that call through the close."

    if has_names:
        led_name = "tech" if best_index_name == "Nasdaq" else best_index_name
        lag_clause = f", though the gains stayed concentrated in {led_name} while the {worst_index_name} slipped"
    else:
        lag_clause = ", though the move stayed uneven across the market"

    if abs(sp_close) >= 0.15:
        return f"We called it {called_sign} this morning and the S&P delivered{lag_clause}."
    return (f"We called it {called_sign} this morning, and the S&P technically agreed{lag_clause} — "
            f"the move itself was thin enough that this is a partial hold more than a clean one.")


def _build_market_narrative(
    mode: str,
    snapshot_data: list,
    headlines: list = None,
    commodities: list = None,
    treasury: dict = None,
    mem: dict = None,
    earnings: list = None,
    # morning-only inputs
    picks_data: dict = None,
    watchlist_premarket: list = None,
    global_indices: list = None,
    # close-only inputs
    movers: dict = None,
    sectors: list = None,
    picks_day_performance: list = None,
    scan_candidates: list = None,
) -> tuple:
    """
    ONE shared narrative engine for both briefs (mode="morning" / "close").

    The standard it is written to: every paragraph carries at least one cause
    chain (observation -> candidate driver from the data -> what that means for
    stocks), written for a reader with no market background (yield, basis point
    and rotation are all defined where they first appear) — and the pipeline only
    sees co-movement, not causes, so a cause is never stated as fact. Every causal
    sentence is hedged ("consistent with", "lines up with", "may", "can") and
    traceable to a number or headline already in the data; when nothing supports
    a cause the output says so plainly instead of inventing one. A flat tape gets
    a WHY too (offsetting forces, a waiting-for event, or "quiet, low-conviction
    session"). Where two drivers conflict, it says which one the tape sided with.

    P1 layers, in order (mode-specific steps noted):
      1. OPENER — morning: tape from the three indices (flat / directional /
         directional-with-laggard / split); close: sector-breadth classification
         (rotation / broad rally / broad selloff / mixed / flat).
      2. ROTATION INTERPRETATION (close) — what the sector pairing is consistent with.
      3. RATES — always, defined, with trailing context; sits BEFORE the
         divergence note so that note can refer back to it.
      4. INDEX-DIVERGENCE EXPLANATION (>=0.3pp spread; a "slight tilt" on a flat tape).
      5. FLAT-DAY WHY (flat tape) — offsetting forces / waiting-for event / quiet session.
      6. COMMODITY — always at least one; EVENT — at most two, signal-tested.
      7. HANDOFF (morning) — Europe and Asia both with numbers; PATH (close).
    If P1 exceeds its word ceiling, the lowest-priority optional sentences are
    dropped (events first); chain-bearing sentences are never dropped.

    P2 is mode-specific: morning = earnings + holdings (best/worst, since-entry,
    headline intersection or an honest "none in the news"); close = outsized
    mover (own sentence, >=8%), ordinary movers, holdings (day vs since-entry,
    explicitly), candidate cross-reference (direction-aware), LOOP-CLOSE.

    Returns (text, log_data). text is "P1\\n\\nP2". For mode=="close", log_data is
    always {} — close's own learning-loop entry is built in close().
    """
    import hashlib
    from datetime import date as _date

    headlines   = headlines if isinstance(headlines, list) else []
    commodities = commodities if isinstance(commodities, list) else []
    sectors     = sectors if isinstance(sectors, list) else []
    mem         = mem if isinstance(mem, dict) else {}
    used_headlines = set()  # nothing gets quoted twice in one summary

    idx = {}
    for item in snapshot_data:
        name = item.get("name", "")
        try:
            idx[name] = float(item.get("pct") or item.get("changesPercentage") or 0)
        except Exception:
            idx[name] = 0.0

    sp, ndx, dow = idx.get("S&P 500", 0.0), idx.get("Nasdaq", 0.0), idx.get("Dow", 0.0)
    tape_tone = _classify_direction(snapshot_data)  # legacy composite label, kept for the stored log field
    named = {"S&P 500": sp, "Nasdaq": ndx, "Dow": dow}
    best_index_name  = max(named, key=lambda k: named[k])
    worst_index_name = min(named, key=lambda k: named[k])
    divergence_spread = named[best_index_name] - named[worst_index_name]

    flat  = all(abs(v) <= _FLAT_BAND for v in (sp, ndx, dow))
    group = _classify_group_direction([sp, ndx, dow])
    if flat:
        tape_dir = "flat"
    elif group["kind"] in ("directional", "directional_with_laggard"):
        tape_dir = group["direction"]
    else:
        tape_dir = "mixed"

    day_hash = int(hashlib.md5(_date.today().isoformat().encode()).hexdigest(), 16)

    treasury_chg = None
    if treasury and treasury.get("yield") is not None:
        try:
            treasury_chg = float(treasury.get("change", 0) or 0)
        except Exception:
            treasury_chg = None

    def _cnum(c, key):
        try:
            return float(c.get(key, 0) or 0)
        except Exception:
            return 0.0

    oil_c  = next((c for c in commodities if "crude" in c.get("name", "").lower()
                  or "oil" in c.get("name", "").lower()), None)
    gold_c = next((c for c in commodities if "gold" in c.get("name", "").lower()), None)
    oil_pct   = _cnum(oil_c, "pct") if oil_c else None
    oil_price = _cnum(oil_c, "price") if oil_c else None
    gold_pct  = _cnum(gold_c, "pct") if gold_c else None

    items = []          # (priority, text) — priority 1 = chain/core (never trimmed), 2-3 = optional
    chain_found = False

    def add(text, pri):
        if text:
            items.append((pri, text))

    # ── 1. OPENER ──────────────────────────────────────────────────────────
    if mode == "close":
        close_tape = _classify_close_tape(sp, ndx, dow, sectors)
        trio = _index_trio_words(sp, ndx, dow)
        if close_tape["kind"] == "rotation":
            dow_word = "closed flat" if abs(dow) < 0.10 else f"closed {'up' if dow >= 0 else 'down'} ({_fmt(dow)})"
            ndx_verb = "gave up" if ndx < 0 else "gained"
            opener = (f"Not a sell-off — a rotation, meaning money moved between parts of the market "
                      f"rather than leaving it. The Dow {dow_word} while the Nasdaq {ndx_verb} "
                      f"{abs(ndx):.2f}%, and {close_tape['up_count']} of {close_tape['total']} sectors "
                      f"finished higher.")
            out_txt = _join_sector_moves(close_tape["down_sectors"][:3])
            in_txt  = _join_sector_moves(close_tape["up_sectors"][:2])
            if out_txt and in_txt:
                opener += f" Money left {out_txt} for {in_txt}."
        elif close_tape["kind"] in ("broad_rally", "broad_selloff"):
            rally = close_tape["kind"] == "broad_rally"
            n = close_tape["up_count"] if rally else close_tape["down_count"]
            opener = (f"Stocks {'rallied' if rally else 'sold off'} broadly today — {n} of "
                      f"{close_tape['total']} sectors {'rose' if rally else 'fell'}, with {trio}. "
                      f"With so many sectors moving together, this was a wide "
                      f"{'rise' if rally else 'decline'} rather than a few big companies "
                      f"{'carrying' if rally else 'dragging'} the index.")
        elif flat:
            opener = (f"Stocks ended close to unchanged today — {trio} — with {close_tape['up_count']} "
                      f"of {close_tape['total']} sectors higher.")
        else:
            best_n, best_p   = close_tape["best_sector"]
            worst_n, worst_p = close_tape["worst_sector"]
            if best_n and (not worst_n or abs(best_p) >= abs(worst_p)):
                driver = f", led by {best_n} ({_fmt(best_p)})"
            elif worst_n:
                driver = f", dragged by {worst_n} ({_fmt(worst_p)})"
            else:
                driver = ""
            opener = f"The tape was mixed today — {trio}{driver}."
        add(opener, 0)
    else:
        add(_morning_opener(sp, ndx, dow, group, day_hash), 0)

    # ── 2. ROTATION INTERPRETATION (close) ─────────────────────────────────
    rotation = {"text": "", "mentions_rates": False, "implies_falling_yields": False, "mentions_commodity": False}
    if mode == "close":
        rotation = _rotation_interpretation_note(_sector_pct_map(sectors), treasury_chg, oil_pct, oil_price)
        if rotation["text"]:
            add(rotation["text"], 1)
            chain_found = True

    # ── 3/4. RATES (before) + DIVERGENCE (after, can refer back) ────────────
    divergence = _divergence_note(sp, ndx, dow, treasury_chg,
                                  sectors=sectors if mode == "close" else None,
                                  movers=movers if mode == "close" else None,
                                  flat=flat, rotation_covered=bool(rotation["text"]),
                                  skip_yields=rotation["mentions_rates"])
    rotation_context = ({"wants_rates_crosscheck": True,
                         "implies_falling_yields": rotation["implies_falling_yields"]}
                        if rotation["mentions_rates"] else None)
    rates = _rates_sentence(treasury, mode, rotation_context=rotation_context,
                            skip_growth_link=divergence["addressed_yields"], tape_dir=tape_dir)
    add(rates["text"], 1)
    chain_found = chain_found or rates["has_chain"]
    add(divergence["text"], 1)
    chain_found = chain_found or divergence["has_chain"]

    # ── events (mined once; flat-day WHY may consume a scheduled one) ───────
    events = _extract_market_events(headlines, mode, max_events=2)

    # ── 5. COMMODITY (>=1) ──────────────────────────────────────────────────
    commodity = {"text": "", "has_chain": False}
    if not rotation["mentions_commodity"]:
        energy_pct = _sector_lookup(_sector_pct_map(sectors), "energy") if mode == "close" else None
        commodity = _commodity_sentence(commodities, tape_dir=tape_dir, energy_pct=energy_pct)
        add(commodity["text"], 1)
        chain_found = chain_found or commodity["has_chain"]

    # Gold moving against stocks (up while stocks fall, down while they rise) is its
    # own signal; mention it even when oil already filled the commodity slot.
    if gold_c is not None and tape_dir in ("lower", "higher") and not commodity["text"].startswith("Gold") \
       and ((gold_pct >= 1.0 and tape_dir == "lower") or (gold_pct <= -1.0 and tape_dir == "higher")):
        gold_extra = _commodity_sentence([gold_c], tape_dir=tape_dir)
        add(gold_extra["text"], 2)
        chain_found = chain_found or gold_extra["has_chain"]

    # ── 6. FLAT-DAY WHY + EVENTS (<=2) ──────────────────────────────────────
    if flat:
        why = _flat_day_why(treasury_chg, oil_pct, gold_pct, events,
                            divergence["addressed_yields"], used_headlines,
                            other_chain=divergence["has_chain"] or commodity["has_chain"] or rotation["mentions_commodity"])
        add(why["text"], 1)
        chain_found = chain_found or why["has_chain"]
    for ev in events:
        if ev["headline"].get("title", "") in used_headlines:
            continue
        add(_event_clause(ev, mode, used_headlines), 3)
        if ev["scheduled"] is True:
            chain_found = True

    # ── 7. HANDOFF (morning) / PATH (close) ─────────────────────────────────
    if mode == "morning":
        add(_overseas_sentence(global_indices, tape_dir), 2)
    else:
        add(_path_note(snapshot_data), 2)

    # A directional day with no supported driver says so instead of guessing.
    if not chain_found and not flat and not divergence["text"]:
        add("Nothing in today's data stands out as a clear driver of the move, so we won't guess at one.", 1)

    # ── P1 assembly with a word ceiling ─────────────────────────────────────
    _, hi = _WORD_COUNT_TARGETS[(mode, "P1")]
    trimmed = []
    while sum(len(t.split()) for _, t in items) > hi:
        droppable = [i for i, (p, _) in enumerate(items) if p >= 2]
        if not droppable:
            break
        worst = max(droppable, key=lambda i: (items[i][0], i))
        items.pop(worst); trimmed.append(1)
    p1_text = " ".join(t for _, t in items)
    if trimmed:
        print(f"[LENGTH] {mode.upper()} P1 exceeded {hi} words; dropped {len(trimmed)} optional sentence(s).")

    macro_theme_for_log = "rate expectations" if rates["text"] else ("commodities" if commodity["text"] else "")
    if mode == "morning":
        recurring = _get_recurring_theme(mem, window=5, threshold=3)
        if recurring and recurring != macro_theme_for_log:
            p1_text += f" (Note: {recurring} has been a persistent theme over the past week.)"

    _log_word_count(mode, "P1", p1_text, diagnostics={
        "divergence >= 0.3pp": bool(divergence["text"]),
        "rotation pattern matched": bool(rotation["text"]),
        "qualifying event found": bool(events),
    })

    # Holdings' sectors (from the performance history) feed the P2 chains.
    perf_since = {ph.get("ticker"): ph.get("pct_change_since_pick")
                  for ph in mem.get("pick_performance_history", [])}
    sector_by_ticker = {ph.get("ticker"): ph.get("sector", "") for ph in mem.get("pick_performance_history", [])}

    p2_sentences = []

    # ── P2 morning ──────────────────────────────────────────────────────────
    if mode == "morning":
        today_iso       = _today_ct_iso()
        earnings_list   = earnings if isinstance(earnings, list) else []
        todays_earnings = [e for e in earnings_list if e.get("date", "") == today_iso and e.get("symbol")]
        if todays_earnings:
            parts = []
            for e in todays_earnings[:3]:
                eps = e.get("eps_estimated")
                try:
                    parts.append(f"{e['symbol']} (analysts expect ${float(eps):.2f} per share)")
                except Exception:
                    parts.append(e["symbol"])
            verb = "reports" if len(parts) == 1 else "report"
            p2_sentences.append(f"{', '.join(parts)} {verb} earnings today. Results can move a stock "
                                f"sharply because they show how profitable a company really is.")

        picks    = picks_data.get("picks", []) if isinstance(picks_data, dict) else []
        changes  = picks_data.get("changes_from_last_week", []) if isinstance(picks_data, dict) else []
        enriched = _enrich_picks_with_perf(picks, mem) if picks else []

        featured = None  # (pick, headline, field)
        for p in enriched:
            sym, name = p.get("ticker", ""), p.get("company", "")
            if not sym:
                continue
            h, is_specific, field = _find_headline_for_symbol(headlines, sym, name, sector="", exclude=used_headlines)
            if h and is_specific:
                pct = p.get("pct_change_since_pick")
                pct_val = pct if isinstance(pct, (int, float)) else 0.0
                if featured is None or pct_val < featured[0].get("pct_change_since_pick", 0.0):
                    featured = (p, h, field)

        if picks:
            n = len(picks)
            valid = [(p.get("ticker", ""), p.get("pct_change_since_pick")) for p in enriched
                     if isinstance(p.get("pct_change_since_pick"), (int, float))]
            if changes:
                p2_sentences.append(f"{len(changes)} of your {n} picks rotated this week — details below.")
            elif valid:
                best  = max(valid, key=lambda t: t[1])
                worst = min(valid, key=lambda t: t[1])
                lead = (f"Your {n} holdings are unchanged this week" if n != 1
                        else "Your one holding is unchanged this week")
                if best[0] == worst[0]:
                    sentence = f"{lead}: {best[0]} is {'up' if best[1] >= 0 else 'down'} {abs(best[1]):.1f}% since entry."
                else:
                    sentence = (f"{lead}: {best[0]} is {'up' if best[1] >= 0 else 'down'} {abs(best[1]):.1f}% "
                                f"since entry and {worst[0]} is {'up' if worst[1] >= 0 else 'down'} "
                                f"{abs(worst[1]):.1f}%.")
                if featured:
                    fp, fh, ffield = featured
                    topic = _headline_topic(fh, ffield, fp.get("ticker", ""), fp.get("company", ""))
                    sentence += (f" {fp.get('ticker', '')} is in today's headlines, which cover {topic}." if topic else
                                 f" {fp.get('ticker', '')} is mentioned in today's headlines, though we can't tell what they cover.")
                elif not headlines:
                    sentence += " We couldn't pull headlines today, so we can't check whether any of your holdings is in the news."
                else:
                    sentence += (" None of your holdings is in today's headlines, so any move today likely "
                                 "tracks the broader market rather than company news.")
                p2_sentences.append(sentence)
            else:
                p2_sentences.append(f"Your {n} holdings are unchanged this week.")

        text = p1_text + ("\n\n" + " ".join(p2_sentences) if p2_sentences else "")
        _log_word_count(mode, "P2", " ".join(p2_sentences), diagnostics={
            "earnings scheduled today": bool(todays_earnings),
            "headline intersection found": bool(featured),
        })

        ldr_name = max(named, key=lambda k: abs(named[k]))
        lag_name = min(named, key=lambda k: named[k])
        log_data = {
            "type":             "morning",
            "direction_called": tape_tone,
            "sp_pct_called":    sp,
            "leading_index":    ldr_name if named[ldr_name] >= 0 else "",
            "lagging_index":    lag_name if named.get(lag_name, 0) < 0 else "",
            "headline_theme":   macro_theme_for_log,
            "commodity_note":   commodity["text"],
            "picks_status":     "rotated" if changes else "holding",
        }
        return text, log_data

    # ── P2 close ────────────────────────────────────────────────────────────
    today_iso      = _today_ct_iso()
    earnings_list  = earnings if isinstance(earnings, list) else []
    earnings_today = {e.get("symbol", "") for e in earnings_list if e.get("date", "") == today_iso}
    movers = movers if isinstance(movers, dict) else {}
    gainers, losers = movers.get("gainers", []), movers.get("losers", [])

    outsized_sentence, outsized_sym = _outsized_mover_note(movers, headlines, used_headlines,
                                                           earnings_today, tape_dir=tape_dir)
    p2_items = []   # (priority, text): 1 = core, 2-4 = optional, trimmed highest-number first

    def add_p2(text, pri):
        if text:
            p2_items.append((pri, text))

    if outsized_sentence:
        add_p2(outsized_sentence, 1)

    def _mover_clause(m: dict, kind: str) -> str:
        if not m or m.get("symbol", "") == outsized_sym:
            return ""
        try:
            pct = float(m.get("pct") or m.get("changesPercentage") or 0)
        except Exception:
            return ""
        if abs(pct) < 2.0:
            return ""
        sym = m.get("symbol", "")
        word = "led" if kind == "best" else "was the day's weakest"
        lead = f"{sym} {word}, {_pct_words(pct)}"
        if sym in earnings_today:
            return f"{lead}, after reporting earnings today"
        h, is_specific, field = _find_headline_for_symbol(
            headlines, sym, m.get("name", ""), sector="", exclude=used_headlines,
        )
        if h and is_specific:
            used_headlines.add(h.get("title", ""))
            topic = _headline_topic(h, field, sym, m.get("name", ""))
            return f"{lead} (a same-day headline covers {topic})" if topic else lead
        return lead

    mover_bits = [c for c in [
        _mover_clause(losers[0] if losers else None, "worst"),
        _mover_clause(gainers[0] if gainers else None, "best"),
    ] if c]
    if mover_bits:
        add_p2("; ".join(mover_bits) + ".", 3)

    valid_day = [(p.get("ticker", ""), p.get("pct")) for p in (picks_day_performance or [])
                 if p.get("ticker") and isinstance(p.get("pct"), (int, float))]
    for k, hs in enumerate(_holdings_close_sentences(valid_day, perf_since, sector_by_ticker, sp, ndx,
                                                     headlines, used_headlines)):
        add_p2(hs, 1 if k == 0 else 4)

    mover_syms = {m.get("symbol", "") for m in (gainers[:1] + losers[:1])}
    candidate_note = _candidate_cross_reference_note(movers, scan_candidates, exclude_syms=mover_syms)
    if candidate_note:
        add_p2(candidate_note, 2)

    loop_close_note = _loop_close_note(snapshot_data, mem, divergence_spread=divergence_spread,
                                       best_index_name=best_index_name, worst_index_name=worst_index_name)
    if loop_close_note:
        add_p2(loop_close_note, 1)

    _, hi2 = _WORD_COUNT_TARGETS[(mode, "P2")]
    while sum(len(t.split()) for _, t in p2_items) > hi2:
        droppable = [i for i, (p, _) in enumerate(p2_items) if p >= 2]
        if not droppable:
            break
        p2_items.pop(max(droppable, key=lambda i: (p2_items[i][0], i)))
    p2_sentences = [t for _, t in p2_items]

    text = p1_text + ("\n\n" + " ".join(p2_sentences) if p2_sentences else "")
    _log_word_count(mode, "P2", " ".join(p2_sentences), diagnostics={
        "outsized mover (>=8%)": bool(outsized_sentence),
        "candidate cross-reference": bool(candidate_note),
        "portfolio day-performance data": bool(valid_day),
        "LOOP-CLOSE note": bool(loop_close_note),
    })
    return text, {}


def _morning_summary_html(
    snapshot_data: list,
    headlines: list,
    picks_data: dict,
    commodities: list = None,
    treasury: dict = None,
    mem: dict = None,
    earnings: list = None,
    watchlist_premarket: list = None,
    global_indices: list = None,
) -> tuple:
    """Returns (html_str, log_data). Renders as two separate <p> tags — P1
    (why premarket is moving) and P2 (what's ahead + portfolio intersection)."""
    text, log_data = _build_market_narrative(
        "morning", snapshot_data, headlines=headlines, commodities=commodities,
        treasury=treasury, mem=mem, earnings=earnings, picks_data=picks_data,
        watchlist_premarket=watchlist_premarket, global_indices=global_indices,
    )
    paragraphs = text.split("\n\n")
    inner = "".join(
        f'<p style="margin:0 0 12px;font-size:16px;color:#1f2937;line-height:1.6">{p}</p>'
        if i < len(paragraphs) - 1 else
        f'<p style="margin:0;font-size:16px;color:#1f2937;line-height:1.6">{p}</p>'
        for i, p in enumerate(paragraphs)
    )
    # Tinted background — this is the point of the email, so it reads as
    # visually distinct from the plain-white data sections beneath it, not
    # just another entry in the list.
    html = _section("What's Going On", inner, bg="#f8fafc")
    return html, log_data


# Sector-family keywords for headline-based causal matching. Word-boundary matched
# against headline title+snippet — same false-match-safe pattern as _MACRO_KEYWORDS.
# Kept as explicit plural/compound variants rather than suffix-wildcard regex —
# wildcarding a short root (e.g. "war" -> "war\w*") would reopen the exact
# false-match bug class already fixed elsewhere ("war" inside "warehouse").
# Each variant here is still a full \b-bounded word, just spelled out.
_SECTOR_HEADLINE_KEYWORDS = {
    "energy":        ["oil", "crude", "opec", "energy prices", "natural gas", "oil prices"],
    "technology":    ["chip", "chips", "chipmaker", "chipmakers", "semiconductor", "semiconductors",
                      "ai stocks", "ai", "artificial intelligence", "cloud", "cloud computing",
                      "software", "software stocks", "cybersecurity", "cyberattack", "big tech",
                      "tech selloff", "tech rally", "downgrade", "guidance cut",
                      "data center", "data centers"],
    "financial":     ["bank", "banks", "banking", "rate cut", "rate hike", "yield", "fed",
                      "lender", "lenders"],
    "health":        ["fda", "drug", "drugs", "biotech", "trial", "trials", "recall",
                      "pharma", "pharmaceutical"],
    "real estate":   ["mortgage rate", "mortgage rates", "housing", "homebuilder", "homebuilders"],
    "utilities":     ["rate cut", "rate hike", "power grid", "electricity prices"],
    "consumer":      ["retail sales", "consumer spending", "holiday sales", "retailer", "retailers"],
    "industrial":    ["manufacturing", "factory", "factories", "supply chain", "tariff", "tariffs"],
    "material":      ["commodity prices", "metals", "mining"],
    "communication": ["streaming", "advertising", "media", "telecom"],
}


def _sector_family(name: str) -> str:
    n = (name or "").lower()
    for fam in _SECTOR_HEADLINE_KEYWORDS:
        if fam in n:
            return fam
    return ""


def _find_headline_for_keywords(headlines: list, keywords: list, exclude: set = None):
    """
    Returns (headline, field) — field is "title" or "snippet", whichever actually
    contained the matching keyword. Matching against title+snippet combined but
    then citing a default field (e.g. snippet-first) can quote an unrelated part
    of the same headline object; tracking the real match location avoids that.

    exclude, if given, is a set of headline titles already cited elsewhere in the
    same summary — skipped so the same headline can't be pasted twice (or three
    times) into one output.
    """
    exclude = exclude or set()
    for h in headlines or []:
        title = h.get("title", "") or ""
        if title in exclude:
            continue
        title_l   = title.lower()
        snippet_l = (h.get("snippet", "") or "").lower()
        for kw in keywords:
            pat = r'\b' + re.escape(kw.lower()) + r'\b'
            if re.search(pat, title_l):
                return h, "title"
            if re.search(pat, snippet_l):
                return h, "snippet"
    return None, None


# Tickers that are also common English words. A bare match on these ("WALL STREET RALLIES NOW")
# is not evidence a headline is about the company, so only the company name counts.
_COMMON_WORD_TICKERS = {"NOW", "ON", "IT", "A", "ALL", "ARE", "FOR", "HAS", "ONE", "BIG", "FAST", "WELL",
                        "REAL", "TRUE", "GOOD", "LOVE", "CARE", "OPEN", "PLAY", "WORK", "TECH", "EAT"}

_CORP_SUFFIXES = {"corporation", "corp", "inc", "holdings", "holding", "co", "ltd", "plc", "company", "group"}


def _company_short_name(name: str) -> str:
    """Strip trailing corporate suffixes (\"AppLovin Corporation\" -> \"AppLovin\")
    so headline matching isn't defeated by the formal legal name."""
    tokens = [t.strip(",.") for t in (name or "").split()]
    while tokens and tokens[-1].lower().strip(".") in _CORP_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _find_headline_for_symbol(headlines: list, symbol: str, company_name: str = "", sector: str = "",
                              exclude: set = None):
    """
    Returns (headline, is_specific, field). is_specific=True means the headline
    names this exact ticker or company. If no literal match exists, falls back to
    a sector-category match (e.g. a "cybersecurity stocks" headline for a
    cybersecurity-sector mover with no ticker of its own in the text) — still
    real signal, just less specific, so the caller can phrase it honestly. field
    tracks whether the match landed in the title or snippet, so citation quotes
    the part that actually matched.

    exclude, if given, is a set of headline titles already cited elsewhere in
    the same summary — skipped so the same headline never gets pasted twice.
    """
    exclude = exclude or set()
    if symbol:
        pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')  # case-sensitive — avoids "app"/"APP" false hits
        name_l  = _company_short_name(company_name).lower()
        if symbol in _COMMON_WORD_TICKERS:
            # NOW / ON / IT are also ordinary English (and an all-caps headline defeats the
            # case-sensitivity above), so the bare ticker proves nothing: require the company name.
            pattern = re.compile(r"(?!x)x")
        for h in headlines or []:
            title, snippet = h.get("title", "") or "", h.get("snippet", "") or ""
            if title in exclude:
                continue
            if pattern.search(title) or (name_l and name_l in title.lower()):
                return h, True, "title"
            if pattern.search(snippet) or (name_l and name_l in snippet.lower()):
                return h, True, "snippet"

    fam = _sector_family(sector)
    if fam:
        h, field = _find_headline_for_keywords(headlines, _SECTOR_HEADLINE_KEYWORDS[fam], exclude=exclude)
        if h:
            return h, False, field

    return None, False, None


def _classify_close_tape(sp: float, ndx: float, dow: float, sectors: list) -> dict:
    """
    Classifies today's close by SECTOR BREADTH, not bare index sign — three
    tiny-but-uniformly-negative indices (Dow -0.01%, S&P -0.24%, Nasdaq -0.52%)
    is not the same event as 8+ of 11 sectors actually falling. Returns kind
    plus the supporting numbers needed to compose the opening sentence.
    """
    sector_vals = []
    for s in sectors or []:
        try:
            pct = float(s.get("pct") if s.get("pct") is not None else s.get("changesPercentage", 0))
        except Exception:
            continue
        sector_vals.append((s.get("sector", ""), pct))

    total        = len(sector_vals)
    up_sectors   = sorted([(n, p) for n, p in sector_vals if p >= 0], key=lambda x: -x[1])
    down_sectors = sorted([(n, p) for n, p in sector_vals if p < 0], key=lambda x: x[1])
    up_count, down_count = len(up_sectors), len(down_sectors)

    if sector_vals:
        best_sector  = max(sector_vals, key=lambda x: x[1])
        worst_sector = min(sector_vals, key=lambda x: x[1])
        spread = best_sector[1] - worst_sector[1]
    else:
        best_sector = worst_sector = ("", 0.0)
        spread = 0.0

    if total >= 8 and (up_count >= 8 or down_count >= 8):
        kind = "broad_rally" if up_count >= 8 else "broad_selloff"
    elif total > 0 and abs(up_count - down_count) <= 2 and spread >= 1.5:
        kind = "rotation"
    else:
        kind = "mixed"

    return {
        "kind": kind, "up_sectors": up_sectors, "down_sectors": down_sectors,
        "best_sector": best_sector, "worst_sector": worst_sector,
        "spread": spread, "up_count": up_count, "down_count": down_count, "total": total,
    }


def _join_sector_moves(pairs: list) -> str:
    parts = [f"{n} ({_fmt(p)})" for n, p in pairs if n]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _path_note(snapshot_data: list) -> str:
    """
    Describes the S&P's intraday shape from open/high/low/close (and, when a
    prior close is available, from the open/close gap against yesterday too)
    — a session that gapped down and recovered reads very differently from
    one that faded into the close, even on an identical closing print.

    Five categories, checked in this priority order (highest first) — more
    than one can technically be true on the same day, and the ordering below
    is a deliberate editorial call, not just detection order:

      1. reversal      — opened on one side of yesterday's close and closed
                          on the other (crossed intraday). Ranked first
                          because it's the most informative shape a session
                          can take: "the S&P closed up 0.3%" reads completely
                          differently once you know it opened down 0.6% and
                          clawed all the way back, versus opening up 0.9% and
                          giving most of it away. Threshold: the open-gap AND
                          the close-move must each be >=0.15% of yesterday's
                          close, on opposite sides of it — big enough to be a
                          real directional swing, not two closing prints a
                          few cents apart that happen to round to opposite
                          signs either side of flat.
      2. gap-and-hold  — opened away from yesterday's close by a real margin
                          (>=0.5%, the same "meaningful move" bar this file
                          already uses for RATES/OIL/GOLD) and held at least
                          half of that gap into the close, same direction as
                          the open. This is what distinguishes a session that
                          gapped and never looked back from one that gapped
                          and gave it all back intraday (which shows up as
                          choppy or faded below instead, not gap-and-hold).
      3. strong finish — closed in the top 15% of the day's own range, on a
                          range that's at least 0.5% of the index.
      4. faded close   — closed in the bottom 15% of the day's own range,
                          same 0.5% range floor.
      5. choppy range  — day's range was at least 1.5% with no clean
                          top/bottom finish.

    #1/#2 need yesterday's close and fall through to #3-5 (the original three
    categories) when it isn't available.
    """
    sp_item = next((s for s in snapshot_data if s.get("name") == "S&P 500"), None)
    if not sp_item:
        return ""
    o, h, l, c = sp_item.get("open"), sp_item.get("day_high"), sp_item.get("day_low"), sp_item.get("price")
    if not all(isinstance(v, (int, float)) for v in (o, h, l, c)) or h == l:
        return ""

    prev = sp_item.get("previous_close")
    if isinstance(prev, (int, float)) and prev:
        open_gap_pct   = (o - prev) / prev * 100
        close_move_pct = (c - prev) / prev * 100
        # 1. REVERSAL — crossed yesterday's close intraday.
        if (open_gap_pct <= -0.15 and close_move_pct >= 0.15) or \
           (open_gap_pct >= 0.15 and close_move_pct <= -0.15):
            if open_gap_pct < 0:
                return ("The S&P opened in the red and clawed back to close green — "
                        "a full reversal off yesterday's close.")
            return ("The S&P opened in the green and slid to close red — "
                    "a full reversal off yesterday's close.")
        # 2. GAP-AND-HOLD — opened away by a real margin, held direction into the close.
        if abs(open_gap_pct) >= 0.5 and (open_gap_pct > 0) == (close_move_pct > 0) \
           and abs(close_move_pct) >= abs(open_gap_pct) * 0.5:
            direction = "higher" if open_gap_pct > 0 else "lower"
            return (f"The S&P gapped {direction} at the open and held it, "
                    f"closing {_fmt(close_move_pct)} from yesterday's close.")

    close_pos     = (c - l) / (h - l)
    day_range_pct = (h - l) / l * 100 if l else 0
    if close_pos >= 0.85 and day_range_pct >= 0.5:
        return "The S&P closed near its highs of the day, a strong finish into the bell."
    if close_pos <= 0.15 and day_range_pct >= 0.5:
        return "The S&P closed near its lows of the day, fading into the bell."
    if day_range_pct >= 1.5:
        return f"It was a choppy session — the S&P swung a {day_range_pct:.1f}% range before settling."
    return ""


def _close_summary_html(
    snapshot_data: list,
    movers: dict,
    sectors: list,
    commodities: list = None,
    treasury: dict = None,
    earnings: list = None,
    headlines: list = None,
    picks_day_performance: list = None,
    scan_candidates: list = None,
    mem: dict = None,
) -> str:
    text, _ = _build_market_narrative(
        "close", snapshot_data, headlines=headlines, commodities=commodities,
        treasury=treasury, mem=mem, earnings=earnings, movers=movers, sectors=sectors,
        picks_day_performance=picks_day_performance, scan_candidates=scan_candidates,
    )
    paragraphs = text.split("\n\n")
    inner = "".join(
        f'<p style="margin:0 0 12px;font-size:16px;color:#1f2937;line-height:1.6">{p}</p>'
        if i < len(paragraphs) - 1 else
        f'<p style="margin:0;font-size:16px;color:#1f2937;line-height:1.6">{p}</p>'
        for i, p in enumerate(paragraphs)
    )
    return _section("What Happened Today", inner, bg="#f8fafc")


# ── Email assemblers ──────────────────────────────────────────────────────────

async def morning(session: ClientSession) -> tuple[str, str]:
    today     = _today_ct()
    today_str = today.strftime("%A, %B %d")
    is_monday = today.weekday() == 0

    print("    fetching snapshot + global + commodities…")
    snapshot, global_idx, commodities_raw, treasury_raw, headlines, econ, earnings, mem, scan_raw = \
        await asyncio.gather(
            call(session, "fetch_market_snapshot"),
            call(session, "fetch_global_indices"),
            call(session, "fetch_commodities"),
            call(session, "fetch_treasury_yield"),
            call(session, "fetch_top_headlines"),
            call(session, "fetch_economic_calendar"),
            call(session, "fetch_earnings_calendar"),
            call(session, "load_memory"),
            call(session, "run_daily_scan"),
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

    snap_list   = snapshot.get("data", [])
    hl_list     = headlines.get("headlines", [])
    global_list = global_idx.get("indices", [])
    comm_list   = commodities_raw.get("commodities", [])
    tsy         = treasury_raw if treasury_raw.get("yield") else {}

    morning_section, log_data = _morning_summary_html(
        snap_list, hl_list, picks_data, comm_list, tsy,
        mem if isinstance(mem, dict) else {},
        earnings.get("earnings", []),
        watchlist,
        global_list,
    )
    body = (
        morning_section
        + _indices(snap_list)
        + _global_indices(global_list)
        + _commodities_and_yields(comm_list, tsy)
        + _headlines(hl_list)
        + _calendar(econ.get("events", []), earnings.get("earnings", []),
                    econ_failed=econ.get("source") == "unavailable")
        + _watchlist(watchlist, "Your Watchlist — Pre-Market")
        + _unified_picks(_enrich_picks_with_perf(picks_data.get("picks", []),
                                                 mem if isinstance(mem, dict) else {}),
                        scan_raw.get("candidates", []),
                        week=picks_data.get("week", ""),
                        changes=picks_data.get("changes_from_last_week", []),
                        scanned=scan_raw.get("scanned", 0),
                        elapsed=scan_raw.get("elapsed_s", 0))
    )
    subject = f"Pippy's Brief — {today_str} Morning Briefing"
    html    = _wrap(body, f"Morning Briefing &nbsp; {today_str}", "Pippy's Brief ☀️")
    return subject, html, log_data


async def close(session: ClientSession) -> tuple[str, str, dict]:
    today = _today_ct().strftime("%A, %B %d")
    print("    fetching snapshot + sectors + movers + commodities + treasury + earnings…")
    snapshot, sectors, movers, commodities_raw, treasury_raw, headlines, earnings_raw, mem, scan_raw, picks_data = \
        await asyncio.gather(
            call(session, "fetch_market_snapshot"),
            call(session, "fetch_sector_performance"),
            call(session, "fetch_top_movers"),
            call(session, "fetch_commodities"),
            call(session, "fetch_treasury_yield"),
            call(session, "fetch_top_headlines"),
            call(session, "fetch_earnings_calendar"),
            call(session, "load_memory"),
            call(session, "run_daily_scan"),
            call(session, "get_weekly_picks"),
        )

    flagged   = mem.get("flagged_tickers", []) if isinstance(mem, dict) else []
    watchlist = []
    for t in flagged[:5]:
        print(f"    EOD {t}…")
        watchlist.append(await call(session, "fetch_stock_data", {"ticker": t}))

    # Portfolio day-performance — today's day-over-day move for each held pick,
    # distinct from the since-entry P&L already shown in the Stock Picks section.
    picks_list = picks_data.get("picks", []) if isinstance(picks_data, dict) else []
    picks_day_performance = []
    for p in picks_list:
        sym = p.get("ticker", "")
        if not sym:
            continue
        print(f"    EOD picks {sym}…")
        d = await call(session, "fetch_stock_data", {"ticker": sym})
        try:
            pct = float(d.get("pct")) if isinstance(d, dict) and d.get("pct") is not None else None
        except Exception:
            pct = None
        picks_day_performance.append({"ticker": sym, "pct": pct})

    snap_list    = snapshot.get("data", [])
    sectors_list = sectors.get("sectors", [])
    comm_list    = commodities_raw.get("commodities", [])
    earn_list    = earnings_raw.get("earnings", [])
    tsy          = treasury_raw if treasury_raw.get("yield") else {}

    body = (
        _close_summary_html(snap_list, movers, sectors_list, comm_list, treasury=tsy, earnings=earn_list,
                            headlines=headlines.get("headlines", []),
                            picks_day_performance=picks_day_performance,
                            scan_candidates=scan_raw.get("candidates", []),
                            mem=mem if isinstance(mem, dict) else {})
        + _indices(snap_list)
        + _movers(movers.get("gainers", []), movers.get("losers", []))
        + _sectors(sectors_list)
        + _commodities_and_yields(comm_list, tsy)
        + _watchlist(watchlist, "Your Watchlist — End of Day")
        + _headlines(headlines.get("headlines", []))
        + _daily_scan(scan_raw.get("candidates", []),
                      scanned=scan_raw.get("scanned", 0),
                      elapsed=scan_raw.get("elapsed_s", 0))
    )
    subject = f"Pippy's Brief — {today} Market Close"
    html    = _wrap(body, f"Market Close &nbsp; {today}", "Pippy's Brief 📊")

    # Build close log entry for the learning loop
    actual_dir = _classify_direction(snap_list)
    sp_val = 0.0
    for item in snap_list:
        if item.get("name") == "S&P 500":
            try:
                sp_val = float(item.get("pct") or item.get("changesPercentage") or 0)
            except Exception:
                pass

    best_s, worst_s = "", ""
    if sectors_list:
        try:
            best_s  = max(sectors_list, key=lambda x: float(x.get("pct") or x.get("changesPercentage") or 0)).get("sector", "")
            worst_s = min(sectors_list, key=lambda x: float(x.get("pct") or x.get("changesPercentage") or 0)).get("sector", "")
        except Exception:
            pass

    biggest_m = ""
    for m in movers.get("gainers", []) + movers.get("losers", []):
        try:
            p = abs(float(m.get("pct") or m.get("changesPercentage") or 0))
            if p >= 2.0:
                sym  = m.get("symbol", "")
                pct  = float(m.get("pct") or m.get("changesPercentage") or 0)
                biggest_m = f"{sym} {'+' if pct > 0 else ''}{pct:.1f}%"
                break
        except Exception:
            pass

    log_data = {
        "type":             "close",
        "actual_direction": actual_dir,
        "actual_sp_pct":    round(sp_val, 2),
        "best_sector":      best_s,
        "worst_sector":     worst_s,
        "biggest_mover":    biggest_m,
    }
    return subject, html, log_data


def _case_study_html(fields: dict, today: str) -> tuple[str, str]:
    """
    Build the standalone Case Study email HTML. No market data, no tickers,
    no prices — pure business-history narrative (hook / story / take).
    """
    topic = fields.get("topic", "Today's Case Study")
    hook  = fields.get("hook", "")
    story = fields.get("story", "")
    take  = fields.get("take", "")

    word_count = len(f"{hook} {story} {take}".split())
    print(f"    topic: {topic[:80]}")
    print(f"    word count: {word_count}")

    body = f"""
    <tr><td class="pippy-section-pad" style="padding:28px 32px;border-bottom:1px solid #e5e7eb">

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Hook</p>
      <p style="margin:0 0 20px;font-size:16px;color:#111827;line-height:1.6">{hook}</p>

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Story</p>
      <p style="margin:0 0 20px;font-size:16px;color:#374151;line-height:1.7">{story}</p>

      <div style="border-left:3px solid #111827;padding:10px 0 10px 14px;margin:0">
        <p style="margin:0 0 3px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">Pippy's Take</p>
        <p style="margin:0;font-size:16px;font-weight:600;color:#111827;line-height:1.5">{take}</p>
      </div>

    </td></tr>"""

    subject = f"Pippy's Brief 🧠 — Case Study: {today}"
    html    = _wrap(body, topic, f"Pippy's Brief 🧠 &nbsp;·&nbsp; {today}")
    return subject, html


async def case_study(session: ClientSession, dry_run: bool = False) -> tuple[str, str, dict]:
    """
    Standalone business-history case study — fully decoupled from market
    status. Zero AI: pulled from the hand-curated CASE_STUDIES library in
    case_studies.py.

    This only PREVIEWS a pick — it does not advance the rotation. The caller
    (run()) is responsible for calling commit_case_study_send(id) after
    send_email succeeds, so a mid-run failure never burns a rotation slot for
    content that was never actually sent.
    """
    today = _today_ct().strftime("%A, %B %d")

    print("    picking next case study from curated library (preview only)…")
    fields = await call(session, "get_next_case_study")

    subject, html = _case_study_html(fields, today)
    log_data = {
        "type":     "case_study",
        "id":       fields.get("id", ""),
        "category": fields.get("category", ""),
        "topic":    fields.get("topic", ""),
        "remaining_in_pass":   fields.get("remaining_in_pass"),
        "low_inventory_alert": fields.get("low_inventory_alert", False),
    }
    return subject, html, log_data


# ── "Already sent today" guard ────────────────────────────────────────────────
#
# Makes a catch-up retrigger (re-hitting the same workflow_dispatch endpoint
# ~45 min after the scheduled time) a safe no-op if the original run already
# sent successfully, and a real recovery if it didn't.
#
# Keyed on send_log.json, NOT on pippy_memory.json's last_email_summary —
# that field is only durable once the LATER "Commit updated memory" YAML step
# succeeds, and that step has already failed on its own (a merge conflict) in
# this project's history, stranding the update in a discarded CI workspace.
# send_log.json is committed and pushed to origin IMMEDIATELY after send_email
# returns, from inside this script, as its own small dedicated commit — before
# any later step in the same run gets a chance to fail. That's what makes it
# survive a job that dies before the later commit step.

def _load_send_log() -> dict:
    if os.path.exists(SEND_LOG_FILE):
        try:
            with open(SEND_LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"sends": []}


def _already_sent_today(mode: str, today_s: str) -> bool:
    log = _load_send_log()
    return any(e.get("mode") == mode and e.get("date") == today_s for e in log.get("sends", []))


def _record_send_and_push(mode: str, today_s: str):
    """
    Record that `mode` sent successfully today, and commit + push that record
    to git immediately — as its own small, dedicated commit, separate from the
    later "Commit updated memory" step. Best-effort: a failure here is logged
    but doesn't crash the run, since the email has already been sent by the
    time this is called; the worst case is the guard being less durable for
    this one run, not a lost or duplicated send.
    """
    log = _load_send_log()
    log.setdefault("sends", []).append({
        "mode": mode, "date": today_s, "timestamp": datetime.now().isoformat(),
    })
    log["sends"] = log["sends"][-60:]
    try:
        with open(SEND_LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        print(f"  [warn] could not write send_log.json: {e}")
        return

    try:
        subprocess.run(["git", "config", "user.name", "Pippy"],
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "pippy@openbell.ai"],
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        subprocess.run(["git", "add", "send_log.json"],
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"],
                              cwd=PROJECT_DIR, capture_output=True)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"Pippy send log — {mode} {today_s}"],
                           cwd=PROJECT_DIR, check=True, capture_output=True)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                           cwd=PROJECT_DIR, check=True, capture_output=True)
            subprocess.run(["git", "push"],
                           cwd=PROJECT_DIR, check=True, capture_output=True)
            print(f"  [send_log] recorded and pushed: {mode} sent {today_s}")
        else:
            print("  [send_log] no change to commit")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else str(e)
        print(f"  [warn] send_log commit/push failed (guard less durable for this run): {stderr}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(mode: str, dry_run: bool = False):
    today_str = _today_ct().strftime("%A, %B %d, %Y")
    # datetime.now() with no tz arg returns the MACHINE's local time (Central on
    # this dev Mac, UTC on a GitHub Actions runner) — labeling it "UTC" without
    # ever converting was a real, confirmed bug (it's what produced a misleading
    # "started at 15:15:01 UTC" that was actually 15:15 CDT, i.e. 20:15 real UTC).
    # datetime.now(timezone.utc) is the actual conversion.
    start_ts  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if dry_run:
        print("=== DRY RUN — no email will be sent, no memory will be saved ===")
    print(f"[Pippy's Brief] {mode.upper()} — {today_str}")
    print(f"[Pippy's Brief] started at {start_ts}")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")],
        env=dict(os.environ),
    )

    today_s = _today_ct_iso()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Startup memory-health check — runs unconditionally, before any mode
            # branching or early-return, so a persistence gap shows up in every
            # single run's log rather than only surfacing when someone happens to
            # go looking for it. Cheap (one extra load_memory call) and read-only.
            startup_mem = await call(session, "load_memory")
            _log_briefing_history_health(startup_mem, today_s)

            market     = await call(session, "is_market_open_today")
            is_open    = bool(market.get("open", False))
            mkt_reason = market.get("reason", "unknown")

            # "Already sent today" guard — checked before doing any of the
            # expensive mode-specific work. Makes a catch-up retrigger (a second
            # workflow_dispatch call ~45 min after the scheduled time) a safe
            # no-op if this mode already sent successfully today. Skipped for
            # dry runs, which never touch this state either way.
            if not dry_run and _already_sent_today(mode, today_s):
                print(f"[Pippy's Brief] Skipped {mode} — already sent successfully today ({today_s}). "
                      f"No-op (safe for a catch-up retrigger).")
                return

            if mode == "casestudy":
                # Fully decoupled from market status — fires unconditionally on its
                # own schedule (weekday noon CT + weekend 8:30am CT).
                subject, html, log_data = await case_study(session, dry_run=dry_run)

            elif mode == "morning":
                non_trading = "weekend" in mkt_reason or "holiday" in mkt_reason
                if non_trading:
                    print(f"[Pippy's Brief] Skipped morning briefing — {mkt_reason}, no session today.")
                    return
                subject, html, log_data = await morning(session)

            elif mode == "close":
                if is_open:
                    # Triggered while market is still open — too early for close summary
                    print("[Pippy's Brief] Skipped close summary — market still open, run again after 4 PM ET.")
                    return
                non_trading = "weekend" in mkt_reason or "holiday" in mkt_reason
                if non_trading:
                    print(f"[Pippy's Brief] Skipped close summary — {mkt_reason}, no session today.")
                    return
                subject, html, log_data = await close(session)

            else:
                print(f"[Pippy's Brief] Unknown mode: {mode}")
                return

            if dry_run:
                print(f"\n--- SUBJECT ---\n{subject}\n")
                print(f"--- HTML BODY ({len(html)} chars) ---")
                print(html[:6000])
                if len(html) > 6000:
                    print(f"  … (truncated, {len(html) - 6000} more chars)")
                if log_data.get("low_inventory_alert"):
                    remaining = log_data.get("remaining_in_pass", 0)
                    print(f"\n--- WOULD ALSO SEND LOW-INVENTORY ALERT ({remaining} case studies remaining) ---")
                print("\n=== DRY RUN COMPLETE — no email sent, no memory saved ===")
            else:
                send_ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")  # same fix as start_ts above
                print(f"  → Sending… (pre-send time: {send_ts})")
                result = await call(session, "send_email",
                                    {"subject": subject, "html_body": html})
                print(f"  {result}")

                # Record the "already sent" marker immediately — before anything
                # else in this run gets a chance to fail. This is what makes the
                # guard durable against a mid-run crash later in this same job.
                _record_send_and_push(mode, today_s)

                if mode == "casestudy":
                    # Only NOW advance the rotation — after send_email has already
                    # succeeded. A failure anywhere before this point (including
                    # never acquiring a runner at all) never burns a story.
                    commit_result = await call(session, "commit_case_study_send",
                                                {"id": log_data.get("id", "")})
                    log_data["remaining_in_pass"]   = commit_result.get("remaining_in_pass",
                                                                        log_data.get("remaining_in_pass"))
                    log_data["low_inventory_alert"] = commit_result.get("low_inventory_alert",
                                                                        log_data.get("low_inventory_alert", False))

                if log_data.get("low_inventory_alert"):
                    remaining = log_data.get("remaining_in_pass", 0)
                    alert_subject = "Pippy's Brief — Case Study Library Running Low"
                    alert_body = (f"<p>Only {remaining} case studies left before the rotation repeats. "
                                  f"Reload the library through Claude.</p>")
                    print(f"  → Sending low-inventory alert ({remaining} remaining)…")
                    alert_result = await call(session, "send_email",
                                              {"subject": alert_subject, "html_body": alert_body})
                    print(f"  {alert_result}")

                mem = await call(session, "load_memory")
                if isinstance(mem, dict):
                    mem["last_email_sent"]    = datetime.now().isoformat()
                    mem["last_email_summary"] = f"{mode} sent {today_str}"
                    mem["email_count"]        = mem.get("email_count", 0) + 1
                    _update_learning_memory(mem, log_data)
                    save_result = await call(session, "save_memory", {"data": mem})
                    if not (isinstance(save_result, dict) and save_result.get("status") == "ok"):
                        print(f"[ERROR] save_memory did not confirm success (got: {save_result}). "
                              f"This send went out, but the briefing_history/learning-loop update "
                              f"for {mode} on {today_str} may be lost.")
                else:
                    # Loud on purpose — this used to fail silently (the block was just
                    # skipped), which is exactly the "write never called" failure mode
                    # this project has been burned by before. The email still sent, but
                    # briefing_history / theme_frequency / pick_performance_history all
                    # went un-updated for this run.
                    print(f"[ERROR] load_memory returned a non-dict ({type(mem).__name__}) after "
                          f"sending the {mode} email — skipping memory save to avoid overwriting "
                          f"real history with garbage. briefing_history was NOT updated for {today_str}.")

            print("[Pippy's Brief] Done.")


async def send_alert(subject: str, body_html: str):
    """
    Send a plain, unstyled notification email via the same send_email path
    used by the regular briefs — for CI-side failure alerts (e.g. a workflow's
    primary job failing), not a scheduled brief. No memory writes, no market
    data, no rotation state touched.
    """
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")],
        env=dict(os.environ),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await call(session, "send_email", {"subject": subject, "html_body": body_html})
            print(f"  {result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["morning", "close", "casestudy", "alert"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full pipeline but skip send_email and save_memory")
    args = parser.parse_args()
    if args.mode == "alert":
        subject = os.environ.get("ALERT_SUBJECT", "Pippy's Brief — Alert")
        body    = os.environ.get("ALERT_BODY", "<p>Alert triggered with no ALERT_BODY set.</p>")
        asyncio.run(send_alert(subject, body))
        return
    asyncio.run(run(args.mode, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
