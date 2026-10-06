"""
test_headline_feed.py — fetch_top_headlines fallback behavior. No network, no live clock.

Run: python3 test_headline_feed.py

Background: Yahoo's per-ticker news endpoint (behind yf.Ticker(...).news) intermittently
404s; yfinance swallows that and returns []. That silently emptied the Top Headlines
section on several production runs. fetch_top_headlines now falls back to yf.Search and
logs loudly (stderr) when it does, or when no source has anything.
"""
import sys, os, io, json, contextlib, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pippy_mcp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


NOW = datetime.datetime.now(datetime.timezone.utc)   # only used to build "fresh"/"stale" fixtures relative to the code under test's own clock


class _FakeTicker:
    def __init__(self, sym, news_by_sym):
        self.news = news_by_sym.get(sym, [])


class _FakeSearch:
    def __init__(self, q, news_count=8, results=None, boom=False):
        if boom:
            raise RuntimeError("search down")
        self.news = results or []


def _run(ticker_news, search_items, search_boom=False):
    orig = (pippy_mcp.yf, pippy_mcp._fmp, pippy_mcp.NEWS_API_KEY)

    class _YF:
        Ticker = staticmethod(lambda sym: _FakeTicker(sym, ticker_news))
        Search = staticmethod(lambda q, news_count=8: _FakeSearch(q, news_count, search_items, search_boom))

    def _dead_fmp(*a, **k):
        raise RuntimeError("FMP dead")
    pippy_mcp.yf, pippy_mcp._fmp, pippy_mcp.NEWS_API_KEY = _YF, _dead_fmp, ""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            out = json.loads(pippy_mcp.fetch_top_headlines())
    finally:
        pippy_mcp.yf, pippy_mcp._fmp, pippy_mcp.NEWS_API_KEY = orig
    return out, err.getvalue()


def _search_item(title, hours_ago):
    return {"title": title, "publisher": "Reuters",
            "providerPublishTime": int((NOW - datetime.timedelta(hours=hours_ago)).timestamp())}


def test_ticker_news_empty_falls_back_to_search_and_logs_it():
    items = [_search_item("Treasury Yields Slip As Inflation Data Cools", 2),
             _search_item("Oil Prices Climb On Supply Worries In Gulf", 5)]
    out, err = _run({}, items)
    titles = [h["title"] for h in out["headlines"]]
    check("search items become headlines", "Treasury Yields Slip As Inflation Data Cools" in titles, f"got {titles}")
    check("source is still labelled yfinance", out["source"] == "yfinance")
    check("publisher is carried into 'site'", all(h["site"] == "Reuters" for h in out["headlines"]))
    check("the fallback is logged to stderr, not silent", "fell back to Search" in err, f"stderr: {err!r}")


def test_ticker_news_present_does_not_call_search():
    called = []
    orig = pippy_mcp._search_news_items
    pippy_mcp._search_news_items = lambda *a, **k: called.append(1) or []
    try:
        iso = (NOW - datetime.timedelta(hours=3)).isoformat()
        news = {"SPY": [{"content": {"title": "Stocks Edge Higher Ahead Of Jobs Report", "pubDate": iso,
                                    "provider": {"displayName": "AP"}, "summary": ""}}]}
        out, err = _run(news, [])
    finally:
        pippy_mcp._search_news_items = orig
    check("Ticker.news items are used as before", [h["title"] for h in out["headlines"]] == ["Stocks Edge Higher Ahead Of Jobs Report"])
    check("search is not consulted when the primary source has items", not called)
    check("no fallback message when nothing failed", "fell back" not in err)


def test_stale_items_from_the_fallback_are_still_filtered():
    out, _ = _run({}, [_search_item("Market Wrap From Last Week Still Circulating Online", 200),
                       _search_item("Fresh Headline About Rates And Inflation Today", 1)])
    titles = [h["title"] for h in out["headlines"]]
    check("a 200-hour-old item is dropped by the 72h gate", titles == ["Fresh Headline About Rates And Inflation Today"], f"got {titles}")


def test_nothing_anywhere_returns_empty_list_and_a_loud_warning():
    out, err = _run({}, [])
    check("empty headlines list, not an error payload", out["headlines"] == [] and out["source"] == "yfinance", f"got {out}")
    check("a WARNING line is written to stderr", "WARNING: no headlines from any source" in err, f"stderr: {err!r}")


def test_search_endpoint_raising_is_survivable():
    out, err = _run({}, [], search_boom=True)
    check("a failing Search degrades to an empty list rather than raising", out["headlines"] == [], f"got {out}")
    check("and still warns", "WARNING" in err)


if __name__ == "__main__":
    for t in (test_ticker_news_empty_falls_back_to_search_and_logs_it, test_ticker_news_present_does_not_call_search,
              test_stale_items_from_the_fallback_are_still_filtered, test_nothing_anywhere_returns_empty_list_and_a_loud_warning,
              test_search_endpoint_raising_is_survivable):
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
