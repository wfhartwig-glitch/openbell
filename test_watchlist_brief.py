"""
test_watchlist_brief.py — facts-only watchlist section. No network, no live clock, no files.

Run: python3 test_watchlist_brief.py
"""
import builtins
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openbell  # noqa: E402
import watchlist_brief as wb  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 23, 21, 0, tzinfo=UTC)          # frozen "run time" (after Friday's close)
FRI = datetime.date(2026, 10, 23)
THU = datetime.date(2026, 10, 22)


def bars(prev, last):
    return [(datetime.date(2026, 10, 21), prev), (THU, prev), (FRI, last)]   # pct = last/prev - 1


def hours_ago(h):
    return NOW - datetime.timedelta(hours=h)


# Fixture: three fictional tickers with three different kinds of move, one of them with news.
#   ALFA  +3.10%, tech: ahead of the market AND its sector      -> "something specific"
#   BRVO  -1.80%, energy: behind the market, in line w/ sector   -> "shared by its sector"
#   CHLH  +0.70%, health care: in line with market and sector    -> "moved with the market"
S_AND_P = 0.80
SECTORS = {"Technology": 1.00, "Energy": -1.90, "Health Care": 0.50}
FIXTURE = {
    "history": {
        "ALFA": bars(100.0, 103.10), "BRVO": bars(50.0, 49.10), "CHLH": bars(200.0, 201.40),
        "^GSPC": bars(5000.0, 5040.0),
        "XLK": bars(100.0, 101.0), "XLE": bars(100.0, 98.1), "XLV": bars(100.0, 100.5),
    },
    "info": {
        "ALFA": {"name": "Alfa Systems, Inc.", "sector": "Technology"},
        "BRVO": {"name": "Bravo Energy Corporation", "sector": "Energy"},
        "CHLH": {"name": "Charlie Health Group", "sector": "Healthcare"},
    },
    "news": {
        "ALFA": [{"title": "Alfa Systems Reports Record Quarterly Revenue, Raises Outlook", "snippet": "", "site": "Wire",
                  "published": hours_ago(3)},
                 {"title": "Alfa Systems Stock Fell Last Week On Analyst Downgrade", "snippet": "", "site": "Wire",
                  "published": hours_ago(80)}],          # too old to be "today's"
        "BRVO": [{"title": "Oil Slides As Inventories Build", "snippet": "", "site": "Wire", "published": hours_ago(2)}],
        "CHLH": [],
    },
}


class FakeClient:
    """Counts every call so the 'fetch once' property can be asserted."""

    def __init__(self, fx=FIXTURE, news_fails=False):
        self.fx, self.news_fails = fx, news_fails
        self.history_calls, self.info_calls, self.news_calls = [], [], []

    def history(self, symbols):
        self.history_calls.append(list(symbols))
        return {s: self.fx["history"].get(s, []) for s in symbols}

    def info(self, symbol):
        self.info_calls.append(symbol)
        return self.fx["info"].get(symbol)

    def news(self, symbol):
        self.news_calls.append(symbol)
        if self.news_fails:
            raise RuntimeError("news down")
        return self.fx["news"].get(symbol, [])


def _data(tickers=("ALFA", "BRVO", "CHLH"), **kw):
    wl = {"s": wb.validate_watchlist(list(tickers))}
    return wb.fetch_market_data(wb.plan_fetch(wl), FakeClient(**kw), NOW)


def _text(html):
    return wb._visible(html)


# ── validation ────────────────────────────────────────────────────────────────

def test_validation_accepts_symbols_and_normalises():
    wl = wb.validate_watchlist([" aapl ", "MSFT", "brk.b", "aapl", "BF-B"])
    check("normalised, de-duplicated, order kept", wl.tickers == ["AAPL", "MSFT", "BRK-B", "BF-B"], str(wl.tickers))
    check("nothing rejected", wl.rejected == [])


