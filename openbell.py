#!/usr/bin/env python3
"""
OpenBell — Three email types: Morning Briefing, Market Close Summary, Deep Dive.
Runs on a schedule in Central Time. Powered by Claude + yfinance + NewsAPI.
"""

import argparse
import json
import os
import re
import smtplib
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import requests
import schedule
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL           = os.getenv("TO_EMAIL")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

PICKS_CACHE_FILE  = "picks_cache.json"
PIPPY_MEMORY_FILE = "pippy_memory.json"
CLAUDE_MODEL      = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 1500

# ── NYSE holidays 2025 & 2026 ─────────────────────────────────────────────────
NYSE_HOLIDAYS = {
    # 2025
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # MLK Day
    "2025-02-17",  # Presidents Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving
    "2025-12-25",  # Christmas
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}


def is_market_open_today() -> bool:
    today = date.today()
    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return today.isoformat() not in NYSE_HOLIDAYS


# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
    "AVGO", "NOW", "CRWD", "PLTR", "DDOG", "TTD", "MELI",
    "CELH", "AXON", "TMDX", "HIMS", "APP", "HOOD", "IONQ",
    "SOUN", "RGTI", "VRT", "ANET", "DECK", "LULU",
]
WELL_KNOWN = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM"}

SECTOR_ETFS = {
    "Technology":        "XLK",
    "Financials":        "XLF",
    "Energy":            "XLE",
    "Health Care":       "XLV",
    "Industrials":       "XLI",
    "Consumer Discret.": "XLY",
    "Consumer Staples":  "XLP",
    "Materials":         "XLB",
    "Real Estate":       "XLRE",
    "Utilities":         "XLU",
    "Comm. Services":    "XLC",
}

DEEP_DIVE_CATEGORIES = [
    "up and coming publicly traded company",
    "privately held company worth knowing about",
    "sector deep dive",
    "housing market analysis",
    "macro theme",
    "historical market event",
    "venture-backed startup in fintech, AI, or infrastructure",
]

# ── Memory ────────────────────────────────────────────────────────────────────
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
    "deep_dive_history": [],
}


def load_memory() -> dict:
    if os.path.exists(PIPPY_MEMORY_FILE):
        try:
            with open(PIPPY_MEMORY_FILE) as f:
                data = json.load(f)
            for k, v in DEFAULT_MEMORY.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_MEMORY)


def save_memory(mem: dict) -> None:
    with open(PIPPY_MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)


# ── Claude ────────────────────────────────────────────────────────────────────
def call_claude(prompt: str, max_tokens: int = CLAUDE_MAX_TOKENS) -> str:
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Claude unavailable: {e}]"


# ── Data fetchers ─────────────────────────────────────────────────────────────
def get_futures_premarket() -> list:
    results = []
    for name, sym in [("S&P 500", "ES=F"), ("Nasdaq", "NQ=F"), ("Dow", "YM=F")]:
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = ((price - prev) / prev) * 100
                results.append({"name": name, "price": price, "pct": pct, "positive": pct >= 0})
            else:
                results.append({"name": name, "price": None, "pct": 0.0, "positive": True})
        except Exception as e:
            results.append({"name": name, "price": None, "pct": 0.0, "positive": True, "error": str(e)})
    return results


def get_closing_snapshot() -> list:
    results = []
    for name, sym in [("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Dow", "^DJI")]:
        try:
            t     = yf.Ticker(sym)
            info  = t.fast_info
            full  = t.info
            price = info.last_price
            prev  = info.previous_close
            low52  = full.get("fiftyTwoWeekLow")
            high52 = full.get("fiftyTwoWeekHigh")
            if price and prev:
                pct = ((price - prev) / prev) * 100
                context = ""
                if price and low52 and high52 and high52 != low52:
                    rng = (price - low52) / (high52 - low52) * 100
                    if rng >= 90:
                        context = "near 52w high"
                    elif rng <= 10:
                        context = "near 52w low"
                    else:
                        context = f"{rng:.0f}% of 52w range"
                results.append({"name": name, "price": price, "pct": pct,
                                 "positive": pct >= 0, "context": context})
            else:
                results.append({"name": name, "price": None, "pct": 0.0,
                                 "positive": True, "context": ""})
        except Exception as e:
            results.append({"name": name, "price": None, "pct": 0.0,
                             "positive": True, "context": "", "error": str(e)})
    return results


