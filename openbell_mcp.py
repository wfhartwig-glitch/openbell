#!/usr/bin/env python3
"""
OpenBell MCP Server — exposes market data tools and email delivery.
Claude Code calls these tools and uses its own intelligence to compose
the briefing, so no separate Anthropic API key is needed.
"""

import json
import os
import smtplib
import time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import yfinance as yf
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL           = os.getenv("TO_EMAIL")
FMP_API_KEY        = os.getenv("FMP_API_KEY")

PICKS_CACHE_FILE = "picks_cache.json"
FMP_BASE         = "https://financialmodelingprep.com/api/v3"

FUTURES_MAP = {
    "S&P 500": "ES=F",
    "Nasdaq":  "NQ=F",
    "Dow":     "YM=F",
}

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "V", "UNH",
    "LLY", "XOM", "AVGO", "MA", "HD",
    "PG", "JNJ", "MRK", "COST", "ABBV",
]

mcp = FastMCP("OpenBell")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_futures() -> str:
    """Fetch pre-market futures for S&P 500, Nasdaq, and Dow Jones."""
    results = []
    for name, ticker in FUTURES_MAP.items():
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct   = ((price - prev) / prev) * 100
                arrow = "▲" if pct >= 0 else "▼"
                results.append(f"{name}: {price:,.2f}  {arrow} {abs(pct):.2f}%")
            else:
                results.append(f"{name}: N/A")
        except Exception as e:
            results.append(f"{name}: Error — {e}")
    return "\n".join(results)


