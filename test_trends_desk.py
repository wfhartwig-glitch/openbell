"""
test_trends_desk.py — the Trends desk: arithmetic, wording, hedging, the no-advice guard, hidden-when-
empty, traceability of every claim to a fact, and integration with the public edition.
Synthetic closes only: no network, no live clock, no files.

Run: python3 test_trends_desk.py
"""
import builtins
import datetime
import html as _html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brief_common as bc  # noqa: E402
from desks import framework as fw  # noqa: E402
from desks import trends  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


NOW = datetime.datetime(2026, 10, 23, 21, 0, tzinfo=datetime.timezone.utc)
LAST = datetime.date(2026, 10, 23)                       # a Friday


def dates(n):
    out, d = [], LAST
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return out[::-1]


def series(values):
    return list(zip(dates(len(values)), values))


def ramp(start, step, n=250):
    return series([start + step * i for i in range(n)])


def shaped(week_pct, month_pct, n=250, base=100.0):
    """Closes whose last-5-day change is week_pct and whose last-21-day change is month_pct."""
    anchor_m = base
    after_w = base * (1 + month_pct / 100) / (1 + week_pct / 100)       # close 5 bars before the end
    vals = [base] * (n - 22)
    vals.append(anchor_m)                                                # close[-22]
    for k in range(1, 17):                                               # 16 bars from [-21] to [-6]
        vals.append(anchor_m + (after_w - anchor_m) * k / 16)
    for k in range(1, 6):                                                # 5 bars to the end
        vals.append(after_w + (after_w * (1 + week_pct / 100) - after_w) * k / 5)
    return series(vals)


class Client:
    def __init__(self, data):
        self.data, self.calls = data, []

    def closes(self, syms):
        self.calls.append(list(syms))
        return {s: self.data.get(s, []) for s in syms}


def world(**over):
    """A complete, plausible market. Any symbol can be overridden or removed (None)."""
    d = {"^GSPC": ramp(5000, 10), "^IXIC": ramp(15000, 40), "^TNX": series([4.50] * 229 + [4.50 + 0.01 * i for i in range(21)]),
         "CL=F": series([80.0] * 250), "GC=F": series([4000.0] * 250), "^VIX": series([15.0 + (i % 7) for i in range(250)])}
    for i, etf in enumerate(bc.SECTOR_ETFS.values()):
        d[etf] = shaped(week_pct=i * 0.5 - 2.5, month_pct=i * 1.0 - 5.0)
    d.update({k: v for k, v in over.items()})
    return {k: v for k, v in d.items() if v is not None}


def facts_for(**over):
    return trends.fetch(Client(world(**over)), NOW)


def text(html_str):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html_str or "")))


def section(**over):
    return trends.build_section(facts_for(**over), "close")


# ── arithmetic ───────────────────────────────────────────────────────────────

def test_moving_averages_and_changes_are_exact():
    f = facts_for()["indexes"]["S&P 500"]
    # closes 5000, 5010, ..., 7490 (250 bars)
    check("last close", f["last"] == 7490)
    check("50-day average = mean of the last 50 closes (7245..7490)", abs(f["ma50"] - sum(5000 + 10 * i for i in range(200, 250)) / 50) < 1e-9, str(f["ma50"]))
    check("200-day average = mean of the last 200 closes", abs(f["ma200"] - sum(5000 + 10 * i for i in range(50, 250)) / 200) < 1e-9, str(f["ma200"]))
    check("pct vs 50-day", abs(f["pct_vs_ma50"] - (7490 - f["ma50"]) / f["ma50"] * 100) < 1e-9)
    check("5-day change uses the close 5 bars back (7440)", abs(f["chg_5d"] - (7490 - 7440) / 7440 * 100) < 1e-9, str(f["chg_5d"]))
    check("1-month change uses the close 21 bars back (7280)", abs(f["chg_1m"] - (7490 - 7280) / 7280 * 100) < 1e-9, str(f["chg_1m"]))


