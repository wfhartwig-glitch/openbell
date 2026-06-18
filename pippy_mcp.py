#!/usr/bin/env python3
"""
Pippy MCP Server — all data tools Pippy needs, exposed over MCP for local
terminal use and importable as plain Python functions for openbell.py cloud runs.
"""

import json
import os
import smtplib
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import yfinance as yf
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

PROJECT_DIR        = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE        = os.path.join(PROJECT_DIR, "pippy_memory.json")
PICKS_FILE         = os.path.join(PROJECT_DIR, "picks_cache.json")
GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL           = os.getenv("TO_EMAIL")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")

DEFAULT_MEMORY = {
    "interests": [],
    "mentioned_stocks": {},
    "expressed_preferences": [],
    "frequent_questions": [],
    "flagged_tickers": [],
    "recurring_themes": {},
    "session_count": 0,
    "last_session": "",
    "last_morning_brief": {},
    "last_close_summary": {},
    "last_deep_dive": {},
    "deep_dive_history": [],
    "email_count": 0,
    "last_email_sent": "",
    "last_email_summary": "",
}

NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

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

mcp = FastMCP("Pippy")


# ── Tool implementations (plain Python — importable from openbell.py) ─────────

def is_market_open_today() -> str:
    today = date.today()
    if today.weekday() >= 5:
        return json.dumps({"open": False, "reason": "weekend"})
    if today.isoformat() in NYSE_HOLIDAYS:
        return json.dumps({"open": False, "reason": "NYSE holiday"})
    return json.dumps({"open": True, "reason": "regular trading day"})


def fetch_market_snapshot() -> str:
    now  = datetime.now()
    mins = now.hour * 60 + now.minute
    live = now.weekday() < 5 and (8 * 60 + 30) <= mins <= (15 * 60)
    pairs = [
        ("S&P 500", "^GSPC" if live else "ES=F"),
        ("Nasdaq",  "^IXIC" if live else "NQ=F"),
        ("Dow",     "^DJI"  if live else "YM=F"),
    ]
    results = []
    for name, sym in pairs:
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = (price - prev) / prev * 100
                results.append({"name": name, "price": round(price, 2),
                                 "pct": round(pct, 2), "arrow": "▲" if pct >= 0 else "▼"})
            else:
                results.append({"name": name, "price": None, "pct": None})
        except Exception as e:
            results.append({"name": name, "error": str(e)})
    ts = now.strftime("%I:%M %p")
    return json.dumps({"timestamp": ts, "mode": "live" if live else "futures", "data": results})


def fetch_stock_data(ticker: str) -> str:
    try:
        t     = yf.Ticker(ticker.upper())
        info  = t.fast_info
        full  = t.info
        price = info.last_price
        prev  = info.previous_close
        pct   = ((price - prev) / prev * 100) if price and prev else None
        headline = ""
        for item in (t.news or [])[:3]:
            h = item.get("content", {}).get("title") or item.get("title", "")
            if h:
                headline = h
                break
        mkt_cap = full.get("marketCap")
        return json.dumps({
            "ticker":     ticker.upper(),
            "name":       full.get("longName", ticker),
            "price":      round(price, 2) if price else None,
            "pct":        round(pct, 2) if pct is not None else None,
            "52w_low":    full.get("fiftyTwoWeekLow"),
            "52w_high":   full.get("fiftyTwoWeekHigh"),
            "pe":         full.get("trailingPE"),
            "fwd_pe":     full.get("forwardPE"),
            "target":     full.get("targetMeanPrice"),
            "market_cap": f"${mkt_cap/1e9:.1f}B" if mkt_cap else None,
            "sector":     full.get("sector"),
            "headline":   headline,
        })
    except Exception as e:
        return json.dumps({"ticker": ticker, "error": str(e)})


def fetch_top_headlines() -> str:
    if NEWS_API_KEY:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={"category": "business", "country": "us",
                        "pageSize": 8, "apiKey": NEWS_API_KEY},
                timeout=10,
            )
            if resp.status_code == 200:
                articles = resp.json().get("articles", [])
                titles = [a["title"] for a in articles
                          if a.get("title") and len(a["title"]) > 20][:5]
                if titles:
                    return json.dumps({"source": "NewsAPI", "headlines": titles})
        except Exception:
            pass
    skip  = ["beginner", "guide", "how to", "what is", "explainer", "tutorial"]
    seen, titles = set(), []
    for sym in ["SPY", "QQQ", "^VIX", "GLD"]:
        try:
            for item in (yf.Ticker(sym).news or []):
                h = item.get("content", {}).get("title") or item.get("title", "")
                if h and h not in seen and len(h) > 20 and not any(k in h.lower() for k in skip):
                    seen.add(h)
                    titles.append(h)
        except Exception:
            continue
        if len(titles) >= 7:
            break
    return json.dumps({"source": "yfinance", "headlines": titles[:5]})


