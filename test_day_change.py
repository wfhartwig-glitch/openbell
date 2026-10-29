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


def _all_tools(fake):
    with patched(fake, ["HOOD", "VRT", "NOW", "APP", "NVDA"]) as err:
        comm = {c["symbol"]: c for c in json.loads(pippy_mcp.fetch_commodities())["commodities"]}
        movers = json.loads(pippy_mcp.fetch_top_movers())
        stocks = {s: json.loads(pippy_mcp.fetch_stock_data(s)) for s in ("HOOD", "VRT", "NOW", "APP", "NVDA")}
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


def test_mismatch_guard_logs_loudly_for_disagreement_over_half_a_point_only():
    _, _, _, err = _all_tools(FakeYF())
    lines = [l for l in err.splitlines() if l.startswith("[PCT-MISMATCH]")]
    flagged = {l.split("]")[1].split(":")[0].strip() for l in lines}
    check("flagged: HOOD, VRT, gold and crude (differences of 8.0, 1.3, 1.1 and 1.7 points)",
          {"HOOD", "VRT", "Gold", "WTI Crude Oil"} <= flagged, str(flagged))
    check("not flagged: NOW, APP, NVDA (within 0.5 points)", not ({"NOW", "APP", "NVDA"} & flagged), str(flagged))
    hood = next(l for l in lines if " HOOD:" in l)
    check("the message carries the computed move, the closes used and the source's move",
          "computed -3.31%" in hood and "94.56 -> 91.43" in hood and "source says +4.68%" in hood and "using the computed move" in hood, hood)
    check("it goes to stderr (stdout is the MCP channel), once per symbol per fetch", all(sum(f" {n}:" in l for l in lines) >= 1 for n in ("HOOD", "VRT")))


def test_guard_threshold_boundary_and_missing_inputs():
    def lines(*a):
        e = io.StringIO()
        with contextlib.redirect_stderr(e):
            pippy_mcp._pct_guard("X", *a)
        return e.getvalue()
    check("exactly 0.5 points apart: silent", lines(1.0, 1.5) == "")
    check("just over 0.5 points: loud", "[PCT-MISMATCH] X" in lines(1.0, 1.51))
    check("negative direction counts the same", "[PCT-MISMATCH]" in lines(-2.0, 0.1))
    check("no source move: silent (nothing to compare)", lines(1.0, None) == "")
    check("no computed move: silent", lines(None, 1.0) == "")


def test_fast_info_previous_close_is_never_the_number_used():
    wild = dict(SOURCE, APP=(259.97, 100.0))          # a source previous_close that is simply wrong
    _, _, stocks, err = _all_tools(FakeYF(source=wild))
    check("APP still -1.08% even though the source says +160%", stocks["APP"]["pct"] == -1.08, str(stocks["APP"]))
    check("and the disagreement is logged", "APP: computed -1.08%" in err)


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
    check("a disagreement with the source's +5.33% is logged", "[PCT-MISMATCH] HOOD (pre-market)" in err.getvalue())


def test_untouched_series_still_use_their_own_sources():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pippy_mcp.py")).read()
    for fn in ("fetch_market_snapshot", "fetch_sector_performance", "fetch_treasury_yield", "fetch_global_indices"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n@mcp.tool()")] if "\n@mcp.tool()" in body else body
        check(f"{fn} is unchanged (indices, sectors and yields matched history on Oct 28)", "_pct_guard" not in body and "_closes_" not in body)


if __name__ == "__main__":
    for t in (test_oct28_numbers_come_from_daily_closes_including_both_sign_flips, test_top_movers_rank_on_the_corrected_moves,
              test_mismatch_guard_logs_loudly_for_disagreement_over_half_a_point_only, test_guard_threshold_boundary_and_missing_inputs,
              test_fast_info_previous_close_is_never_the_number_used, test_fallbacks_are_loud_not_silent,
              test_premarket_move_is_measured_from_the_last_completed_session, test_untouched_series_still_use_their_own_sources):
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