def test_validation_rejects_anything_that_is_not_a_plain_ticker():
    bad = ["AAPL 100", "AAPL:50", "$150", "100", "TSLA@200", "", "  ", "TOOLONGX", "^GSPC", "BTC-USD", "AA PL", "😀"]
    wl = wb.validate_watchlist(bad + ["MSFT"])
    check("only the real ticker survives", wl.tickers == ["MSFT"], str(wl.tickers))
    check("every bad entry is rejected with a reason", len(wl.rejected) == len(bad) and all(r.reason for r in wl.rejected), str(wl.rejected))
    wl = wb.validate_watchlist([{"ticker": "AAPL", "shares": 10}, ("AAPL", 10), 5, None, 1.5])
    check("non-string entries (a position as a dict/tuple/number) are rejected, not parsed", wl.tickers == [] and len(wl.rejected) == 5, str(wl))
    for notlist in ("AAPL,MSFT", {"AAPL": 10}, None, 7):
        try:
            wb.validate_watchlist(notlist)
            ok = False
        except wb.WatchlistError:
            ok = True
        check(f"{notlist!r} is not a list: clean error", ok)


def test_cap_of_15():
    letters = [f"{a}{b}" for a in "ABCDEFGH" for b in "XYZ"][:16]
    try:
        wb.validate_watchlist(letters)
        ok = False
    except wb.TooManyTickers as e:
        ok = "15" in str(e)
    check("16 distinct tickers raise TooManyTickers (never silently truncated)", ok)
    check("exactly 15 is accepted", len(wb.validate_watchlist(letters[:15]).tickers) == 15)
    check("duplicates don't count toward the cap", len(wb.validate_watchlist(letters[:15] * 3).tickers) == 15)


def test_function_signatures_take_tickers_only():
    import inspect
    banned = re.compile(r"share|quantity|amount|dollar|cost|basis|position|price_paid|value", re.I)
    names = []
    for f in (wb.validate_watchlist, wb.plan_fetch, wb.fetch_market_data, wb.build_watchlist_section,
              wb.build_watchlist_page, wb.build_for_subscribers, wb.render_for_subscribers):
        names += list(inspect.signature(f).parameters)
    check("no parameter anywhere asks for shares, amounts or cost basis", not [n for n in names if banned.search(n)], str(names))
    fields = [f for c in (wb.Watchlist, wb.TickerData, wb.MarketData) for f in c.__dataclass_fields__]
    check("no dataclass field holds a position", not [n for n in fields if re.search(r"share|quantity|cost|basis|position", n, re.I)], str(fields))


# ── fetch plan and sharing ────────────────────────────────────────────────────

def test_each_unique_ticker_is_fetched_once_across_subscribers():
    subs = {"u1": ["ALFA", "BRVO"], "u2": ["BRVO", "CHLH"], "u3": ["ALFA", "BRVO", "CHLH"], "u4": ["ALFA"] * 3}
    client = FakeClient()
    results, plan = wb.build_for_subscribers(subs, client, "close", NOW)
    check("one batched history request for everyone", len(client.history_calls) == 1, str(len(client.history_calls)))
    check("that request carries the union + S&P + 11 sector ETFs",
          set(client.history_calls[0]) == {"ALFA", "BRVO", "CHLH", "^GSPC"} | set(wb.SECTOR_ETFS.values()))
    check("profile fetched once per unique ticker", sorted(client.info_calls) == ["ALFA", "BRVO", "CHLH"], str(client.info_calls))
    check("news fetched once per unique ticker", sorted(client.news_calls) == ["ALFA", "BRVO", "CHLH"], str(client.news_calls))
    check("every subscriber got a section", all(r.html for r in results.values()))
    check("plan: 7 requests, versus more if each subscriber fetched their own",
          plan.total_calls == 7 and plan.naive_calls > plan.total_calls, f"{plan.total_calls} vs {plan.naive_calls}")
    print("   " + plan.describe().replace("\n", "\n   "))


