"""
Trends desk — where the market has been, from daily closes. Facts only, hedged, no predictions.

  * S&P 500 and Nasdaq against their 50-day and 200-day simple moving averages; 5-day and 1-month changes
  * Sector leadership over 1 week and 1 month (the 11 sector ETFs)
  * The 10-year yield, crude oil and gold over the past week and month
  * The VIX level and its recent range
  * A two-sentence plain-English read on the pattern, written from fixed templates

One batched request for 17 symbols (Yahoo Finance daily closes via yfinance): no key, no cost, no LLM.
Every fact carries its source and the date of the bar it comes from. Market-wide only: this desk is
handed nothing from the owner's picks, holdings or memory.

Windows are trading days: 5 = about a week, 21 = about a month. Thresholds below are judgment calls.
Idea credit: the batching/ordering approach was informed by reading github.com/Jakeschincariol/day-trader-skill
(MIT); no code was copied.
"""
import datetime
import zoneinfo

import brief_common as bc

NAME = "trends"
PRIVATE = False
TIMEOUT = 25.0

SOURCE = "Yahoo Finance daily closes (via yfinance)"
INDEXES = {"S&P 500": "^GSPC", "Nasdaq": "^IXIC"}
YIELD_SYMBOL, OIL_SYMBOL, GOLD_SYMBOL, VIX_SYMBOL = "^TNX", "CL=F", "GC=F", "^VIX"
W1, M1 = 5, 21                       # trading days in "a week" and "a month"
MIN_FOR_MA50, MIN_FOR_MA200 = 50, 200

# Judgment calls (not market conventions): below these a multi-week move is called "little changed".
YIELD_MOVE_PP = 0.10                 # percentage points
COMMODITY_MOVE_PCT = 1.0
# Conventional VIX bands used for plain-English labels.
VIX_BANDS = ((15, "low"), (20, "moderate"), (30, "elevated"))


# ── data ─────────────────────────────────────────────────────────────────────

def symbols() -> list:
    return list(INDEXES.values()) + list(bc.SECTOR_ETFS.values()) + [YIELD_SYMBOL, OIL_SYMBOL, GOLD_SYMBOL, VIX_SYMBOL]


class YFinanceCloses:
    """Default client: ONE batched request -> {symbol: [(date, close), ...]} oldest first."""

    def closes(self, syms):
        import yfinance as yf
        df = yf.download(list(syms), period="14mo", interval="1d", auto_adjust=False, group_by="ticker",
                         progress=False, threads=False)
        out = {}
        for s in syms:
            try:
                out[s] = [(i.date(), float(v)) for i, v in df[s]["Close"].dropna().items() if float(v) > 0]
            except Exception:
                out[s] = []
        return out


def _change(closes, n, pct=True):
    """Change over n trading days between the last close and the close n bars earlier."""
    if len(closes) <= n:
        return None
    a, b = closes[-1 - n][1], closes[-1][1]
    return (b - a) / a * 100 if pct else b - a


def _ma(closes, n):
    return sum(c for _, c in closes[-n:]) / n if len(closes) >= n else None


def _stamp(closes, symbol, fetched):
    return {"symbol": symbol, "source": SOURCE, "as_of": closes[-1][0].isoformat(), "fetched_at": fetched}


def fetch(client=None, now=None) -> dict:
    """-> facts dict, or {} when there is nothing usable. Never raises for missing symbols."""
    client = client or YFinanceCloses()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    fetched = now.replace(microsecond=0).isoformat()
    data = client.closes(symbols())
    facts = {"desk": NAME, "source": SOURCE, "fetched_at": fetched, "indexes": {}, "sectors": [], "macro": {}, "vix": None}

    for name, sym in INDEXES.items():
        c = data.get(sym) or []
        if len(c) <= M1:
            continue
        ma50, ma200 = _ma(c, MIN_FOR_MA50), _ma(c, MIN_FOR_MA200)
        last = c[-1][1]
        f = _stamp(c, sym, fetched)
        f.update({"last": last, "ma50": ma50, "ma200": ma200,
                  "pct_vs_ma50": (last - ma50) / ma50 * 100 if ma50 else None,
                  "pct_vs_ma200": (last - ma200) / ma200 * 100 if ma200 else None,
                  "chg_5d": _change(c, W1), "chg_1m": _change(c, M1)})
        facts["indexes"][name] = f

    for sector, etf in bc.SECTOR_ETFS.items():
        c = data.get(etf) or []
        w, m = _change(c, W1), _change(c, M1)
        if w is None or m is None:
            continue
        f = _stamp(c, etf, fetched)
        f.update({"sector": sector, "chg_1w": w, "chg_1m": m})
        facts["sectors"].append(f)

    c = data.get(YIELD_SYMBOL) or []
    if len(c) > M1:
        f = _stamp(c, YIELD_SYMBOL, fetched)
        f.update({"level": c[-1][1], "chg_1w_pp": _change(c, W1, pct=False), "chg_1m_pp": _change(c, M1, pct=False)})
        facts["macro"]["10-year Treasury yield"] = f
    for name, sym in (("Crude oil", OIL_SYMBOL), ("Gold", GOLD_SYMBOL)):
        c = data.get(sym) or []
        if len(c) > M1:
            f = _stamp(c, sym, fetched)
            f.update({"level": c[-1][1], "chg_1w": _change(c, W1), "chg_1m": _change(c, M1)})
            facts["macro"][name] = f

    c = data.get(VIX_SYMBOL) or []
    if len(c) > M1:
        month = [v for _, v in c[-M1:]]
        f = _stamp(c, VIX_SYMBOL, fetched)
        f.update({"level": c[-1][1], "low_1m": min(month), "high_1m": max(month)})
        facts["vix"] = f

    if not (facts["indexes"] or facts["sectors"] or facts["macro"] or facts["vix"]):
        return {}
    dates = [f["as_of"] for f in list(facts["indexes"].values()) + facts["sectors"]]
    facts["as_of"] = max(dates) if dates else None
    facts["session_in_progress"] = _session_in_progress(now, facts["as_of"])
    return facts


