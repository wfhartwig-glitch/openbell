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
        if econ_failed:
            msg = '<p style="margin:0;font-size:13px;color:#9ca3af">Macro event data unavailable (source error). No tracked earnings in the next two weeks.</p>'
        else:
            msg = '<p style="margin:0;font-size:13px;color:#9ca3af">No major events or tracked earnings in the next two weeks.</p>'
        return _section("This Week's Calendar", msg)
    return _section("This Week's Calendar", f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _daily_scan(candidates: list, scanned: int = 0, elapsed: float = 0) -> str:
    if not candidates:
        return _section("Today's Top Scored Candidates",
                        '<p style="margin:0;font-size:13px;color:#9ca3af">Scan unavailable — no data returned.</p>')
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
          <p style="margin:0 0 3px;font-size:14px;line-height:1.4">
            <span style="font-weight:700;color:#9ca3af">{i}.</span>
            <span style="font-weight:700;color:#111827">{ticker}</span>
            <span style="color:#6b7280;font-size:12px">{company}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280;line-height:1.5">
            {sector}{" · " + risk if risk else ""} ·
            <span style="font-weight:700;color:{score_color}">Score {score:.0f}</span> ·
            <span style="font-weight:700;color:{mom_color}">{mom_str}</span>
          </p>
          <p style="margin:0;font-size:12px;color:#374151;line-height:1.5">{rationale}</p>
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
          <p style="margin:0 0 3px;font-size:14px;font-weight:700;color:#111827">
            {ticker} <span style="font-weight:500;color:#6b7280;font-size:12px">{p.get("company","")}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280">
            {p.get("sector","")} · <span style="font-weight:700;color:{GREEN}">Holding · {weeks_held}w</span>
          </p>
          <p style="margin:0;font-size:12px;color:#374151;line-height:1.5">{pct_str}{note}{flag}</p>
        </div>"""

    new_candidates = [c for c in top_scan if c.get("ticker", "") not in held_tickers][:4]
    for c in new_candidates:
        score    = c.get("score", 0)
        momentum = c.get("momentum", 0)
        mom_str  = f'{"▲" if momentum >= 0 else "▼"} {abs(momentum):.1f}% (3mo)'
        detail   = f"Score {score:.0f} — {c.get('rationale','')} — {mom_str}"
        cards += f"""
        <div style="{_border()}padding:12px 0">
          <p style="margin:0 0 3px;font-size:14px;font-weight:700;color:#111827">
            {c.get("ticker","")} <span style="font-weight:500;color:#6b7280;font-size:12px">{c.get("company","")}</span>
          </p>
          <p style="margin:0 0 6px;font-size:11px;color:#6b7280">
            {c.get("sector","")} · <span style="font-weight:700;color:#7c3aed">New candidate</span>
          </p>
          <p style="margin:0;font-size:12px;color:#374151;line-height:1.5">{detail}</p>
        </div>"""

    changes_html = ""
    if changes:
        items = "".join(f'<li style="margin:3px 0;font-size:12px;color:#6b7280">{c}</li>' for c in changes)
        changes_html = f'<p style="margin:12px 0 4px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em">Changes This Week</p><ul style="margin:0;padding-left:18px">{items}</ul>'

    explainer = ('<p style="margin:0 0 12px;font-size:12px;color:#6b7280">'
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
    """If current 10y is within ~10bp of its trailing 6mo high/low, name that —
    with a day-of-week ("Tuesday's high") if the extreme was recent enough for
    that to actually mean something, else a generic "6-month" framing."""
    if current is None:
        return ""
    for kind, extreme_val, day_name in (("high", six_mo_high, six_mo_high_day),
                                        ("low",  six_mo_low,  six_mo_low_day)):
        if extreme_val is None:
            continue
        if abs(float(current) - float(extreme_val)) <= 0.10:
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


_TAPE_OPEN_HIGHER = [
    "Futures are firm and broad — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "The tape is broadly higher into the open — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "Pre-market action is solidly positive — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
]
_TAPE_OPEN_HIGHER_MODEST = [
    "Futures are narrowly higher — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "The tape is modestly higher into the open — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "Pre-market action is quietly positive — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
]
_TAPE_OPEN_LOWER = [
    "Futures are under pressure — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "The tape is broadly lower into the open — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "Pre-market action is soft — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
]
_TAPE_OPEN_LOWER_MODEST = [
    "Futures are narrowly lower — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "The tape is modestly lower into the open — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "Pre-market action is quietly soft — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
]
_TAPE_OPEN_MIXED = [
    "Futures are mixed — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "The tape is split into the open — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
    "Pre-market action is directionless — S&P {sp}, Nasdaq {ndx}, Dow {dow}",
]

_RATES_LEAD = ["The 10-year is at", "The 10-year sits at", "The benchmark 10-year is trading at"]
_RATES_TAILWIND = [
    "Yields backing off that level relieves the pressure on long-duration equities.",
    "That pullback in yields takes some pressure off long-duration names.",
    "Easing yields give richly-valued growth stocks more room to work.",
]
_RATES_HEADWIND = [
    "Rising yields add pressure to long-duration equities.",
    "That move up in yields is a headwind for richly-valued growth names.",
    "Climbing yields tighten the multiple math on long-duration stocks.",
]
_OIL_LEAD = [
    "WTI at ${price:.2f} ({pct}) keeps the inflation input hot",
    "Crude at ${price:.2f} ({pct}) keeps inflation in the conversation",
    "Oil's move to ${price:.2f} ({pct}) keeps the inflation input live",
]
_GOLD_LEAD = [
    "gold at ${price:,.0f} ({pct}) says the hedge bid hasn't gone anywhere",
    "gold's move to ${price:,.0f} ({pct}) shows the hedge bid is still there",
    "gold at ${price:,.0f} ({pct}) suggests risk-hedging demand is intact",
]
_OIL_GOLD_CONNECTOR = [
    "Two things cut the other way:", "Working against that:", "On the other side of the ledger:",
]
_OIL_SOLO_LEAD = ["Working against that,", "Cutting the other way,", "One thing pushing back:"]


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
    ONE shared priority ladder for both briefs (mode="morning" / "close") —
    RATES, OIL/GOLD, and BREADTH all live here once, not forked into two
    copies that can (and did) drift apart. Mode-specific rules layer on top
    of the shared ladder rather than reimplementing it:

      - morning: HANDOFF (Europe confirm/contradict, Asia flat-or-not) after
        the shared ladder, plus a SENTIMENT-GATE headline fallback if the
        entire ladder stays silent (a genuinely quiet premarket). P2 is
        forward-looking earnings + a portfolio/headline intersection line.
      - close: RATE-SENSITIVE COMPOSITE is checked BEFORE the shared RATES
        step (both would otherwise redundantly cite the same yield move) and
        PATH (intraday shape) is appended after the ladder. P2 is movers +
        candidate cross-reference + portfolio day-performance + LOOP-CLOSE.

    Two rules within the "shared" ladder still branch on mode, deliberately:

      - BREADTH: premarket has no sector-level data at all (sectors isn't
        even fetched pre-open), so morning's breadth read is a same-tape
        Dow-vs-Nasdaq gap qualifier clause tacked onto the opening sentence,
        while close's is a full sector-breadth classification (rotation /
        broad rally / broad selloff / mixed) via _classify_close_tape. Same
        question ("what's actually moving under the index-level number"),
        answered with whatever breadth data that time of day actually has.
      - RATES: the number/threshold/extreme-vs-6mo-range logic is identical
        for both, but the framing differs — morning is forward-looking
        ("could be a headwind/tailwind" for a session that hasn't happened
        yet); close is a past-tense recap (the session's already over, so
        there's nothing left to be a headwind FOR). Same trigger, same
        numbers, different tense.

    One real behavior change from this consolidation: the close brief now
    gets the same standalone OIL/GOLD sentence morning always had (previously
    close only ever mentioned gold as one of two possible RATE-SENSITIVE
    COMPOSITE corroborators, and never mentioned oil at all). Gold is skipped
    here if the composite note already cited it, so it's never named twice
    in the same brief.

    Returns (text, log_data). text is "P1\\n\\nP2" (caller splits on the blank
    line for two <p> tags). For mode=="close", log_data is always {} — close's
    own learning-loop log entry is built independently in close() from raw
    snapshot/sector/mover data (a different shape than morning's log_data),
    unchanged by this consolidation.
    """
    import hashlib
    from datetime import date as _date

    headlines   = headlines if isinstance(headlines, list) else []
    commodities = commodities if isinstance(commodities, list) else []
    sectors     = sectors if isinstance(sectors, list) else []
    mem         = mem if isinstance(mem, dict) else {}

    idx = {}
    for item in snapshot_data:
        name = item.get("name", "")
        try:
            idx[name] = float(item.get("pct") or item.get("changesPercentage") or 0)
        except Exception:
            idx[name] = 0.0

    sp, ndx, dow = idx.get("S&P 500", 0.0), idx.get("Nasdaq", 0.0), idx.get("Dow", 0.0)
    tape_tone = _classify_direction(snapshot_data)  # 'higher' / 'lower' / 'mixed' / 'unknown'
    all_vals  = [v for v in (sp, ndx, dow) if v != 0.0]
    max_move  = max((abs(v) for v in all_vals), default=0.0)

    day_hash = int(hashlib.md5(_date.today().isoformat().encode()).hexdigest(), 16)

    p1_sentences = []

    # ── BREADTH-inflected opener ───────────────────────────────────────────
    # mode-conditional by data availability, not a duplicate implementation —
    # see docstring.
    close_tape = None
    if mode == "close":
        close_tape = _classify_close_tape(sp, ndx, dow, sectors)
        if close_tape["kind"] == "rotation":
            dow_word = "closed flat" if abs(dow) < 0.10 else f"closed {'up' if dow >= 0 else 'down'} ({_fmt(dow)})"
            ndx_verb = "gave up" if ndx < 0 else "gained"
            opener = (f"Not a sell-off — a rotation. The Dow {dow_word} while the Nasdaq {ndx_verb} "
                     f"{abs(ndx):.2f}%, and {close_tape['up_count']} of {close_tape['total']} sectors finished green.")
            out_txt = _join_sector_moves(close_tape["down_sectors"][:3])
            in_txt  = _join_sector_moves(close_tape["up_sectors"][:2])
            if out_txt and in_txt:
                opener += f" Money left {out_txt} for {in_txt}."
            p1_sentences.append(opener)
        elif close_tape["kind"] in ("broad_rally", "broad_selloff"):
            verb      = "rallied" if close_tape["kind"] == "broad_rally" else "sold off"
            dir_count = close_tape["up_count"] if close_tape["kind"] == "broad_rally" else close_tape["down_count"]
            color     = "green" if close_tape["kind"] == "broad_rally" else "red"
            p1_sentences.append(
                f"Markets {verb} today — {dir_count} of {close_tape['total']} sectors {color}. "
                f"S&P {_fmt(sp)}, Nasdaq {_fmt(ndx)}, Dow {_fmt(dow)}."
            )
        else:
            best_n, best_p   = close_tape["best_sector"]
            worst_n, worst_p = close_tape["worst_sector"]
            if best_n and (not worst_n or abs(best_p) >= abs(worst_p)):
                driver = f", led by {best_n} ({_fmt(best_p)})"
            elif worst_n:
                driver = f", dragged by {worst_n} ({_fmt(worst_p)})"
            else:
                driver = ""
            p1_sentences.append(f"The tape was mixed today — S&P {_fmt(sp)}, Nasdaq {_fmt(ndx)}, Dow {_fmt(dow)}{driver}.")
    else:
        # Magnitude-sensitive: a +0.03% tape is technically "higher" per
        # _classify_direction but calling it "solidly positive" overstates a
        # session that's essentially flat.
        if tape_tone == "higher":
            tape_pool = _TAPE_OPEN_HIGHER if max_move > 0.5 else _TAPE_OPEN_HIGHER_MODEST
        elif tape_tone == "lower":
            tape_pool = _TAPE_OPEN_LOWER if max_move > 0.5 else _TAPE_OPEN_LOWER_MODEST
        else:
            tape_pool = _TAPE_OPEN_MIXED
        tape_open = _rot_phrase(tape_pool, day_hash, "tape_open").format(
            sp=_fmt(sp), ndx=_fmt(ndx), dow=_fmt(dow),
        )
        breadth_gap = dow - ndx
        if breadth_gap >= 0.30:
            tape_open += ", with cyclicals edging out tech — a rotation cue, not a megacap one"
        elif breadth_gap <= -0.30:
            tape_open += ", with megacap tech leading the tape"
        p1_sentences.append(tape_open + ".")

    # ── RATES / RATE-SENSITIVE COMPOSITE — |Δ10y| >= 4bp ──────────────────────
    rates_fired  = False
    gold_cited   = False  # tracks whether gold was already named by the composite, for OIL/GOLD below
    used_headlines = set()  # dedup — nothing gets quoted twice in one summary

    treasury_chg = None
    if treasury and treasury.get("yield") is not None:
        try:
            treasury_chg = float(treasury.get("change", 0) or 0)
        except Exception:
            treasury_chg = None

    gold_pct_for_composite = None
    for c in commodities:
        if "gold" in c.get("name", "").lower():
            try:
                gold_pct_for_composite = float(c.get("pct", 0) or 0)
            except Exception:
                pass
            break

    if mode == "close":
        composite_note = _rate_sensitive_composite_note(sp, sectors, gold_pct_for_composite, treasury_chg,
                                                        headlines, used_headlines)
        if composite_note:
            p1_sentences.append(composite_note)
            rates_fired = True
            if isinstance(gold_pct_for_composite, (int, float)) and abs(gold_pct_for_composite) >= 1.5:
                gold_cited = True

    if not rates_fired and treasury_chg is not None and abs(treasury_chg) >= 0.04:  # 4bp, in percentage-point units
        rates_fired = True
        yld = treasury.get("yield", 0)
        bp  = abs(treasury_chg) * 100
        dir_word = "down" if treasury_chg < 0 else "up"
        extreme = _rate_extreme_note(
            yld, treasury.get("six_mo_high"), treasury.get("six_mo_high_day"),
            treasury.get("six_mo_low"), treasury.get("six_mo_low_day"),
        )
        extreme_clause = f", {extreme}" if extreme else ""
        if mode == "morning":
            # Forward-looking framing — the session hasn't happened yet.
            lead = _rot_phrase(_RATES_LEAD, day_hash, "rates_lead")
            tail = _rot_phrase(_RATES_TAILWIND if treasury_chg < 0 else _RATES_HEADWIND, day_hash, "rates_tail")
            p1_sentences.append(f"{lead} {yld:.2f}%, {dir_word} ~{bp:.0f}bp{extreme_clause}.")
            p1_sentences.append(tail)
        else:
            # Past-tense recap — nothing left to be a headwind/tailwind FOR.
            p1_sentences.append(f"The 10-year closed at {yld:.2f}%, {dir_word} "
                                f"~{bp:.0f}bp{extreme_clause} on the session.")

    # ── OIL + GOLD — combined into one sentence when both trip ────────────────
    # Shared by both modes (see docstring for the close-mode behavior change).
    oil_txt = gold_txt = ""
    oil = next((c for c in commodities if "crude" in c.get("name", "").lower()
                or "oil" in c.get("name", "").lower()), None)
    if oil:
        try:
            opct, oprice, ochange = (float(oil.get("pct", 0) or 0),
                                     float(oil.get("price", 0) or 0),
                                     float(oil.get("change", 0) or 0))
        except Exception:
            opct = oprice = ochange = 0.0
        if abs(opct) >= 2.0 or _crossed_round_10(oprice, ochange):
            oil_txt = _rot_phrase(_OIL_LEAD, day_hash, "oil_lead").format(price=oprice, pct=_fmt(opct))

    if not gold_cited:
        gold = next((c for c in commodities if "gold" in c.get("name", "").lower()), None)
        if gold:
            try:
                gpct, gprice = float(gold.get("pct", 0) or 0), float(gold.get("price", 0) or 0)
            except Exception:
                gpct = gprice = 0.0
            if abs(gpct) >= 1.0:
                gold_txt = _rot_phrase(_GOLD_LEAD, day_hash, "gold_lead").format(price=gprice, pct=_fmt(gpct))

    if oil_txt and gold_txt:
        connector = _rot_phrase(_OIL_GOLD_CONNECTOR, day_hash, "og_connector")
        p1_sentences.append(f"{connector} {oil_txt}, and {gold_txt}.")
    elif oil_txt:
        p1_sentences.append(f"{_rot_phrase(_OIL_SOLO_LEAD, day_hash, 'oil_solo')} {oil_txt}.")
    elif gold_txt:
        p1_sentences.append(f"{_rot_phrase(_OIL_SOLO_LEAD, day_hash, 'gold_solo')} {gold_txt}.")

    ladder_fired = rates_fired or bool(oil_txt) or bool(gold_txt)

    # ── HANDOFF (morning-only) — Europe confirm/contradict; Asia flat-or-not ──
    handoff_fired = False
    if mode == "morning":
        global_list = global_indices if isinstance(global_indices, list) else []
        europe_list = [g for g in global_list if g.get("session") == "Europe"]
        asia_list   = [g for g in global_list if g.get("session") == "Asia (overnight)"]

        handoff_clause_parts = []
        if europe_list:
            try:
                europe_vals = [float(g.get("pct", 0) or 0) for g in europe_list]
            except Exception:
                europe_vals = []
            if europe_vals:
                europe_avg  = sum(europe_vals) / len(europe_vals)
                europe_desc = ", ".join(f"{g.get('name','')} {_fmt(g.get('pct', 0))}" for g in europe_list[:2])
                if tape_tone in ("higher", "lower"):
                    confirms = (tape_tone == "higher" and europe_avg > 0) or (tape_tone == "lower" and europe_avg < 0)
                    handoff_clause_parts.append(
                        f"Europe confirms the tone ({europe_desc})" if confirms
                        else f"Europe is pulling the other way ({europe_desc})"
                    )
                else:
                    handoff_clause_parts.append(f"Europe is mixed too ({europe_desc})")
                handoff_fired = True

        if asia_list:
            try:
                asia_vals = [float(g.get("pct", 0) or 0) for g in asia_list]
            except Exception:
                asia_vals = []
            if asia_vals:
                if all(abs(v) < 0.15 for v in asia_vals):
                    handoff_clause_parts.append("Asia was flat overnight and gave no handoff")
                else:
                    asia_avg = sum(asia_vals) / len(asia_vals)
                    handoff_clause_parts.append(f"Asia leaned {'higher' if asia_avg > 0 else 'lower'} overnight")
                handoff_fired = True

        if handoff_clause_parts:
            p1_sentences.append("; ".join(handoff_clause_parts) + ".")
        ladder_fired = ladder_fired or handoff_fired

    # ── PATH (close-only) ─────────────────────────────────────────────────────
    if mode == "close":
        path_txt = _path_note(snapshot_data)
        if path_txt:
            p1_sentences.append(path_txt)

    # ── Fallback (morning-only): nothing on the ladder tripped — SENTIMENT
    # GATE applies. Close never needs this — its breadth-classification opener
    # always fires unconditionally, so P1 is never silent to begin with.
    macro_theme_for_log = "rate expectations" if rates_fired else ("commodities" if (oil_txt or gold_txt) else "")
    if mode == "morning" and not ladder_fired:
        fallback_used = False
        for h in headlines[:5]:
            title = h.get("title", "") if isinstance(h, dict) else str(h)
            if not title or len(title) <= 15 or not _headline_is_market_relevant(title):
                continue
            sentiment = _classify_headline_sentiment(title)
            if not _sentiment_gate_ok(sentiment, tape_tone):
                continue  # contradicts (or reads uncertain under) a confident tape — skip
            p1_sentences.append(f"On the tape: {title.rstrip('.')}.")
            fallback_used = True
            break
        if not fallback_used:
            p1_sentences.append("No single catalyst stands out in early trading.")

    p1_text = " ".join(p1_sentences)

    if mode == "morning":
        recurring = _get_recurring_theme(mem, window=5, threshold=3)
        if recurring and recurring != macro_theme_for_log:
            p1_text += f" (Note: {recurring} has been a persistent theme over the past week.)"

    # ── P2 — mode-specific; inherently different content, not a shared rule ──
    p2_sentences = []

    if mode == "morning":
        today_iso       = _today_ct_iso()
        earnings_list   = earnings if isinstance(earnings, list) else []
        todays_earnings = [e for e in earnings_list if e.get("date", "") == today_iso]
        if todays_earnings:
            parts = []
            for e in todays_earnings[:3]:
                sym = e.get("symbol", "")
                if not sym:
                    continue
                eps = e.get("eps_estimated")
                try:
                    parts.append(f"{sym} reports, EPS est. ${float(eps):.2f}" if eps is not None else f"{sym} reports")
                except Exception:
                    parts.append(f"{sym} reports")
            if parts:
                p2_sentences.append("Today: " + "; ".join(parts) + ".")

        picks    = picks_data.get("picks", []) if isinstance(picks_data, dict) else []
        changes  = picks_data.get("changes_from_last_week", []) if isinstance(picks_data, dict) else []
        enriched = _enrich_picks_with_perf(picks, mem) if picks else []

        # PORTFOLIO INTERSECTION — does any held ticker/company name appear in
        # today's headlines? Match on ticker AND full company name via
        # _find_headline_for_symbol, which already guards against substring
        # collisions (case-sensitive ticker match, corporate-suffix-stripped
        # company name) — the exact class of bug flagged before.
        featured = None  # (pick, headline, field)
        for p in enriched:
            sym  = p.get("ticker", "")
            name = p.get("company", "")
            if not sym:
                continue
            h, is_specific, field = _find_headline_for_symbol(headlines, sym, name, sector="")
            if h and is_specific:
                pct = p.get("pct_change_since_pick")
                pct_val = pct if isinstance(pct, (int, float)) else 0.0
                if featured is None or pct_val < featured[0].get("pct_change_since_pick", 0.0):
                    featured = (p, h, field)

        if picks:
            n = len(picks)
            if changes:
                picks_sentence = f"{len(changes)} of your {n} picks rotated this week — details below."
            else:
                picks_sentence = f"Your {n} picks are unchanged"
                if featured:
                    fp, fh, ffield = featured
                    fsym  = fp.get("ticker", "")
                    fpct  = fp.get("pct_change_since_pick")
                    pct_str = _fmt(fpct) if isinstance(fpct, (int, float)) else "—"
                    picks_sentence += (f", but {fsym} is the one to watch — worst holding at {pct_str} "
                                      f"and in today's headlines over {_cite_headline(fh, ffield)}.")
                else:
                    picks_sentence += "."
            p2_sentences.append(picks_sentence)

        text = p1_text + ("\n\n" + " ".join(p2_sentences) if p2_sentences else "")

        named    = {"S&P 500": sp, "Nasdaq": ndx, "Dow": dow}
        ldr_name = max(named, key=lambda k: abs(named[k]))
        ldr_val  = named[ldr_name]
        lag_name = min(named, key=lambda k: named[k])
        log_data = {
            "type":             "morning",
            "direction_called": tape_tone,
            "leading_index":    ldr_name if ldr_val >= 0 else "",
            "lagging_index":    lag_name if named.get(lag_name, 0) < 0 else "",
            "headline_theme":   macro_theme_for_log,
            "commodity_note":   oil_txt or gold_txt,
            "picks_status":     "rotated" if changes else "holding",
        }
        return text, log_data

    # ── close-only P2: movers + candidate cross-ref + portfolio day-perf + LOOP-CLOSE
    today_iso      = _today_ct_iso()
    earnings_list  = earnings if isinstance(earnings, list) else []
    earnings_today = {e.get("symbol", "") for e in earnings_list if e.get("date", "") == today_iso}
    movers = movers if isinstance(movers, dict) else {}

    def _mover_clause(m: dict, label: str) -> str:
        if not m:
            return ""
        try:
            pct = float(m.get("pct") or m.get("changesPercentage") or 0)
        except Exception:
            return ""
        if abs(pct) < 2.0:
            return ""
        sym = m.get("symbol", "")
        verb = "led at" if label == "best" else "was the day's worst at"
        if sym in earnings_today:
            return f"{sym} {verb} {_fmt(pct)}, following this morning's earnings report"
        # either the headline is specifically about this name, or it's omitted —
        # no sector-category or generic-macro "backdrop" attachment for a named mover.
        h, is_specific, field = _find_headline_for_symbol(
            headlines, sym, m.get("name", ""), sector="", exclude=used_headlines,
        )
        if h and is_specific:
            used_headlines.add(h.get("title", ""))
            return f"{sym} {verb} {_fmt(pct)} — {_cite_headline(h, field)}"
        return f"{sym} {verb} {_fmt(pct)}"

    gainers, losers = movers.get("gainers", []), movers.get("losers", [])
    mover_bits = [c for c in [
        _mover_clause(losers[0] if losers else None, "worst"),
        _mover_clause(gainers[0] if gainers else None, "best"),
    ] if c]
    if mover_bits:
        # Single terminator at the join site — _mover_clause never adds its own,
        # so this is the one place a period gets added, regardless of whether
        # the citation-bearing clause is first, last, or the only one.
        p2_sentences.append("; ".join(mover_bits) + ".")

    mover_syms = {m.get("symbol", "") for m in (gainers[:1] + losers[:1])}
    candidate_note = _candidate_cross_reference_note(movers, scan_candidates, exclude_syms=mover_syms)
    if candidate_note:
        p2_sentences.append(candidate_note)

    portfolio_note = _portfolio_day_performance_note(picks_day_performance)
    if portfolio_note:
        p2_sentences.append(portfolio_note)

    loop_close_note = _loop_close_note(snapshot_data, mem)
    if loop_close_note:
        p2_sentences.append(loop_close_note)

    text = p1_text + ("\n\n" + " ".join(p2_sentences) if p2_sentences else "")
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
        f'<p style="margin:0 0 10px;font-size:14px;color:#374151;line-height:1.6">{p}</p>'
        if i < len(paragraphs) - 1 else
        f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.6">{p}</p>'
        for i, p in enumerate(paragraphs)
    )
    html = _section("What's Going On", inner)
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