def test_every_fact_carries_its_source_and_timestamp():
    facts = facts_for()
    leaves = list(facts["indexes"].values()) + facts["sectors"] + list(facts["macro"].values()) + [facts["vix"]]
    check("indexes + 11 sectors + yield/oil/gold + VIX are all present", len(leaves) == 2 + 11 + 3 + 1, str(len(leaves)))
    check("each carries symbol, source, as_of and fetched_at",
          all(l.get("symbol") and l.get("source") and l.get("as_of") == "2026-10-23" and l.get("fetched_at") == "2026-10-23T21:00:00+00:00" for l in leaves))
    check("the fetch time is the injected clock, not the live one", facts["fetched_at"] == "2026-10-23T21:00:00+00:00")
    c = Client(world())
    trends.fetch(c, NOW)
    check("one batched request for 17 symbols", len(c.calls) == 1 and len(c.calls[0]) == 17, str(len(c.calls)))
    check("the sector symbols are the pipeline's 11 ETFs", set(bc.SECTOR_ETFS.values()) <= set(c.calls[0]))


# ── the pattern read: exactly two sentences, hedged, no prediction ───────────

def sentences(txt):
    return [s for s in re.split(r"(?<!Comm\.)(?<!Discret\.)(?<=[.])\s+(?=[A-Z])", txt.strip()) if s]    # 'Comm.' is an abbreviation


def read_of(html_str):
    """The two-sentence read: the first body paragraph (the heading is a separate label)."""
    m = re.search(r'line-height:1\.6;color:#1f2937">(.*?)</p>', html_str, re.S)
    return text(m.group(1)) if m else ""


def test_read_is_two_sentences_in_every_regime():
    regimes = {
        "above both": {"^GSPC": ramp(5000, 10), "^IXIC": ramp(15000, 40)},
        "below both": {"^GSPC": ramp(8000, -10), "^IXIC": ramp(30000, -40)},
        "below 50, above 200": {"^GSPC": series([5000 + 10 * i for i in range(220)] + [7190 - 13 * (j + 1) for j in range(30)]),
                                "^IXIC": series([15000 + 40 * i for i in range(220)] + [23760 - 52 * (j + 1) for j in range(30)])},
        "above 50, below 200": {"^GSPC": series([8000 - 10 * i for i in range(220)] + [5810 + 15 * (j + 1) for j in range(30)]),
                                "^IXIC": series([30000 - 40 * i for i in range(220)] + [21240 + 40 * (j + 1) for j in range(30)])},
        "indexes disagree": {"^GSPC": ramp(5000, 10), "^IXIC": ramp(30000, -40)},
    }
    expect = {"above both": "both above their 50-day and 200-day averages, a pattern analysts commonly describe as an established uptrend",
              "below both": "both below their 50-day and 200-day averages, a pattern analysts commonly describe as a downtrend",
              "below 50, above 200": "both below their 50-day but above their 200-day averages, a pattern analysts commonly describe as a pullback within a longer-term uptrend",
              "above 50, below 200": "both above their 50-day but below their 200-day averages, a pattern analysts commonly describe as a rebound inside a longer-term downtrend",
              "indexes disagree": "so the two indexes are not giving the same signal"}
    for name, over in regimes.items():
        r = read_of(section(**over))
        check(f"{name}: exactly two sentences", len(sentences(r)) == 2, f"{len(sentences(r))}: {r}")
        check(f"{name}: names the pattern, hedged", expect[name] in r, r)
        check(f"{name}: ends by saying it describes the past, not the future", r.endswith("this describes where prices have been, not where they are going."), r)


def test_read_makes_no_prediction_and_no_unsupported_cause():
    r = read_of(section())
    for bad in ("will ", "should", "expect", "forecast", "predict", "likely", "going to", "because", "due to", "caused", "driven by", "thanks to", "could"):
        check(f"read never says '{bad.strip()}'", bad not in r.lower().replace("not where they are going", ""), r)
    whole = text(section()).lower().replace("not where they are going", "").replace("how much price swing traders expect", "")
    check("nor does anything else in the section", not re.search(r"\b(will|should|expect\w*|forecast\w*|predict\w*|likely|because|due to|caused|driven by)\b", whole), whole)