def test_cost_does_not_scale_with_subscribers():
    one = wb.plan_fetch({"s0": wb.validate_watchlist(["ALFA", "BRVO"])})
    many = wb.plan_fetch({f"s{i}": wb.validate_watchlist(["ALFA", "BRVO"]) for i in range(500)})
    check("500 subscribers with the same list cost the same as one", one.total_calls == many.total_calls == 5)
    check("an empty run costs nothing", wb.plan_fetch({}).total_calls == 0)


def test_unknown_tickers_cost_no_profile_or_news_calls_and_are_reported():
    client = FakeClient()
    wl = wb.validate_watchlist(["ALFA", "ZZZZ"])
    data = wb.fetch_market_data(wb.plan_fetch({"s": wl}), client, NOW)
    check("no profile/news request for a ticker with no price history", client.info_calls == ["ALFA"] and client.news_calls == ["ALFA"])
    html = wb.build_watchlist_section(wl, data, "close")
    check("the unknown ticker is reported plainly", "We couldn't find market data for ZZZZ" in _text(html), _text(html))
    check("the known ticker is still rendered", "Alfa Systems" in _text(html))


def test_sector_etf_list_matches_the_pipeline():
    import pippy_mcp
    check("SECTOR_ETFS mirrors pippy_mcp.SECTOR_ETFS", wb.SECTOR_ETFS == pippy_mcp.SECTOR_ETFS)


# ── the fixture: three moves, one with news ───────────────────────────────────

def test_fixture_numbers():
    d = _data()
    check("S&P +0.80%", abs(d.sp_pct - 0.80) < 1e-9)
    check("ALFA +3.10%, BRVO -1.80%, CHLH +0.70%",
          [round(d.tickers[t].pct, 2) for t in ("ALFA", "BRVO", "CHLH")] == [3.10, -1.80, 0.70])
    check("sectors mapped from Yahoo's names (Healthcare -> Health Care)", d.tickers["CHLH"].sector == "Health Care")
    check("the 80-hour-old headline was filtered out as not 'today's'", len(d.tickers["ALFA"].news) == 1)


def test_close_edition_text():
    d = _data()
    wl = wb.validate_watchlist(["ALFA", "BRVO", "CHLH"])
    t = _text(wb.build_watchlist_section(wl, d, "close"))
    check("heading", "Your Watchlist — Today" in t)
    check("ALFA: price, move, versus market and sector",
          "Alfa Systems closed at $103.10, up 3.10% today." in t
          and "ahead of the S&P 500 (up 0.80%) and ahead of its sector, Technology (up 1.00%)" in t, t)
    check("ALFA: hedged 'specific to the stock', no cause claimed",
          "something specific to the stock may have mattered, though the data can't say what" in t)
    check("BRVO: behind the market, in line with its sector -> sector reading",
          "Bravo Energy closed at $49.10, down 1.80% today." in t and "behind the S&P 500 (up 0.80%)" in t
          and "in line with its sector, Energy (down 1.90%)" in t and "shared by its sector rather than the whole market" in t, t)
    check("CHLH: in line with both -> moved with the market",
          "in line with the S&P 500 (up 0.80%) and in line with its sector, Health Care (up 0.50%)" in t
          and "mostly moved with the market" in t, t)


def test_morning_edition_reports_the_last_session_with_its_date():
    d = _data()
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "morning"))
    check("morning heading", "Your Watchlist — Last Session" in t)
    check("states the session date from the data, not the clock", "last closed at $103.10, up 3.10% in the last session (Fri Oct 23)" in t, t)


def test_news_is_topic_only_and_never_quoted():
    d = _data()
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA", "BRVO", "CHLH"]), d, "close"))
    check("ALFA: topic summary", "A same-day headline covers its latest earnings or results" in t, t)
    check("hedged: the data can't show it drove the move", "can't show whether that had anything to do with the move" in t)
    check("BRVO's market-wide oil headline does not name BRVO, so no news is claimed",
          t.count("No same-day headline we found mentions it.") == 2, t)         # BRVO and CHLH
    raw = ["Alfa Systems Reports Record Quarterly Revenue, Raises Outlook", "Oil Slides As Inventories Build"]
    check("no headline title or 3-word fragment of one appears",
          not any(" ".join(x.split()[i:i + 3]).lower() in t.lower() for x in raw for i in range(len(x.split()) - 2)), t)
    check("no quotation marks around anything", '"' not in t and "“" not in t)


