#!/usr/bin/env python3
"""
Pippy MCP Server — all data tools using FMP (primary) + yfinance (fallback).
Importable as plain Python functions for openbell.py cloud runs.
Run directly as MCP server for local pippy.py terminal use.
"""

import json
import os
import smtplib
import time
from datetime import date, datetime, timedelta
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
FMP_API_KEY        = os.getenv("FMP_API_KEY", "")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")
FMP_BASE           = "https://financialmodelingprep.com/api/v3"

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

# Hardcoded holiday list kept as fallback for is_market_open_today
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
WELL_KNOWN = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
              "AVGO", "NFLX", "AMD", "INTC", "CRM", "ADBE", "ORCL", "GS", "WMT"}

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmp(path: str, params: dict = None) -> list | dict:
    """Make an FMP API call. Raises on error or empty/error response."""
    p = {"apikey": FMP_API_KEY, **(params or {})}
    r = requests.get(f"{FMP_BASE}{path}", params=p, timeout=10)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and ("Error Message" in data or "message" in data):
        raise ValueError(f"FMP error: {data}")
    if not data:
        raise ValueError("FMP returned empty response")
    return data


def _ts() -> str:
    return datetime.now().isoformat()


# ── Tool implementations ──────────────────────────────────────────────────────

def is_market_open_today() -> str:
    ts = _ts()
    try:
        data = _fmp("/is-the-market-open")
        open_ = bool(data.get("isTheStockMarketOpen", False))
        return json.dumps({"source": "FMP", "open": open_,
                           "reason": "FMP live market status", "timestamp": ts})
    except Exception as e:
        # Fallback: hardcoded holiday list
        try:
            today = date.today()
            if today.weekday() >= 5:
                return json.dumps({"source": "fallback", "open": False,
                                   "reason": "weekend", "timestamp": ts})
            if today.isoformat() in NYSE_HOLIDAYS:
                return json.dumps({"source": "fallback", "open": False,
                                   "reason": "NYSE holiday", "timestamp": ts})
            return json.dumps({"source": "fallback", "open": True,
                               "reason": "regular trading day", "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_market_snapshot() -> str:
    ts = _ts()
    index_map = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow"}
    try:
        data = _fmp("/quotes/index")
        results = []
        for item in data:
            sym = item.get("symbol", "")
            if sym in index_map:
                results.append({
                    "name":     index_map[sym],
                    "symbol":   sym,
                    "price":    item.get("price"),
                    "change":   item.get("change"),
                    "pct":      item.get("changesPercentage"),
                    "dayLow":   item.get("dayLow"),
                    "dayHigh":  item.get("dayHigh"),
                    "arrow":    "▲" if (item.get("changesPercentage") or 0) >= 0 else "▼",
                })
        results.sort(key=lambda x: ["S&P 500", "Nasdaq", "Dow"].index(x["name"]) if x["name"] in ["S&P 500","Nasdaq","Dow"] else 9)
        if results:
            return json.dumps({"source": "FMP", "data": results, "timestamp": ts})
        raise ValueError("no index data in response")
    except Exception as e:
        # Fallback: yfinance
        try:
            now   = datetime.now()
            mins  = now.hour * 60 + now.minute
            live  = now.weekday() < 5 and (8*60+30) <= mins <= (15*60)
            pairs = [("S&P 500","^GSPC" if live else "ES=F"),
                     ("Nasdaq", "^IXIC" if live else "NQ=F"),
                     ("Dow",    "^DJI"  if live else "YM=F")]
            results = []
            for name, sym in pairs:
                info  = yf.Ticker(sym).fast_info
                price = info.last_price
                prev  = info.previous_close
                if price and prev:
                    pct = (price - prev) / prev * 100
                    results.append({"name": name, "price": round(price,2),
                                    "pct": round(pct,2), "arrow": "▲" if pct>=0 else "▼"})
            return json.dumps({"source": "fallback", "data": results, "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_stock_data(ticker: str) -> str:
    ts  = _ts()
    sym = ticker.upper()
    try:
        quote = _fmp(f"/quote/{sym}")
        q     = quote[0] if isinstance(quote, list) else quote
        # news
        headline = ""
        try:
            news = _fmp("/stock_news", {"tickers": sym, "limit": 1})
            if news:
                headline = news[0].get("title", "")
        except Exception:
            pass
        mkt_cap = q.get("marketCap")
        return json.dumps({
            "source":     "FMP",
            "ticker":     sym,
            "name":       q.get("name", sym),
            "price":      q.get("price"),
            "pct":        q.get("changesPercentage"),
            "change":     q.get("change"),
            "52w_low":    q.get("yearLow"),
            "52w_high":   q.get("yearHigh"),
            "pe":         q.get("pe"),
            "market_cap": f"${mkt_cap/1e9:.1f}B" if mkt_cap else None,
            "avg_volume": q.get("avgVolume"),
            "headline":   headline,
            "timestamp":  ts,
        })
    except Exception as e:
        # Fallback: yfinance
        try:
            t     = yf.Ticker(sym)
            fi    = t.fast_info
            full  = t.info
            price = fi.last_price
            prev  = fi.previous_close
            pct   = ((price - prev) / prev * 100) if price and prev else None
            headline = ""
            for item in (t.news or [])[:2]:
                h = item.get("content", {}).get("title") or item.get("title", "")
                if h:
                    headline = h; break
            mkt_cap = full.get("marketCap")
            return json.dumps({
                "source": "fallback", "ticker": sym, "name": full.get("longName", sym),
                "price": round(price,2) if price else None, "pct": round(pct,2) if pct else None,
                "52w_low": full.get("fiftyTwoWeekLow"), "52w_high": full.get("fiftyTwoWeekHigh"),
                "pe": full.get("trailingPE"),
                "market_cap": f"${mkt_cap/1e9:.1f}B" if mkt_cap else None,
                "headline": headline, "timestamp": ts,
            })
        except Exception as e2:
            return json.dumps({"source": "unavailable", "ticker": sym, "error": str(e2), "timestamp": ts})


def fetch_top_headlines() -> str:
    ts = _ts()
    try:
        data = _fmp("/stock_news", {"limit": 10})
        skip = ["beginner", "guide", "how to", "what is", "explainer"]
        headlines = [
            {"title": a["title"], "snippet": a.get("text","")[:120],
             "site": a.get("site",""), "published": a.get("publishedDate","")[:10]}
            for a in data
            if a.get("title") and len(a["title"]) > 20
            and not any(k in a["title"].lower() for k in skip)
        ][:5]
        if headlines:
            return json.dumps({"source": "FMP", "headlines": headlines, "timestamp": ts})
        raise ValueError("no usable headlines")
    except Exception as e:
        # Fallback 1: NewsAPI
        if NEWS_API_KEY:
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/top-headlines",
                    params={"category": "business", "country": "us",
                            "pageSize": 8, "apiKey": NEWS_API_KEY},
                    timeout=10,
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
                titles = [{"title": a["title"], "snippet": "", "site": a.get("source",{}).get("name",""), "published": ""}
                          for a in articles if a.get("title") and len(a["title"]) > 20][:5]
                if titles:
                    return json.dumps({"source": "NewsAPI", "headlines": titles, "timestamp": ts})
            except Exception:
                pass
        # Fallback 2: yfinance
        try:
            skip  = ["beginner", "guide", "how to", "what is", "explainer"]
            seen, titles = set(), []
            for sym in ["SPY", "QQQ", "^VIX", "GLD"]:
                for item in (yf.Ticker(sym).news or []):
                    h = item.get("content", {}).get("title") or item.get("title", "")
                    if h and h not in seen and len(h) > 20 and not any(k in h.lower() for k in skip):
                        seen.add(h)
                        titles.append({"title": h, "snippet": "", "site": "Yahoo Finance", "published": ""})
                if len(titles) >= 7: break
            return json.dumps({"source": "fallback", "headlines": titles[:5], "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_sector_performance() -> str:
    ts = _ts()
    try:
        data = _fmp("/sectors-performance")
        # FMP returns list of {sector, changesPercentage}
        sectors = []
        for item in data:
            pct_raw = item.get("changesPercentage", "0%")
            pct_str = str(pct_raw).replace("%", "").strip()
            try:
                pct = round(float(pct_str), 2)
            except ValueError:
                pct = None
            sectors.append({"sector": item.get("sector"), "pct": pct})
        sectors.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
        return json.dumps({"source": "FMP", "sectors": sectors, "timestamp": ts})
    except Exception as e:
        # Fallback: yfinance sector ETFs
        try:
            results = []
            for sector, sym in SECTOR_ETFS.items():
                info  = yf.Ticker(sym).fast_info
                price = info.last_price
                prev  = info.previous_close
                pct   = round((price-prev)/prev*100, 2) if price and prev else None
                results.append({"sector": sector, "etf": sym, "pct": pct})
            results.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
            return json.dumps({"source": "fallback", "sectors": results, "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_top_movers() -> str:
    ts = _ts()
    try:
        gainers_raw = _fmp("/stock_market/gainers")
        losers_raw  = _fmp("/stock_market/losers")
        def _clean(items):
            return [{"symbol": i.get("symbol"), "name": i.get("name",""),
                     "price": i.get("price"), "pct": i.get("changesPercentage")}
                    for i in items[:3]]
        return json.dumps({"source": "FMP",
                           "gainers": _clean(gainers_raw),
                           "losers":  _clean(losers_raw),
                           "timestamp": ts})
    except Exception as e:
        # Fallback: yfinance scan
        try:
            scan = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM",
                    "AVGO","NOW","CRWD","PLTR","DDOG","CELH","AXON","HIMS",
                    "APP","VRT","ANET","SOUN","TTD","MELI","HOOD","DECK","LULU"]
            results = []
            for ticker in scan:
                info  = yf.Ticker(ticker).fast_info
                price = info.last_price
                prev  = info.previous_close
                if price and prev:
                    pct = round((price-prev)/prev*100, 2)
                    results.append({"symbol": ticker, "pct": pct, "price": round(price,2)})
            results.sort(key=lambda x: x["pct"], reverse=True)
            return json.dumps({"source": "fallback",
                               "gainers": results[:3], "losers": results[-3:][::-1],
                               "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_economic_calendar() -> str:
    ts    = _ts()
    today = date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    try:
        data = _fmp("/economic_calendar",
                    {"from": today.isoformat(), "to": week_end})
        # Filter US events, sort by date, flag high impact
        events = [
            {"date": e.get("date","")[:10], "event": e.get("event",""),
             "country": e.get("country",""), "impact": e.get("impact",""),
             "actual": e.get("actual"), "estimate": e.get("estimate")}
            for e in data
            if e.get("country","").upper() in ("US","USA","UNITED STATES","")
        ]
        events.sort(key=lambda x: x["date"])
        return json.dumps({"source": "FMP", "events": events[:15], "timestamp": ts})
    except Exception as e:
        # Fallback: yfinance earnings scan
        try:
            events = []
            today_str = today.isoformat()
            watch = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM",
                     "GS","WMT","COST","NKE","FDX","ORCL","ADBE","CRM","INTC","AMD","NFLX","DIS"]
            for ticker in watch:
                cal = yf.Ticker(ticker).calendar
                if cal is None: continue
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        for d in (ed if isinstance(ed, list) else [ed]):
                            if str(d)[:10] == today_str:
                                events.append({"date": today_str, "event": f"{ticker} reports earnings", "impact": "High"})
                elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
                    for d in cal.loc["Earnings Date"]:
                        if str(d)[:10] == today_str:
                            events.append({"date": today_str, "event": f"{ticker} reports earnings", "impact": "High"})
            note = "No major events found. Check marketwatch.com/economy-politics/calendar."
            return json.dumps({"source": "fallback",
                               "events": events[:8] if events else [{"date": today_str, "event": note, "impact": ""}],
                               "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "error": str(e2), "timestamp": ts})


def fetch_premarket_data(ticker: str) -> str:
    """Fetch pre-market price and movement for a specific ticker via FMP."""
    ts  = _ts()
    sym = ticker.upper()
    try:
        data = _fmp(f"/pre-post-market/{sym}")
        item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        return json.dumps({
            "source":  "FMP",
            "ticker":  sym,
            "price":   item.get("price"),
            "change":  item.get("change"),
            "pct":     item.get("changesPercentage"),
            "timestamp": ts,
        })
    except Exception as e:
        # Fallback: yfinance pre-market (fast_info)
        try:
            fi    = yf.Ticker(sym).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            pct   = round((price-prev)/prev*100, 2) if price and prev else None
            return json.dumps({"source": "fallback", "ticker": sym,
                               "price": price, "pct": pct, "timestamp": ts})
        except Exception as e2:
            return json.dumps({"source": "unavailable", "ticker": sym,
                               "error": str(e2), "timestamp": ts})


def fetch_earnings_calendar() -> str:
    """Fetch earnings announcements for the current week via FMP."""
    ts    = _ts()
    today = date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    try:
        data = _fmp("/earning_calendar",
                    {"from": today.isoformat(), "to": week_end})
        # Filter to meaningful companies (those in our watchlist or WELL_KNOWN)
        tracked = WELL_KNOWN | set(WATCHLIST)
        earnings = []
        for e in data:
            sym = e.get("symbol", "")
            if sym in tracked or (e.get("marketCap") and e["marketCap"] > 10_000_000_000):
                earnings.append({
                    "symbol":         sym,
                    "date":           e.get("date","")[:10],
                    "eps_estimated":  e.get("epsEstimated"),
                    "rev_estimated":  e.get("revenueEstimated"),
                    "time":           e.get("time",""),
                })
        earnings.sort(key=lambda x: x["date"])
        # Always include all if filtered list is small
        if len(earnings) < 3:
            earnings = [{"symbol": e.get("symbol"), "date": e.get("date","")[:10],
                         "eps_estimated": e.get("epsEstimated"), "rev_estimated": e.get("revenueEstimated")}
                        for e in data[:10]]
        return json.dumps({"source": "FMP", "earnings": earnings[:10], "timestamp": ts})
    except Exception as e:
        return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


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
        if beta is None: beta = 1.0
        large = mkt_cap and mkt_cap > 50_000_000_000
        if beta < 0.8 and large: return "Low"
        if beta < 1.3 and large: return "Medium"
        if beta < 2.0: return "High"
        return "Speculative"

    candidates = []
    for ticker in WATCHLIST:
        try:
            info = yf.Ticker(ticker).info
            if not info or not info.get("currentPrice"): continue
            price  = info.get("currentPrice", 0)
            target = info.get("targetMeanPrice") or price
            upside = ((target - price) / price * 100) if price else 0
            rec    = info.get("recommendationKey", "").lower()
            parts  = []
            if upside > 5: parts.append(f"analyst target implies {upside:.0f}% upside")
            if rec in ("buy","strong_buy"): parts.append(f"rated {rec.replace('_',' ')} by analysts")
            eg = info.get("earningsGrowth")
            if eg and eg > 0.1: parts.append(f"earnings up {eg*100:.0f}% YoY")
            rg = info.get("revenueGrowth")
            if rg and rg > 0.05: parts.append(f"revenue growing {rg*100:.0f}% YoY")
            fpe = info.get("forwardPE")
            if fpe and fpe < 30: parts.append(f"forward P/E {fpe:.1f}x")
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
    """Check if the US stock market is open today via FMP live status. Call at the start of every scheduled run."""
    return is_market_open_today()

@mcp.tool()
def fetch_market_snapshot_mcp() -> str:
    """Fetch live S&P 500, Nasdaq, and Dow data via FMP. Call at the start of every morning briefing or when the user asks about the market."""
    return fetch_market_snapshot()

@mcp.tool()
def fetch_stock_data_mcp(ticker: str) -> str:
    """Fetch live price, 52-week range, P/E, market cap, and latest news for a specific ticker via FMP. Call any time a stock is mentioned."""
    return fetch_stock_data(ticker)

@mcp.tool()
def fetch_top_headlines_mcp() -> str:
    """Fetch today's top 5 financial headlines via FMP stock news. Call for briefings and news questions."""
    return fetch_top_headlines()

@mcp.tool()
def fetch_sector_performance_mcp() -> str:
    """Fetch % change for all S&P 500 sectors via FMP. Call for every close summary."""
    return fetch_sector_performance()

@mcp.tool()
def fetch_top_movers_mcp() -> str:
    """Fetch top 3 gainers and top 3 losers via FMP market movers API. Call for close summaries."""
    return fetch_top_movers()

@mcp.tool()
def fetch_economic_calendar_mcp() -> str:
    """Fetch US economic events for the current week via FMP economic calendar. Call for morning briefings."""
    return fetch_economic_calendar()

@mcp.tool()
def fetch_premarket_data_mcp(ticker: str) -> str:
    """Fetch pre-market price and movement for a specific ticker via FMP. Call this for every flagged ticker during morning briefing."""
    return fetch_premarket_data(ticker)

@mcp.tool()
def fetch_earnings_calendar_mcp() -> str:
    """Fetch earnings announcements scheduled for the current week via FMP. Call this for every morning briefing to include in the weekly calendar section."""
    return fetch_earnings_calendar()

@mcp.tool()
def load_memory_mcp() -> str:
    """Load Pippy's full persistent memory. Always call at the start of every session and email write."""
    return load_memory()

@mcp.tool()
def save_memory_mcp(data: dict) -> str:
    """Save the full updated memory object. Always call after every email and at the end of every terminal session."""
    return save_memory(data)

@mcp.tool()
def get_monthly_picks_mcp() -> str:
    """Read the current month's stock picks. Call for morning briefings and picks questions."""
    return get_monthly_picks()

@mcp.tool()
def generate_monthly_picks_mcp() -> str:
    """Generate new monthly stock picks using fundamentals. Only call on the 1st of the month or when cache is stale."""
    return generate_monthly_picks()

@mcp.tool()
def send_email_mcp(subject: str, html_body: str) -> str:
    """Send the final composed HTML email via Gmail SMTP. Call only after the full email body is written."""
    return send_email(subject, html_body)

@mcp.tool()
def get_last_email_summary_mcp() -> str:
    """Return a summary of the last email Pippy wrote. Call when the user asks what was covered recently."""
    return get_last_email_summary()


if __name__ == "__main__":
    mcp.run()