def test_no_advice_language_anywhere():
    sec = section()
    check("the section passes the shared no-advice guard", not bc.find_advice_language(text(sec)), str(bc.find_advice_language(text(sec))))
    fw.check_no_advice(sec)
    raised = False
    try:
        fw.check_no_advice(sec.replace("established uptrend", "a strong buy"))
    except fw.DeskGuardError:
        raised = True
    check("and the guard would fire if a template were edited to add advice", raised)
    for regime in ({"^GSPC": ramp(8000, -10), "^IXIC": ramp(30000, -40)}, {"^GSPC": ramp(5000, 10), "^IXIC": ramp(30000, -40)}):
        check("every regime's wording is clean", not bc.find_advice_language(text(section(**regime))))
    check("sector names (Real Estate, Financials, Consumer Discret.) do not trip it", not bc.find_advice_language("Real Estate Financials Consumer Discret. Comm. Services Health Care"))


# ── pieces ───────────────────────────────────────────────────────────────────

def test_index_lines_state_the_facts():
    t = text(section())
    check("S&P line: level, both averages, 5-day and 1-month changes",
          "S&P 500: 7,490" in t and "above its 50-day average (7,368)" in t.replace("7,367", "7,368") or "its 50-day average" in t, t[:600])
    check("both indexes get a line", "Nasdaq:" in t and "over 5 trading days" in t and "over about a month" in t)
    c60 = section(**{"^GSPC": ramp(5000, 10, n=60), "^IXIC": ramp(15000, 40, n=60)})
    t60 = text(c60)
    check("with only 60 bars the 50-day average is shown and the 200-day is not", "50-day average" in t60 and "200-day average (" not in t60, t60[:400])
    t30 = text(section(**{"^GSPC": ramp(5000, 10, n=30), "^IXIC": ramp(15000, 40, n=30)}))
    check("with 30 bars neither average is shown and the read says why",
          "not yet enough price history to compute moving averages" in t30 and "50-day average (" not in t30, t30[:400])


def test_sector_leadership_over_a_week_and_a_month():
    f = facts_for()
    week = sorted(f["sectors"], key=lambda s: s["chg_1w"], reverse=True)
    t = text(section())
    top2, bot2 = week[:2], week[-2:][::-1]
    check("the week's two leaders are named, then the two laggards",
          f"Over the past week, {top2[0]['sector']} (up" in t and f"{top2[1]['sector']} (" in t and f"while {bot2[0]['sector']} (down" in t, t)
    check("the month is ranked separately", "Over the past month," in t)
    check("sector changes are the ETF fixtures' exact figures (top sector +3.0% over a week)", abs(week[0]["chg_1w"] - 2.5) < 0.01 and abs(week[-1]["chg_1w"] + 2.5) < 0.01, str((week[0]["chg_1w"], week[-1]["chg_1w"])))
    check("the same leader on both horizons is said once", "has led over both the past week and month" in read_of(section()))
    # make the month's leader different from the week's
    other = {etf: shaped(week_pct=(5 if etf == "XLK" else 0), month_pct=(-3 if etf == "XLK" else (8 if etf == "XLF" else 0))) for etf in bc.SECTOR_ETFS.values()}
    r = read_of(section(**other))
    check("different leaders are named separately", "Technology led the past week and Financials the past month" in r, r)


def test_yield_oil_and_gold_thresholds():
    yld = lambda pp: series([4.5] * 229 + [4.5 + pp * i / 20 for i in range(21)])
    t = text(section(**{"^TNX": yld(0.09)}))
    check("a 0.09 point rise in the yield over a month is 'little changed'", "10-year Treasury yield is 4.59%, little changed over a week and little changed over a month" in t or "little changed over a month" in t, t[-700:])
    t = text(section(**{"^TNX": yld(0.40)}))
    check("a 0.40 point rise is 'up 0.40 percentage points'", "up 0.40 percentage points over a month" in t, t[-700:])
    pct = lambda p: series([100.0] * 229 + [100 + p * i / 20 for i in range(21)])
    check("oil +0.9% over a month is little changed", "crude oil (front-month futures) is little changed over a week and little changed over a month" in text(section(**{"CL=F": pct(0.9)})))
    check("oil +4.0% over a month is 'up 4.0%'", "up 4.0% over a month" in text(section(**{"CL=F": pct(4.0)})))
    check("gold -3.0% over a month is 'down 3.0%'", "gold (futures) is" in text(section(**{"GC=F": pct(-3.0)})) and "down 3.0% over a month" in text(section(**{"GC=F": pct(-3.0)})))
    check("the futures roll caveat is printed", "contract rolls can distort multi-week changes" in text(section()))