def test_news_failure_is_not_reported_as_no_news():
    d = _data(news_fails=True)
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "close"))
    check("says it couldn't pull headlines instead of claiming there were none",
          "couldn't pull headlines today" in t and "No same-day headline" not in t, t)



def _fake_yfinance(search=None, ticker_news=None):
    """A stand-in `yfinance` module for YFinanceClient.news. A callable raises when called."""
    import types
    m = types.ModuleType("yfinance")

    class Search:
        def __init__(self, q, news_count=8):
            r = search() if callable(search) else search
            self.news = r

    class Ticker:
        def __init__(self, sym):
            r = ticker_news() if callable(ticker_news) else ticker_news
            self.news = r
    m.Search, m.Ticker = Search, Ticker
    return m


def _with_fake_yf(fake, fn):
    real = sys.modules.get("yfinance")
    sys.modules["yfinance"] = fake
    try:
        return fn()
    finally:
        if real is None:
            del sys.modules["yfinance"]
        else:
            sys.modules["yfinance"] = real


def test_news_lookup_uses_search_first_and_ticker_news_as_backup():
    ep = int(hours_ago(2).timestamp())
    items = [{"title": "Alfa Systems Reports Record Quarterly Revenue", "publisher": "Wire", "providerPublishTime": ep}]
    boom = lambda: (_ for _ in ()).throw(RuntimeError("search 500"))
    c = wb.YFinanceClient()
    got = _with_fake_yf(_fake_yfinance(search=items, ticker_news=boom), lambda: c.news("ALFA"))
    check("Search works: used, and Ticker.news is never touched", len(got) == 1 and got[0]["published"].tzinfo is not None, str(got))
    check("Search works but is empty: returns [] (a real 'no news', not a failure)",
          _with_fake_yf(_fake_yfinance(search=[], ticker_news=boom), lambda: c.news("ALFA")) == [])
    iso = hours_ago(1).isoformat().replace("+00:00", "Z")
    backup = [{"content": {"title": "Alfa Systems Raises Outlook", "pubDate": iso, "provider": {"displayName": "AP"}, "summary": "s"}}]
    got = _with_fake_yf(_fake_yfinance(search=boom, ticker_news=backup), lambda: c.news("ALFA"))
    check("Search fails: falls back to Ticker.news and parses its format", got and got[0]["title"] == "Alfa Systems Raises Outlook"
          and got[0]["site"] == "AP" and got[0]["published"] == hours_ago(1), str(got))
    try:
        _with_fake_yf(_fake_yfinance(search=boom, ticker_news=[]), lambda: c.news("ALFA"))
        ok = False
    except RuntimeError:
        ok = True
    check("Search fails and Ticker.news is empty (its swallowed 404): a failure, not 'no news'", ok)
    try:
        _with_fake_yf(_fake_yfinance(search=boom, ticker_news=boom), lambda: c.news("ALFA"))
        ok = False
    except RuntimeError:
        ok = True
    check("both sources raise: a failure", ok)


def test_news_failing_for_every_ticker_keeps_the_facts_and_says_so():
    subs = {"a": ["ALFA", "BRVO", "CHLH"]}
    client = FakeClient(news_fails=True)
    results, plan = wb.build_for_subscribers(subs, client, "close", NOW)
    t = _text(results["a"].html)
    check("every ticker still shows price, move and comparisons",
          "Alfa Systems closed at $103.10, up 3.10% today." in t and "Bravo Energy closed at $49.10" in t and "Charlie Health closed at $201.40" in t, t)
    check("each one says headlines couldn't be pulled", t.count("We couldn't pull headlines today, so we can't say whether it is in the news.") == 3, t)
    check("none claims 'no same-day headline' or a topic", "No same-day headline" not in t and "headline covers" not in t)
    check("the run did not abort: every ticker was attempted once", sorted(client.news_calls) == ["ALFA", "BRVO", "CHLH"])


