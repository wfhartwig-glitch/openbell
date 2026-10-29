"""
test_day_change.py — per-stock and commodity day changes come from the last two DAILY closes,
never from yfinance's fast_info.previous_close.

Regression fixture = Oct 28, 2026. The emailed numbers (derived from fast_info.previous_close) vs
what the daily closes say:

    HOOD  +4.68%  ->  -3.31%   (sign flip: the "previous close" was its post-earnings after-hours price)
    GOLD  +0.52%  ->  -0.56%   (sign flip)
    VRT  +15.18%  -> +16.48%
    CRUDE +1.19%  ->  +2.88%
    NOW   -2.97%  ->  -3.00%      (source close enough: no warning)
    APP   -1.26%  ->  -1.08%      (ditto)
    NVDA  -1.26%  ->  -1.17%      (ditto)

No network, no live clock, no files. Run: python3 test_day_change.py
"""
import contextlib
import datetime
import io
import json
import os
import sys
import types

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pippy_mcp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


D = datetime.date
# (Oct 26, Oct 27, Oct 28) daily closes as Yahoo's history reports them today
CLOSES = {
    "HOOD":  [92.84, 94.56, 91.43],
    "VRT":   [199.78, 208.56, 242.94],
    "NOW":   [158.18, 154.78, 150.13],
    "APP":   [260.74, 262.82, 259.97],
    "NVDA":  [216.00, 217.94, 215.38],
    "GC=F":  [4038.4, 4074.6, 4051.6],
    "CL=F":  [101.76, 99.90, 102.78],
}
# fast_info as it behaved at 20:01Z on Oct 28: (last_price, previous_close) implied by the emailed price and percent
SOURCE = {
    "HOOD": (91.43, 87.34), "VRT": (243.10, 211.06), "NOW": (149.85, 154.44), "APP": (259.97, 263.29),
    "NVDA": (215.38, 218.13), "GC=F": (4077.5, 4056.41), "CL=F": (102.55, 101.34),
}
EXPECT = {"HOOD": -3.31, "VRT": 16.48, "NOW": -3.00, "APP": -1.08, "NVDA": -1.17, "GC=F": -0.56, "CL=F": 2.88}
DATES = [D(2026, 10, 26), D(2026, 10, 27), D(2026, 10, 28)]


def _frame(vals, dates=DATES):
    return pd.DataFrame({"Close": vals}, index=pd.to_datetime(dates))


class FakeYF(types.ModuleType):
    def __init__(self, closes=None, source=None, history_fails=False, batch_fails=False):
        super().__init__("yfinance")
        self.closes = CLOSES if closes is None else closes
        self.source = SOURCE if source is None else source
        self.history_fails, self.batch_fails = history_fails, batch_fails

    def Ticker(self, sym):
        outer = self
        last, prev = outer.source.get(sym, (None, None))

        def history(period="10d", interval="1d", auto_adjust=False, **k):
            if outer.history_fails:
                raise RuntimeError("history down")
            return _frame(outer.closes[sym], outer.closes.get("_dates", DATES)[-len(outer.closes[sym]):])
        return types.SimpleNamespace(fast_info=types.SimpleNamespace(last_price=last, previous_close=prev), history=history,
                                     info={"shortName": f"{sym} Inc", "longName": f"{sym} Incorporated", "sector": "Technology"}, news=[])

    def download(self, symbols, **k):
        if self.batch_fails:
            raise RuntimeError("batch down")
        return pd.concat({s: _frame(self.closes[s]) for s in symbols if s in self.closes}, axis=1)


@contextlib.contextmanager
def patched(fake, universe=None):
    saved = {k: getattr(pippy_mcp, k) for k in ("yf", "_fmp", "MOVERS_UNIVERSE")}
    pippy_mcp.yf = fake
    pippy_mcp._fmp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FMP unavailable"))
    if universe is not None:
        pippy_mcp.MOVERS_UNIVERSE = universe
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            yield err
    finally:
        for k, v in saved.items():
            setattr(pippy_mcp, k, v)


RAW = {}          # the last run's raw tool payloads, for the summary-line tests