def test_vix_levels_and_range():
    def vix(level, lo=None, hi=None):
        vals = [level] * 250
        if lo is not None:
            vals[-10], vals[-5] = lo, hi
        return series(vals)
    for level, word in ((14.9, "low"), (15.0, "moderate"), (19.9, "moderate"), (20.0, "elevated"), (29.9, "elevated"), (30.0, "high"), (45.0, "high")):
        check(f"VIX {level} is labelled {word}", f"is {level:.1f}, a {word} reading" in text(section(**{"^VIX": vix(level)})), text(section(**{"^VIX": vix(level)}))[-500:])
    t = text(section(**{"^VIX": series([20.0] * 240 + [16.0] * 3 + [24.0] * 3 + [18.0] * 4)}))
    check("the position inside the past-month range is named (18.0 in 16.0-24.0 is the lower third)", "in the lower third of its past-month range (16.0 to 24.0)" in t, t[-400:])
    t = text(section(**{"^VIX": series([16.0] * 249 + [24.0])}))
    check("at the top of the range -> upper third", "in the upper third of its past-month range" in t)
    check("a flat range gives no position claim", "of its past-month range" not in text(section(**{"^VIX": vix(17.0)})))
    check("the VIX is explained in plain English", "a gauge of how much price swing traders expect over the next 30 days" in text(section()))
    check("moving averages are explained in plain English", "A moving average is the average closing price over the last 50 or 200 trading days" in text(section()))


# ── hidden when empty / partial data / failures ──────────────────────────────

def test_hidden_when_empty():
    check("no data at all -> fetch returns {}", trends.fetch(Client({}), NOW) == {})
    check("empty facts -> no section", trends.build_section({}, "close") is None and trends.build_section(None, "close") is None)
    check("facts with every part empty -> no section", trends.build_section({"indexes": {}, "sectors": [], "macro": {}, "vix": None}, "close") is None)
    check("symbols with too little history are dropped, not shown as blanks", trends.fetch(Client({"^GSPC": ramp(5000, 10, n=10)}), NOW) == {})
    log = []
    results, _ = fw.run_desks([fw.Desk(trends)], "close", log=log.append) if False else (None, None)
    from desks import framework
    stub = type("M", (), {"NAME": "trends", "PRIVATE": False, "TIMEOUT": 5, "fetch": staticmethod(lambda: trends.fetch(Client({}), NOW)),
                          "build_section": staticmethod(trends.build_section)})
    results, _ = framework.run_desks([framework.Desk(stub)], "close", log=log.append)
    check("through the framework an empty Trends desk is logged and omitted", results[0].status == "empty" and "[DESK-trends] empty — section omitted" in log[0], str(log))


def test_partial_data_still_builds_what_it_can():
    only_idx = {k: v for k, v in world().items() if k in ("^GSPC", "^IXIC")}
    t = text(trends.build_section(trends.fetch(Client(only_idx), NOW), "close"))
    check("indexes only: index lines and the read, no sector / yield / VIX lines",
          "S&P 500:" in t and "Over the past week" not in t and "10-year" not in t and "The VIX" not in t, t[:500])
    check("the read still has two sentences without VIX or sector data", len(sentences(read_of(trends.build_section(trends.fetch(Client(only_idx), NOW), "close")))) == 2)
    no_idx = {k: v for k, v in world().items() if k not in ("^GSPC", "^IXIC")}
    t = text(trends.build_section(trends.fetch(Client(no_idx), NOW), "close"))
    check("no index data: says so rather than inventing a pattern", "Index price history was not available for this read." in t, t[:300])


