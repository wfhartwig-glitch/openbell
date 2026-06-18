#!/usr/bin/env python3
"""
OpenBell — Daily pre-market briefing email agent.
Uses FMP for market data, yfinance for futures, Gmail SMTP to send.
No AI API required.
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

load_dotenv()

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL           = os.getenv("TO_EMAIL")
FMP_API_KEY        = os.getenv("FMP_API_KEY")

PICKS_CACHE_FILE = "picks_cache.json"
PIPPY_MEMORY_FILE = "pippy_memory.json"
FMP_BASE         = "https://financialmodelingprep.com/api/v3"

FUTURES_MAP = {
    "S&P 500": "ES=F",
    "Nasdaq":  "NQ=F",
    "Dow":     "YM=F",
}

# Mix of blue-chip and under-the-radar names across sectors
WATCHLIST = [
    # Mega-cap well-known
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
    # Solid but less talked about
    "AVGO", "NOW", "CRWD", "PLTR", "DDOG", "TTD", "MELI",
    "CELH", "AXON", "TMDX", "HIMS", "APP", "HOOD", "IONQ",
    "SOUN", "RGTI", "VRT", "ANET", "DECK", "LULU",
]


def get_futures():
    now   = datetime.now()
    mins  = now.hour * 60 + now.minute
    # CT market hours: 8:30 AM – 3:00 PM
    is_open = now.weekday() < 5 and (8*60+30) <= mins <= (15*60)
    tickers = {
        "S&P 500": "^GSPC" if is_open else "ES=F",
        "Nasdaq":  "^IXIC" if is_open else "NQ=F",
        "Dow":     "^DJI"  if is_open else "YM=F",
    }
    results = []
    for name, ticker in tickers.items():
        try:
            t     = yf.Ticker(ticker)
            info  = t.fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct   = ((price - prev) / prev) * 100
                arrow = "▲" if pct >= 0 else "▼"
                results.append({"name": name, "price": f"{price:,.2f}",
                                 "change": f"{arrow} {abs(pct):.2f}%", "positive": pct >= 0})
            else:
                results.append({"name": name, "price": "N/A", "change": "N/A", "positive": True})
        except Exception as e:
            results.append({"name": name, "price": "N/A", "change": str(e), "positive": True})
    return results


def get_headlines():
    """Pull live headlines from Yahoo Finance news via yfinance — always current."""
    skip_keywords = ["beginner", "guide", "how to", "what is", "explainer", "glossary", "tutorial"]
    seen, titles = set(), []
    for ticker in ["SPY", "QQQ", "^VIX", "GLD"]:
        try:
            news = yf.Ticker(ticker).news or []
            for item in news:
                title = item.get("content", {}).get("title") or item.get("title", "")
                if (title and title not in seen and len(title) > 20
                        and not any(kw in title.lower() for kw in skip_keywords)):
                    seen.add(title)
                    titles.append(title)
        except Exception:
            continue
        if len(titles) >= 8:
            break
    return titles[:5] if titles else ["Check finance.yahoo.com for today's market headlines."]


def get_economic_events():
    """Pull today's earnings reports and macro events via yfinance."""
    events = []
    today_str = date.today().isoformat()

    # Today's earnings from high-profile tickers
    watch = ["AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","GS","WMT",
             "COST","NKE","FDX","ORCL","ADBE","CRM","INTC","AMD","NFLX","DIS"]
    for ticker in watch:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            # calendar can be a dict with 'Earnings Date' key
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    dates = ed if isinstance(ed, list) else [ed]
                    for d in dates:
                        d_str = str(d)[:10]
                        if d_str == today_str:
                            events.append({"event": f"{ticker} reports earnings", "date": "", "impact": "High"})
            else:
                # DataFrame format
                if hasattr(cal, "loc") and "Earnings Date" in cal.index:
                    for d in cal.loc["Earnings Date"]:
                        if str(d)[:10] == today_str:
                            events.append({"event": f"{ticker} reports earnings", "date": "", "impact": "High"})
        except Exception:
            continue

    if not events:
        events.append({
            "event": "No major earnings today. Check marketwatch.com/economy-politics/calendar for macro events.",
            "date": "", "impact": ""
        })
    return events[:6]