@contextlib.contextmanager
def _verbose(on=True):
    old = os.environ.get("PIPPY_VERBOSE")
    if on:
        os.environ["PIPPY_VERBOSE"] = "1"
    else:
        os.environ.pop("PIPPY_VERBOSE", None)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PIPPY_VERBOSE", None)
        else:
            os.environ["PIPPY_VERBOSE"] = old


def _all_tools(fake, verbose=False):
    with _verbose(verbose), patched(fake, ["HOOD", "VRT", "NOW", "APP", "NVDA"]) as err:
        comm_raw = json.loads(pippy_mcp.fetch_commodities())
        movers = json.loads(pippy_mcp.fetch_top_movers())
        stocks = {s: json.loads(pippy_mcp.fetch_stock_data(s)) for s in ("HOOD", "VRT", "NOW", "APP", "NVDA")}
    comm = {c["symbol"]: c for c in comm_raw["commodities"]}
    RAW.update(comm=comm_raw, movers=movers, stocks=stocks)
    return comm, movers, stocks, err.getvalue()


def test_oct28_numbers_come_from_daily_closes_including_both_sign_flips():
    comm, movers, stocks, _ = _all_tools(FakeYF())
    for s, want in (("HOOD", -3.31), ("VRT", 16.48), ("NOW", -3.00), ("APP", -1.08), ("NVDA", -1.17)):
        check(f"fetch_stock_data {s}: {want:+.2f}%", stocks[s]["pct"] == want, str(stocks[s]["pct"]))
    check("HOOD is DOWN, not the +4.68% the source implied (sign flip #1)", stocks["HOOD"]["pct"] < 0)
    check("gold is DOWN 0.56%, not the +0.52% the source implied (sign flip #2)", comm["GC=F"]["pct"] == -0.56 and comm["GC=F"]["pct"] < 0, str(comm["GC=F"]))
    check("crude is +2.88%, not +1.19%", comm["CL=F"]["pct"] == 2.88, str(comm["CL=F"]))
    check("the commodity price and change are the day's close and the close-to-close change",
          comm["GC=F"]["price"] == 4051.6 and comm["GC=F"]["change"] == -23.0 and comm["CL=F"]["price"] == 102.78, str(comm))
    check("the displayed price is the official close, not an after-hours print", stocks["VRT"]["price"] == 242.94, str(stocks["VRT"]["price"]))


def test_top_movers_rank_on_the_corrected_moves():
    _, movers, _, _ = _all_tools(FakeYF())
    g = {m["symbol"]: m["pct"] for m in movers["gainers"]}
    l = {m["symbol"]: m["pct"] for m in movers["losers"]}
    check("VRT leads the gainers at +16.48%", movers["gainers"][0]["symbol"] == "VRT" and g["VRT"] == 16.48, str(movers["gainers"]))
    check("HOOD is the day's biggest LOSER at -3.31% (it was listed as a +4.68% gainer)", movers["losers"][0]["symbol"] == "HOOD" and l["HOOD"] == -3.31, str(movers["losers"]))
    check("HOOD is not among the gainers", "HOOD" not in g)
    check("movers still report the universe size", movers["universe_size"] == 5 and movers["source"] == "fallback")
    check("movers are enriched with names", all(m.get("name") for m in movers["gainers"] + movers["losers"]))


def test_mismatches_are_returned_in_the_payloads_and_the_server_stays_quiet_by_default():
    _, _, _, err = _all_tools(FakeYF())
    check("default: no per-symbol [PCT-MISMATCH] lines on stderr", "[PCT-MISMATCH]" not in err, err[:300])
    recs = {m["symbol"]: m for k in ("comm", "movers") for m in RAW[k]["pct_mismatches"]}
    check("flagged in the payloads: HOOD, VRT, Gold and WTI Crude Oil",
          {"HOOD", "VRT", "Gold", "WTI Crude Oil"} <= set(recs), str(set(recs)))
    check("not flagged: NOW, APP, NVDA (within 0.5 points)", not ({"NOW", "APP", "NVDA"} & set(recs)), str(set(recs)))
    hood = recs["HOOD"]
    check("a record carries both numbers, the gap and the closes used",
          hood["computed"] == -3.31 and hood["source"] == 4.68 and hood["diff"] == 7.99 and hood["prev_close"] == 94.56 and hood["last_close"] == 91.43, str(hood))
    check("pct_checked lists everything that was compared (so a denominator exists)", {"HOOD", "NOW", "Gold"} <= set(RAW["movers"]["pct_checked"]) | set(RAW["comm"]["pct_checked"]))
    check("fetch_stock_data returns its own record too", RAW["stocks"]["HOOD"]["pct_mismatches"][0]["symbol"] == "HOOD" and RAW["stocks"]["NOW"]["pct_mismatches"] == [])