@mcp.tool()
def get_headlines() -> str:
    """Fetch top 5 market-moving news headlines from Financial Modeling Prep."""
    try:
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/stock_news",
            params={"limit": 5, "apikey": FMP_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json()
        lines = [f"{i+1}. {a['title']} ({a.get('symbol','')})" for i, a in enumerate(articles)]
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch headlines: {e}"


@mcp.tool()
def get_economic_events() -> str:
    """Fetch today's economic calendar events from Financial Modeling Prep."""
    today = date.today().isoformat()
    try:
        resp = requests.get(
            f"{FMP_BASE}/economic_calendar",
            params={"from": today, "to": today, "apikey": FMP_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json()
        important = [e for e in events if e.get("impact") in ("High", "Medium")]
        show = important[:6] if important else events[:6]
        if not show:
            return "No major economic events scheduled today."
        lines = []
        for e in show:
            t      = e.get("date", "")[-8:-3] if e.get("date") else ""
            impact = e.get("impact", "")
            lines.append(f"{t}  {e.get('event','')}  [{impact}]")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch economic calendar: {e}"


@mcp.tool()
def get_market_movers() -> str:
    """Fetch today's top gaining, losing, and most active US stocks from FMP."""
    sections = []
    for label, path in [("Top Gainers", "gainers"), ("Top Losers", "losers"), ("Most Active", "actives")]:
        try:
            resp = requests.get(
                f"{FMP_BASE}/stock_market/{path}",
                params={"apikey": FMP_API_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            stocks = resp.json()[:4]
            lines  = [f"  {s.get('symbol','?')} {s.get('name','')} {s.get('changesPercentage','?')}%" for s in stocks]
            sections.append(f"{label}:\n" + "\n".join(lines))
        except Exception as e:
            sections.append(f"{label}: Error — {e}")
    return "\n\n".join(sections)


@mcp.tool()
def get_monthly_picks() -> str:
    """
    Return the top 5 analyst-consensus stock picks for this month.
    Cached in picks_cache.json — only hits FMP on the 1st of each month
    or when the cache is missing.
    """
    month_key = date.today().strftime("%Y-%m")

    if os.path.exists(PICKS_CACHE_FILE):
        with open(PICKS_CACHE_FILE) as f:
            cache = json.load(f)
        if month_key in cache:
            picks = cache[month_key]
            lines = []
            for p in picks:
                lines.append(
                    f"{p['ticker']} — {p['company']} ({p['sector']})\n"
                    f"  {p['rationale']}"
                )
            return "\n\n".join(lines)
    else:
        cache = {}

    # Build fresh picks from FMP analyst recommendations
    candidates = []
    for ticker in WATCHLIST:
        try:
            rec_resp = requests.get(
                f"{FMP_BASE}/analyst-stock-recommendations/{ticker}",
                params={"apikey": FMP_API_KEY},
                timeout=8,
            )
            rec_resp.raise_for_status()
            data = rec_resp.json()
            if not data:
                continue
            latest     = data[0]
            strong_buy = latest.get("analystRatingsbuy", 0) + latest.get("analystRatingsStrongBuy", 0)
            total      = sum([
                latest.get("analystRatingsbuy", 0),
                latest.get("analystRatingsStrongBuy", 0),
                latest.get("analystRatingsHold", 0),
                latest.get("analystRatingsSell", 0),
                latest.get("analystRatingsStrongSell", 0),
            ])
            if total == 0:
                continue

            profile_resp = requests.get(
                f"{FMP_BASE}/profile/{ticker}",
                params={"apikey": FMP_API_KEY},
                timeout=8,
            )
            profile_resp.raise_for_status()
            profiles = profile_resp.json()
            profile  = profiles[0] if profiles else {}

            candidates.append({
                "ticker":  ticker,
                "company": profile.get("companyName", ticker),
                "sector":  profile.get("sector", "—"),
                "buy_pct": strong_buy / total,
                "rationale": (
                    f"{int(strong_buy/total*100)}% of {total} analysts rate it Buy/Strong Buy. "
                    f"Industry: {profile.get('industry','—')}."
                ),
            })
            time.sleep(0.15)
        except Exception:
            continue

    candidates.sort(key=lambda x: x["buy_pct"], reverse=True)
    picks = [
        {k: v for k, v in p.items() if k != "buy_pct"}
        for p in candidates[:5]
    ]

    cache[month_key] = picks
    with open(PICKS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    lines = []
    for p in picks:
        lines.append(f"{p['ticker']} — {p['company']} ({p['sector']})\n  {p['rationale']}")
    return "\n\n".join(lines)


@mcp.tool()
def send_briefing_email(
    futures: str,
    headlines: str,
    economic_events: str,
    what_to_watch: str,
    monthly_picks: str,
) -> str:
    """
    Assemble and send the OpenBell briefing email via Gmail SMTP.

    Args:
        futures: Output from get_futures()
        headlines: Output from get_headlines()
        economic_events: Output from get_economic_events()
        what_to_watch: 3-5 sentence plain-English market summary written by Claude
        monthly_picks: Output from get_monthly_picks()
    """
    today     = date.today()
    today_str = today.strftime("%A, %B %d, %Y")
    subject   = f"OpenBell — {today.strftime('%A, %B %d')} Market Brief"

    def section(title: str, body: str) -> str:
        return (
            f'<h2 style="font-size:15px;text-transform:uppercase;letter-spacing:.05em;'
            f'color:#374151;margin:0 0 10px">{title}</h2>'
            f'<pre style="font-family:Georgia,serif;font-size:14px;line-height:1.7;'
            f'white-space:pre-wrap;margin:0 0 24px">{body}</pre>'
        )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Georgia,serif;max-width:640px;margin:0 auto;padding:24px;color:#111;background:#fff">
  <h1 style="font-size:22px;margin:0 0 4px">OpenBell</h1>
  <p style="margin:0 0 24px;color:#6b7280;font-size:13px">{today_str} &mdash; Pre-Market Briefing</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px">
  {section("Futures Snapshot", futures)}
  {section("Top Headlines", headlines)}
  {section("Economic Events Today", economic_events)}
  {section("What to Watch", what_to_watch)}
  {section("Top 5 Picks of the Month", monthly_picks)}
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:28px 0 12px">
  <p style="font-size:11px;color:#9ca3af;margin:0">
    Sent by OpenBell &mdash; not financial advice.
  </p>
</body>
</html>"""

    text = "\n\n".join([
        f"OPENBELL — {today_str}",
        "FUTURES SNAPSHOT\n" + futures,
        "TOP HEADLINES\n" + headlines,
        "ECONOMIC EVENTS TODAY\n" + economic_events,
        "WHAT TO WATCH\n" + what_to_watch,
        "TOP 5 PICKS OF THE MONTH\n" + monthly_picks,
        "Not financial advice. Sent by OpenBell.",
    ])

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"OpenBell <{GMAIL_ADDRESS}>"
    msg["To"]      = TO_EMAIL

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, TO_EMAIL, msg.as_string())

    return f"Email sent to {TO_EMAIL} with subject: {subject}"


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