# ── summarizer false positives (reusing the shared guards, plus the name check) ──────

def _td(symbol, name, titles, sector="Technology"):
    return wb.TickerData(symbol, True, name, sector, 10.0, 1.0, FRI,
                         [{"title": x, "snippet": "", "site": "w", "published": hours_ago(1)} for x in titles])


def test_false_positives_fall_back_rather_than_guess():
    cases = [
        ("AAPL", "Apple Inc.", "Pineapple Prices Surge After Hurricane", "company name inside another word"),
        ("V", "Visa Inc.", "Advisable Moves For Travel Seekers", "short company name inside another word"),
        ("V", "Visa Inc.", "Visa-Free Travel Expands Across Europe", "company name as a hyphenated compound"),
        ("V", "Visa Inc.", "A V-Shaped Recovery Lifts Markets", "one-letter ticker as English"),
        ("NOW", "ServiceNow, Inc.", "WALL STREET RALLIES NOW AS YIELDS FALL", "ticker that is a common word"),
        ("IT", "Gartner, Inc.", "WHY IT STOCKS ARE SELLING OFF TODAY", "two-letter ticker as English"),
        ("MELI", "MercadoLibre, Inc.", "Walmart Earnings Beat Estimates As Shoppers Trade Down, MELI Also Higher",
         "another company's earnings"),
    ]
    for sym, name, title, why in cases:
        s = wb.headline_sentence(_td(sym, name, [title]))
        check(f"{why}: '{title[:36]}...' makes no topic claim", "headline covers" not in s, s)
    s = wb.headline_sentence(_td("AAPL", "Apple Inc.", ["Pineapple Prices Surge After Hurricane"]))
    check("a bogus name match is treated as no mention at all", s == "No same-day headline we found mentions it.", s)
    s = wb.headline_sentence(_td("MELI", "MercadoLibre, Inc.", ["MercadoLibre Spotted At Industry Conference In Sao Paulo"]))
    check("a real mention with no classifiable topic uses the honest fallback",
          s == "A same-day headline mentions it, though we can't tell what it covers.", s)
    s = wb.headline_sentence(_td("NOW", "ServiceNow, Inc.", ["ServiceNow Announces Acquisition Of AI Startup"]))
    check("control: a real company-name match is classified", "an acquisition or merger" in s, s)
    s = wb.headline_sentence(_td("AAPL", "Apple Inc.", ["Apple Reports Record Quarterly Revenue"]))
    check("control: whole-word company name matches", "its latest earnings or results" in s, s)


def test_analyst_topics_use_neutral_wording():
    for title in ("Alfa Systems Upgraded By Analysts After Strong Results", "Alfa Systems Downgraded By Two Brokerages",
                  "Alfa Systems Price Target Raised To New High"):
        s = wb.headline_sentence(_td("ALFA", "Alfa Systems, Inc.", [title]))
        check(f"'{title[:34]}...' -> neutral wording", "analyst commentary" in s and not wb.find_advice_language(s), s)


# ── advice-language guard ─────────────────────────────────────────────────────

def test_guard_detects_each_forbidden_word():
    for w in ("buy", "Sell", "hold", "holding", "thesis", "target", "price target", "upside", "score", "rating", "rated",
              "since entry", "recommend", "upgrade", "downgraded", "outperform", "you should"):
        check(f"guard flags '{w}'", wb.find_advice_language(f"The stock is a {w} here."), w)
    check("guard is quiet on a normal sentence", not wb.find_advice_language("Alfa closed at $10, up 1.00% today, ahead of the S&P 500."))