def get_market_movers():
    """Scan a broad watchlist via yfinance and return top gainers and losers."""
    scan = [
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","AVGO","NOW",
        "CRWD","PLTR","DDOG","CELH","AXON","HIMS","APP","VRT","ANET","SOUN",
    ]
    results = []
    for ticker in scan:
        try:
            info = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = ((price - prev) / prev) * 100
                results.append({"symbol": ticker, "changesPercentage": round(pct, 2)})
        except Exception:
            continue
    results.sort(key=lambda x: x["changesPercentage"], reverse=True)
    return {
        "gainers": results[:3],
        "losers":  results[-3:][::-1],
        "active":  [],
    }


def build_what_to_watch(futures, movers):
    positive = sum(1 for f in futures if f["positive"])
    tone = {3: "broadly positive", 0: "broadly negative"}.get(positive, "mixed")

    sp = next((f for f in futures if f["name"] == "S&P 500"), None)
    nq = next((f for f in futures if f["name"] == "Nasdaq"), None)
    sp_str = f"S&P 500 futures {sp['change']}" if sp and sp["price"] != "N/A" else ""
    nq_str = f"Nasdaq {nq['change']}" if nq and nq["price"] != "N/A" else ""
    intro = f"Futures signal a {tone} open — {', '.join(filter(None,[sp_str,nq_str]))}."

    lines = [intro]
    if movers.get("gainers"):
        names = ", ".join(
            f"{g['symbol']} (+{g['changesPercentage']}%)" for g in movers["gainers"]
        )
        lines.append(f"Leading the move higher: {names}.")
    if movers.get("losers"):
        names = ", ".join(
            f"{g['symbol']} ({g['changesPercentage']}%)" for g in movers["losers"]
        )
        lines.append(f"Under pressure: {names}.")
    lines.append("Watch for any pre-market earnings, Fed commentary, or macro data that could shift direction.")
    return " ".join(lines)


WELL_KNOWN = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM"}


def risk_level(beta, market_cap, rev_growth):
    """Return Low / Medium / High / Speculative based on volatility + size."""
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