def _cite_headline(h: dict, field: str = None) -> str:
    """
    Quote if <=15 words (copyright-safe), else paraphrase/truncate. If field
    ("title" or "snippet") is given, cite that part specifically — it's the
    part that actually matched, so this avoids quoting an unrelated portion of
    the same headline object.

    Contract: the returned fragment NEVER carries its own trailing period —
    every call site supplies exactly one closing period itself, always. An
    earlier version self-terminated short quotes with "." before the closing
    quote mark, which produced a double period at any call site that also
    closed its own sentence after it. That recurred at three separate call
    sites (each patched individually) before being fixed here, at the source,
    instead of patched per-caller yet again.
    """
    if field == "title":
        text = h.get("title", "") or h.get("snippet", "")
    else:
        text = h.get("snippet", "") or h.get("title", "")
    text = (text or "").strip().rstrip(".")
    words = text.split()
    if len(words) <= 15:
        return f'"{text}"'
    return text[:140].rstrip(".") + "…"


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


def _rate_sensitive_composite_note(sp_pct: float, sectors: list, gold_pct, treasury_chg,
                                   headlines: list = None, used_headlines: set = None) -> str:
    """
    Fires on a rate-driven READ of the sector map even when no single number
    trips its own threshold: Utilities AND Real Estate both lagging the S&P by
    >=0.5pp, plus gold or the 10-year corroborating. Phrased as a reading of
    the data ("the shape of..."), never as an asserted cause. If a Fed/macro
    headline exists for the same day, it's named alongside as correlation,
    not claimed as the cause — placed side by side, per the rule's own
    instruction, and the correlation is left to stand on its own.
    """
    util_pct = re_pct = None
    for s in sectors or []:
        n = (s.get("sector") or "").lower()
        try:
            pct = float(s.get("pct") if s.get("pct") is not None else s.get("changesPercentage", 0))
        except Exception:
            continue
        if "utilities" in n:
            util_pct = pct
        elif "real estate" in n:
            re_pct = pct
    if util_pct is None or re_pct is None:
        return ""
    if (sp_pct - util_pct) < 0.5 or (sp_pct - re_pct) < 0.5:
        return ""

    gold_ok  = isinstance(gold_pct, (int, float)) and abs(gold_pct) >= 1.5
    bp       = abs(treasury_chg) * 100 if isinstance(treasury_chg, (int, float)) else 0
    yield_ok = bp >= 4
    if not (gold_ok or yield_ok):
        return ""

    bits = []
    if gold_ok:
        bits.append(f"gold {'sold off' if gold_pct < 0 else 'jumped'} {abs(gold_pct):.2f}%")
    if yield_ok:
        bits.append(f"the 10-year moved ~{bp:.0f}bp")
    driver_txt = " and ".join(bits)
    driver_txt = driver_txt[0].upper() + driver_txt[1:]
    note = (f"{driver_txt} while Utilities and Real Estate both lagged the S&P — "
           f"the shape of a hawkish rate repricing, not broad risk-off")

    # Name a same-day Fed/macro headline as correlation, never as asserted cause.
    used_headlines = used_headlines if used_headlines is not None else set()
    h, field = _find_headline_for_keywords(headlines or [], list(_MACRO_KEYWORDS.keys()), exclude=used_headlines)
    if h:
        used_headlines.add(h.get("title", ""))
        return note + f", on a day when {_cite_headline(h, field)}."
    return note + "."


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