def _session_in_progress(now, as_of) -> bool:
    """True when the newest bar is today's and the US market has not closed yet (a manual mid-day run).
    The scheduled morning run (before the open) and close run (after 4 PM ET) are never in progress."""
    if not as_of:
        return False
    try:
        et = now.astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:                        # no tz database on the runner: never let a label drop the section
        return False
    return datetime.date.fromisoformat(as_of) == et.date() and et.weekday() < 5 and et.time() < datetime.time(16, 0)


# ── wording ──────────────────────────────────────────────────────────────────

def _rel(pct) -> str:
    if abs(pct) < 0.1:
        return "about level with"
    return f"{abs(pct):.1f}% {'above' if pct > 0 else 'below'}"


def _mv(v, unit="%", little=0.0) -> str:
    """'up 1.2%' / 'down 0.4 percentage points' / 'little changed' (inside `little`, or rounding to zero)."""
    if abs(v) < little or (unit == "%" and round(abs(v), 1) == 0):
        return "little changed"
    return f"{'up' if v > 0 else 'down'} {abs(v):.1f}%" if unit == "%" else f"{'up' if v > 0 else 'down'} {abs(v):.2f} percentage points"


def _state(f):
    a, b = f.get("pct_vs_ma50"), f.get("pct_vs_ma200")
    if a is None or b is None:
        return None
    return ("above" if a >= 0 else "below", "above" if b >= 0 else "below")


# (vs 50-day, vs 200-day) -> (one index, two indexes, what analysts call it)
_STATE_TEXT = {
    ("above", "above"): ("above both its 50-day and 200-day averages", "both above their 50-day and 200-day averages", "an established uptrend"),
    ("below", "above"): ("below its 50-day but above its 200-day average", "both below their 50-day but above their 200-day averages", "a pullback within a longer-term uptrend"),
    ("above", "below"): ("above its 50-day but below its 200-day average", "both above their 50-day but below their 200-day averages", "a rebound inside a longer-term downtrend"),
    ("below", "below"): ("below both its 50-day and 200-day averages", "both below their 50-day and 200-day averages", "a downtrend"),
}


def _cap(text: str) -> str:
    """Upper-case the first letter only (str.capitalize() would lower-case 'VIX' and 'Treasury')."""
    return text[:1].upper() + text[1:]


def _vix_label(level) -> str:
    for cap, word in VIX_BANDS:
        if level < cap:
            return word
    return "high"


def _read(facts) -> str:
    """Two sentences, from fixed templates. Describes where prices have been; predicts nothing."""
    idx = facts.get("indexes", {})
    known = {n: _state(f) for n, f in idx.items() if _state(f)}
    if len(idx) == 2 and len(known) == 2 and len(set(known.values())) == 1:
        s = next(iter(known.values()))
        first = f"The S&P 500 and Nasdaq are {_STATE_TEXT[s][1]}, a pattern analysts commonly describe as {_STATE_TEXT[s][2]}."
    elif len(known) == 2:
        (n1, s1), (n2, s2) = known.items()
        first = f"The {n1} is {_STATE_TEXT[s1][0]} while the {n2} is {_STATE_TEXT[s2][0]}, so the two indexes are not giving the same signal."
    elif known:
        n, s = next(iter(known.items()))
        first = f"The {n} is {_STATE_TEXT[s][0]}, which is usually described as {_STATE_TEXT[s][2]}."
    elif idx:
        n, f = next(iter(idx.items()))
        first = (f"There is not yet enough price history to compute moving averages; the {n} is "
                 f"{_mv(f['chg_1m']) if f.get('chg_1m') is not None else 'in view'} over about a month.")
    else:
        first = "Index price history was not available for this read."

    secs = facts.get("sectors", [])
    bits = []
    vix = facts.get("vix")
    if vix:
        bits.append(f"volatility is {_vix_label(vix['level'])} (VIX {vix['level']:.1f})")
    if secs:
        w = max(secs, key=lambda s: s["chg_1w"])["sector"]
        m = max(secs, key=lambda s: s["chg_1m"])["sector"]
        bits.append(f"{w} has led over both the past week and month" if w == m else f"{w} led the past week and {m} the past month")
    tail = "this describes where prices have been, not where they are going."
    second = f"{_cap(' and '.join(bits))}; {tail}" if bits else _cap(tail)
    return f"{first} {second}"


