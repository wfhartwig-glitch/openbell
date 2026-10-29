"""
test_scan_filter.py — the daily scan must not surface bad data rows as "top candidates".

Oct 28: PARA scored 7044 on "analyst target implies 7043% upside". PARA is a recycled ticker
(Paramount Global now trades as PSKY; Yahoo's PARA is an unrelated ~$2M microcap with a bogus
$240 target). No network, no live clock, no private files (MEMORY_FILE and the scan cache are
pointed at a temp dir).

Run: python3 test_scan_filter.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openbell  # noqa: E402
import pippy_mcp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


GOOD = {"currentPrice": 100.0, "targetMeanPrice": 150.0, "marketCap": 5e9, "recommendationKey": "buy", "sector": "Technology",
        "shortName": "Good Co", "beta": 1.2}


def test_outlier_reasons():
    r = pippy_mcp._scan_outlier_reason
    check("a normal row passes", r("GOOD", GOOD) is None)
    check("the Oct 28 PARA row (7043% upside, $2M cap) is excluded",
          r("PARA", {"currentPrice": 4.3, "targetMeanPrice": 240.0, "marketCap": 2.2e6}) is not None)
    check("a known recycled ticker is excluded whatever its numbers look like", "Paramount" in r("PARA", GOOD))
    check("upside above the cap is excluded, with the number in the reason",
          "7043%" in (r("XYZ", {"currentPrice": 4.3, "targetMeanPrice": 307.0, "marketCap": 5e9}) or "") or
          "exceeds the 300% sanity cap" in (r("XYZ", {"currentPrice": 4.3, "targetMeanPrice": 307.0, "marketCap": 5e9}) or ""))
    check("exactly at the cap (300%) is kept, just over is excluded",
          r("X", dict(GOOD, currentPrice=10, targetMeanPrice=40)) is None and r("X", dict(GOOD, currentPrice=10, targetMeanPrice=40.5)) is not None)
    check("a real high-upside name (SOUN-like, 183%) is kept", r("SOUN", dict(GOOD, currentPrice=10, targetMeanPrice=28.3, marketCap=4e9)) is None)
    check("a market cap under the floor is excluded", "below the $300M floor" in (r("TINY", dict(GOOD, marketCap=2.2e6)) or ""))
    check("exactly at the floor is kept", r("EDGE", dict(GOOD, marketCap=pippy_mcp.SCAN_MIN_MARKET_CAP)) is None)
    check("missing target / market cap fields never raise and are not penalised", r("N", {"currentPrice": 10}) is None)
    check("negative upside (target below price) is fine", r("N", dict(GOOD, targetMeanPrice=50.0)) is None)
    check("PARA is no longer in the scan universe and PSKY is", "PARA" not in pippy_mcp.SCAN_UNIVERSE and "PSKY" in pippy_mcp.SCAN_UNIVERSE)


class _FakeYF(types.ModuleType):
    def __init__(self, infos):
        super().__init__("yfinance")
        self._infos = infos

    def download(self, *a, **k):
        raise RuntimeError("no network")        # -> run_daily_scan falls back to zero momentum

    def Ticker(self, sym):
        outer = self
        return types.SimpleNamespace(info=outer._infos[sym])


def _run_scan(infos, universe, tmp):
    saved = {k: getattr(pippy_mcp, k) for k in ("yf", "SCAN_UNIVERSE", "WATCHLIST", "MEMORY_FILE", "SCAN_CACHE_FILE")}
    pippy_mcp.yf = _FakeYF(infos)
    pippy_mcp.SCAN_UNIVERSE, pippy_mcp.WATCHLIST = list(universe), []
    pippy_mcp.MEMORY_FILE = os.path.join(tmp, "no_memory.json")             # never read the real memory
    pippy_mcp.SCAN_CACHE_FILE = os.path.join(tmp, "scan_cache.json")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            out = json.loads(pippy_mcp.run_daily_scan())
    finally:
        for k, v in saved.items():
            setattr(pippy_mcp, k, v)
    return out, err.getvalue()


def test_run_daily_scan_drops_outliers_and_reports_them():
    infos = {
        "GOOD": GOOD,
        "PARA": {"currentPrice": 4.3, "targetMeanPrice": 240.0, "marketCap": 2.2e6, "shortName": "Banzai International, Inc.", "recommendationKey": "none"},
        "BIGT": dict(GOOD, shortName="Big Target Co", currentPrice=10, targetMeanPrice=70, marketCap=8e9),       # 600% upside
        "MICR": dict(GOOD, shortName="Micro Co", marketCap=1.5e8),
        "OK2": dict(GOOD, shortName="Second Co", targetMeanPrice=130.0),
    }
    with tempfile.TemporaryDirectory() as tmp:
        out, err = _run_scan(infos, list(infos), tmp)
        cache = json.load(open(os.path.join(tmp, "scan_cache.json")))
    tickers = [c["ticker"] for c in out["candidates"]]
    check("good rows are scored", set(tickers) == {"GOOD", "OK2"}, str(tickers))
    check("the bogus rows never become candidates", not {"PARA", "BIGT", "MICR"} & set(tickers))
    check("the top score is sane (nothing near 7044)", max(c["score"] for c in out["candidates"]) < 200, str(out["candidates"]))
    ex = {x["ticker"]: x for x in out["excluded"]}
    check("each exclusion is returned with its reason", set(ex) == {"PARA", "BIGT", "MICR"} and all(x["reason"] for x in ex.values()), str(out["excluded"]))
    check("the company name is kept so the log is readable", ex["PARA"]["name"] == "Banzai International, Inc.")
    check("the server logs each exclusion to stderr (stdout is the MCP channel)", "excluded PARA" in err and "excluded BIGT" in err, err)
    check("the exclusions are cached with the day's scan", {x["ticker"] for x in cache["excluded"]} == {"PARA", "BIGT", "MICR"})
    check("'scanned' counts only rows that were scored", out["scanned"] == 2, str(out["scanned"]))


def test_cached_scan_still_reports_exclusions():
    with tempfile.TemporaryDirectory() as tmp:
        infos = {"GOOD": GOOD, "BIGT": dict(GOOD, currentPrice=10, targetMeanPrice=70)}
        _run_scan(infos, list(infos), tmp)
        saved = (pippy_mcp.SCAN_CACHE_FILE,)
        pippy_mcp.SCAN_CACHE_FILE = os.path.join(tmp, "scan_cache.json")
        try:
            again = json.loads(pippy_mcp.run_daily_scan())
        finally:
            pippy_mcp.SCAN_CACHE_FILE = saved[0]
    check("the second call of the day is served from cache", again["source"] == "cache")
    check("and still carries the exclusions", [x["ticker"] for x in again["excluded"]] == ["BIGT"], str(again))


def test_the_run_log_names_every_exclusion():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        openbell._log_scan_exclusions({"excluded": [{"ticker": "PARA", "name": "Banzai International, Inc.", "reason": "ticker reassigned"},
                                                    {"ticker": "BIGT", "name": "", "reason": "upside 600% exceeds the 300% sanity cap"}]})
        openbell._log_scan_exclusions({})
        openbell._log_scan_exclusions(None)
    out = buf.getvalue()
    check("one [SCAN-FILTER] line per exclusion, with name and reason",
          "[SCAN-FILTER] PARA (Banzai International, Inc.) excluded: ticker reassigned" in out and "[SCAN-FILTER] BIGT" in out, out)
    check("nothing is printed when nothing was excluded (or the scan failed)", out.count("[SCAN-FILTER]") == 2)


def test_an_excluded_row_cannot_reach_the_email():
    # What the email shows: the top 5 of the (already filtered) candidates.
    cands = [{"ticker": "SOUN", "company": "SoundHound AI, Inc.", "sector": "Technology", "score": 212, "momentum": -22.1,
              "rationale": "analyst target implies 183% upside", "price": 10.0, "risk_level": "aggressive"}]
    html = openbell._daily_scan(cands, 77, 34.0)
    check("a filtered scan renders without the bad row", "PARA" not in html and "7044" not in html and "SOUN" in html)


if __name__ == "__main__":
    for t in (test_outlier_reasons, test_run_daily_scan_drops_outliers_and_reports_them, test_cached_scan_still_reports_exclusions,
              test_the_run_log_names_every_exclusion, test_an_excluded_row_cannot_reach_the_email):
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