def fetch_sector_performance() -> str:
    results = []
    for sector, sym in SECTOR_ETFS.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            pct   = round((price - prev) / prev * 100, 2) if price and prev else None
            results.append({"sector": sector, "etf": sym, "pct": pct})
        except Exception:
            results.append({"sector": sector, "etf": sym, "pct": None})
    results.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
    return json.dumps({"sectors": results})


def fetch_top_movers() -> str:
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
                pct = round((price - prev) / prev * 100, 2)
                results.append({"symbol": ticker, "pct": pct, "price": round(price, 2)})
        except Exception:
            continue
    results.sort(key=lambda x: x["pct"], reverse=True)
    return json.dumps({"gainers": results[:3], "losers": results[-3:][::-1]})


def fetch_economic_calendar() -> str:
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
    note = "No major earnings today. See marketwatch.com/economy-politics/calendar for macro events."
    return json.dumps({"events": events[:6] if events else [{"event": note, "impact": ""}]})


def load_memory() -> str:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                data = json.load(f)
            for k, v in DEFAULT_MEMORY.items():
                data.setdefault(k, v)
            return json.dumps(data, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e), **DEFAULT_MEMORY})
    return json.dumps(DEFAULT_MEMORY)


def save_memory(data: dict) -> str:
    data["last_session"]  = datetime.now().isoformat()
    data["session_count"] = data.get("session_count", 0) + 1
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return f"Memory saved. Session #{data['session_count']} complete."


def get_monthly_picks() -> str:
    month_key = date.today().strftime("%Y-%m")
    if os.path.exists(PICKS_FILE):
        try:
            with open(PICKS_FILE) as f:
                cache = json.load(f)
            if month_key in cache and cache[month_key]:
                return json.dumps({"month": month_key, "picks": cache[month_key]})
        except Exception:
            pass
    return json.dumps({"month": month_key, "picks": [],
                       "note": "No picks yet — call generate_monthly_picks"})