def _sector_line(secs, key, label) -> str:
    ranked = sorted(secs, key=lambda s: s[key], reverse=True)
    lead = " and ".join(f"{s['sector']} ({_mv(s[key])})" for s in ranked[:2])
    lag = " and ".join(f"{s['sector']} ({_mv(s[key])})" for s in ranked[-2:][::-1])
    return f"Over the {label}, {lead} led the sectors, while {lag} trailed."


def build_section(facts, mode="close"):
    if not facts:
        return None
    idx, secs, macro, vix = facts.get("indexes", {}), facts.get("sectors", []), facts.get("macro", {}), facts.get("vix")
    if not (idx or secs or macro or vix):
        return None
    p = lambda txt, last=False: (f'<p style="margin:0 0 {0 if last else 10}px;font-size:16px;line-height:1.5;color:#374151">{txt}</p>')
    out = [f'<p style="margin:0 0 14px;font-size:16px;line-height:1.6;color:#1f2937">{_read(facts)}</p>']

    for name, f in idx.items():
        bits = []
        if f.get("pct_vs_ma50") is not None:
            bits.append(f"{_rel(f['pct_vs_ma50'])} its 50-day average ({f['ma50']:,.0f})")
        if f.get("pct_vs_ma200") is not None:
            bits.append(f"{_rel(f['pct_vs_ma200'])} its 200-day average ({f['ma200']:,.0f})")
        moves = [x for x in (f"{_mv(f['chg_5d'])} over 5 trading days" if f.get("chg_5d") is not None else "",
                             f"{_mv(f['chg_1m'])} over about a month" if f.get("chg_1m") is not None else "") if x]
        out.append(p(f"{name}: {f['last']:,.0f}" + (f", {' and '.join(bits)}" if bits else "") + (f"; {' and '.join(moves)}." if moves else ".")))

    if secs:
        out.append(p(_sector_line(secs, "chg_1w", "past week")))
        out.append(p(_sector_line(secs, "chg_1m", "past month")))

    if macro:
        said = []
        y = macro.get("10-year Treasury yield")
        if y:
            said.append(f"the 10-year Treasury yield is {y['level']:.2f}%, {_mv(y['chg_1w_pp'], 'pp', YIELD_MOVE_PP)} over a week and "
                        f"{_mv(y['chg_1m_pp'], 'pp', YIELD_MOVE_PP)} over a month")
        for nm in ("Crude oil", "Gold"):
            f = macro.get(nm)
            if f:
                tag = " (front-month futures)" if nm == "Crude oil" else " (futures)"
                said.append(f"{nm.lower()}{tag} is {_mv(f['chg_1w'], '%', COMMODITY_MOVE_PCT)} over a week and {_mv(f['chg_1m'], '%', COMMODITY_MOVE_PCT)} over a month")
        out.append(p(f"{_cap('; '.join(said))}."))

    if vix:
        pos = ""
        if vix["high_1m"] > vix["low_1m"]:
            frac = (vix["level"] - vix["low_1m"]) / (vix["high_1m"] - vix["low_1m"])
            pos = f", in the {'lower third' if frac < 1/3 else 'middle' if frac < 2/3 else 'upper third'} of its past-month range ({vix['low_1m']:.1f} to {vix['high_1m']:.1f})"
        out.append(p(f"The VIX, a gauge of how much price swing traders expect over the next 30 days, is {vix['level']:.1f}, "
                     f"a {_vix_label(vix['level'])} reading{pos}.", last=True))

    asof = facts.get("as_of")
    when = datetime.date.fromisoformat(asof).strftime("%b %d") if asof else "the latest session"
    note = (f"A moving average is the average closing price over the last 50 or 200 trading days; being above it means recent prices are "
            f"higher than that average. Sources: {facts.get('source', SOURCE)}, latest bar {when}. Futures figures use the front-month "
            f"contract, so contract rolls can distort multi-week changes.")
    out.append(f'<p style="margin:12px 0 0;font-size:12px;line-height:1.5;color:#6b7280">{note}</p>')
    label = (f"Market Trends — as of {when}, session in progress" if facts.get("session_in_progress")
             else f"Market Trends — as of the {when} close")
    return bc._section(label, "".join(out))