def test_real_company_names_with_advice_words_do_not_trip_the_guard():
    wl = wb.validate_watchlist(["BBY", "TGT"])
    fx = {"history": {"BBY": bars(100, 101), "TGT": bars(100, 99), "^GSPC": bars(100, 100.5)},
          "info": {"BBY": {"name": "Best Buy Co., Inc.", "sector": "Consumer Cyclical"},
                   "TGT": {"name": "Target Corporation", "sector": "Consumer Defensive"}},
          "news": {"BBY": [{"title": "Best Buy Reports Strong Quarterly Results", "snippet": "", "site": "w", "published": hours_ago(2)}],
                   "TGT": []}}
    data = wb.fetch_market_data(wb.plan_fetch({"s": wl}), FakeClient(fx), NOW)
    html = wb.build_watchlist_section(wl, data, "close")
    check("'Best Buy' and 'Target Corporation' render without raising", "Best Buy" in html and "Target" in html)
    check("and Best Buy's earnings headline is still summarised by topic", "its latest earnings or results" in _text(html))


def test_generation_fails_if_advice_language_is_introduced():
    real = wb.relative_sentences
    wb.relative_sentences = lambda td, data: "Analysts rate it a buy with 20% upside."
    try:
        wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), _data(), "close")
        raised = False
    except wb.AdviceLanguageError as e:
        raised = "buy" in str(e) and "upside" in str(e)
    finally:
        wb.relative_sentences = real
    check("generation raises rather than emitting the section", raised)


def test_no_advice_or_cause_overreach_in_any_generated_text():
    d = _data()
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA", "BRVO", "CHLH"]), d, "close"))
    check("fixture output is clean under the guard (names masked)", not wb.find_advice_language(t, ["ALFA", "BRVO", "CHLH"]), t)
    check("no unhedged causal words", not re.search(r"\b(because|due to|caused|driven by|thanks to|as a result)\b", t, re.I), t)
    check("hedged readings present", t.count("though the data can't") >= 3)


def test_footer_line_and_structure():
    html = wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), _data(), "close")
    check("the exact footer line is in the section", wb.FOOTER_LINE in html and
          wb.FOOTER_LINE == "A watchlist summary of public market data for the tickers you chose. Not investment advice.")
    check("short: well under 200 words for one ticker", len(_text(html).split()) < 200, str(len(_text(html).split())))
    check("rejected entries are listed cleanly, not silently dropped",
          "Not included: 100 (not a valid ticker symbol)" in _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA", "100"]), _data(), "close")))
    page = wb.build_watchlist_page(wb.validate_watchlist(["ALFA"]), _data(), "close", "Fri Oct 23")
    check("the preview page reuses the responsive/dark-mode shell and has no private footer",
          'name="viewport"' in page and "automated daily market briefing" not in page)


def test_html_escapes_third_party_text():
    d = _data()
    d.tickers["ALFA"].name = "Alfa <b>&</b> Co"
    html = wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "close")
    check("a hostile company name is escaped", "<b>&</b>" not in html and "&lt;b&gt;" in html)


def test_degraded_inputs():
    d = _data()
    d.sp_pct = None
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "close"))
    check("no S&P data: still compares with the sector", "ahead of its sector, Technology" in t and "S&P 500 (" not in t, t)
    d.sector_pct = {}
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "close"))
    check("no S&P and no sector: says so, makes no comparison", "couldn't pull the S&P 500 or sector moves" in t, t)
    d = _data()
    d.tickers["ALFA"].sector = None
    t = _text(wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "close"))
    check("an ETF/no-sector ticker: market comparison plus an honest note", "We have no sector comparison for it." in t, t)
    try:
        wb.fetch_market_data(wb.plan_fetch({"s": wb.validate_watchlist(["ALFA"])}),
                             type("C", (), {"history": lambda self, s: (_ for _ in ()).throw(RuntimeError("down"))})(), NOW)
        ok = False
    except wb.DataUnavailable:
        ok = True
    check("price history down: a clean DataUnavailable, not a crash or fabricated numbers", ok)
    try:
        wb.build_watchlist_section(wb.validate_watchlist(["ALFA"]), d, "weekly")
        ok = False
    except ValueError:
        ok = True
    check("unknown edition rejected", ok)


