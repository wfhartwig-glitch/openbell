#!/usr/bin/env python3
"""
Pippy MCP Server — pure data layer. No AI calls. No Anthropic SDK.
Run as: python pippy_mcp.py
Exposes all tools over stdio for openbell.py and pippy.py to use as MCP clients.
"""

import json
import os
import smtplib
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import logging

import requests
import yfinance as yf
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# Route all MCP/library INFO logs to a file — never to stdout/stderr
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "pippy_mcp.log"),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger().setLevel(logging.WARNING)

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
WELL_KNOWN = {
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
    "AVGO", "NFLX", "AMD", "INTC", "CRM", "ADBE", "ORCL", "GS", "WMT",
}

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


# ── Tools ─────────────────────────────────────────────────────────────────────

def _is_market_open_now_fallback() -> bool:
    """ET time-window check: 9:30 AM – 4:00 PM ET, weekdays, non-holidays."""
    import pytz
    et      = pytz.timezone("America/New_York")
    now_et  = datetime.now(et)
    today   = now_et.date()
    if now_et.weekday() >= 5:
        return False
    if today.isoformat() in NYSE_HOLIDAYS:
        return False
    open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now_et < close_t


@mcp.tool()
def is_market_open_today() -> str:
    """Check if the US market is open RIGHT NOW. Uses FMP live status; falls back to ET time-window check."""
    ts = _ts()
    try:
        data  = _fmp("/is-the-market-open")
        open_ = bool(data.get("isTheStockMarketOpen", False))
        return json.dumps({"source": "FMP", "open": open_,
                           "reason": "FMP live market status", "timestamp": ts})
    except Exception:
        open_ = _is_market_open_now_fallback()
        reason = "ET time-window fallback (9:30 AM – 4:00 PM ET)"
        if not open_:
            import pytz
            et     = pytz.timezone("America/New_York")
            now_et = datetime.now(et)
            if now_et.weekday() >= 5:
                reason = "weekend"
            elif now_et.date().isoformat() in NYSE_HOLIDAYS:
                reason = "NYSE holiday"
            else:
                reason = "outside trading hours (9:30 AM – 4:00 PM ET)"
        return json.dumps({"source": "fallback", "open": open_,
                           "reason": reason, "timestamp": ts})