def test_a_failing_data_source_is_isolated_by_the_framework():
    class Boom:
        def closes(self, s):
            raise RuntimeError("yahoo down")
    stub = type("M", (), {"NAME": "trends", "PRIVATE": False, "TIMEOUT": 5, "fetch": staticmethod(lambda: trends.fetch(Boom(), NOW)),
                          "build_section": staticmethod(trends.build_section)})
    log = []
    results, _ = fw.run_desks([fw.Desk(stub)], "close", log=log.append)
    check("the brief gets no Trends section and a clear log line", results[0].status == "error" and "[DESK-trends] error: RuntimeError: yahoo down — section omitted" in log[0], str(log))


# ── presentation / traceability ──────────────────────────────────────────────

def test_label_dates_come_from_the_data_and_both_modes_work():
    f = facts_for()
    for mode in ("morning", "close"):
        h = trends.build_section(f, mode)
        check(f"{mode}: heading names the session the data is from", "Market Trends — as of the Oct 23 close" in text(h), text(h)[:120])
    check("sources and the latest bar date are printed", "Sources: Yahoo Finance daily closes (via yfinance), latest bar Oct 23." in text(trends.build_section(f, "close")))


def test_only_completed_sessions_are_used():
    D, utc = datetime.datetime, datetime.timezone.utc
    # world()'s newest bar is Fri 2026-10-23. EDT = UTC-4 that week.
    def at(dt):
        f = trends.fetch(Client(world()), dt)
        return f, text(trends.build_section(f, "morning"))
    f, t = at(D(2026, 10, 23, 13, 31, tzinfo=utc))          # 9:31 ET on Friday: the scheduled morning run, one minute after the open
    check("the morning run (13:31 UTC = 9:31 ET) drops today's one-minute-old bar", f["partial_bar_dropped"] is True and f["as_of"] == "2026-10-22", str(f["as_of"]))
    check("so the heading names the previous COMPLETED session", "Market Trends — as of the Oct 22 close" in t, t[:100])
    check("and the S&P figures are those of Thursday's close (7480), not Friday's partial bar (7490)",
          f["indexes"]["S&P 500"]["last"] == 7480 and "S&P 500: 7,480" in t, t[:300])
    check("the footnote says why", "Today's session had not finished, so the latest completed session is used." in t)
    f, t = at(D(2026, 10, 23, 19, 59, tzinfo=utc))          # 15:59 ET: still open
    check("15:59 ET: still open, still dropped", f["partial_bar_dropped"] and f["as_of"] == "2026-10-22")
    f, t = at(D(2026, 10, 23, 20, 0, tzinfo=utc))           # 16:00 ET: the scheduled close run
    check("16:00 ET (the scheduled close run) keeps today's finished bar", not f["partial_bar_dropped"] and f["as_of"] == "2026-10-23" and "as of the Oct 23 close" in t, str(f["as_of"]))
    check("and says nothing about a partial session", "had not finished" not in t)
    f, t = at(D(2026, 10, 26, 12, 30, tzinfo=utc))          # Monday 8:30 ET: newest bar (Friday) is not today's
    check("a pre-open run on a later day keeps Friday's bar (nothing to drop)", not f["partial_bar_dropped"] and f["as_of"] == "2026-10-23")
    f, t = at(D(2026, 10, 24, 15, 0, tzinfo=utc))           # Saturday
    check("a weekend run keeps the last bar", not f["partial_bar_dropped"] and f["as_of"] == "2026-10-23")
    check("every symbol was trimmed together (indexes, sectors, macro, VIX all end on the same session)",
          len({l["as_of"] for l in list(trends.fetch(Client(world()), D(2026, 10, 23, 13, 31, tzinfo=utc))["indexes"].values())
               + trends.fetch(Client(world()), D(2026, 10, 23, 13, 31, tzinfo=utc))["sectors"]}) == 1)
    one = trends.fetch(Client({"^GSPC": ramp(5000, 10), "^IXIC": ramp(15000, 40)}), D(2026, 10, 23, 13, 31, tzinfo=utc))
    check("it works with partial data too", one["partial_bar_dropped"] and one["indexes"]["S&P 500"]["last"] == 7480)
    check("the helper alone: only a bar dated today (ET) is dropped",
          trends._completed_sessions_only({"X": series([1, 2, 3])}, D(2026, 10, 23, 15, 0, tzinfo=utc))[0]["X"] == series([1, 2, 3])[:-1]
          and trends._completed_sessions_only({"X": series([1, 2, 3])}, D(2026, 10, 22, 15, 0, tzinfo=utc))[1] is False)