def test_bad_subscriber_does_not_block_the_others():
    subs = {"good": ["ALFA", "BRVO"], "toomany": [f"{a}{b}" for a in "ABCDEFGH" for b in "XYZ"], "junk": "AAPL,MSFT",
            "mixed": ["CHLH", "AAPL 100"]}
    results, plan = wb.build_for_subscribers(subs, FakeClient(), "close", NOW)
    check("valid subscribers are rendered", results["good"].html and results["mixed"].html)
    check("over-cap and malformed lists get an error, not a crash", "at most 15" in results["toomany"].error and results["junk"].error)
    check("the bad lists add nothing to the fetch plan", plan.tickers == ["ALFA", "BRVO", "CHLH"], str(plan.tickers))
    check("a partly-bad list still works and reports what it dropped", results["mixed"].rejected and "Not included" in _text(results["mixed"].html))


# ── privacy ───────────────────────────────────────────────────────────────────

def test_never_reads_private_data():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_brief.py")).read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for bad in ("pippy_memory", "picks_cache", "send_log", "case_study", "load_memory", "get_weekly_picks",
                "generate_weekly_picks", "pick_performance", "flagged_tickers", "briefing_history",
                "open(", "MEMORY_FILE", "PICKS_FILE"):
        check(f"module code never mentions {bad}", bad not in code.replace('"""', ""), bad)
    import ast
    imported = {n.names[0].name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)} | \
               {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    check("it imports nothing that can reach the private data (no pippy_mcp, no mcp)", not {m for m in imported if m and ("pippy" in m or m.startswith("mcp"))}, str(imported))
    opened = []
    real_open = builtins.open
    builtins.open = lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
    try:
        wb.build_for_subscribers({"a": ["ALFA", "BRVO", "CHLH"], "b": ["BRVO"]}, FakeClient(), "close", NOW)
    finally:
        builtins.open = real_open
    check("a full multi-subscriber build opens no files at all", opened == [], str(opened))



def test_runtime_audit_import_and_full_build_touch_no_private_file():
    """Runs in a clean interpreter with an audit hook: every file opened, process spawned and network
    event during `import watchlist_brief` plus a full multi-subscriber build."""
    import json
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    code = r"""
import sys, os, json
ev = {"open": [], "spawn": [], "net": []}
def hook(e, a):
    if e == "open" and isinstance(a[0], (str, bytes)) and not str(a[0]).endswith((".pyc", ".so", ".pyd")):
        ev["open"].append(os.path.abspath(str(a[0])))
    elif e in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.system"): ev["spawn"].append(e)
    elif e in ("socket.connect", "socket.getaddrinfo"): ev["net"].append(e)
sys.addaudithook(hook); sys.path.insert(0, os.getcwd())
import watchlist_brief as wb, test_watchlist_brief as T
wb.build_for_subscribers({"a": ["ALFA", "BRVO", "CHLH"], "b": ["BRVO"]}, T.FakeClient(), "close", T.NOW)
repo = os.getcwd()
print(json.dumps({"opened": sorted({os.path.relpath(p, repo) for p in ev["open"] if p.startswith(repo) and "__pycache__" not in p}),
                  "pippy_mcp": "pippy_mcp" in sys.modules, "spawn": ev["spawn"], "net": ev["net"]}))
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True, text=True)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    allowed = {".env", "openbell.py", "watchlist_brief.py", "test_watchlist_brief.py"}
    check("the only repo files opened are source files plus .env (read by openbell's load_dotenv)",
          set(r["opened"]) <= allowed, str(r["opened"]))
    check("no memory / picks / send-log / case-study / scan-cache file is opened",
          not [p for p in r["opened"] if p.endswith(".json")], str(r["opened"]))
    check("pippy_mcp (the server that owns the memory and picks tools) is never imported", r["pippy_mcp"] is False)
    check("no subprocess is started and no network call is made by import + build with a fake client",
          r["spawn"] == [] and r["net"] == [], str(r))

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items(), key=lambda kv: list(globals()).index(kv[0])) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