def get_headlines(max_count: int = 5) -> list:
    if NEWS_API_KEY:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={"category": "business", "country": "us",
                        "pageSize": 10, "apiKey": NEWS_API_KEY},
                timeout=10,
            )
            if resp.status_code == 200:
                articles = resp.json().get("articles", [])
                titles = [a["title"] for a in articles
                          if a.get("title") and len(a["title"]) > 20][:max_count]
                if titles:
                    return titles
        except Exception:
            pass

    # yfinance fallback
    skip  = ["beginner", "guide", "how to", "what is", "explainer", "glossary", "tutorial"]
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
        if len(titles) >= max_count + 3:
            break
    return titles[:max_count] or ["Check finance.yahoo.com for today's headlines."]


def get_sector_performance() -> list:
    results = []
    for sector, sym in SECTOR_ETFS.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = ((price - prev) / prev) * 100
                results.append({"sector": sector, "etf": sym, "pct": pct, "positive": pct >= 0})
            else:
                results.append({"sector": sector, "etf": sym, "pct": 0.0, "positive": True})
        except Exception:
            results.append({"sector": sector, "etf": sym, "pct": 0.0, "positive": True})
    results.sort(key=lambda x: x["pct"], reverse=True)
    return results


def get_market_movers() -> dict:
    scan = [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
        "AVGO", "NOW", "CRWD", "PLTR", "DDOG", "CELH", "AXON", "HIMS",
        "APP", "VRT", "ANET", "SOUN", "TTD", "MELI", "HOOD", "DECK", "LULU",
    ]
    results = []
    for ticker in scan:
        try:
            info  = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = ((price - prev) / prev) * 100
                results.append({"symbol": ticker, "pct": round(pct, 2)})
        except Exception:
            continue
    results.sort(key=lambda x: x["pct"], reverse=True)
    return {"gainers": results[:3], "losers": results[-3:][::-1]}


def get_watchlist_snapshot(flagged_tickers: list) -> list:
    results = []
    for ticker in flagged_tickers:
        try:
            info  = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = ((price - prev) / prev) * 100
                results.append({"ticker": ticker, "price": price,
                                 "pct": pct, "positive": pct >= 0})
            else:
                results.append({"ticker": ticker, "price": None, "pct": 0.0, "positive": True})
        except Exception:
            results.append({"ticker": ticker, "price": None, "pct": 0.0, "positive": True})
    return results


def get_economic_events() -> list:
    events = []
    today_str = date.today().isoformat()
    watch = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
             "GS", "WMT", "COST", "NKE", "FDX", "ORCL", "ADBE", "CRM",
             "INTC", "AMD", "NFLX", "DIS"]
    for ticker in watch:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    for d in (ed if isinstance(ed, list) else [ed]):
                        if str(d)[:10] == today_str:
                            events.append({"event": f"{ticker} reports earnings", "impact": "High"})
            elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
                for d in cal.loc["Earnings Date"]:
                    if str(d)[:10] == today_str:
                        events.append({"event": f"{ticker} reports earnings", "impact": "High"})
        except Exception:
            continue
    return events[:6] if events else [
        {"event": "No major earnings scheduled. See marketwatch.com/economy-politics/calendar.", "impact": ""}
    ]


# ── Monthly picks ─────────────────────────────────────────────────────────────
def risk_level(beta, market_cap, rev_growth) -> str:
    if beta is None:
        beta = 1.0
    large_cap = market_cap and market_cap > 50_000_000_000
    if beta < 0.8 and large_cap:
        return "Low"
    if beta < 1.3 and large_cap:
        return "Medium"
    if beta < 2.0:
        return "High"
    return "Speculative"