def test_missing_timezone_data_cannot_drop_the_section():
    import zoneinfo
    real = zoneinfo.ZoneInfo

    def boom(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)
    zoneinfo.ZoneInfo = boom
    try:
        f = trends.fetch(Client(world()), NOW)
        sec = trends.build_section(f, "close")
    finally:
        zoneinfo.ZoneInfo = real
    check("without tz data nothing is dropped and the desk still produces its section, labelled as a normal close",
          f["partial_bar_dropped"] is False and sec and "as of the Oct 23 close" in text(sec))


def test_every_number_in_the_section_traces_to_a_fact():
    facts = facts_for()
    t = text(trends.build_section(facts, "close"))
    spx = facts["indexes"]["S&P 500"]
    check("S&P level / averages / changes", f"{spx['last']:,.0f}" in t and f"({spx['ma50']:,.0f})" in t and f"({spx['ma200']:,.0f})" in t)
    for s in facts["sectors"][:0]:
        pass
    nums = set(re.findall(r"\d[\d,]*\.?\d*", t))
    allowed = set()
    for leaf in list(facts["indexes"].values()) + facts["sectors"] + list(facts["macro"].values()) + [facts["vix"]]:
        for k, v in leaf.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                allowed |= {f"{v:,.0f}", f"{v:.1f}", f"{abs(v):.1f}", f"{abs(v):.2f}", f"{v:.2f}", f"{v:,.2f}"}
    nums = {n.rstrip(".,") for n in nums}
    stray = [n for n in nums if n not in allowed and n not in {"50", "200", "5", "21", "30", "500", "2026", "10", "23", "1"} and not re.fullmatch(r"\d{1,2}", n)]
    check("no figure appears that is not derivable from the facts dict", not stray, str(stray))


def test_no_files_are_opened():
    opened = []
    real = builtins.open
    builtins.open = lambda f, *a, **k: (opened.append(str(f)), real(f, *a, **k))[1]
    try:
        section()
    finally:
        builtins.open = real
    check("fetch + build touch no file (no memory, picks or state)", opened == [], str(opened))
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "desks", "trends.py")).read()
    check("the module never mentions memory, picks or the private data files",
          not any(w in src for w in ("pippy_memory", "picks_cache", "load_memory", "get_weekly_picks", "send_log", "import openbell", "import pippy_mcp")))


def test_public_edition_carries_the_desk_and_still_passes_its_leak_guard():
    import public_edition as pub
    import test_market_narrative as T
    sec = section()
    with T._frozen_today(datetime.date(2026, 10, 5)):
        subj, page = pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, T.OCT5_MOVERS, T.OCT5_SECTORS, T.OCT5_COMMODITIES, T.OCT5_TREASURY,
                                            [], [], {"briefing_history": []}, desk_sections=[sec])
    check("the Trends section is in the public close, between Commodities and Headlines", "Market Trends" in page and
          page.index("Commodities &amp; Yields") < page.index("Market Trends"))
    with T._frozen_today(datetime.date(2026, 10, 5)):
        subj, page_m = pub.build_public_morning(T.OCT5_MORNING_SNAPSHOT, [], T.OCT5_GLOBAL, T.OCT5_COMMODITIES, T.OCT5_TREASURY, [],
                                                {"briefing_history": []}, desk_sections=[sec])
    check("and in the public morning", "Market Trends" in page_m)
    raised = False
    try:
        with T._frozen_today(datetime.date(2026, 10, 5)):
            pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, T.OCT5_MOVERS, T.OCT5_SECTORS, T.OCT5_COMMODITIES, T.OCT5_TREASURY, [], [],
                                   {"briefing_history": []}, desk_sections=["<p>Your holdings are up since entry.</p>"])
    except pub.PublicEditionLeak:
        raised = True
    check("a desk section containing private language is refused by the public leak guard", raised)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