@mcp.tool()
def fetch_market_snapshot() -> str:
    """Fetch live S&P 500, Nasdaq, and Dow data. Returns JSON with 'data' list."""
    ts = _ts()
    index_map = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow"}
    try:
        data    = _fmp("/quotes/index")
        results = []
        for item in data:
            sym = item.get("symbol", "")
            if sym in index_map:
                results.append({
                    "name":    index_map[sym],
                    "symbol":  sym,
                    "price":   item.get("price"),
                    "change":  item.get("change"),
                    "pct":     item.get("changesPercentage"),
                    "dayLow":  item.get("dayLow"),
                    "dayHigh": item.get("dayHigh"),
                })
        results.sort(key=lambda x: ["S&P 500","Nasdaq","Dow"].index(x["name"])
                     if x["name"] in ["S&P 500","Nasdaq","Dow"] else 9)
        if results:
            return json.dumps({"source": "FMP", "data": results, "timestamp": ts})
        raise ValueError("no index data")
    except Exception:
        try:
            now    = datetime.now()
            mins   = now.hour * 60 + now.minute
            live   = now.weekday() < 5 and (8*60+30) <= mins <= (15*60)
            pairs  = [("S&P 500", "^GSPC" if live else "ES=F"),
                      ("Nasdaq",  "^IXIC" if live else "NQ=F"),
                      ("Dow",     "^DJI"  if live else "YM=F")]
            results = []
            for name, sym in pairs:
                fi    = yf.Ticker(sym).fast_info
                price = fi.last_price
                prev  = fi.previous_close
                if price and prev:
                    pct = (price - prev) / prev * 100
                    results.append({"name": name, "price": round(price, 2), "pct": round(pct, 2)})
            return json.dumps({"source": "fallback", "data": results, "timestamp": ts})
        except Exception as e:
            return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_stock_data(ticker: str) -> str:
    """Fetch live price, 52-week range, P/E, market cap, and latest headline for a ticker."""
    ts  = _ts()
    sym = ticker.upper()
    try:
        quote = _fmp(f"/quote/{sym}")
        q     = quote[0] if isinstance(quote, list) else quote
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
    except Exception:
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
                    headline = h
                    break
            mkt_cap = full.get("marketCap")
            return json.dumps({
                "source":     "fallback",
                "ticker":     sym,
                "name":       full.get("longName", sym),
                "price":      round(price, 2) if price else None,
                "pct":        round(pct, 2) if pct else None,
                "52w_low":    full.get("fiftyTwoWeekLow"),
                "52w_high":   full.get("fiftyTwoWeekHigh"),
                "pe":         full.get("trailingPE"),
                "market_cap": f"${mkt_cap/1e9:.1f}B" if mkt_cap else None,
                "headline":   headline,
                "timestamp":  ts,
            })
        except Exception as e:
            return json.dumps({"source": "unavailable", "ticker": sym,
                               "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_top_headlines() -> str:
    """Fetch today's top financial headlines. Returns JSON with 'headlines' list."""
    ts = _ts()
    try:
        data  = _fmp("/stock_news", {"limit": 10})
        skip  = ["beginner", "guide", "how to", "what is", "explainer"]
        headlines = [
            {"title": a["title"], "snippet": a.get("text", "")[:120],
             "site": a.get("site", ""), "published": a.get("publishedDate", "")[:10]}
            for a in data
            if a.get("title") and len(a["title"]) > 20
            and not any(k in a["title"].lower() for k in skip)
        ][:5]
        if headlines:
            return json.dumps({"source": "FMP", "headlines": headlines, "timestamp": ts})
        raise ValueError("no usable headlines")
    except Exception:
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
                titles = [
                    {"title": a["title"], "snippet": "", "site": a.get("source", {}).get("name", ""), "published": ""}
                    for a in articles if a.get("title") and len(a["title"]) > 20
                ][:5]
                if titles:
                    return json.dumps({"source": "NewsAPI", "headlines": titles, "timestamp": ts})
            except Exception:
                pass
        try:
            skip = ["beginner", "guide", "how to", "what is", "explainer"]
            seen, titles = set(), []
            for sym in ["SPY", "QQQ", "^VIX", "GLD"]:
                for item in (yf.Ticker(sym).news or []):
                    h = item.get("content", {}).get("title") or item.get("title", "")
                    if h and h not in seen and len(h) > 20 and not any(k in h.lower() for k in skip):
                        seen.add(h)
                        titles.append({"title": h, "snippet": "", "site": "Yahoo Finance", "published": ""})
                if len(titles) >= 7:
                    break
            return json.dumps({"source": "fallback", "headlines": titles[:5], "timestamp": ts})
        except Exception as e:
            return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_sector_performance() -> str:
    """Fetch % change for all S&P 500 sectors. Returns JSON with 'sectors' list."""
    ts = _ts()
    try:
        data    = _fmp("/sectors-performance")
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
    except Exception:
        try:
            results = []
            for sector, sym in SECTOR_ETFS.items():
                fi    = yf.Ticker(sym).fast_info
                price = fi.last_price
                prev  = fi.previous_close
                pct   = round((price - prev) / prev * 100, 2) if price and prev else None
                results.append({"sector": sector, "etf": sym, "pct": pct})
            results.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
            return json.dumps({"source": "fallback", "sectors": results, "timestamp": ts})
        except Exception as e:
            return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_top_movers() -> str:
    """Fetch top 3 gainers and top 3 losers for the day. Returns JSON with 'gainers' and 'losers'."""
    ts = _ts()
    try:
        gainers_raw = _fmp("/stock_market/gainers")
        losers_raw  = _fmp("/stock_market/losers")

        def _clean(items):
            return [{"symbol": i.get("symbol"), "name": i.get("name", ""),
                     "price": i.get("price"), "pct": i.get("changesPercentage")}
                    for i in items[:3]]

        return json.dumps({"source": "FMP",
                           "gainers": _clean(gainers_raw),
                           "losers":  _clean(losers_raw),
                           "timestamp": ts})
    except Exception:
        try:
            scan = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM",
                    "AVGO","NOW","CRWD","PLTR","DDOG","CELH","AXON","HIMS",
                    "APP","VRT","ANET","SOUN","TTD","MELI","HOOD","DECK","LULU"]
            results = []
            for ticker in scan:
                fi    = yf.Ticker(ticker).fast_info
                price = fi.last_price
                prev  = fi.previous_close
                if price and prev:
                    pct = round((price - prev) / prev * 100, 2)
                    results.append({"symbol": ticker, "pct": pct, "price": round(price, 2)})
            results.sort(key=lambda x: x["pct"], reverse=True)
            return json.dumps({"source": "fallback",
                               "gainers": results[:3],
                               "losers":  results[-3:][::-1],
                               "timestamp": ts})
        except Exception as e:
            return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_economic_calendar() -> str:
    """Fetch US economic events for the current week. Returns JSON with 'events' list."""
    ts       = _ts()
    today    = date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    try:
        data   = _fmp("/economic_calendar", {"from": today.isoformat(), "to": week_end})
        events = [
            {"date": e.get("date", "")[:10], "event": e.get("event", ""),
             "country": e.get("country", ""), "impact": e.get("impact", ""),
             "actual": e.get("actual"), "estimate": e.get("estimate")}
            for e in data
            if e.get("country", "").upper() in ("US", "USA", "UNITED STATES", "")
        ]
        events.sort(key=lambda x: x["date"])
        return json.dumps({"source": "FMP", "events": events[:15], "timestamp": ts})
    except Exception as e:
        return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_earnings_calendar() -> str:
    """Fetch earnings announcements for the current week. Returns JSON with 'earnings' list."""
    ts       = _ts()
    today    = date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    try:
        data    = _fmp("/earning_calendar", {"from": today.isoformat(), "to": week_end})
        tracked = WELL_KNOWN | set(WATCHLIST)
        earnings = []
        for e in data:
            sym = e.get("symbol", "")
            if sym in tracked or (e.get("marketCap") and e["marketCap"] > 10_000_000_000):
                earnings.append({
                    "symbol":        sym,
                    "date":          e.get("date", "")[:10],
                    "eps_estimated": e.get("epsEstimated"),
                    "rev_estimated": e.get("revenueEstimated"),
                    "time":          e.get("time", ""),
                })
        earnings.sort(key=lambda x: x["date"])
        if len(earnings) < 3:
            earnings = [{"symbol": e.get("symbol"), "date": e.get("date", "")[:10],
                         "eps_estimated": e.get("epsEstimated"), "rev_estimated": e.get("revenueEstimated")}
                        for e in data[:10]]
        return json.dumps({"source": "FMP", "earnings": earnings[:10], "timestamp": ts})
    except Exception as e:
        return json.dumps({"source": "unavailable", "error": str(e), "timestamp": ts})


@mcp.tool()
def fetch_premarket_data(ticker: str) -> str:
    """Fetch pre-market price and movement for a specific ticker."""
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
    except Exception:
        try:
            fi    = yf.Ticker(sym).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            pct   = round((price - prev) / prev * 100, 2) if price and prev else None
            return json.dumps({"source": "fallback", "ticker": sym,
                               "price": price, "pct": pct, "timestamp": ts})
        except Exception as e:
            return json.dumps({"source": "unavailable", "ticker": sym,
                               "error": str(e), "timestamp": ts})


@mcp.tool()
def load_memory() -> str:
    """Load Pippy's persistent memory from pippy_memory.json."""
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


@mcp.tool()
def save_memory(data: dict) -> str:
    """Save updated memory object to pippy_memory.json."""
    data["last_session"]  = datetime.now().isoformat()
    data["session_count"] = data.get("session_count", 0) + 1
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return f"Memory saved. Session #{data['session_count']}."


@mcp.tool()
def get_monthly_picks() -> str:
    """Read the current month's stock picks from picks_cache.json."""
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


@mcp.tool()
def generate_monthly_picks() -> str:
    """Generate new monthly stock picks using yfinance fundamentals. Caches result."""
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
        if beta < 0.8 and large:  return "Low"
        if beta < 1.3 and large:  return "Medium"
        if beta < 2.0:            return "High"
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

    known  = sorted([c for c in candidates if c["is_known"]],    key=lambda x: x["score"], reverse=True)[:3]
    hidden = sorted([c for c in candidates if not c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    picks  = [{k: v for k, v in p.items() if k not in ("score", "is_known")} for p in known + hidden]
    cache[month_key] = picks
    with open(PICKS_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return json.dumps({"month": month_key, "picks": picks})


@mcp.tool()
def get_weekly_picks() -> str:
    """Return this week's stock picks from picks_cache.json. ISO week format e.g. 2026-W25."""
    week_key = date.today().strftime("%Y-W%W")
    if os.path.exists(PICKS_FILE):
        try:
            with open(PICKS_FILE) as f:
                cache = json.load(f)
            if week_key in cache:
                return json.dumps({"week": week_key, **cache[week_key]})
        except Exception:
            pass
    return json.dumps({"week": week_key, "picks": [],
                       "note": "No picks yet for this week — call generate_weekly_picks"})


@mcp.tool()
def generate_weekly_picks() -> str:
    """
    Generate or update this week's stock picks. Evaluates last week's picks first —
    keeps each one if the thesis still holds, replaces only what has genuinely shifted.
    Tracks performance history and uses it to improve future selections.
    """
    today    = date.today()
    week_key = today.strftime("%Y-W%W")

    cache = {}
    if os.path.exists(PICKS_FILE):
        try:
            with open(PICKS_FILE) as f:
                cache = json.load(f)
        except Exception:
            pass

    # Already generated this week — return as-is
    if week_key in cache and cache[week_key].get("picks"):
        return json.dumps({"week": week_key, **cache[week_key], "note": "from cache"})

    # ── helpers ───────────────────────────────────────────────────────────────

    def _risk(beta, mkt_cap):
        if beta is None:
            beta = 1.0
        large = mkt_cap and mkt_cap > 50_000_000_000
        if beta < 0.8 and large:  return "Low"
        if beta < 1.3 and large:  return "Medium"
        if beta < 2.0:            return "High"
        return "Speculative"

    def _score_candidate(info: dict) -> float:
        price  = info.get("currentPrice", 0)
        target = info.get("targetMeanPrice") or price
        upside = ((target - price) / price * 100) if price else 0
        rec    = info.get("recommendationKey", "").lower()
        score  = upside + (20 if rec in ("buy", "strong_buy") else 0)
        eg = info.get("earningsGrowth")
        if eg and eg > 0.15: score += 10
        rg = info.get("revenueGrowth")
        if rg and rg > 0.10: score += 8
        fpe = info.get("forwardPE")
        if fpe and fpe < 25: score += 5
        return score

    def _rationale(info: dict) -> str:
        price  = info.get("currentPrice", 0)
        target = info.get("targetMeanPrice") or price
        upside = ((target - price) / price * 100) if price else 0
        rec    = info.get("recommendationKey", "").lower()
        parts  = []
        if upside > 5:  parts.append(f"analyst target implies {upside:.0f}% upside")
        if rec in ("buy", "strong_buy"): parts.append(f"rated {rec.replace('_',' ')} by analysts")
        eg = info.get("earningsGrowth")
        if eg and eg > 0.1:  parts.append(f"earnings up {eg*100:.0f}% YoY")
        rg = info.get("revenueGrowth")
        if rg and rg > 0.05: parts.append(f"revenue growing {rg*100:.0f}% YoY")
        fpe = info.get("forwardPE")
        if fpe and fpe < 30: parts.append(f"forward P/E {fpe:.1f}x")
        return ". ".join(parts[:3]) + "." if parts else f"{info.get('sector','—')} holding."

    # ── load memory for performance context ───────────────────────────────────

    mem = {}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                mem = json.load(f)
        except Exception:
            pass

    perf_history  = mem.get("pick_performance_history", [])
    lessons       = mem.get("lessons_learned", [])

    # Compute sector/risk performance bias from recent history
    sector_scores: dict[str, list[float]] = {}
    risk_scores:   dict[str, list[float]] = {}
    for ph in perf_history[-30:]:  # last 30 pick-weeks
        pct = ph.get("pct_change_since_pick")
        if pct is None:
            continue
        s = ph.get("sector", "")
        r = ph.get("risk_level", "")
        if s: sector_scores.setdefault(s, []).append(pct)
        if r: risk_scores.setdefault(r, []).append(pct)

    sector_bias = {s: sum(v)/len(v) for s, v in sector_scores.items() if v}
    risk_bias   = {r: sum(v)/len(v) for r, v in risk_scores.items()   if v}

    # ── find last week's picks ────────────────────────────────────────────────

    last_week_key  = (today - __import__("datetime").timedelta(weeks=1)).strftime("%Y-W%W")
    last_week_data = cache.get(last_week_key, {})
    last_picks     = last_week_data.get("picks", [])

    # If no last week, check the most recent week in cache
    if not last_picks:
        week_keys = sorted([k for k in cache if k.startswith("20") and "W" in k], reverse=True)
        if week_keys:
            last_picks = cache[week_keys[0]].get("picks", [])

    # ── evaluate existing picks ───────────────────────────────────────────────

    kept_picks  = []
    dropped     = []
    changes_log = []

    for pick in last_picks:
        ticker = pick.get("ticker", "")
        try:
            info  = yf.Ticker(ticker).info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if not price:
                dropped.append(pick)
                changes_log.append(f"Dropped {ticker} — could not fetch current price")
                continue

            prev_price = pick.get("price_when_picked") or price
            pct_change = ((price - prev_price) / prev_price * 100) if prev_price else 0

            # Drop signals: sharp breakdown, broken 52w low, very bad fundamentals
            low_52w    = info.get("fiftyTwoWeekLow", 0)
            drop_reason = None

            if pct_change < -12:
                drop_reason = f"dropped {abs(pct_change):.1f}% since picked — momentum breakdown"
            elif low_52w and price < low_52w * 1.02:
                drop_reason = "trading at/below 52-week low — technical breakdown"
            elif info.get("recommendationKey", "").lower() in ("sell", "strong_sell", "underperform"):
                drop_reason = "analysts downgraded to sell/underperform"

            if drop_reason:
                dropped.append(pick)
                changes_log.append(f"Replaced {ticker} — {drop_reason}")
            else:
                held = dict(pick)
                held["price_now"]           = round(price, 2)
                held["pct_change_this_week"] = round(pct_change, 2)
                held["weeks_held"]          = pick.get("weeks_held", 1) + 1
                held["status"]              = "holding"
                held["note"]                = f"Holding — thesis intact, {_rationale(info)}"
                held.pop("price_when_picked", None)
                held["price_when_picked"]   = round(prev_price, 2)
                kept_picks.append(held)
            time.sleep(0.1)
        except Exception:
            # Can't evaluate — keep it by default
            held = dict(pick)
            held["weeks_held"] = pick.get("weeks_held", 1) + 1
            held["status"]     = "holding"
            held["note"]       = "Holding — data unavailable, keeping position"
            kept_picks.append(held)

    # ── fill empty slots from candidate pool ──────────────────────────────────

    slots_needed = 5 - len(kept_picks)
    existing_tickers = {p["ticker"] for p in kept_picks}

    if slots_needed > 0:
        candidates = []
        for ticker in WATCHLIST:
            if ticker in existing_tickers:
                continue
            try:
                info = yf.Ticker(ticker).info
                if not info or not info.get("currentPrice"):
                    continue
                price = info.get("currentPrice", 0)
                score = _score_candidate(info)

                # Apply sector/risk bias from performance history
                sector = info.get("sector", "")
                risk   = _risk(info.get("beta"), info.get("marketCap"))
                score += sector_bias.get(sector, 0) * 0.5
                score += risk_bias.get(risk, 0) * 0.3

                candidates.append({
                    "ticker":            ticker,
                    "company":           info.get("longName", ticker),
                    "sector":            sector,
                    "risk_level":        risk,
                    "rationale":         _rationale(info),
                    "status":            "new",
                    "note":              f"New pick — {_rationale(info)}",
                    "weeks_held":        1,
                    "price_when_picked": round(price, 2),
                    "score":             score,
                    "is_known":          ticker in WELL_KNOWN,
                })
                time.sleep(0.1)
            except Exception:
                continue

        # Balance: at least 2 well-known, up to 3 hidden gems
        known  = sorted([c for c in candidates if c["is_known"]],    key=lambda x: x["score"], reverse=True)
        hidden = sorted([c for c in candidates if not c["is_known"]], key=lambda x: x["score"], reverse=True)

        new_picks = []
        k_need = min(max(slots_needed - min(slots_needed // 2, len(hidden)), 0), len(known))
        h_need = slots_needed - k_need
        new_picks = known[:k_need] + hidden[:h_need]

        # Note what replaced what
        for i, np_ in enumerate(new_picks):
            if i < len(dropped):
                old = dropped[i].get("ticker", "?")
                np_["note"] = f"New pick (replaced {old}) — {np_['rationale']}"

        kept_picks.extend([{k: v for k, v in p.items() if k != "score"} for p in new_picks])

    # ── update performance history in memory ──────────────────────────────────

    perf_map = {ph["ticker"]: ph for ph in perf_history}
    for pick in kept_picks:
        ticker     = pick["ticker"]
        price_now  = pick.get("price_now") or pick.get("price_when_picked")
        price_pick = pick.get("price_when_picked") or price_now
        pct        = round(((price_now - price_pick) / price_pick * 100), 2) if price_now and price_pick else None

        if ticker in perf_map:
            perf_map[ticker]["price_now"]           = price_now
            perf_map[ticker]["pct_change_since_pick"] = pct
            perf_map[ticker]["still_held"]          = True
        else:
            perf_map[ticker] = {
                "ticker":               ticker,
                "sector":               pick.get("sector", ""),
                "risk_level":           pick.get("risk_level", ""),
                "week_picked":          week_key,
                "week_dropped":         None,
                "price_when_picked":    price_pick,
                "price_now":            price_now,
                "pct_change_since_pick": pct,
                "still_held":           True,
            }

    # Mark dropped picks as no longer held
    for pick in dropped:
        t = pick.get("ticker", "")
        if t in perf_map:
            perf_map[t]["still_held"]  = False
            perf_map[t]["week_dropped"] = week_key

    mem["pick_performance_history"] = list(perf_map.values())

    # ── generate lessons_learned note ─────────────────────────────────────────

    recent_held = [ph for ph in perf_history if ph.get("still_held") and ph.get("pct_change_since_pick") is not None]
    lesson_note = ""
    if recent_held:
        avg_pct = sum(ph["pct_change_since_pick"] for ph in recent_held) / len(recent_held)
        best_sector = max(sector_bias, key=sector_bias.get) if sector_bias else None
        worst_sector = min(sector_bias, key=sector_bias.get) if sector_bias else None
        parts = [f"Active picks avg {avg_pct:+.1f}% since entry"]
        if best_sector and sector_bias[best_sector] > 2:
            parts.append(f"{best_sector} picks outperforming — leaning toward more")
        if worst_sector and sector_bias[worst_sector] < -2:
            parts.append(f"{worst_sector} picks underperforming — weighting away")
        lesson_note = "; ".join(parts) + "."

    if lesson_note:
        lessons.append({"week": week_key, "note": lesson_note})
        mem["lessons_learned"] = lessons[-20:]  # keep last 20 weeks

    # Save memory
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)

    # ── save picks ────────────────────────────────────────────────────────────

    week_data = {
        "generated":             datetime.now().isoformat(),
        "picks":                 kept_picks,
        "changes_from_last_week": changes_log,
    }
    cache[week_key] = week_data
    with open(PICKS_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    return json.dumps({"week": week_key, **week_data})


@mcp.tool()
def send_email(subject: str, html_body: str) -> str:
    """Send an HTML email via Gmail SMTP."""
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Pippy's Brief <{GMAIL_ADDRESS}>"
        msg["To"]      = TO_EMAIL
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, TO_EMAIL, msg.as_string())
        return f"Email sent to {TO_EMAIL}."
    except Exception as e:
        return f"Email failed: {e}"


@mcp.tool()
def get_last_email_summary() -> str:
    """Return a summary of the last email sent by OpenBell."""
    if not os.path.exists(MEMORY_FILE):
        return json.dumps({"note": "No email history found."})
    try:
        with open(MEMORY_FILE) as f:
            mem = json.load(f)
        return json.dumps({
            "last_email_sent":    mem.get("last_email_sent", ""),
            "last_email_summary": mem.get("last_email_summary", ""),
            "email_count":        mem.get("email_count", 0),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