def get_weekly_picks():
    # Cache key = year + ISO week number so it refreshes every Monday
    today    = date.today()
    week_key = today.strftime("%Y-W%W")
    cache    = {}

    if os.path.exists(PICKS_CACHE_FILE):
        with open(PICKS_CACHE_FILE) as f:
            cache = json.load(f)
    if week_key in cache and cache[week_key]:
        return cache[week_key]

    print("  • Generating weekly picks via yfinance (takes ~60s)…")
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

            # Build rationale
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
                "ticker":    ticker,
                "company":   company,
                "sector":    sector,
                "risk":      risk,
                "score":     score,
                "is_known":  is_known,
                "rationale": rationale,
            })
            time.sleep(0.1)
        except Exception:
            continue

    # Pick 3 well-known + 3 under-the-radar, sorted by score within each group
    known    = sorted([c for c in candidates if c["is_known"]],     key=lambda x: x["score"], reverse=True)[:3]
    hidden   = sorted([c for c in candidates if not c["is_known"]], key=lambda x: x["score"], reverse=True)[:3]
    picks    = known + hidden

    clean = [{k: v for k, v in p.items() if k not in ("score", "is_known")} for p in picks]
    cache[week_key] = clean
    with open(PICKS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return clean


def send_email(today_str, futures, headlines, econ_events, what_to_watch, picks, watchlist_data=None):
    today   = date.today()
    subject = f"OpenBell — {today.strftime('%A, %B %d')} Market Brief"

    # --- plain text ---
    def divider(title): return f"\n{title}\n" + "-" * 40

    futures_text = "\n".join(f"  {f['name']:<12} {f['price']:>12}   {f['change']}" for f in futures)
    headlines_text = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(headlines))
    econ_text = "\n".join(
        f"  {e.get('date','')[-8:-3]}  {e.get('event','')}  [{e.get('impact','')}]"
        for e in econ_events
    ) or "  No major events today."
    picks_text = "\n".join(
        f"  {p['ticker']} — {p['company']} ({p['sector']}) | Risk: {p.get('risk','—')}\n    {p['rationale']}"
        for p in picks
    )

    watchlist_text = ""
    if watchlist_data:
        watchlist_text = "\n".join(
            f"  {w['ticker']:<8} ${w['price']:>10}   {w['change']}" for w in watchlist_data
        )

    text_sections = [
        f"OPENBELL — {today_str}", "=" * 40,
        divider("FUTURES SNAPSHOT"), futures_text,
        divider("TOP HEADLINES"), headlines_text,
        divider("ECONOMIC EVENTS TODAY"), econ_text,
        divider("WHAT TO WATCH"), what_to_watch,
    ]
    if watchlist_text:
        text_sections += [divider("YOUR WATCHLIST"), watchlist_text]
    text_sections += [divider("TOP PICKS OF THE WEEK"), picks_text,
                      "\nNot financial advice. Sent by OpenBell."]
    text = "\n".join(text_sections)

    # --- HTML ---
    futures_rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0">{f["name"]}</td>'
        f'<td style="padding:4px 12px 4px 0;font-weight:bold">{f["price"]}</td>'
        f'<td style="padding:4px 0;color:{"#16a34a" if f["positive"] else "#dc2626"};font-weight:bold">{f["change"]}</td></tr>'
        for f in futures
    )
    headlines_html = "".join(f"<li style='margin:6px 0'>{h}</li>" for h in headlines)

    if econ_events and "Could not" in econ_events[0].get("event", ""):
        econ_html = f'<p>{econ_events[0]["event"]}</p>'
    else:
        econ_rows = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#6b7280">{e.get("date","")[-8:-3]}</td>'
            f'<td style="padding:4px 12px 4px 0">{e.get("event","")}</td>'
            f'<td style="padding:4px 0;color:{"#dc2626" if e.get("impact")=="High" else "#d97706"};font-size:12px">{e.get("impact","")}</td></tr>'
            for e in econ_events
        )
        econ_html = f'<table style="border-collapse:collapse;font-size:14px">{econ_rows}</table>'

    risk_colors = {"Low": "#16a34a", "Medium": "#d97706", "High": "#dc2626", "Speculative": "#7c3aed"}
    picks_rows = "".join(
        f'<tr style="border-bottom:1px solid #f3f4f6">'
        f'<td style="padding:7px 12px 7px 0;font-weight:bold">{p["ticker"]}</td>'
        f'<td style="padding:7px 12px 7px 0">{p["company"]}</td>'
        f'<td style="padding:7px 12px 7px 0;color:#6b7280;font-size:13px">{p["sector"]}</td>'
        f'<td style="padding:7px 12px 7px 0;font-weight:bold;color:{risk_colors.get(p.get("risk","Medium"),"#d97706")};font-size:12px;white-space:nowrap">{p.get("risk","—")}</td>'
        f'<td style="padding:7px 0;font-size:13px">{p["rationale"]}</td></tr>'
        for p in picks
    )

    def section(title, body):
        return (f'<h2 style="font-size:14px;text-transform:uppercase;letter-spacing:.05em;'
                f'color:#374151;margin:0 0 8px">{title}</h2>'
                f'<div style="margin-bottom:22px">{body}</div>')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:Georgia,serif;max-width:620px;margin:0 auto;padding:24px;color:#111;background:#fff">
  <h1 style="font-size:22px;margin:0 0 4px">OpenBell</h1>
  <p style="margin:0 0 22px;color:#6b7280;font-size:13px">{today_str} &mdash; Pre-Market Briefing</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px">
  {section("Futures Snapshot", f'<table style="border-collapse:collapse;font-size:15px">{futures_rows}</table>')}
  {section("Top Headlines", f'<ul style="margin:0;padding-left:20px;font-size:15px;line-height:1.6">{headlines_html}</ul>')}
  {section("Economic Events Today", econ_html)}
  {section("What to Watch", f'<p style="font-size:15px;line-height:1.7;margin:0">{what_to_watch}</p>')}
  {section("Your Watchlist",
    '<table style="border-collapse:collapse;font-size:15px">' +
    "".join(
        f'<tr><td style="padding:4px 12px 4px 0;font-weight:bold">{w["ticker"]}</td>'
        f'<td style="padding:4px 12px 4px 0">${w["price"]}</td>'
        f'<td style="padding:4px 0;color:{"#16a34a" if w["positive"] else "#dc2626"};font-weight:bold">{w["change"]}</td></tr>'
        for w in (watchlist_data or [])
    ) + '</table>'
  ) if watchlist_data else ""}
  {section("Top Picks of the Week",
    f'<table style="border-collapse:collapse;font-size:14px;width:100%">'
    f'<tr style="border-bottom:2px solid #e5e7eb;color:#6b7280;font-size:11px;text-transform:uppercase">'
    f'<td style="padding:3px 12px 3px 0">Ticker</td><td style="padding:3px 12px 3px 0">Company</td>'
    f'<td style="padding:3px 12px 3px 0">Sector</td><td style="padding:3px 12px 3px 0">Risk</td>'
    f'<td style="padding:3px 0">Rationale</td></tr>'
    f'{picks_rows}</table>'
    f'<p style="font-size:11px;color:#9ca3af;margin:8px 0 0">'
    f'3 well-known + 3 under-the-radar picks, refreshed weekly. For informational use only.</p>'
  )}
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 10px">
  <p style="font-size:11px;color:#9ca3af;margin:0">Sent by OpenBell — automated pre-market briefing.</p>