def test_verbose_mode_prints_every_symbol_with_both_numbers():
    _, _, _, err = _all_tools(FakeYF(), verbose=True)
    lines = [l for l in err.splitlines() if l.startswith("[PCT-MISMATCH]")]
    flagged = {l.split("]")[1].split(":")[0].strip() for l in lines}
    check("verbose: per-symbol lines for HOOD, VRT, Gold, WTI", {"HOOD", "VRT", "Gold", "WTI Crude Oil"} <= flagged, str(flagged))
    check("and still not for NOW / APP / NVDA", not ({"NOW", "APP", "NVDA"} & flagged))
    hood = next(l for l in lines if " HOOD:" in l)
    check("the line carries the computed move, the closes used and the source's move",
          "computed -3.31%" in hood and "94.56 -> 91.43" in hood and "source says +4.68%" in hood and "using the computed move" in hood, hood)


def test_one_summary_line_per_run_names_the_three_worst_with_both_numbers():
    import openbell
    _all_tools(FakeYF())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        openbell._log_pct_mismatches(RAW["comm"], RAW["movers"], list(RAW["stocks"].values()))
    out = buf.getvalue().strip().splitlines()
    check("exactly one line", len(out) == 1, str(out))
    line = out[0]
    check("it is tagged and counts mismatched symbols out of those checked",
          line.startswith("[PCT-MISMATCH] 4 of 7 symbols differ by more than 0.5 points"), line)
    check("the three worst, worst first, each with both numbers",
          "Worst: HOOD computed -3.31% vs source +4.68%; WTI Crude Oil computed +2.88% vs source +1.19%; VRT computed +16.48% vs source +15.18%." in line.replace("Gold computed", "Gold computed"), line)
    check("it points at --verbose for the rest", "--verbose" in line)
    check("a symbol seen in several tool results is counted once", "4 of 7" in line)
    clean = io.StringIO()
    with contextlib.redirect_stdout(clean):
        openbell._log_pct_mismatches({"pct_checked": ["A", "B"], "pct_mismatches": []}, [], None, {"x": 1})
    check("no mismatches -> no line at all", clean.getvalue() == "")
    two = io.StringIO()
    with contextlib.redirect_stdout(two):
        openbell._log_pct_mismatches({"pct_checked": ["A", "B", "C"], "pct_mismatches": [
            {"symbol": "A", "computed": 1.0, "source": 3.0, "diff": 2.0}, {"symbol": "B", "computed": -1.0, "source": 1.0, "diff": 2.0}]})
    check("fewer than three offenders is fine", two.getvalue().startswith("[PCT-MISMATCH] 2 of 3 symbols") and "Worst: A computed +1.00% vs source +3.00%; B" in two.getvalue(), two.getvalue())


def test_verbose_flag_reaches_the_server_process():
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    help_text = subprocess.run([sys.executable, "openbell.py", "--help"], cwd=here, capture_output=True, text=True).stdout
    check("openbell.py advertises --verbose", "--verbose" in help_text, help_text[:300])
    src = open(os.path.join(here, "openbell.py")).read()
    check("--verbose sets PIPPY_VERBOSE before the server is spawned (env=dict(os.environ))",
          'os.environ["PIPPY_VERBOSE"] = "1"' in src and src.index('os.environ["PIPPY_VERBOSE"]') < src.index("asyncio.run(run(args.mode"), "")


def test_guard_threshold_boundary_and_missing_inputs():
    g = lambda *a: pippy_mcp._pct_guard("X", *a)
    check("exactly 0.5 points apart: no record", g(1.0, 1.5) is None)
    check("just over 0.5 points: a record", g(1.0, 1.51) is not None and g(1.0, 1.51)["diff"] == 0.51)
    check("negative direction counts the same", g(-2.0, 0.1) is not None)
    check("no source move: nothing to compare", g(1.0, None) is None)
    check("no computed move: nothing to compare", g(None, 1.0) is None)