def _candidate_cross_reference_note(movers: dict, scan_candidates: list, exclude_syms: set = None) -> str:
    """"Worth flagging" — a stock that moved sharply today also scored into
    today's daily fundamentals scan, an independent signal worth naming."""
    exclude_syms = exclude_syms or set()
    candidate_map = {c.get("ticker"): c.get("score") for c in (scan_candidates or []) if c.get("ticker")}
    for m in (movers.get("gainers", []) or []) + (movers.get("losers", []) or []):
        sym = m.get("symbol", "")
        if not sym or sym in exclude_syms or sym not in candidate_map:
            continue
        try:
            pct = float(m.get("pct") or m.get("changesPercentage") or 0)
        except Exception:
            continue
        if abs(pct) < 2.0:
            continue
        score = candidate_map[sym]
        verb = "fell" if pct < 0 else "gained"
        try:
            score_str = f"{float(score):.0f}"
        except Exception:
            score_str = str(score)
        return f"Worth flagging: {sym} {verb} {abs(pct):.2f}% today and still scored into the candidate list at {score_str}."
    return ""


def _portfolio_day_performance_note(picks_day_performance: list) -> str:
    """How the user's OWN held picks did today (day-over-day), not their
    since-entry P&L (that's the Stock Picks section) — a distinct question."""
    valid = [(p.get("ticker", ""), p.get("pct")) for p in (picks_day_performance or [])
            if p.get("ticker") and isinstance(p.get("pct"), (int, float))]
    if not valid:
        return ""
    best, worst = max(valid, key=lambda x: x[1]), min(valid, key=lambda x: x[1])
    up_count = sum(1 for _, p in valid if p >= 0)
    if up_count == len(valid):
        tone = "were higher"
    elif up_count == 0:
        tone = "were lower"
    else:
        tone = "were mixed"
    if best[0] == worst[0]:
        return f"Your one held pick was {'higher' if best[1] >= 0 else 'lower'} today: {best[0]} {_fmt(best[1])}."
    return f"Your picks {tone} today: {best[0]} led at {_fmt(best[1])}, {worst[0]} lagged at {_fmt(worst[1])}."