</body></html>"""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"OpenBell <{GMAIL_ADDRESS}>"
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, TO_EMAIL, msg.as_string())


def load_pippy_memory() -> dict:
    if os.path.exists(PIPPY_MEMORY_FILE):
        try:
            with open(PIPPY_MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_watchlist_snapshot(flagged_tickers: list) -> list[dict]:
    """Fetch pre-market data for Pippy's flagged tickers."""
    results = []
    for ticker in flagged_tickers:
        try:
            info  = yf.Ticker(ticker).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct   = ((price - prev) / prev) * 100
                arrow = "▲" if pct >= 0 else "▼"
                results.append({"ticker": ticker, "price": f"{price:.2f}",
                                 "change": f"{arrow} {abs(pct):.2f}%", "positive": pct >= 0})
        except Exception:
            results.append({"ticker": ticker, "price": "N/A", "change": "N/A", "positive": True})
    return results


def run_briefing():
    today     = date.today()
    today_str = today.strftime("%A, %B %d, %Y")
    print(f"[OpenBell] Running briefing for {today_str}…")

    # Load Pippy memory for watchlist and context
    pippy_mem       = load_pippy_memory()
    flagged_tickers = pippy_mem.get("flagged_tickers", [])
    watchlist_data  = []
    if flagged_tickers:
        print(f"  • Fetching your watchlist ({', '.join(flagged_tickers)})…")
        watchlist_data = get_watchlist_snapshot(flagged_tickers)

    print("  • Fetching futures…")
    futures = get_futures()

    print("  • Fetching headlines…")
    headlines = get_headlines()

    print("  • Fetching economic calendar…")
    econ_events = get_economic_events()

    print("  • Fetching market movers…")
    movers = get_market_movers()
    what_to_watch = build_what_to_watch(futures, movers)

    print("  • Loading weekly picks (first run of the week takes ~60s)…")
    picks = get_weekly_picks()

    print("  • Sending email…")
    send_email(today_str, futures, headlines, econ_events, what_to_watch, picks, watchlist_data)

    # Write back to Pippy memory
    try:
        pippy_mem["last_email_sent"] = datetime.now().isoformat()
        top_gainers = [m["symbol"] for m in movers.get("gainers", [])][:2]
        sp = next((f for f in futures if f["name"] == "S&P 500"), None)
        direction = "up" if sp and sp["positive"] else "down"
        pippy_mem["last_email_summary"] = (
            f"Sent {today_str}. S&P futures {direction}. "
            f"Leaders: {', '.join(top_gainers) if top_gainers else 'N/A'}. "
            f"Top pick: {picks[0]['ticker'] if picks else 'N/A'}."
        )
        with open(PIPPY_MEMORY_FILE, "w") as f:
            json.dump(pippy_mem, f, indent=2)
    except Exception:
        pass

    print(f"[OpenBell] Done — email sent to {TO_EMAIL}.")


if __name__ == "__main__":
    run_briefing()