def test_fast_info_previous_close_is_never_the_number_used():
    wild = dict(SOURCE, APP=(259.97, 100.0))          # a source previous_close that is simply wrong
    _, _, stocks, err = _all_tools(FakeYF(source=wild))
    check("APP still -1.08% even though the source says +160%", stocks["APP"]["pct"] == -1.08, str(stocks["APP"]))
    check("and the disagreement is reported", any(m["symbol"] == "APP" and m["computed"] == -1.08 for m in RAW["stocks"]["APP"]["pct_mismatches"]))


def test_fallbacks_are_loud_not_silent():
    comm, movers, stocks, err = _all_tools(FakeYF(history_fails=True, batch_fails=True))
    check("history down: the tools still answer, from the source", stocks["HOOD"]["pct"] == 4.68 and comm["GC=F"]["pct"] == 0.52, f"{stocks['HOOD']['pct']} {comm['GC=F']['pct']}")
    check("and say so on stderr for each symbol", err.count("[PCT-FALLBACK]") >= 5 and "[PCT-FALLBACK] HOOD" in err, err[:300])
    one = {k: [v[-1]] for k, v in CLOSES.items()}
    _, _, stocks, err = _all_tools(FakeYF(closes=one))
    check("a single close is not enough to compute a move: falls back loudly", "fewer than two daily closes" in err and stocks["NVDA"]["pct"] == -1.26, err[:200])
    broken = {"HOOD": (None, None)}
    with patched(FakeYF(source=broken, history_fails=True, batch_fails=True), ["HOOD"]):
        out = json.loads(pippy_mcp.fetch_top_movers())
    check("no usable data at all: no fabricated mover", out["gainers"] == [] and out["losers"] == [], str(out))


def test_premarket_move_is_measured_from_the_last_completed_session():
    closes = {"HOOD": [94.56, 91.43, 70.00], "_dates": [D(2026, 10, 27), D(2026, 10, 28), D(2026, 10, 29)]}   # last bar = today, in progress
    src = {"HOOD": (92.00, 87.34)}
    real = pippy_mcp._today_ct
    pippy_mcp._today_ct = lambda now=None: D(2026, 10, 29)
    try:
        with patched(FakeYF(closes=closes, source=src)) as err:
            out = json.loads(pippy_mcp.fetch_premarket_data("HOOD"))
    finally:
        pippy_mcp._today_ct = real
    check("pre-market base is Oct 28's official close (91.43), not today's partial bar or the source's base",
          out["pct"] == round((92.00 - 91.43) / 91.43 * 100, 2) == 0.62, str(out))
    check("a disagreement with the source's +5.33% is reported", out["pct_mismatches"] and out["pct_mismatches"][0]["symbol"] == "HOOD (pre-market)", str(out))


def test_untouched_series_still_use_their_own_sources():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pippy_mcp.py")).read()
    for fn in ("fetch_market_snapshot", "fetch_sector_performance", "fetch_treasury_yield", "fetch_global_indices"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n@mcp.tool()")] if "\n@mcp.tool()" in body else body
        check(f"{fn} is unchanged (indices, sectors and yields matched history on Oct 28)", "_pct_guard" not in body and "_closes_" not in body)


if __name__ == "__main__":
    for t in (test_oct28_numbers_come_from_daily_closes_including_both_sign_flips, test_top_movers_rank_on_the_corrected_moves,
              test_mismatches_are_returned_in_the_payloads_and_the_server_stays_quiet_by_default, test_verbose_mode_prints_every_symbol_with_both_numbers,
              test_one_summary_line_per_run_names_the_three_worst_with_both_numbers, test_verbose_flag_reaches_the_server_process, test_guard_threshold_boundary_and_missing_inputs,
              test_fast_info_previous_close_is_never_the_number_used, test_fallbacks_are_loud_not_silent,
              test_premarket_move_is_measured_from_the_last_completed_session, test_untouched_series_still_use_their_own_sources):
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