def _loop_close_note(snapshot_data: list, mem: dict) -> str:
    """
    Closes the loop against this morning's call. If today's morning record is
    missing from briefing_history (a silent persistence failure — the exact
    class this project has hit before when a git commit step failed after a
    successful send), that's logged explicitly here rather than just silently
    producing no sentence, so the gap is visible in the run's own log output.
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
    called = morning_entry.get("direction_called", "unknown")
    actual = _classify_direction(snapshot_data)
    if called == "unknown" or actual == "unknown":
        return ""
    if called == actual:
        return f"This morning's {called} call held through the close."
    return f"This morning we called the tape {called}; it closed {actual} instead."

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
        f'<p style="margin:0 0 10px;font-size:14px;color:#374151;line-height:1.6">{p}</p>'
        if i < len(paragraphs) - 1 else
        f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.6">{p}</p>'
        for i, p in enumerate(paragraphs)
    )
    return _section("What Happened Today", inner)


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
    <tr><td style="padding:28px 32px;border-bottom:1px solid #e5e7eb">

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Hook</p>
      <p style="margin:0 0 20px;font-size:15px;color:#111827;line-height:1.6">{hook}</p>

      <p style="margin:0 0 6px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">The Story</p>
      <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.7">{story}</p>

      <div style="border-left:3px solid #111827;padding:10px 0 10px 14px;margin:0">
        <p style="margin:0 0 3px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">Pippy's Take</p>
        <p style="margin:0;font-size:14px;font-weight:600;color:#111827;line-height:1.5">{take}</p>
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