def generate_monthly_picks() -> str:
    month_key = date.today().strftime("%Y-%m")
    cache = {}
    if os.path.exists(PICKS_FILE):
        try:
            with open(PICKS_FILE) as f:
                cache = json.load(f)
        except Exception:
            pass
    if month_key in cache and cache[month_key]:
        return json.dumps({"month": month_key, "picks": cache[month_key], "note": "from cache"})

    def _risk(beta, mkt_cap):
        if beta is None:
            beta = 1.0
        large = mkt_cap and mkt_cap > 50_000_000_000
        if beta < 0.8 and large:
            return "Low"
        if beta < 1.3 and large:
            return "Medium"
        if beta < 2.0:
            return "High"
        return "Speculative"

    candidates = []
    for ticker in WATCHLIST:
        try:
            info = yf.Ticker(ticker).info
            if not info or not info.get("currentPrice"):
                continue
            price  = info.get("currentPrice", 0)
            target = info.get("targetMeanPrice") or price
            upside = ((target - price) / price * 100) if price else 0
            rec    = info.get("recommendationKey", "").lower()
            parts  = []
            if upside > 5:
                parts.append(f"analyst target implies {upside:.0f}% upside")
            if rec in ("buy", "strong_buy"):
                parts.append(f"rated {rec.replace('_',' ')} by analysts")
            eg = info.get("earningsGrowth")
            if eg and eg > 0.1:
                parts.append(f"earnings up {eg*100:.0f}% YoY")
            rg = info.get("revenueGrowth")
            if rg and rg > 0.05:
                parts.append(f"revenue growing {rg*100:.0f}% YoY")
            fpe = info.get("forwardPE")
            if fpe and fpe < 30:
                parts.append(f"forward P/E {fpe:.1f}x")
            candidates.append({
                "ticker":    ticker,
                "company":   info.get("longName", ticker),
                "sector":    info.get("sector", "—"),
                "risk":      _risk(info.get("beta"), info.get("marketCap")),
                "rationale": ". ".join(parts[:3]) + "." if parts else f"{info.get('sector','—')} name.",
                "score":     upside + (20 if rec in ("buy","strong_buy") else 0),
                "is_known":  ticker in WELL_KNOWN,
            })
            time.sleep(0.1)
        except Exception:
            continue

    known  = sorted([c for c in candidates if c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    hidden = sorted([c for c in candidates if not c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    picks  = [{k: v for k, v in p.items() if k not in ("score","is_known")} for p in known + hidden]
    cache[month_key] = picks
    with open(PICKS_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return json.dumps({"month": month_key, "picks": picks})


def send_email(subject: str, html_body: str) -> str:
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"OpenBell <{GMAIL_ADDRESS}>"
        msg["To"]      = TO_EMAIL
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, TO_EMAIL, msg.as_string())
        return f"Email sent successfully to {TO_EMAIL}."
    except Exception as e:
        return f"Email send failed: {e}"


def get_last_email_summary() -> str:
    if not os.path.exists(MEMORY_FILE):
        return json.dumps({"note": "No email history found."})
    try:
        with open(MEMORY_FILE) as f:
            mem = json.load(f)
        return json.dumps({
            "last_email_sent":    mem.get("last_email_sent", ""),
            "last_email_summary": mem.get("last_email_summary", ""),
            "last_morning_brief": mem.get("last_morning_brief", {}),
            "last_close_summary": mem.get("last_close_summary", {}),
            "last_deep_dive":     mem.get("last_deep_dive", {}),
            "email_count":        mem.get("email_count", 0),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── FastMCP registrations (for local pippy.py terminal use) ──────────────────

@mcp.tool()
def is_market_open_today_mcp() -> str:
    """Check if the US stock market is open today. Returns true for weekdays that are not NYSE holidays. Call at the start of every scheduled run."""
    return is_market_open_today()

@mcp.tool()
def fetch_market_snapshot_mcp() -> str:
    """Fetch live S&P 500, Nasdaq, and Dow futures data. Call at the start of every morning briefing or when the user asks what the market is doing."""
    return fetch_market_snapshot()

@mcp.tool()
def fetch_stock_data_mcp(ticker: str) -> str:
    """Fetch live price, 52-week high/low, P/E ratio, and latest headline for a specific stock ticker. Call any time a specific stock is mentioned."""
    return fetch_stock_data(ticker)

@mcp.tool()
def fetch_top_headlines_mcp() -> str:
    """Fetch today's top 5 market-moving financial headlines via NewsAPI. Call for morning briefings and any time the user asks about news."""
    return fetch_top_headlines()

@mcp.tool()
def fetch_sector_performance_mcp() -> str:
    """Fetch end of day % change for all 11 S&P 500 sectors via sector ETFs. Call for every close summary."""
    return fetch_sector_performance()

@mcp.tool()
def fetch_top_movers_mcp() -> str:
    """Fetch the top 3 gaining and top 3 declining stocks on the day. Call for close summaries."""
    return fetch_top_movers()

@mcp.tool()
def fetch_economic_calendar_mcp() -> str:
    """Fetch key economic events scheduled for the current week including earnings. Call for morning briefings."""
    return fetch_economic_calendar()

@mcp.tool()
def load_memory_mcp() -> str:
    """Load the full pippy_memory.json file. Always call this at the start of every session and every email write."""
    return load_memory()

@mcp.tool()
def save_memory_mcp(data: dict) -> str:
    """Save the full updated memory object back to pippy_memory.json. Always call after every email and at the end of every terminal session."""
    return save_memory(data)

@mcp.tool()
def get_monthly_picks_mcp() -> str:
    """Read and return the current month's stock picks from picks_cache.json. Call for morning briefings and when the user asks about picks."""
    return get_monthly_picks()

@mcp.tool()
def generate_monthly_picks_mcp() -> str:
    """Generate a new set of monthly stock picks. Only call on the 1st of the month or when picks_cache.json is empty or stale."""
    return generate_monthly_picks()

@mcp.tool()
def send_email_mcp(subject: str, html_body: str) -> str:
    """Send the final composed HTML email via Gmail SMTP. Call only after the full email body has been written."""
    return send_email(subject, html_body)

@mcp.tool()
def get_last_email_summary_mcp() -> str:
    """Return a summary of the last email Pippy wrote. Call when the user asks what the email said or what was covered recently."""
    return get_last_email_summary()


if __name__ == "__main__":
    mcp.run()