def get_monthly_picks() -> list:
    month_key = date.today().strftime("%Y-%m")
    cache = {}
    if os.path.exists(PICKS_CACHE_FILE):
        try:
            with open(PICKS_CACHE_FILE) as f:
                cache = json.load(f)
        except Exception:
            pass
    if month_key in cache and cache[month_key]:
        return cache[month_key]

    print("  • Generating monthly picks via yfinance (~60s)…")
    candidates = []
    for ticker in WATCHLIST:
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            if not info or not info.get("currentPrice"):
                continue
            price        = info.get("currentPrice", 0)
            target       = info.get("targetMeanPrice") or price
            upside       = ((target - price) / price * 100) if price else 0
            rec          = info.get("recommendationKey", "").lower()
            sector       = info.get("sector", "—")
            company      = info.get("longName", ticker)
            beta         = info.get("beta")
            market_cap   = info.get("marketCap")
            rev_growth   = info.get("revenueGrowth")
            earn_growth  = info.get("earningsGrowth")
            fwd_pe       = info.get("forwardPE")
            profit_margin = info.get("profitMargins")
            is_known     = ticker in WELL_KNOWN

            parts = []
            if upside > 5:
                parts.append(f"analyst target implies {upside:.0f}% upside (${price:.0f} → ${target:.0f})")
            if rec in ("buy", "strong_buy"):
                parts.append(f"rated {rec.replace('_', ' ')} by analysts")
            if earn_growth and earn_growth > 0.1:
                parts.append(f"earnings up {earn_growth*100:.0f}% YoY")
            if rev_growth and rev_growth > 0.05:
                parts.append(f"revenue growing {rev_growth*100:.0f}% YoY")
            if fwd_pe and fwd_pe < 30:
                parts.append(f"forward P/E {fwd_pe:.1f}x")
            if profit_margin and profit_margin > 0.15:
                parts.append(f"{profit_margin*100:.0f}% profit margin")
            if not parts:
                parts.append(f"{sector} name with improving fundamentals")

            rationale = ". ".join(parts[:3]) + "."
            risk      = risk_level(beta, market_cap, rev_growth)
            score     = upside + (20 if rec in ("buy", "strong_buy") else 0)
            candidates.append({
                "ticker": ticker, "company": company, "sector": sector,
                "risk": risk, "score": score, "is_known": is_known, "rationale": rationale,
            })
            time.sleep(0.1)
        except Exception:
            continue

    known  = sorted([c for c in candidates if c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    hidden = sorted([c for c in candidates if not c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    clean  = [{k: v for k, v in p.items() if k not in ("score", "is_known")} for p in known + hidden]
    cache[month_key] = clean
    with open(PICKS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return clean


# ── HTML helpers ──────────────────────────────────────────────────────────────
def _section(title: str, body: str) -> str:
    return (
        f'<h2 style="font-size:12px;text-transform:uppercase;letter-spacing:.07em;'
        f'color:#6b7280;margin:26px 0 8px;border-bottom:1px solid #e5e7eb;padding-bottom:4px">'
        f'{title}</h2><div>{body}</div>'
    )


def _base_html(title: str, subtitle: str, body: str) -> str:
    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        f'max-width:620px;margin:0 auto;padding:24px 20px;color:#111;background:#fff;'
        f'font-size:15px;line-height:1.6">'
        f'<h1 style="font-size:22px;margin:0 0 2px;font-weight:700">{title}</h1>'
        f'<p style="margin:0 0 20px;color:#6b7280;font-size:13px">{subtitle}</p>'
        f'<hr style="border:none;border-top:2px solid #111;margin:0 0 4px">'
        f'{body}'
        f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:28px 0 10px">'
        f'<p style="font-size:11px;color:#9ca3af;margin:0">'
        f'Sent by OpenBell — automated market briefing. Not financial advice.</p>'
        f'</body></html>'
    )


def _color(positive: bool) -> str:
    return "#16a34a" if positive else "#dc2626"


def _fmt_pct(pct: float) -> str:
    return f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%"


def _price_row(name: str, price, pct: float, positive: bool, extra: str = "") -> str:
    if price is None:
        return (f'<tr><td style="padding:5px 14px 5px 0;font-weight:600">{name}</td>'
                f'<td colspan="3" style="color:#9ca3af">Unavailable</td></tr>')
    extra_td = f'<td style="padding:5px 0;font-size:12px;color:#6b7280">{extra}</td>' if extra else ""
    return (
        f'<tr><td style="padding:5px 14px 5px 0;font-weight:600">{name}</td>'
        f'<td style="padding:5px 14px 5px 0">${price:,.2f}</td>'
        f'<td style="padding:5px {"14px" if extra else "0"} 5px 0;'
        f'color:{_color(positive)};font-weight:700">{_fmt_pct(pct)}</td>'
        f'{extra_td}</tr>'
    )


# ── Email: Morning Briefing ───────────────────────────────────────────────────
def send_morning_briefing():
    today_str = date.today().strftime("%A, %B %d")
    print(f"[OpenBell] Morning Briefing — {today_str}")

    mem    = load_memory()
    errors = []

    try:
        print("  • Fetching futures…")
        futures = get_futures_premarket()
    except Exception as e:
        futures = []; errors.append(f"Futures: {e}")

    try:
        print("  • Fetching headlines…")
        headlines = get_headlines(5)
    except Exception as e:
        headlines = ["Headlines unavailable."]; errors.append(f"Headlines: {e}")

    try:
        print("  • Fetching calendar…")
        econ_events = get_economic_events()
    except Exception as e:
        econ_events = [{"event": "Calendar unavailable.", "impact": ""}]; errors.append(f"Calendar: {e}")

    watchlist_data = []
    if mem.get("flagged_tickers"):
        try:
            print(f"  • Fetching watchlist ({', '.join(mem['flagged_tickers'])})…")
            watchlist_data = get_watchlist_snapshot(mem["flagged_tickers"])
        except Exception as e:
            errors.append(f"Watchlist: {e}")

    try:
        print("  • Loading monthly picks…")
        picks = get_monthly_picks()
    except Exception as e:
        picks = []; errors.append(f"Picks: {e}")

    # Claude: What to Watch Today
    print("  • Claude: What to Watch Today…")
    futures_str  = "; ".join(f"{f['name']} {_fmt_pct(f['pct'])}" for f in futures if f.get("price")) or "unavailable"
    headlines_str = "\n".join(f"- {h}" for h in headlines[:5])
    what_to_watch = call_claude(
        f"You are Pippy, writing 'What to Watch Today' in a morning briefing email.\n"
        f"Today: {today_str}. Pre-market futures: {futures_str}.\n"
        f"Today's headlines:\n{headlines_str}\n\n"
        f"Write 2-3 plain sentences reasoning about what the futures and headlines mean for today's session. "
        f"Reference actual numbers. No markdown, no bullets.",
        max_tokens=300,
    )

    # Claude: one-sentence headline commentary
    print("  • Claude: headline commentary…")
    headlines_with_comment = []
    if headlines and "unavailable" not in headlines[0].lower():
        raw_comments = call_claude(
            f"For each headline, write ONE sentence (max 15 words) on why it matters to investors.\n"
            f"Reply with just the sentences, one per line, same order.\n\n"
            + "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines[:5])),
            max_tokens=400,
        )
        comment_lines = [l.lstrip("0123456789. ").strip() for l in raw_comments.split("\n") if l.strip()]
        for i, h in enumerate(headlines[:5]):
            headlines_with_comment.append({"headline": h, "comment": comment_lines[i] if i < len(comment_lines) else ""})
    else:
        headlines_with_comment = [{"headline": h, "comment": ""} for h in headlines]

    # Build HTML sections
    futures_rows = "".join(_price_row(f["name"], f.get("price"), f["pct"], f["positive"]) for f in futures)
    s1 = _section("Pre-Market Snapshot",
                  f'<table style="border-collapse:collapse;font-size:15px">{futures_rows}</table>')

    s2 = _section("What to Watch Today",
                  f'<p style="margin:0;line-height:1.7">{what_to_watch}</p>')

    hl_items = "".join(
        f'<li style="margin:8px 0"><span style="font-weight:500">{h["headline"]}</span>'
        + (f'<br><span style="font-size:13px;color:#6b7280">{h["comment"]}</span>' if h["comment"] else "")
        + "</li>"
        for h in headlines_with_comment
    )
    s3 = _section("Top Market Headlines",
                  f'<ul style="margin:0;padding-left:20px;font-size:14px">{hl_items}</ul>')

    if "unavailable" in (econ_events[0].get("event","")).lower():
        econ_body = f'<p style="color:#9ca3af">{econ_events[0]["event"]}</p>'
    else:
        risk_map = {"High": "#dc2626", "Medium": "#d97706"}
        econ_rows = "".join(
            f'<tr><td style="padding:4px 14px 4px 0">{e["event"]}</td>'
            f'<td style="padding:4px 0;color:{risk_map.get(e.get("impact",""),"#9ca3af")};'
            f'font-size:12px;font-weight:700">{e.get("impact","")}</td></tr>'
            for e in econ_events
        )
        econ_body = f'<table style="border-collapse:collapse;font-size:14px">{econ_rows}</table>'
    s4 = _section("This Week's Calendar", econ_body)

    s5 = ""
    if watchlist_data:
        wl_rows = "".join(_price_row(w["ticker"], w.get("price"), w["pct"], w["positive"]) for w in watchlist_data)
        s5 = _section("Your Watchlist",
                      f'<table style="border-collapse:collapse;font-size:15px">{wl_rows}</table>')

    risk_colors = {"Low": "#16a34a", "Medium": "#d97706", "High": "#dc2626", "Speculative": "#7c3aed"}
    picks_rows = "".join(
        f'<tr style="border-bottom:1px solid #f3f4f6">'
        f'<td style="padding:6px 12px 6px 0;font-weight:700">{p["ticker"]}</td>'
        f'<td style="padding:6px 12px 6px 0">{p["company"]}</td>'
        f'<td style="padding:6px 12px 6px 0;color:#6b7280;font-size:13px">{p["sector"]}</td>'
        f'<td style="padding:6px 12px 6px 0;color:{risk_colors.get(p.get("risk","Medium"),"#d97706")};'
        f'font-size:12px;font-weight:700">{p.get("risk","—")}</td>'
        f'<td style="padding:6px 0;font-size:13px">{p["rationale"]}</td></tr>'
        for p in picks
    ) if picks else '<tr><td style="color:#9ca3af">No picks available.</td></tr>'
    s6 = _section("Monthly Stock Picks",
        f'<table style="border-collapse:collapse;font-size:14px;width:100%">'
        f'<tr style="border-bottom:2px solid #e5e7eb;font-size:11px;text-transform:uppercase;color:#9ca3af">'
        f'<td style="padding:3px 12px 3px 0">Ticker</td><td style="padding:3px 12px 3px 0">Company</td>'
        f'<td style="padding:3px 12px 3px 0">Sector</td><td style="padding:3px 12px 3px 0">Risk</td>'
        f'<td style="padding:3px 0">Rationale</td></tr>'
        f'{picks_rows}</table>'
        f'<p style="font-size:11px;color:#9ca3af;margin:6px 0 0">Refreshed monthly. Informational only.</p>')

    errors_html = ""
    if errors:
        errors_html = _section("Notes",
            f'<p style="font-size:12px;color:#9ca3af">Fetch issues: {"; ".join(errors)}</p>')

    today_label = date.today().strftime("%A, %B %d")
    html = _base_html("OpenBell ☀️", f"{today_label} — Morning Briefing",
                      s1 + s2 + s3 + s4 + s5 + s6 + errors_html)
    _send_email(f"OpenBell ☀️ — {today_label} Morning Briefing", html)

    mem["last_email_sent"]    = datetime.now().isoformat()
    sp = next((f for f in futures if f["name"] == "S&P 500"), None)
    direction = "up" if sp and sp["positive"] else "down"
    mem["last_email_summary"] = f"Morning briefing sent {today_label}. S&P futures {direction}."
    save_memory(mem)
    print(f"[OpenBell] Morning Briefing sent to {TO_EMAIL}.")


# ── Email: Market Close Summary ───────────────────────────────────────────────
def send_close_summary():
    today_label = date.today().strftime("%A, %B %d")
    print(f"[OpenBell] Market Close Summary — {today_label}")

    mem    = load_memory()
    errors = []

    try:
        print("  • Fetching closing prices…")
        closing = get_closing_snapshot()
    except Exception as e:
        closing = []; errors.append(f"Closing snapshot: {e}")

    try:
        print("  • Fetching headlines…")
        headlines = get_headlines(5)
    except Exception as e:
        headlines = []; errors.append(f"Headlines: {e}")

    try:
        print("  • Fetching movers…")
        movers = get_market_movers()
    except Exception as e:
        movers = {"gainers": [], "losers": []}; errors.append(f"Movers: {e}")

    try:
        print("  • Fetching sectors…")
        sectors = get_sector_performance()
    except Exception as e:
        sectors = []; errors.append(f"Sectors: {e}")

    watchlist_data = []
    if mem.get("flagged_tickers"):
        try:
            print("  • Fetching watchlist EOD…")
            watchlist_data = get_watchlist_snapshot(mem["flagged_tickers"])
        except Exception as e:
            errors.append(f"Watchlist: {e}")

    # Claude: Today's Story
    print("  • Claude: Today's Story…")
    closing_str  = "; ".join(f"{c['name']} {_fmt_pct(c['pct'])}" for c in closing if c.get("price")) or "unavailable"
    headlines_str = "\n".join(f"- {h}" for h in headlines[:5])
    todays_story = call_claude(
        f"You are Pippy, writing 'Today's Story' in a market close email.\n"
        f"Today: {today_label}. Closing prices: {closing_str}.\n"
        f"Today's headlines:\n{headlines_str}\n\n"
        f"Write 3-4 plain sentences explaining what drove the market today — cause and effect. "
        f"Reference actual numbers and specific news. No markdown, no bullets.",
        max_tokens=400,
    )

    # Claude: movers commentary
    print("  • Claude: movers commentary…")
    movers_commentary: dict = {}
    all_movers = movers["gainers"] + movers["losers"]
    if all_movers:
        raw = call_claude(
            f"For each stock, write ONE sentence (max 12 words) on why it moved today.\n"
            f"Reply as SYMBOL: reason, one per line.\n\n"
            + "\n".join(f"{m['symbol']}: {m['pct']:+.2f}%" for m in all_movers),
            max_tokens=300,
        )
        for line in raw.split("\n"):
            if ":" in line:
                sym, reason = line.split(":", 1)
                movers_commentary[sym.strip().upper()] = reason.strip()

    # Claude: watchlist commentary
    watchlist_commentary: dict = {}
    if watchlist_data:
        print("  • Claude: watchlist commentary…")
        sp_pct = next((c["pct"] for c in closing if c["name"] == "S&P 500"), 0.0)
        wl_lines = "\n".join(
            f"{w['ticker']}: {w['pct']:+.2f}% (S&P {sp_pct:+.2f}%)"
            for w in watchlist_data if w.get("price")
        )
        raw = call_claude(
            f"For each ticker, write ONE sentence on how it performed vs the broader market.\n"
            f"Reply as TICKER: sentence, one per line.\n\n{wl_lines}",
            max_tokens=300,
        )
        for line in raw.split("\n"):
            if ":" in line:
                sym, comment = line.split(":", 1)
                watchlist_commentary[sym.strip().upper()] = comment.strip()

    # Claude: What to Watch Tomorrow
    print("  • Claude: What to Watch Tomorrow…")
    what_tomorrow = call_claude(
        f"You are Pippy. Write 2 plain sentences on what to watch in tomorrow's market session.\n"
        f"Base it on: today's close ({closing_str}) and any upcoming calendar events.\n"
        f"No markdown. Be specific.",
        max_tokens=200,
    )

    # Build HTML
    close_rows = "".join(
        _price_row(c["name"], c.get("price"), c["pct"], c["positive"], c.get("context",""))
        for c in closing
    )
    s1 = _section("Closing Snapshot",
                  f'<table style="border-collapse:collapse;font-size:15px">{close_rows}</table>')

    s2 = _section("Today's Story",
                  f'<p style="margin:0;line-height:1.7">{todays_story}</p>')

    def _mover_block(items: list, positive: bool, label: str) -> str:
        color = _color(positive)
        rows  = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;font-weight:600">{m["symbol"]}</td>'
            f'<td style="padding:4px 12px 4px 0;color:{color};font-weight:700">{m["pct"]:+.2f}%</td>'
            f'<td style="padding:4px 0;font-size:13px;color:#6b7280">{movers_commentary.get(m["symbol"],"")}</td></tr>'
            for m in items
        )
        return (f'<div style="margin-right:24px">'
                f'<div style="font-size:11px;font-weight:700;color:{color};margin-bottom:4px;text-transform:uppercase">{label}</div>'
                f'<table style="border-collapse:collapse;font-size:14px">{rows}</table></div>')

    movers_html = (
        f'<div style="display:flex;flex-wrap:wrap">'
        f'{_mover_block(movers["gainers"], True, "Gainers")}'
        f'{_mover_block(movers["losers"], False, "Losers")}'
        f'</div>'
    )
    s3 = _section("Top Movers", movers_html)

    best  = sectors[0] if sectors else None
    worst = sectors[-1] if sectors else None
    sector_rows = "".join(
        f'<tr style="{"background:#f0fdf4" if i == 0 else "background:#fef2f2" if i == len(sectors)-1 else ""}">'
        f'<td style="padding:4px 14px 4px 0">{s["sector"]}</td>'
        f'<td style="padding:4px 0;color:{_color(s["positive"])};font-weight:700">{s["pct"]:+.2f}%</td></tr>'
        for i, s in enumerate(sectors)
    )
    best_worst = ""
    if best and worst:
        best_worst = (
            f'<p style="font-size:13px;color:#6b7280;margin:6px 0 0">'
            f'Best: <strong>{best["sector"]}</strong> {best["pct"]:+.2f}% &nbsp;|&nbsp; '
            f'Worst: <strong>{worst["sector"]}</strong> {worst["pct"]:+.2f}%</p>'
        )
    s4 = _section("Sector Performance",
                  f'<table style="border-collapse:collapse;font-size:14px">{sector_rows}</table>{best_worst}')

    s5 = ""
    if watchlist_data:
        wl_rows = "".join(
            f'<tr><td style="padding:5px 12px 5px 0;font-weight:600">{w["ticker"]}</td>'
            f'<td style="padding:5px 12px 5px 0">${w["price"]:,.2f}</td>'
            f'<td style="padding:5px 12px 5px 0;color:{_color(w["positive"])};font-weight:700">{_fmt_pct(w["pct"])}</td>'
            f'<td style="padding:5px 0;font-size:13px;color:#6b7280">{watchlist_commentary.get(w["ticker"],"")}</td></tr>'
            if w.get("price") else
            f'<tr><td style="padding:5px 12px 5px 0;font-weight:600">{w["ticker"]}</td>'
            f'<td colspan="3" style="color:#9ca3af">N/A</td></tr>'
            for w in watchlist_data
        )
        s5 = _section("Your Watchlist EOD",
                      f'<table style="border-collapse:collapse;font-size:15px">{wl_rows}</table>')

    s6 = _section("What to Watch Tomorrow",
                  f'<p style="margin:0;line-height:1.7">{what_tomorrow}</p>')

    errors_html = ""
    if errors:
        errors_html = _section("Notes",
            f'<p style="font-size:12px;color:#9ca3af">Fetch issues: {"; ".join(errors)}</p>')

    html = _base_html("OpenBell 📊", f"{today_label} — Market Close Summary",
                      s1 + s2 + s3 + s4 + s5 + s6 + errors_html)
    _send_email(f"OpenBell 📊 — {today_label} Market Close", html)

    mem["last_email_sent"] = datetime.now().isoformat()
    sp = next((c for c in closing if c["name"] == "S&P 500"), None)
    direction = "up" if sp and sp["positive"] else "down"
    story_short = (todays_story[:120].rstrip(".") + ".") if todays_story and not todays_story.startswith("[") else f"S&P {direction} on the day."
    mem["last_email_summary"] = story_short
    save_memory(mem)
    print(f"[OpenBell] Market Close Summary sent to {TO_EMAIL}.")


# ── Email: Deep Dive ──────────────────────────────────────────────────────────
def send_deep_dive():
    today_label = date.today().strftime("%A, %B %d")
    print(f"[OpenBell] Deep Dive — {today_label}")

    mem     = load_memory()
    history = mem.get("deep_dive_history", [])
    recent  = [h["category"] for h in history[-3:]]
    avail   = [c for c in DEEP_DIVE_CATEGORIES if c not in recent] or DEEP_DIVE_CATEGORIES

    interests_str = ", ".join(mem.get("interests", [])) or "general market topics"
    flagged_str   = ", ".join(mem.get("flagged_tickers", [])) or "none"
    cats_str      = "\n".join(f"- {c}" for c in avail)

    print("  • Claude: generating deep dive…")
    raw = call_claude(
        f"You are Pippy, writing a Weekend/Holiday Deep Dive for an engaged retail investor.\n"
        f"Today: {today_label}. User interests: {interests_str}. Watchlist: {flagged_str}.\n"
        f"Recent categories (avoid these): {', '.join(recent) or 'none'}\n\n"
        f"Choose ONE category from:\n{cats_str}\n\n"
        f"Write the deep dive with these exact labels (no markdown, no asterisks, no # headers):\n\n"
        f"CATEGORY: [exact category from the list]\n\n"
        f"TOPIC: [one line — what today's deep dive is about]\n\n"
        f"THE SETUP: [2-3 sentences on why this is relevant right now]\n\n"
        f"THE DEEP DIVE: [4-6 sentences — numbers, context, what is happening and why it matters]\n\n"
        f"WHAT TO WATCH: [2 sentences on what to track going forward]\n\n"
        f"PIPPY'S TAKE: [one direct opinion or prediction from Pippy — confident, no hedging]",
        max_tokens=900,
    )

    def _extract(label: str) -> str:
        m = re.search(rf"{re.escape(label)}:\s*(.*?)(?=\n[A-Z ]+:|$)", raw, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    chosen_category = _extract("CATEGORY") or avail[0]
    topic      = _extract("TOPIC") or "Market Deep Dive"
    setup      = _extract("THE SETUP") or raw
    dive       = _extract("THE DEEP DIVE")
    what_watch = _extract("WHAT TO WATCH")
    take       = _extract("PIPPY'S TAKE")

    s1 = _section("Today's Topic",
                  f'<p style="font-size:17px;font-weight:600;margin:0">{topic}</p>')
    s2 = _section("The Setup",
                  f'<p style="margin:0;line-height:1.7">{setup}</p>')
    s3 = _section("The Deep Dive",
                  f'<p style="margin:0;line-height:1.7">{dive}</p>') if dive else ""
    s4 = _section("What to Watch",
                  f'<p style="margin:0;line-height:1.7">{what_watch}</p>') if what_watch else ""
    s5 = _section("Pippy's Take",
                  f'<p style="margin:0;font-weight:700;font-size:16px;line-height:1.6">{take}</p>') if take else ""

    html = _base_html("OpenBell 📚", f"{today_label} — Deep Dive", s1 + s2 + s3 + s4 + s5)
    _send_email(f"OpenBell 📚 — {today_label} Deep Dive", html)

    history.append({"date": date.today().isoformat(), "category": chosen_category})
    mem["deep_dive_history"] = history[-10:]
    mem["last_email_sent"]   = datetime.now().isoformat()
    mem["last_email_summary"] = f"Deep dive sent {today_label}: {topic}"
    save_memory(mem)
    print(f"[OpenBell] Deep Dive sent to {TO_EMAIL}.")


# ── Send helper ───────────────────────────────────────────────────────────────
def _send_email(subject: str, html: str) -> None:
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"OpenBell <{GMAIL_ADDRESS}>"
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, TO_EMAIL, msg.as_string())


# ── Scheduled jobs ────────────────────────────────────────────────────────────
def job_morning():
    if is_market_open_today():
        send_morning_briefing()
    else:
        send_deep_dive()


def job_close():
    if is_market_open_today():
        send_close_summary()
    else:
        print("[OpenBell] Market closed today — skipping close summary.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="OpenBell email scheduler")
    parser.add_argument(
        "--now",
        choices=["morning", "close", "deepdive", "auto"],
        help="Send one email immediately and exit. 'auto' uses schedule logic.",
    )
    args = parser.parse_args()

    if args.now == "morning":
        send_morning_briefing()
    elif args.now == "close":
        send_close_summary()
    elif args.now == "deepdive":
        send_deep_dive()
    elif args.now == "auto":
        job_morning()
    else:
        print("[OpenBell] Scheduler starting (Central Time)…")
        print("  8:30 AM CT → Morning Briefing (weekdays) or Deep Dive (weekends/holidays)")
        print("  3:00 PM CT → Market Close Summary (weekdays only)")
        schedule.every().day.at("08:30").do(job_morning)
        schedule.every().day.at("15:00").do(job_close)
        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    main()
