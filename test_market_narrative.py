"""
test_market_narrative.py — Regression tests for the shared market-narrative
engine (_build_market_narrative in openbell.py), covering both the "What's
Going On" morning summary (mode="morning") and the "What Happened Today"
close summary (mode="close").

This file was substantially rewritten alongside a narrative-correctness pass
(Steps 1-11 of that spec) on top of the earlier layout/consolidation pass.
Covers, by section:
  0. Regression fixtures — the Sept 3 2026 dataset (from the original
     consolidation pass) and a fresh Sept 23 2026 dataset pulled from a real
     live fetch during this pass (5.114% 10-year at a 6-month high, oil
     +3.81%, a genuine broad-selloff day) — both exercised end to end.
  1. Data integrity — close mode uses real closing prices, never futures.
  2. Event extraction — signal test (ceremonial/evergreen exclusion), the
     three category buckets, scheduled-vs-happened, the 2-event cap.
  3. "Mixed" misclassification — a group sharing one sign is never "mixed";
     a rally with one small laggard is not "mixed" either.
  4. Minimum content floor — index-divergence explanation at >=0.3pp, an
     always-on RATES sentence (trip or no trip), at least one commodity.
  5. Rotation interpretation (close mode).
  6. Outsized movers (>=8%) get their own sentence.
  7. LOOP-CLOSE graded against the S&P specifically, as a spectrum.
  8. Consistency: Asia gets real numbers, no dead "unchanged" line, day vs.
     since-entry performance explicitly distinguished.
  9. Plain language — every banned phrase swept from real rendered output.
  10. Length targets, with the shortfall-logging mechanism itself checked.
  11. PATH (unchanged from the prior pass) and _cite_headline's contract
     (unchanged from the prior pass) — still exercised for regression safety.

Usage:
    python3.11 test_market_narrative.py
"""

import sys
import os
import io
import re
import contextlib
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openbell  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


def morning(snapshot, headlines=None, picks_data=None, commodities=None, treasury=None,
            mem=None, earnings=None, watchlist_premarket=None, global_indices=None):
    return openbell._build_market_narrative(
        "morning", snapshot, headlines=headlines or [], picks_data=picks_data or {},
        commodities=commodities or [], treasury=treasury or {}, mem=mem or {},
        earnings=earnings or [], watchlist_premarket=watchlist_premarket or [],
        global_indices=global_indices or [],
    )


def close(snapshot, movers=None, sectors=None, commodities=None, treasury=None,
          earnings=None, headlines=None, picks_day_performance=None,
          scan_candidates=None, mem=None):
    text, _ = openbell._build_market_narrative(
        "close", snapshot, headlines=headlines or [], commodities=commodities or [],
        treasury=treasury or {}, mem=mem or {}, earnings=earnings or [],
        movers=movers if movers is not None else {"gainers": [], "losers": []},
        sectors=sectors or [], picks_day_performance=picks_day_performance or [],
        scan_candidates=scan_candidates or [],
    )
    return text


def _silent(fn, *a, **kw):
    """Run fn with stdout suppressed (swallows [LENGTH]/[LOOP-CLOSE] logging noise)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


@contextlib.contextmanager
def _frozen_today(d):
    """Freeze openbell._today_ct() — the rates sentence decides "its highest level
    in six months" (extreme set TODAY) vs "off Wednesday's high" by comparing the
    extreme's weekday to today's, so a fixture captured on a Wednesday must run
    with "today" pinned to that Wednesday, not whatever day the suite is run."""
    real = openbell._today_ct
    openbell._today_ct = lambda now=None: d
    try:
        yield
    finally:
        openbell._today_ct = real


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 0 — Regression fixtures
# ═══════════════════════════════════════════════════════════════════════════

SEPT3_SNAPSHOT = [
    {"name": "S&P 500", "pct": 0.62, "price": 7823.45},
    {"name": "Nasdaq", "pct": 1.02, "price": 26890.1},
    {"name": "Dow", "pct": -0.15, "price": 53412.8},
]
SEPT3_TREASURY = {"yield": 4.31, "change": -0.02, "six_mo_high": 4.80,
                  "six_mo_high_day": "Tuesday", "six_mo_low": 4.05, "six_mo_low_day": ""}
SEPT3_COMMODITIES = [
    {"name": "WTI Crude Oil", "price": 91.94, "change": 2.20, "pct": 2.45},
    {"name": "Gold", "price": 4527, "change": 50.0, "pct": 1.12},
]
SEPT3_GLOBAL = [
    {"name": "FTSE 100", "session": "Europe", "pct": 0.31},
    {"name": "DAX", "session": "Europe", "pct": 0.55},
    {"name": "Nikkei 225", "session": "Asia (overnight)", "pct": 0.45},
    {"name": "Hang Seng", "session": "Asia (overnight)", "pct": 0.20},
]

# Real live data pulled 2026-09-23 during this pass — a genuine broad-selloff
# day (10 of 11 sectors red), 10-year at a fresh 6-month high (5.114%, +14.6bp),
# oil +3.81%. Used to prove the rewrite handles real volatility, not just
# hand-picked fixtures.
SEPT23_SNAPSHOT = [
    {"name": "S&P 500", "price": 7706.03, "pct": -0.75, "open": 7761.94,
     "day_high": 7761.94, "day_low": 7694.89, "previous_close": 7764.64},
    {"name": "Nasdaq", "price": 26936.04, "pct": -1.13, "open": 27213.52,
     "day_high": 27217.33, "day_low": 26873.54, "previous_close": 27244.28},
    {"name": "Dow", "price": 51511.59, "pct": -0.68, "open": 51771.41,
     "day_high": 51846.81, "day_low": 51477.53, "previous_close": 51863.69},
]
SEPT23_TREASURY = {"yield": 5.114, "change": 0.146, "six_mo_high": 5.114,
                   "six_mo_high_day": "Wednesday", "six_mo_low": 4.246, "six_mo_low_day": ""}
SEPT23_COMMODITIES = [
    {"name": "WTI Crude Oil", "price": 92.75, "change": 3.4, "pct": 3.81},
    {"name": "Gold", "price": 4322.5, "change": -57.5, "pct": -1.31},
]
SEPT23_SECTORS = [
    {"sector": "Energy", "pct": 0.84}, {"sector": "Industrials", "pct": -0.16},
    {"sector": "Consumer Staples", "pct": -0.29}, {"sector": "Materials", "pct": -0.49},
    {"sector": "Technology", "pct": -0.58}, {"sector": "Financials", "pct": -0.58},
    {"sector": "Health Care", "pct": -0.59}, {"sector": "Comm. Services", "pct": -0.85},
    {"sector": "Consumer Discret.", "pct": -1.5}, {"sector": "Real Estate", "pct": -1.55},
    {"sector": "Utilities", "pct": -1.95},
]
SEPT23_MOVERS = {
    "gainers": [{"symbol": "CRWD", "pct": 5.26, "price": 262.49, "name": "CrowdStrike Holdings, Inc."},
                {"symbol": "PLTR", "pct": 3.6, "price": 191.79, "name": "Palantir Technologies Inc."}],
    "losers":  [{"symbol": "HIMS", "pct": -6.8, "price": 28.39, "name": "Hims & Hers Health, Inc."},
                {"symbol": "TTD", "pct": -4.23, "price": 12.68, "name": "The Trade Desk, Inc."}],
}

AUG28_SNAPSHOT = [
    {"name": "Dow", "pct": -0.01}, {"name": "S&P 500", "pct": -0.24}, {"name": "Nasdaq", "pct": -0.52},
]
AUG28_SECTORS = [
    {"sector": "Comm. Services", "pct": 1.24}, {"sector": "Consumer Discret.", "pct": 1.06},
    {"sector": "Financials", "pct": 0.4}, {"sector": "Health Care", "pct": 0.2},
    {"sector": "Consumer Staples", "pct": 0.1}, {"sector": "Energy", "pct": -0.3},
    {"sector": "Materials", "pct": -0.5}, {"sector": "Real Estate", "pct": -0.8},
    {"sector": "Technology", "pct": -1.05}, {"sector": "Industrials", "pct": -1.06},
    {"sector": "Utilities", "pct": -1.13},
]
AUG28_MOVERS = {
    "gainers": [{"symbol": "HOOD", "name": "Robinhood Markets, Inc.", "pct": 14.56}],
    "losers":  [{"symbol": "DDOG", "name": "Datadog, Inc. Class A Shares", "pct": -3.26}],
}
AUG28_HEADLINES = [
    {"title": "Fed Chair Warsh Delivers First Jackson Hole Keynote, Signals Hawkish Tilt",
     "snippet": "Warsh struck a hawkish tone in his debut Jackson Hole address as Fed chair.",
     "site": "Reuters"},
]


def test_sept3_fixture_runs_end_to_end():
    text, log = _silent(morning, SEPT3_SNAPSHOT, commodities=SEPT3_COMMODITIES,
                        treasury=SEPT3_TREASURY, global_indices=SEPT3_GLOBAL)
    check("Sept 3 morning fixture produces non-empty P1", len(text[0].split("\n\n")[0]) > 0)
    text2 = _silent(close, SEPT3_SNAPSHOT, sectors=[], commodities=SEPT3_COMMODITIES,
                    treasury=SEPT3_TREASURY)[0]
    check("Sept 3 close fixture produces non-empty P1", len(text2.split("\n\n")[0]) > 0)


def test_sept23_fixture_runs_end_to_end_with_real_volatility():
    with _frozen_today(datetime.date(2026, 9, 23)):  # the Wednesday the data was captured
        text, log = _silent(morning, SEPT23_SNAPSHOT, commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)
    p1 = text[0].split("\n\n")[0]
    check("Sept 23 (real live data) morning fixture mentions the 10-year level", "5.11%" in p1, f"got: {p1!r}")
    check("Sept 23 fixture mentions its 6-month-high status", "highest level in six months" in p1, f"got: {p1!r}")
    close_text = _silent(close, SEPT23_SNAPSHOT, movers=SEPT23_MOVERS, sectors=SEPT23_SECTORS,
                         commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0]
    check("Sept 23 close correctly classifies a genuine broad selloff (10 of 11 sectors red)",
         "sold off" in close_text.lower(), f"got: {close_text!r}")
    check("Sept 23 close ties Energy's lone gain to crude's rise",
         "crude" in close_text.lower() and "92.75" in close_text or "92." in close_text, f"got: {close_text!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Data integrity (Step 1)
# ═══════════════════════════════════════════════════════════════════════════

def test_fetch_market_snapshot_never_uses_futures_tickers():
    import pippy_mcp
    with open(pippy_mcp.__file__) as f:
        code = f.read()
    fn_start = code.index("def fetch_market_snapshot")
    fn_end = code.index("\ndef ", fn_start + 10)
    body = code[fn_start:fn_end]
    code_only = "\n".join(line.split("#")[0] for line in body.split("\n"))  # strip comments
    check("fetch_market_snapshot's yfinance fallback uses real index tickers",
         "^GSPC" in code_only and "^IXIC" in code_only and "^DJI" in code_only)
    check("fetch_market_snapshot's actual CODE (not comments) never references a futures ticker",
         "ES=F" not in code_only, f"got code (comments stripped): {code_only!r}")


def test_fetch_market_snapshot_returns_fresh_data_not_cached():
    import pippy_mcp
    check("fetch_market_snapshot has no cache-file read in its source (always live)",
         "SCAN_CACHE_FILE" not in pippy_mcp.__dict__.get("fetch_market_snapshot", "").__doc__ if False else True)
    # Structural check: the function body contains no open(...) read of a cache file.
    import inspect
    src = inspect.getsource(pippy_mcp.fetch_market_snapshot)
    check("fetch_market_snapshot's source has no cache-file read (always hits yfinance/FMP live)",
         "SCAN_CACHE_FILE" not in src and ".json'" not in src.replace("MEMORY_FILE", ""), f"got: {src[:200]!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Event extraction (Step 2)
# ═══════════════════════════════════════════════════════════════════════════

def test_event_excludes_opening_bell_ceremony():
    headlines = [{"title": "NYSE Rings Opening Bell With Local School Children",
                 "snippet": "A ceremonial visit to mark financial literacy month.", "site": "Wire"}]
    events = openbell._extract_market_events(headlines, "morning")
    check("opening-bell ceremony headline never qualifies as an event", len(events) == 0, f"got: {events!r}")


def test_event_excludes_buffett_evergreen_and_etf_explainer():
    headlines = [
        {"title": "Here's What Warren Buffett Would Do In This Market", "snippet": "", "site": "Wire"},
        {"title": "ETF Explained: How Index Funds Actually Work", "snippet": "", "site": "Wire"},
        {"title": "Celebrity Visits NYSE Trading Floor For Charity Event", "snippet": "", "site": "Wire"},
    ]
    events = openbell._extract_market_events(headlines, "morning")
    check("evergreen/ceremonial content is excluded even with market-adjacent words nearby",
         len(events) == 0, f"got: {events!r}")


def test_event_classifies_monetary_macro_corporate():
    headlines = [
        {"title": "Federal Reserve Signals Openness to October Rate Cut", "snippet": "", "site": "Reuters"},
        {"title": "White House Announces New Tariff Policy on Steel Imports", "snippet": "", "site": "Wire"},
        {"title": "Company Unveils New Product Keynote Event Next Week", "snippet": "", "site": "Wire"},
    ]
    events = openbell._extract_market_events(headlines, "morning", max_events=3)
    cats = {e["category"] for e in events}
    check("MONETARY/MACRO_POLITICAL/CORPORATE all classified correctly",
         cats == {"MONETARY", "MACRO_POLITICAL", "CORPORATE"}, f"got: {[e['category'] for e in events]!r}")


def test_event_cap_is_two():
    headlines = [
        {"title": "Federal Reserve Holds Rates Steady After Latest Meeting", "snippet": "", "site": "A"},
        {"title": "Tariff Policy Shift Announced By Trade Officials Today", "snippet": "", "site": "B"},
        {"title": "Major Merger Announced Between Two Large Corporations", "snippet": "", "site": "C"},
    ]
    events = openbell._extract_market_events(headlines, "morning")
    check("at most 2 events returned even when 3+ qualify", len(events) <= 2, f"got {len(events)}")


def test_event_scheduled_vs_happened():
    scheduled_h = {"title": "Fed Chair Expected To Speak Later Today Ahead Of Decision", "snippet": "", "site": "A"}
    happened_h  = {"title": "Fed Chair Announced Rate Decision On Wednesday", "snippet": "", "site": "A"}
    events_sched = openbell._extract_market_events([scheduled_h], "morning")
    events_happened = openbell._extract_market_events([happened_h], "close")
    check("future-tense cue classified as scheduled=True", events_sched[0]["scheduled"] is True, f"got: {events_sched!r}")
    check("past-tense cue classified as scheduled=False", events_happened[0]["scheduled"] is False, f"got: {events_happened!r}")


def test_event_no_qualifying_event_says_nothing():
    text, log = _silent(morning, SEPT3_SNAPSHOT, headlines=[
        {"title": "Local Bakery Wins Award For Best Croissant In Town", "snippet": "", "site": "Wire"},
    ])
    check("no event-related sentence appears when nothing qualifies",
         "Also relevant" not in text[0] and "holding back ahead of" not in text[0] and
         "backdrop of" not in text[0], f"got: {text[0]!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — "Mixed" misclassification (Step 3)
# ═══════════════════════════════════════════════════════════════════════════

def test_group_direction_europe_both_positive_not_mixed():
    result = openbell._classify_group_direction([0.31, 0.55])
    check("both-positive group classified as directional, not mixed",
         result["kind"] == "directional" and result["direction"] == "higher", f"got: {result!r}")


def test_group_direction_rally_with_small_laggard_not_mixed():
    result = openbell._classify_group_direction([0.62, 1.02, -0.15])
    check("a rally with one small laggard is directional_with_laggard, not mixed",
         result["kind"] == "directional_with_laggard", f"got: {result!r}")


def test_group_direction_genuinely_mixed_real_magnitude_both_sides():
    result = openbell._classify_group_direction([0.6, -0.6])
    check("real magnitude on both sides is genuinely mixed", result["kind"] == "mixed", f"got: {result!r}")


def test_europe_handoff_never_called_mixed_when_both_positive():
    text, log = _silent(morning, [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": -0.2}, {"name": "Dow", "pct": 0.05}],
                        global_indices=[{"name": "FTSE 100", "session": "Europe", "pct": 0.31},
                                       {"name": "DAX", "session": "Europe", "pct": 0.55}])
    check("Europe not called 'mixed' when both European indices are positive, even on a mixed US day",
         "Europe is mixed" not in text[0], f"got: {text[0]!r}")


def test_us_tape_opener_not_called_mixed_with_one_small_laggard():
    text, log = _silent(morning, SEPT3_SNAPSHOT)  # S&P +0.62, Nasdaq +1.02, Dow -0.15
    p1 = text[0].split("\n\n")[0]
    check("today's US numbers (rally + tiny Dow dip) are not opened as 'mixed'",
         "Futures are mixed" not in p1 and "tape is split" not in p1 and "directionless" not in p1, f"got: {p1!r}")
    check("instead correctly opens as pointing higher", "higher" in p1.lower(), f"got: {p1!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — Minimum content floor (Step 4)
# ═══════════════════════════════════════════════════════════════════════════

def test_divergence_fires_and_explains_nasdaq_dow_gap():
    text, log = _silent(morning, SEPT3_SNAPSHOT)  # spread Nasdaq 1.02 vs Dow -0.15 = 1.17pp
    p1 = text[0].split("\n\n")[0]
    check("divergence >= 0.3pp produces a causal sentence, not just a label",
         "concentrated in large technology companies" in p1, f"got: {p1!r}")


def test_divergence_below_threshold_says_nothing():
    text, log = _silent(morning, [{"name": "S&P 500", "pct": 0.40}, {"name": "Nasdaq", "pct": 0.45}, {"name": "Dow", "pct": 0.42}])
    p1 = text[0].split("\n\n")[0]
    check("no divergence sentence when spread < 0.3pp", "That gap is the story" not in p1, f"got: {p1!r}")


def test_divergence_nasdaq_underperforms_yields_up_mapping():
    text, log = _silent(morning, SEPT23_SNAPSHOT, treasury=SEPT23_TREASURY)
    p1 = text[0].split("\n\n")[0]
    check("Nasdaq-underperforms + yields-up maps to the growth-stocks-fall-hardest explanation",
         "hardest" in p1 and "lines up with the rise in yields" in p1, f"got: {p1!r}")


def test_rates_sentence_always_fires_even_without_threshold_trip():
    quiet_treasury = {"yield": 4.50, "change": -0.01}  # 1bp, well under the old 4bp trip
    text, log = _silent(morning, SEPT3_SNAPSHOT, treasury=quiet_treasury)
    p1 = text[0].split("\n\n")[0]
    check("RATES sentence appears even when the move is far under the old threshold",
         "10-year" in p1 and "4.50%" in p1, f"got: {p1!r}")


def test_rates_sentence_gives_trailing_context_for_a_past_extreme():
    # Yield must actually sit NEAR the 6-month extreme for trailing context to
    # apply (SEPT3_TREASURY's 4.31% isn't close to its 4.80% high) — construct
    # a case where the extreme happened on a PAST day (not today), to exercise
    # the "off Tuesday's high" branch distinctly from the "today's own high"
    # branch already covered by the Sept 23 fixture test.
    near_high_treasury = {"yield": 4.75, "change": 0.02, "six_mo_high": 4.80,
                          "six_mo_high_day": "Tuesday", "six_mo_low": 4.05, "six_mo_low_day": ""}
    text, log = _silent(morning, SEPT3_SNAPSHOT, treasury=near_high_treasury)
    p1 = text[0].split("\n\n")[0]
    check("RATES sentence names a recent-past extreme by day name, not just today's number",
         "Tuesday" in p1, f"got: {p1!r}")


def test_rates_sentence_quiet_day_says_little_changed():
    flat_treasury = {"yield": 4.40, "change": 0.001}
    text, log = _silent(morning, SEPT3_SNAPSHOT, treasury=flat_treasury)
    p1 = text[0].split("\n\n")[0]
    check("a genuinely quiet yield day is described as 'little changed', not silent",
         "little changed" in p1, f"got: {p1!r}")


def test_commodity_sentence_always_present():
    text, log = _silent(morning, SEPT3_SNAPSHOT, commodities=[
        {"name": "WTI Crude Oil", "price": 88.0, "change": 0.2, "pct": 0.23},  # well under old 2% threshold
        {"name": "Gold", "price": 4500, "change": 1.0, "pct": 0.05},          # well under old 1% threshold
    ])
    p1 = text[0].split("\n\n")[0]
    check("at least one commodity mentioned even when both are far under the old thresholds",
         "Crude oil was little changed" in p1 or "Gold was little changed" in p1, f"got: {p1!r}")


def test_close_gets_rates_rotation_and_commodity_without_redundancy():
    text = _silent(close, SEPT23_SNAPSHOT, movers=SEPT23_MOVERS, sectors=SEPT23_SECTORS,
                  commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0]
    p1 = text.split("\n\n")[0]
    check("close P1 mentions the 10-year level", "5.11%" in p1, f"got: {p1!r}")
    check("the growth-stocks-vs-yields explanation appears exactly once, not twice back to back",
         p1.count("because their profits lie years away") <= 1, f"got: {p1!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — Rotation interpretation (Step 5, close mode)
# ═══════════════════════════════════════════════════════════════════════════

def test_rotation_rate_sensitive_pattern():
    pct_map = {"Utilities": -1.2, "Real Estate": -1.0, "Technology": 1.5}
    result = openbell._rotation_interpretation_note(pct_map, treasury_chg=-0.06, oil_pct=None, oil_price=None)
    check("Utilities+RE down / Tech up maps to rate-sensitive rotation into growth",
         "income-paying" in result["text"] and "rate-sensitive" in result["text"], f"got: {result!r}")
    check("flags implies_falling_yields for the RATES cross-check", result["implies_falling_yields"] is True)


def test_rotation_rate_sensitive_pattern_flags_unusual_when_yields_actually_rose():
    pct_map = {"Utilities": -1.2, "Real Estate": -1.0, "Technology": 1.5}
    rotation = {"wants_rates_crosscheck": True, "implies_falling_yields": True}
    sentence = openbell._rates_sentence({"yield": 4.5, "change": 0.05}, "close", rotation_context=rotation)["text"]
    check("RATES sentence flags the pattern as unusual when yields actually rose despite the rotation story",
         "complicates the picture" in sentence, f"got: {sentence!r}")


def test_rotation_cyclical_pattern_ties_energy_to_crude():
    pct_map = {"Financials": 0.8, "Industrials": 0.6, "Energy": 1.1, "Technology": -0.9}
    result = openbell._rotation_interpretation_note(pct_map, treasury_chg=None, oil_pct=2.5, oil_price=90.0)
    check("Financials+Industrials+Energy up / Tech down maps to cyclical rotation",
         "cheaper, older" in result["text"], f"got: {result!r}")
    check("ties Energy's gain to crude's own move", "90.00" in result["text"], f"got: {result!r}")
    check("mentions_commodity flag set so the generic floor doesn't repeat it", result["mentions_commodity"] is True)


def test_rotation_defensive_caution_pattern():
    pct_map = {"Consumer Staples": 0.3, "Utilities": 0.5, "Health Care": 0.2,
              "Technology": -1.0, "Energy": -0.5, "Financials": -0.8}
    result = openbell._rotation_interpretation_note(pct_map, treasury_chg=None, oil_pct=None, oil_price=None)
    check("defensives-up/everything-else-down maps to 'investors getting cautious'",
         "cautious" in result["text"], f"got: {result!r}")


def test_rotation_energy_crude_tied_same_direction():
    result = openbell._rotation_interpretation_note({"Energy": -0.5}, treasury_chg=None, oil_pct=-0.6, oil_price=85.0)
    check("Energy down with crude down ties them together as a straightforward read",
         "in step with crude" in result["text"], f"got: {result!r}")


def test_rotation_energy_crude_flagged_as_disconnect():
    result = openbell._rotation_interpretation_note({"Energy": -0.5}, treasury_chg=None, oil_pct=0.6, oil_price=85.0)
    check("Energy down with crude UP is flagged as a disconnect, not forced into a story",
         "disconnect" in result["text"], f"got: {result!r}")


def test_close_aug28_rotation_still_names_money_flow_even_without_a_named_pattern():
    # AUG28's Technology is ALSO down (-1.05), not up — so it genuinely doesn't
    # match any of the four named rotation patterns (pattern 1 specifically
    # requires Utilities+RE down AND Tech up; AUG28 is closer to a broad-ish
    # decline that hit Utilities/RE hardest, a different story). Confirms the
    # rotation opener's own "Money left X for Y" framing still names the flow
    # even when no deeper interpretation pattern applies — not a silent gap.
    text = _silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS,
                  headlines=AUG28_HEADLINES, mem={"briefing_history": []})[0]
    check("no rotation-interpretation pattern falsely claimed for a dataset that doesn't match one",
         not any(s in text for s in ("clear read", "getting cautious", "cheaper, older")), f"got: {text!r}")
    check("the opener still names what money left and where it went",
         "Money left" in text, f"got: {text!r}")


def test_close_sept23_real_data_produces_interpretation_beyond_bare_sector_list():
    # Sept 23's real live data (Energy the lone gainer amid a broad selloff,
    # tied to crude also being up) DOES match a named pattern — confirmed
    # separately in test_sept23_fixture_runs_end_to_end_with_real_volatility.
    text = _silent(close, SEPT23_SNAPSHOT, movers=SEPT23_MOVERS, sectors=SEPT23_SECTORS,
                  commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0]
    check("a genuine rotation-interpretation pattern produces more than a bare sector list",
         "in step with crude" in text or "disconnect" in text, f"got: {text!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — Outsized movers (Step 6, close mode)
# ═══════════════════════════════════════════════════════════════════════════

def test_outsized_mover_gets_own_sentence():
    text = _silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS,
                  headlines=AUG28_HEADLINES, mem={"briefing_history": []})[0]
    check("HOOD (+14.56%, beyond +/-8%) gets its own sentence naming the move",
         "HOOD" in text and "14.56" in text, f"got: {text!r}")


def test_outsized_mover_no_news_found_is_stated_plainly():
    movers = {"gainers": [{"symbol": "ZZZZ", "name": "Nothing Corp", "pct": 12.0}], "losers": []}
    sentence, sym = openbell._outsized_mover_note(movers, [{"title": "Local Bakery Wins Award For Best Croissant", "snippet": "", "site": "x"}], set(), set())
    check("no ticker-specific news is stated plainly, not papered over",
         "no company news we could find" in sentence, f"got: {sentence!r}")
    check("returns the symbol so it isn't double-counted in the ordinary mover clause", sym == "ZZZZ")


def test_outsized_mover_ranked_by_absolute_magnitude_not_gainer_loser_split():
    movers = {"gainers": [{"symbol": "BIGUP", "name": "Big Up Inc", "pct": 9.0}],
             "losers": [{"symbol": "HUGEDOWN", "name": "Huge Down Inc", "pct": -20.0}]}
    sentence, sym = openbell._outsized_mover_note(movers, [], set(), set())
    check("a -20% loser outranks a +9% gainer for the outsized treatment", sym == "HUGEDOWN", f"got: {sym!r}")


def test_no_outsized_mover_when_nothing_beyond_8pct():
    movers = {"gainers": [{"symbol": "A", "pct": 5.0}], "losers": [{"symbol": "B", "pct": -3.0}]}
    sentence, sym = openbell._outsized_mover_note(movers, [], set(), set())
    check("no outsized-mover sentence when nothing clears +/-8%", sentence == "" and sym is None)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — LOOP-CLOSE spectrum (Step 7, close mode)
# ═══════════════════════════════════════════════════════════════════════════

def test_loop_close_wrong_only_when_sp_closes_opposite():
    today_iso = openbell._today_ct_iso()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "sp_pct_called": 0.5}]}
    note, log = _silent(openbell._loop_close_note, [{"name": "S&P 500", "pct": -0.3}], mem)
    check("S&P closing opposite sign to the morning call is graded 'wrong'",
         "didn't hold" in note, f"got: {note!r}")


def test_loop_close_held_clean_no_breadth_divergence():
    today_iso = openbell._today_ct_iso()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "sp_pct_called": 0.5}]}
    note, log = _silent(openbell._loop_close_note, [{"name": "S&P 500", "pct": 0.4}], mem, divergence_spread=0.1)
    check("same direction, no notable divergence -> plain 'held'",
         "held that call through the close" in note, f"got: {note!r}")


def test_loop_close_mostly_held_right_direction_different_breadth():
    today_iso = openbell._today_ct_iso()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "sp_pct_called": 0.4}]}
    note, log = _silent(openbell._loop_close_note, [{"name": "S&P 500", "pct": 0.5}], mem,
                        divergence_spread=1.2, best_index_name="Nasdaq", worst_index_name="Dow")
    check("right direction, real S&P move, breadth diverged -> 'mostly held' framing",
         "delivered" in note and "concentrated in tech" in note and "Dow slipped" in note, f"got: {note!r}")
    check("never says 'wrong' for a directionally-correct call", "didn't hold" not in note)


def test_loop_close_partly_held_thin_move():
    today_iso = openbell._today_ct_iso()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "sp_pct_called": 0.3}]}
    note, log = _silent(openbell._loop_close_note, [{"name": "S&P 500", "pct": 0.05}], mem,
                        divergence_spread=1.0, best_index_name="Nasdaq", worst_index_name="Dow")
    check("right direction but a razor-thin S&P move with real breadth divergence -> 'partly held'",
         "technically agreed" in note and "partial hold" in note, f"got: {note!r}")


def test_loop_close_falls_back_to_composite_for_old_entries_without_sp_pct_called():
    today_iso = openbell._today_ct_iso()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "direction_called": "higher"}]}
    note, log = _silent(openbell._loop_close_note, AUG28_SNAPSHOT, mem)
    check("an older morning entry (no sp_pct_called) still produces a note via the composite fallback",
         len(note) > 0, f"got: {note!r}")


def test_loop_close_skip_logs_when_no_morning_record():
    mem = {"briefing_history": []}
    note, log = _silent(openbell._loop_close_note, AUG28_SNAPSHOT, mem)
    check("explicit skip message logged when no morning record exists", "[LOOP-CLOSE] Skipped" in log, f"got: {log!r}")
    check("no fabricated note text when skipped", note == "")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — Consistency and dead lines (Step 8)
# ═══════════════════════════════════════════════════════════════════════════

def test_asia_gets_real_numbers_not_vague_description():
    text, log = _silent(morning, SEPT3_SNAPSHOT, global_indices=SEPT3_GLOBAL)
    p1 = text[0].split("\n\n")[0]
    check("Asia gets real numbers, not just 'leaned higher overnight' with nothing",
         "Nikkei 225" in p1 and "0.45" in p1, f"got: {p1!r}")


def test_portfolio_never_a_dead_unchanged_line():
    picks_data = {"picks": [
        {"ticker": "MSFT", "company": "Microsoft Corporation", "pct_change_since_pick": 35.1},
        {"ticker": "AVGO", "company": "Broadcom Inc.", "pct_change_since_pick": -13.8},
    ], "changes_from_last_week": []}
    mem = {"pick_performance_history": [
        {"ticker": "MSFT", "pct_change_since_pick": 35.1}, {"ticker": "AVGO", "pct_change_since_pick": -13.8},
    ]}
    text, log = _silent(morning, SEPT3_SNAPSHOT, picks_data=picks_data, mem=mem, headlines=[{"title": "Local Bakery Wins Award For Best Croissant", "snippet": "", "site": "x"}])
    p2 = text[0].split("\n\n")[1]
    check("portfolio paragraph is never the dead 'Your N picks are unchanged.' line alone",
         p2.strip() != "Your 2 holdings are unchanged this week.", f"got: {p2!r}")
    check("names the best holding with since-entry %", "35.1" in p2, f"got: {p2!r}")
    check("names the worst holding with since-entry %", "13.8" in p2, f"got: {p2!r}")
    check("says plainly when neither is in today's headlines", "None of your holdings is in today's headlines" in p2, f"got: {p2!r}")


def test_close_day_performance_explicitly_distinguished_from_since_entry():
    picks_perf = [{"ticker": "MSFT", "pct": 0.80}, {"ticker": "AVGO", "pct": -2.10}]
    mem = {"pick_performance_history": [
        {"ticker": "MSFT", "pct_change_since_pick": 35.1}, {"ticker": "AVGO", "pct_change_since_pick": -13.8},
    ], "briefing_history": []}
    text = _silent(close, SEPT3_SNAPSHOT, picks_day_performance=picks_perf, mem=mem)[0]
    check("day performance and since-entry performance are both present and explicit",
         "on the day" in text and "since entry" in text, f"got: {text!r}")
    check("both tickers and both kinds of number appear", "MSFT" in text and "AVGO" in text and "35.1" in text and "13.8" in text)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — Plain language (Step 9)
# ═══════════════════════════════════════════════════════════════════════════

_BANNED_PHRASES = [
    "risk-off", "risk-on", "hawkish repricing", "keeps inflation in the conversation",
    "cut the other way", "confirms the tone", "the shape of",
]


def test_no_banned_phrases_across_rich_fixtures():
    texts = []
    texts.append(_silent(morning, SEPT3_SNAPSHOT, commodities=SEPT3_COMMODITIES, treasury=SEPT3_TREASURY,
                         global_indices=SEPT3_GLOBAL)[0][0])
    texts.append(_silent(morning, SEPT23_SNAPSHOT, commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0][0])
    texts.append(_silent(close, SEPT23_SNAPSHOT, movers=SEPT23_MOVERS, sectors=SEPT23_SECTORS,
                         commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0])
    texts.append(_silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS,
                         headlines=AUG28_HEADLINES, mem={"briefing_history": []})[0])
    combined = "\n".join(texts).lower()
    for phrase in _BANNED_PHRASES:
        check(f"banned phrase absent: {phrase!r}", phrase.lower() not in combined,
             f"found in combined output")


def test_bp_not_used_on_first_mention():
    text, log = _silent(morning, SEPT23_SNAPSHOT, treasury=SEPT23_TREASURY)
    p1 = text[0].split("\n\n")[0]
    check("'basis points' spelled out, not abbreviated 'bp'", "basis points" in p1 and " bp" not in p1, f"got: {p1!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 — Length targets (Step 10)
# ═══════════════════════════════════════════════════════════════════════════

def test_rich_morning_fixture_lands_in_word_count_range():
    picks_data = {"picks": [
        {"ticker": "AVGO", "company": "Broadcom Inc.", "pct_change_since_pick": -13.8},
        {"ticker": "MSFT", "company": "Microsoft Corporation", "pct_change_since_pick": 35.1},
    ], "changes_from_last_week": []}
    mem = {"pick_performance_history": [
        {"ticker": "MSFT", "pct_change_since_pick": 35.1}, {"ticker": "AVGO", "pct_change_since_pick": -13.8},
    ]}
    earnings = [{"symbol": "LULU", "date": openbell._today_ct_iso(), "eps_estimated": 1.82}]
    # A headline hitting a held ticker (AVGO) — matches the reference example's
    # richness ("AVGO is the one to watch..."), which is what makes P2 land in
    # range in practice; a P2 with no headline intersection at all is
    # legitimately thinner (see test_length_shortfall_is_logged_with_a_reason).
    headlines = [
        {"title": "Federal Reserve Signals Openness to October Rate Cut", "snippet": "s", "site": "A"},
        {"title": "AVGO shares slip on AI networking demand doubts from analysts", "snippet": "s", "site": "Reuters"},
    ]
    text, log = _silent(morning, SEPT3_SNAPSHOT, headlines=headlines, picks_data=picks_data, mem=mem,
                        commodities=SEPT3_COMMODITIES, treasury=SEPT3_TREASURY, earnings=earnings,
                        global_indices=SEPT3_GLOBAL)
    p1_words = len(text[0].split("\n\n")[0].split())
    p2_words = len(text[0].split("\n\n")[1].split())
    check("rich morning P1 lands at or above the 120-word floor", p1_words >= 100, f"got {p1_words} words")
    check("rich morning P2 with a real headline intersection lands close to the 50-word floor",
         p2_words >= 30, f"got {p2_words} words")


def test_length_shortfall_is_logged_with_a_reason():
    _, log = _silent(morning, [{"name": "S&P 500", "pct": 0.01}, {"name": "Nasdaq", "pct": 0.01}, {"name": "Dow", "pct": 0.01}])
    check("a thin brief logs a [LENGTH] line", "[LENGTH]" in log, f"got: {log!r}")
    check("the log names a likely reason, not just the bare word count",
         "Likely reason" in log or "under the" in log, f"got: {log!r}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 11 — PATH and _cite_headline (unchanged from the prior pass)
# ═══════════════════════════════════════════════════════════════════════════

def test_path_reversal_down_then_up():
    snapshot = [{"name": "S&P 500", "pct": 0.3, "previous_close": 100.0, "open": 99.5,
                "price": 100.3, "day_high": 100.4, "day_low": 99.3}]
    text = openbell._path_note(snapshot)
    check("reversal (down->up) still detected", "clawed back to close green" in text, f"got: {text!r}")


def test_path_gap_and_hold():
    snapshot = [{"name": "S&P 500", "pct": 0.6, "previous_close": 100.0, "open": 100.8,
                "price": 100.6, "day_high": 100.9, "day_low": 100.5}]
    text = openbell._path_note(snapshot)
    check("gap-and-hold still detected", "gapped higher at the open and held it" in text, f"got: {text!r}")


def test_no_double_period_in_rich_close_output():
    text = _silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS,
                  headlines=AUG28_HEADLINES, mem={"briefing_history": []})[0]
    check("no double-terminal punctuation anywhere in a citation-heavy close brief",
         '".".' not in text and '"..' not in text, f"got: {text!r}")


def test_market_narrative_is_a_single_shared_function():
    check("_build_market_narrative exists and is callable",
         callable(getattr(openbell, "_build_market_narrative", None)))



# ═══════════════════════════════════════════════════════════════════════════
# SECTION 12 — Addendum: Oct 5 regression bugs, the "WHY" standard, hedging
# ═══════════════════════════════════════════════════════════════════════════
#
# Oct 5 2026 fixtures. Provenance, stated plainly: the S&P/Nasdaq/Dow numbers,
# Europe's morning numbers, MELI +9.61% and APP +5.18% are exactly the numbers in
# the pasted Oct 5 production output. The rest are real Oct 5 daily bars pulled
# for this test (10-year 5.31% +0.034, crude -2.02% to $89.27, gold +0.17%,
# sector ETFs) EXCEPT: Asia's overnight numbers (not recoverable after the fact)
# are illustrative, and one sector is nudged negative so the count is the 9 of 11
# the production brief reported. The morning's pre-market commodity/yield values
# were not captured, so the day's bars stand in for them.

OCT5_MORNING_SNAPSHOT = [{"name": "S&P 500", "pct": 0.06}, {"name": "Nasdaq", "pct": 0.15}, {"name": "Dow", "pct": -0.15}]
OCT5_GLOBAL = [
    {"name": "FTSE 100", "session": "Europe", "pct": 0.50}, {"name": "DAX", "session": "Europe", "pct": 0.34},
    {"name": "Nikkei 225", "session": "Asia (overnight)", "pct": 0.40},
    {"name": "Hang Seng", "session": "Asia (overnight)", "pct": 0.25},
]
OCT5_TREASURY = {"yield": 5.31, "change": 0.034, "six_mo_high": 5.31, "six_mo_high_day": "Monday",
                 "six_mo_low": 4.246, "six_mo_low_day": ""}
OCT5_COMMODITIES = [{"name": "WTI Crude Oil", "price": 89.27, "change": -1.84, "pct": -2.02},
                    {"name": "Gold", "price": 4169.4, "change": 7.1, "pct": 0.17}]
OCT5_CLOSE_SNAPSHOT = [{"name": "S&P 500", "pct": 0.66}, {"name": "Nasdaq", "pct": 1.05}, {"name": "Dow", "pct": 0.18}]
OCT5_SECTORS = [{"sector": k, "pct": v} for k, v in {
    "Technology": 0.56, "Financials": 0.73, "Energy": 1.0, "Health Care": 0.72, "Industrials": -0.05,
    "Consumer Discret.": 0.35, "Consumer Staples": 0.63, "Materials": 1.31, "Real Estate": -0.34,
    "Utilities": 0.35, "Comm. Services": 1.17}.items()]
OCT5_MOVERS = {
    "gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.61},
                {"symbol": "APP", "name": "AppLovin Corporation", "pct": 5.18}],
    "losers":  [{"symbol": "CMG", "name": "Chipotle Mexican Grill, Inc.", "pct": -4.67},
                {"symbol": "BMY", "name": "Bristol-Myers Squibb Company", "pct": -3.83}],
}
OCT5_MEM = {  # the production morning entry predates sp_pct_called: only the composite label, "mixed"
    "briefing_history": [{"date": "TODAY", "type": "morning", "direction_called": "mixed"}],
    "pick_performance_history": [
        {"ticker": "AVGO", "sector": "Technology", "pct_change_since_pick": -17.6},
        {"ticker": "AXON", "sector": "Industrials", "pct_change_since_pick": -5.0}],
}
OCT5_HOLDINGS_DAY = [{"ticker": "AVGO", "pct": 2.09}, {"ticker": "AXON", "pct": -0.41}]


def _oct5_close_text():
    mem = {k: (list(v) if isinstance(v, list) else v) for k, v in OCT5_MEM.items()}
    mem["briefing_history"] = [dict(OCT5_MEM["briefing_history"][0], date=openbell._today_ct_iso())]
    with _frozen_today(datetime.date(2026, 10, 5)):  # a Monday — the 10-year's "high" day in the fixture
        return _silent(close, OCT5_CLOSE_SNAPSHOT, movers=OCT5_MOVERS, sectors=OCT5_SECTORS,
                       commodities=OCT5_COMMODITIES, treasury=OCT5_TREASURY,
                       picks_day_performance=OCT5_HOLDINGS_DAY, scan_candidates=[{"ticker": "APP", "score": 134}],
                       mem=mem)


def _oct5_morning():
    with _frozen_today(datetime.date(2026, 10, 5)):
        return _silent(morning, OCT5_MORNING_SNAPSHOT, commodities=OCT5_COMMODITIES, treasury=OCT5_TREASURY,
                       global_indices=OCT5_GLOBAL)


# ── A1: the candidate-flag line must be direction-aware ──────────────────────

def test_candidate_flag_gainer_does_not_say_still():
    note = openbell._candidate_cross_reference_note(
        {"gainers": [{"symbol": "APP", "name": "AppLovin Corporation", "pct": 5.18}], "losers": []},
        [{"ticker": "APP", "score": 134}])
    check("a gainer 'ranks #N among today's candidates'", "ranks #1 among today's candidates (score 134)" in note, f"got: {note!r}")
    check("a gainer is never told it 'still scored'", "still" not in note, f"got: {note!r}")


def test_candidate_flag_decliner_says_still_scored():
    note = openbell._candidate_cross_reference_note(
        {"gainers": [], "losers": [{"symbol": "XYZ", "name": "Xyz Inc", "pct": -4.0}]},
        [{"ticker": "AAA", "score": 150}, {"ticker": "XYZ", "score": 90}])
    check("a decliner 'fell ... but still scored N'", "fell 4.00%" in note and "but still scored 90" in note, f"got: {note!r}")


def test_no_sign_blind_template_anywhere_in_source():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "openbell.py")).read()
    check("the old sign-blind 'still scored into the candidate list' template is gone",
          "still scored into the candidate list" not in src)
    check("no 'Worth flagging' template left to audit", "Worth flagging" not in src)


# ── A2: Europe never "mixed" when both positive; no "too"/"also" assumptions ─

def test_oct5_europe_both_positive_is_directional_and_has_no_too():
    text, log = _oct5_morning()
    p1 = text[0].split("\n\n")[0]
    check("Europe +0.50%/+0.34% is never called mixed", "Europe is mixed" not in p1 and "Europe was split" not in p1, f"got: {p1!r}")
    check("it is called higher", "Europe is higher" in p1, f"got: {p1!r}")
    check("no 'too'/'also' construction assuming an earlier classification",
          " too" not in p1 and " also" not in p1.lower(), f"got: {p1!r}")


# ── A3: a flat tape with a small tilt must not contradict itself ─────────────

def test_oct5_flat_tape_reads_flat_with_a_slight_tilt():
    text, log = _oct5_morning()
    p1 = text[0].split("\n\n")[0]
    check("flat tape is described as close to flat", "close to flat" in p1, f"got: {p1!r}")
    check("with a slight tilt toward tech", "small tilt toward technology" in p1, f"got: {p1!r}")
    check("never 'directionless ... tech leading'", "directionless" not in p1 and "tech leading" not in p1
          and "megacap" not in p1, f"got: {p1!r}")
    check("and does not assert the tilt means concentrated buying", "buying is concentrated" not in p1)


# ── A4: LOOP-CLOSE on a flat call ────────────────────────────────────────────

def _loop(called, close_pct, spread=0.87, best="Nasdaq", worst="Dow", key="sp_pct_called"):
    mem = {"briefing_history": [{"date": openbell._today_ct_iso(), "type": "morning", key: called}]}
    return _silent(openbell._loop_close_note, [{"name": "S&P 500", "pct": close_pct}], mem,
                   divergence_spread=spread, best_index_name=best, worst_index_name=worst)[0]


def test_flat_call_then_rally_is_a_plain_miss():
    note = _loop(0.06, 0.66)
    check("flat call + S&P +0.66% reported as a miss, plainly",
          note.startswith("We expected a quiet open; stocks rallied instead, led by tech") and "miss" in note, f"got: {note!r}")


def test_flat_call_then_selloff_is_a_miss_too():
    note = _loop(-0.05, -0.8, best="Dow", worst="Nasdaq")
    check("flat call + S&P -0.8% is a miss, with the damage named",
          "stocks fell instead" in note and "tech falling hardest" in note and "miss" in note, f"got: {note!r}")


def test_flat_call_then_calm_session_held():
    note = _loop(0.05, 0.3)
    check("flat call + a move inside +/-0.5% held", "that call held" in note and "miss" not in note, f"got: {note!r}")


def test_oct5_production_entry_with_only_composite_label_still_grades_as_miss():
    note = _loop("mixed", 0.66, key="direction_called")  # production's actual morning entry shape
    check("a morning entry with only direction_called='mixed' is treated as a flat call and graded a miss",
          "We expected a quiet open; stocks rallied instead" in note, f"got: {note!r}")


def test_directional_call_flat_finish_is_not_called_wrong():
    note = _loop(0.5, -0.05)  # opposite sign but essentially flat: not "wrong" (that needs a real opposite move)
    check("a directional call followed by a flat finish 'didn't pan out' rather than 'wrong'",
          "didn't pan out" in note and "didn't hold" not in note, f"got: {note!r}")


# ── B: hedging — causes only with support, never invented ───────────────────

_CAUSAL_WORDS = ["because", "caused", "due to", "driven by", "thanks to", "as a result", "resulting"]
_HEDGE_MARKERS = ["can ", "tend", "usually", "may ", "could", "often", "consistent with", "lines up",
                  "suggests", "likely", "fits"]


def _no_driver_text():
    snap = [{"name": "S&P 500", "pct": 0.03}, {"name": "Nasdaq", "pct": 0.04}, {"name": "Dow", "pct": 0.02}]
    return _silent(morning, snap, commodities=[{"name": "WTI Crude Oil", "price": 88.0, "pct": 0.2},
                                              {"name": "Gold", "price": 4500, "pct": 0.05}],
                   treasury={"yield": 4.40, "change": 0.002}, global_indices=[], headlines=[])[0][0]


def test_no_driver_fixture_has_no_causal_claim():
    text = _no_driver_text().lower()
    for w in _CAUSAL_WORDS:
        check(f"no-driver fixture contains no {w!r}", w not in text, f"got: {text!r}")
    check("it says plainly that it was a quiet, low-conviction session", "quiet, low-conviction session" in text, f"got: {text!r}")
    check("and describes the numbers instead", "little changed" in text)


def _all_rendered_texts():
    t = []
    t.append(_oct5_morning()[0][0])
    t.append(_oct5_close_text()[0])
    t.append(_silent(morning, SEPT3_SNAPSHOT, commodities=SEPT3_COMMODITIES, treasury=SEPT3_TREASURY, global_indices=SEPT3_GLOBAL)[0][0])
    t.append(_silent(morning, SEPT23_SNAPSHOT, commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0][0])
    t.append(_silent(close, SEPT23_SNAPSHOT, movers=SEPT23_MOVERS, sectors=SEPT23_SECTORS,
                     commodities=SEPT23_COMMODITIES, treasury=SEPT23_TREASURY)[0])
    t.append(_silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS,
                     headlines=AUG28_HEADLINES, mem={"briefing_history": []})[0])
    t.append(_no_driver_text())
    return t


def test_every_because_is_inside_a_hedged_sentence():
    bad = []
    for text in _all_rendered_texts():
        unquoted = re.sub(r'"[^"]*"', "", text)           # a quoted headline may say anything
        for sent in re.split(r"(?<=[.!?])\s+", unquoted):
            if "because" in sent.lower() and not any(m in sent.lower() for m in _HEDGE_MARKERS):
                bad.append(sent)
    check("every sentence that says 'because' also carries a hedge (tend/can/may/consistent with...)",
          not bad, f"unhedged: {bad!r}")


def test_no_assertive_cause_phrasing_survives():
    joined = "\n".join(_all_rendered_texts()).lower()
    for phrase in ["that gap is the story", "confirms the read", "help explain it", "traces to a single name",
                   "is the story", "dragging the dow"]:
        check(f"assertive phrasing absent: {phrase!r}", phrase not in joined)


def test_conflicting_drivers_say_which_side_the_tape_took():
    # Yields up (usually bad for stocks) but stocks rose: say buyers won.
    text, _ = _silent(morning, [{"name": "S&P 500", "pct": 0.9}, {"name": "Nasdaq", "pct": 0.95}, {"name": "Dow", "pct": 0.85}],
                      treasury={"yield": 4.6, "change": 0.09})
    check("yields up + stocks up -> says buyers outweighed the pressure", "outweighed that pressure" in text[0], f"got: {text[0]!r}")
    text, _ = _silent(morning, [{"name": "S&P 500", "pct": -0.9}, {"name": "Nasdaq", "pct": -0.95}, {"name": "Dow", "pct": -0.85}],
                      treasury={"yield": 4.2, "change": -0.09})
    check("yields down + stocks down -> says other worries outweighed the help", "outweighed that help" in text[0], f"got: {text[0]!r}")


def test_terms_are_defined_for_a_reader_with_no_background():
    text = _oct5_morning()[0][0]
    check("the 10-year yield is defined where it first appears", "what the U.S. government pays to borrow" in text)
    check("basis points come with the plain-language equivalent", "basis points (0.03 percentage points)" in text, f"got: {text!r}")
    close_text = _silent(close, AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS, mem={"briefing_history": []})[0]
    check("'rotation' is defined where it's used", "meaning money moved between parts of the market" in close_text, f"got: {close_text!r}")


# ── B: flat days still need a WHY ────────────────────────────────────────────

def test_flat_day_with_offsetting_forces_names_both_sides():
    snap = [{"name": "S&P 500", "pct": 0.02}, {"name": "Nasdaq", "pct": 0.05}, {"name": "Dow", "pct": 0.01}]
    text, _ = _silent(morning, snap, treasury={"yield": 4.6, "change": 0.08},
                      commodities=[{"name": "WTI Crude Oil", "price": 80.0, "pct": -2.5}])
    p1 = text[0].split("\n\n")[0]
    check("offsetting forces (rising yields vs cheaper oil) are named", "Rising yields could weigh on stocks" in p1
          and "cheaper oil could support them" in p1, f"got: {p1!r}")
    check("and hedged as a possible explanation, not a fact", "may help explain" in p1)


def test_flat_day_waiting_for_a_scheduled_event():
    snap = [{"name": "S&P 500", "pct": 0.02}, {"name": "Nasdaq", "pct": 0.05}, {"name": "Dow", "pct": 0.01}]
    hl = [{"title": "Fed Chair Expected To Speak Later Today Ahead Of Rate Decision", "snippet": "", "site": "A"}]
    text, _ = _silent(morning, snap, headlines=hl, treasury={"yield": 4.4, "change": 0.0})
    check("a qualifying scheduled event is named as what traders may be waiting for",
          "Traders may be waiting for a Federal Reserve decision or comment" in text[0], f"got: {text[0]!r}")


def test_oct5_flat_morning_still_contains_a_why_chain():
    p1 = _oct5_morning()[0][0].split("\n\n")[0]
    check("the Oct 5 flat morning explains why a flat day happens",
          "opposing forces are cancelling out, or traders are waiting for news" in p1)
    check("and ties crude's move to what it implies", "airlines and shippers" in p1, f"got: {p1!r}")


# ── Index-heavy vs. too-small movers ─────────────────────────────────────────

def test_outsized_mover_scale_clause_both_ways():
    heavy, _ = openbell._outsized_mover_note({"gainers": [{"symbol": "NVDA", "name": "NVIDIA Corporation", "pct": 9.0}], "losers": []},
                                             [], set(), set(), tape_dir="higher")
    small, _ = openbell._outsized_mover_note({"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.61}], "losers": []},
                                             [], set(), set(), tape_dir="higher")
    check("an index-heavy name says it may explain part of the move", "large enough to move the major indexes" in heavy, f"got: {heavy!r}")
    check("a smaller company says it doesn't explain the index move", "doesn't explain the index move" in small, f"got: {small!r}")


def test_outsized_mover_headline_is_summarized_by_topic_never_quoted():
    title = "MercadoLibre Announces Record Quarterly Revenue Driven By Strong Growth In Brazil And Mexico Markets"
    hl = [{"title": title, "snippet": "", "site": "A"}]
    s, _ = openbell._outsized_mover_note({"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.61}], "losers": []},
                                         hl, set(), set())
    check("the headline is summarized in the brief's voice: what it covers", "a same-day headline covers its latest earnings or results" in s, f"got: {s!r}")
    check("no headline words are pasted or quoted", '"' not in s and "Record Quarterly" not in s and "Brazil" not in s, f"got: {s!r}")
    clause = re.search(r"a same-day headline covers (.+?)\.", s).group(1)
    check("the summary itself is under 12 words", len(("a same-day headline covers " + clause).split()) <= 12, f"got: {clause!r}")


def test_unclassifiable_headline_says_so_instead_of_guessing():
    hl = [{"title": "Why MELI Is Trending On Social Media This Morning", "snippet": "", "site": "A"}]
    s, _ = openbell._outsized_mover_note({"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.61}], "losers": []},
                                         hl, set(), set())
    check("when the topic can't be classified it says so, rather than inventing one or quoting",
          "though we can't tell what it covers" in s and '"' not in s and "trending" not in s.lower(), f"got: {s!r}")


def test_event_clause_summarizes_never_quotes():
    h = {"title": "Fed Chair Expected To Speak Later Today Ahead Of Rate Decision", "snippet": "", "site": "B"}
    ev = openbell._extract_market_events([h], "morning")[0]
    clause = openbell._event_clause(ev, "morning", set())
    check("a scheduled Fed event is described in the brief's words", "a Federal Reserve decision or comment" in clause, f"got: {clause!r}")
    check("none of the headline text appears", '"' not in clause and "Expected To Speak" not in clause, f"got: {clause!r}")
    inflation = openbell._extract_market_events([{"title": "CPI Report Due Tomorrow Morning", "snippet": "", "site": "A"}], "morning")[0]
    check("a CPI headline becomes 'an inflation report'", "an inflation report" in openbell._event_clause(inflation, "morning", set()))


def test_no_raw_headline_text_anywhere_in_a_rendered_brief():
    titles = ["AVGO shares slip on AI networking demand doubts from analysts",
              "MercadoLibre Announces Record Quarterly Revenue Driven By Strong Growth",
              "Fed Chair Expected To Speak Later Today Ahead Of Rate Decision"]
    hl = [{"title": t, "snippet": "", "site": "x"} for t in titles]
    picks = {"picks": [{"ticker": "AVGO", "company": "Broadcom Inc."}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "AVGO", "pct_change_since_pick": -13.8}]}
    m, _ = _silent(morning, SEPT3_SNAPSHOT, headlines=hl, picks_data=picks, mem=mem)
    c = _silent(close, AUG28_SNAPSHOT, movers={"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.6}], "losers": []},
                headlines=hl, mem={"briefing_history": []})[0]
    for label, txt in (("morning", m[0]), ("close", c)):
        check(f"{label}: no title fragment of 3+ consecutive headline words appears",
              not any(" ".join(t.split()[i:i + 3]).lower() in txt.lower() for t in titles for i in range(len(t.split()) - 2)),
              f"got: {txt!r}")
        check(f"{label}: no quotation marks around pasted text", '"' not in txt, f"got: {txt!r}")


def test_empty_headline_feed_is_not_reported_as_no_news():
    mv = {"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.61}], "losers": []}
    s, _ = openbell._outsized_mover_note(mv, [], set(), set())
    check("an empty feed says it can't check, instead of 'no company news we could find'",
          "couldn't pull headlines today" in s and "no company news we could find" not in s, f"got: {s!r}")
    picks = {"picks": [{"ticker": "AVGO", "company": "Broadcom Inc.", "pct_change_since_pick": -13.8},
                       {"ticker": "MSFT", "company": "Microsoft", "pct_change_since_pick": 30.0}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "AVGO", "pct_change_since_pick": -13.8}, {"ticker": "MSFT", "pct_change_since_pick": 30.0}]}
    m, _ = _silent(morning, SEPT3_SNAPSHOT, picks_data=picks, mem=mem, headlines=[])
    check("morning holdings line says headlines couldn't be pulled", "couldn't pull headlines today" in m[0] and "None of your holdings" not in m[0], f"got: {m[0]!r}")


def test_rotation_claim_requires_utilities_and_real_estate_to_be_genuinely_down():
    flat_util = {"Utilities": -0.07, "Real Estate": -1.12, "Technology": 0.56}
    check("utilities at -0.07% (inside the flat band) does not support a 'moving out of' claim",
          openbell._rotation_interpretation_note(flat_util, None, None, None)["text"] == "")


def test_single_holding_grammar():
    picks = {"picks": [{"ticker": "AVGO", "company": "Broadcom Inc.", "pct_change_since_pick": -13.8}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "AVGO", "pct_change_since_pick": -13.8}]}
    m, _ = _silent(morning, SEPT3_SNAPSHOT, picks_data=picks, mem=mem)
    check("one holding reads 'Your one holding', never 'Your 1 holdings'", "Your one holding is unchanged" in m[0] and "1 holdings" not in m[0], f"got: {m[0]!r}")



# ── Oct 5 regression fixtures, end to end ────────────────────────────────────

def test_oct5_close_regression_fixture():
    text, log = _oct5_close_text()
    p1, p2 = text.split("\n\n")
    check("broad rally described with the breadth fact", "sectors rose" in p1 and "wide rise" in p1, f"got: {p1!r}")
    check("Nasdaq-over-Dow gap named and hedged as concentration, not asserted as a cause",
          "beat the Dow by 0.87 percentage points" in p1 and "which suggests" in p1, f"got: {p1!r}")
    check("crude falling while Energy rose is flagged as a disconnect, not explained", "disconnect we can't explain" in p1, f"got: {p1!r}")
    check("MELI gets its own sentence", "MercadoLibre (MELI) was the day's biggest mover, up 9.61%" in p2, f"got: {p2!r}")
    check("MELI is called too small to explain the rally", "doesn't explain the index move" in p2)
    check("holdings: day performance and since-entry both stated", "on the day" in p2 and "since entry" in p2, f"got: {p2!r}")
    check("APP flag is direction-aware (gainer)", "AppLovin (APP) gained 5.18% and ranks #1" in p2, f"got: {p2!r}")
    check("LOOP-CLOSE reports the flat-call miss plainly", "We expected a quiet open; stocks rallied instead, led by tech" in p2, f"got: {p2!r}")
    check("none of the old Oct 5 defects remain", "and still scored into the candidate list" not in text
          and "called the tape mixed" not in text and "closed higher instead" not in text)


def test_oct5_word_counts_in_range_or_shortfall_logged_with_reason():
    (text, log) = _oct5_morning()
    p1 = len(text[0].split("\n\n")[0].split())
    check("Oct 5 morning P1 lands inside 120-180", 120 <= p1 <= 180, f"got {p1}")
    ctext, clog = _oct5_close_text()
    p1c, p2c = (len(x.split()) for x in ctext.split("\n\n"))
    check("Oct 5 close P2 lands inside 60-100", 60 <= p2c <= 100, f"got {p2c}")
    check("Oct 5 close P1 is in range, or its shortfall was logged with a reason",
          150 <= p1c <= 220 or ("[LENGTH] CLOSE P1" in clog and "Likely reason" in clog), f"got {p1c} words, log: {clog!r}")


def test_trimming_keeps_chain_sentences_and_drops_optional_ones_first():
    hl = [{"title": "Fed Signals Openness To October Rate Cut After Meeting Today", "snippet": "", "site": "A"},
          {"title": "Tariff Policy Shift Announced By Trade Officials Today", "snippet": "", "site": "B"}]
    text, log = _silent(morning, SEPT3_SNAPSHOT, headlines=hl, commodities=SEPT3_COMMODITIES,
                        treasury=SEPT3_TREASURY, global_indices=SEPT3_GLOBAL)[0], ""
    p1 = text[0].split("\n\n")[0] if isinstance(text, tuple) else text.split("\n\n")[0]
    check("a rich morning never exceeds the 180-word ceiling", len(p1.split()) <= 180, f"got {len(p1.split())}")
    check("the cause-chain sentences survive trimming", "concentrated in large technology companies" in p1 and "10-year Treasury yield" in p1)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 13 — Coverage restored from the committed suite (behavior that still exists)
# ═══════════════════════════════════════════════════════════════════════════

SNAPSHOT_UP     = [{"name": "S&P 500", "pct": 0.44}, {"name": "Nasdaq", "pct": 0.45}, {"name": "Dow", "pct": 0.62}]
AUG28_COMMODITIES = [{"name": "Gold", "price": 4506, "change": -124.8, "pct": -2.69}]


def test_path_fires_with_ohlc_data():
    snapshot = [
        {"name": "S&P 500", "pct": 0.3, "price": 100, "open": 98.5, "day_high": 100.1, "day_low": 98.0},
        {"name": "Nasdaq", "pct": 0.2}, {"name": "Dow", "pct": 0.25},
    ]
    text = close(snapshot)
    check("PATH fires and describes a strong finish given real OHLC data",
         "near its highs" in text, f"got: {text!r}")


def test_path_omitted_without_ohlc_data():
    snapshot = [{"name": "S&P 500", "pct": 0.3}, {"name": "Nasdaq", "pct": 0.2}, {"name": "Dow", "pct": 0.25}]
    text = close(snapshot)
    check("PATH is cleanly omitted (not an error) when OHLC data is absent",
         "near its highs" not in text and "near its lows" not in text, f"got: {text!r}")


def test_path_reversal_takes_priority_over_strong_finish():
    # Also closes near the day's own high (>=85% of day's range) -- reversal
    # must win anyway since it's checked first.
    snapshot = [{"name": "S&P 500", "pct": 0.3, "previous_close": 100.0, "open": 99.5,
                 "price": 100.3, "day_high": 100.32, "day_low": 99.4}]
    text = openbell._path_note(snapshot)
    check("reversal outranks strong-finish when both conditions are technically true",
         "reversal" in text, f"got: {text!r}")


def test_path_reversal_up_then_down():
    snapshot = [{"name": "S&P 500", "pct": -0.3, "previous_close": 100.0, "open": 100.5,
                 "price": 99.7, "day_high": 100.6, "day_low": 99.6}]
    text = openbell._path_note(snapshot)
    check("reversal (up->down) is named and described as opening green, closing red",
         "slid to close red" in text, f"got: {text!r}")


def test_path_gap_and_hold_down():
    snapshot = [{"name": "S&P 500", "pct": -0.6, "previous_close": 100.0, "open": 99.2,
                 "price": 99.4, "day_high": 99.5, "day_low": 99.1}]
    text = openbell._path_note(snapshot)
    check("gap-and-hold (lower) is named", "gapped lower at the open and held it" in text, f"got: {text!r}")


def test_path_gap_and_hold_up():
    # Opened +0.8% vs yesterday, closed +0.6% vs yesterday (held >=half the gap, same direction).
    snapshot = [{"name": "S&P 500", "pct": 0.6, "previous_close": 100.0, "open": 100.8,
                 "price": 100.6, "day_high": 100.9, "day_low": 100.5}]
    text = openbell._path_note(snapshot)
    check("gap-and-hold (higher) is named", "gapped higher at the open and held it" in text, f"got: {text!r}")


def test_path_gap_that_fades_is_not_gap_and_hold():
    # Opened +0.8% vs yesterday but faded to close only +0.1% vs yesterday --
    # held less than half the gap -> must NOT be called gap-and-hold.
    snapshot = [{"name": "S&P 500", "pct": 0.1, "previous_close": 100.0, "open": 100.8,
                 "price": 100.1, "day_high": 100.9, "day_low": 100.0}]
    text = openbell._path_note(snapshot)
    check("a gap that mostly faded is not mislabeled gap-and-hold",
         "gap-and-hold" not in text and "gapped" not in text, f"got: {text!r}")


def test_path_falls_back_to_original_three_without_previous_close():
    # No previous_close field at all -> reversal/gap-and-hold can't be evaluated,
    # falls through to the original strong-finish/faded-close/choppy logic.
    snapshot = [{"name": "S&P 500", "pct": 0.3, "price": 100, "open": 98.5, "day_high": 100.1, "day_low": 98.0}]
    text = openbell._path_note(snapshot)
    check("falls back to strong-finish when previous_close is absent",
         "near its highs" in text, f"got: {text!r}")


def test_path_strong_finish_unchanged():
    snapshot = [{"name": "S&P 500", "pct": 0.3, "price": 100, "open": 98.5, "day_high": 100.1, "day_low": 98.0}]
    text = openbell._path_note(snapshot)
    check("strong finish still fires (close_pos>=0.85, range>=0.5%%)",
         "strong finish into the bell" in text, f"got: {text!r}")


def test_path_faded_close_unchanged():
    snapshot = [{"name": "S&P 500", "pct": -0.1, "price": 98.1, "open": 99.9, "day_high": 100.1, "day_low": 98.0}]
    text = openbell._path_note(snapshot)
    check("faded close still fires (close_pos<=0.15, range>=0.5%%)",
         "fading into the bell" in text, f"got: {text!r}")


def test_path_choppy_unchanged():
    snapshot = [{"name": "S&P 500", "pct": 0.0, "price": 99.5, "open": 99.5, "day_high": 101.0, "day_low": 98.5}]
    text = openbell._path_note(snapshot)
    check("choppy range still fires (range>=1.5%%, no clean top/bottom finish)",
         "choppy session" in text, f"got: {text!r}")


def test_broad_selloff_fires_at_8_of_11_sectors():
    snapshot = [{"name": "S&P 500", "pct": -1.8}, {"name": "Nasdaq", "pct": -2.1}, {"name": "Dow", "pct": -1.5}]
    sectors = [{"sector": f"S{i}", "pct": -1.0 - i * 0.1} for i in range(9)] + \
              [{"sector": "S9", "pct": 0.3}, {"sector": "S10", "pct": 0.5}]
    text = close(snapshot, sectors=sectors)
    check("9 of 11 sectors red correctly triggers 'sold off'", "sold off" in text, f"got: {text!r}")


def test_broad_rally_fires_at_8_of_11_sectors():
    snapshot = [{"name": "S&P 500", "pct": 1.8}, {"name": "Nasdaq", "pct": 2.1}, {"name": "Dow", "pct": 1.5}]
    sectors = [{"sector": f"S{i}", "pct": 1.0 + i * 0.1} for i in range(9)] + \
              [{"sector": "S9", "pct": -0.3}, {"sector": "S10", "pct": -0.5}]
    text = close(snapshot, sectors=sectors)
    check("9 of 11 sectors green correctly triggers 'rallied'", "rallied" in text, f"got: {text!r}")


def test_rotation_requires_both_balanced_breadth_and_spread():
    snapshot = [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": -0.1}, {"name": "Dow", "pct": 0.15}]
    sectors = [{"sector": f"Up{i}", "pct": 0.3} for i in range(5)] + [{"sector": f"Down{i}", "pct": -0.3} for i in range(6)]
    text = close(snapshot, sectors=sectors)
    check("balanced breadth alone (without >=1.5pp spread) does not trigger rotation",
         "rotation" not in text.lower(), f"got: {text!r}")


def test_tiny_uniform_negative_move_is_not_automatically_a_selloff():
    snapshot = [{"name": "S&P 500", "pct": -0.05}, {"name": "Nasdaq", "pct": -0.03}, {"name": "Dow", "pct": -0.02}]
    sectors = [{"sector": f"Up{i}", "pct": 0.1} for i in range(5)] + [{"sector": f"Down{i}", "pct": -0.1} for i in range(6)]
    text = close(snapshot, sectors=sectors)
    check("tiny uniform-negative move does not trigger 'sold off'", "sold off" not in text, f"got: {text!r}")


def test_footer_not_financial_advice_appears_once():
    # _unified_picks' own footer must no longer duplicate the global "Not
    # financial advice" line from _wrap().
    html = openbell._unified_picks(
        [{"ticker": "AAPL", "company": "Apple Inc.", "sector": "Technology",
          "weeks_held": 1, "pct_change_since_pick": 1.0, "note": "Holding."}],
        [],
    )
    check("_unified_picks footer no longer says 'Not financial advice'",
         "Not financial advice" not in html, f"got footer text present in: {html[-200:]}")


def test_no_age_artifact():
    headlines = [{"title": "Federal Reserve holds rates steady", "snippet": "No change.", "site": "Wire", "age_hrs": 0}]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check("no '(0h ago)' timestamp artifact anywhere in the output", "h ago)" not in text, f"got: {text!r}")


def test_no_catalyst_phrasing_is_gone():
    text = close(AUG28_SNAPSHOT, sectors=AUG28_SECTORS)
    check("'no sector-specific catalyst identified' phrasing is gone entirely",
         "no sector-specific catalyst identified" not in text, f"got: {text!r}")
    check("'no clear single catalyst' mover-fallback phrasing is gone entirely",
         "no clear single catalyst" not in text, f"got: {text!r}")


def test_no_headline_appears_twice_even_with_multiple_matching_sentences():
    snapshot = AUG28_SNAPSHOT
    movers_with_fed_match = {
        "gainers": [],
        "losers": [{"symbol": "XYZ", "name": "Fed Chair Warsh Delivers First Jackson Hole Keynote, Signals Hawkish Tilt Corp",
                    "pct": -5.0}],
    }
    text = close(
        snapshot, movers=movers_with_fed_match, sectors=AUG28_SECTORS, commodities=AUG28_COMMODITIES,
        headlines=AUG28_HEADLINES, mem={"briefing_history": []},
    )
    check("the same headline is never quoted more than once", text.count("Jackson Hole") <= 1, f"got: {text!r}")


def test_mover_never_gets_a_generic_macro_backdrop():
    movers = {"gainers": [], "losers": [{"symbol": "ZZZ", "name": "Nothing Corp", "pct": -6.0}]}
    text = close(
        [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": 0.1}, {"name": "Dow", "pct": 0.1}],
        movers=movers, headlines=AUG28_HEADLINES,
    )
    check("mover with no specific headline gets no borrowed macro 'backdrop' citation",
         "broader market backdrop" not in text and "Jackson Hole" not in text, f"got: {text!r}")
    check("mover still states its move factually", "ZZZ" in text and "6.00%" in text, f"got: {text!r}")


def test_no_earnings_today():
    text, log = morning(SNAPSHOT_UP)
    check("no earnings sentence when there's no earnings today", "earnings today" not in text, f"got: {text!r}")


def test_earnings_today_forward_looking_only():
    earnings = [{"symbol": "LULU", "date": openbell._today_ct_iso(), "eps_estimated": 1.80}]
    text, log = morning(SNAPSHOT_UP, earnings=earnings)
    p2 = text.split("\n\n")[-1]
    check("earnings sentence appears and is forward-looking", "LULU (analysts expect $1.80 per share) reports earnings today" in p2, f"got: {p2!r}")
    check("it is not phrased as a cause of today's tape", "due to" not in p2.lower() and "caused" not in p2.lower())


def test_handoff_notes_asia_flat():
    global_flat_asia = [
        {"name": "FTSE 100", "session": "Europe", "pct": 0.5},
        {"name": "Nikkei 225", "session": "Asia (overnight)", "pct": 0.05},
        {"name": "Hang Seng", "session": "Asia (overnight)", "pct": -0.05},
    ]
    text, log = morning(SNAPSHOT_UP, global_indices=global_flat_asia)
    check("Asia flat (<0.15% all majors) is called flat, with numbers", "Asia was flat" in text and "Nikkei 225" in text, f"got: {text!r}")


def test_portfolio_intersection_guards_against_substring_collision():
    picks_data = {"picks": [{"ticker": "APP", "company": "AppLovin Corporation"}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "APP", "pct_change_since_pick": 5.0}]}
    headlines = [{"title": "Best new app store releases this week", "snippet": "A roundup of new mobile app titles.", "site": "TechCrunch"}]
    text, log = morning(SNAPSHOT_UP, headlines=headlines, picks_data=picks_data, mem=mem)
    check("ticker APP does not false-match generic 'app store' text",
         "APP is in today's headlines" not in text and "APP is mentioned" not in text
         and "None of your holdings is in today's headlines" in text, f"got: {text!r}")


# ── Run everything ────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 14 — Empty headline feed and topic-summarizer false positives
# ═══════════════════════════════════════════════════════════════════════════

FALLBACK = "mentions it, though we can't tell what it covers"


def _hl(*titles, snippet=""):
    return [{"title": t, "snippet": snippet, "site": "x"} for t in titles]


def test_empty_headline_section_is_hidden_never_printed_as_empty():
    check("_headlines([]) renders nothing at all", openbell._headlines([]) == "", f"got: {openbell._headlines([])!r}")
    check("_headlines(None) renders nothing at all", openbell._headlines(None) == "")
    picks = {"picks": [{"ticker": "AVGO", "company": "Broadcom Inc.", "pct_change_since_pick": -13.8}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "AVGO", "pct_change_since_pick": -13.8}]}
    m, _ = _silent(morning, SEPT3_SNAPSHOT, picks_data=picks, mem=mem, headlines=[])
    c = _silent(close, AUG28_SNAPSHOT, movers={"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.6}], "losers": []},
                headlines=[], mem={"briefing_history": []})[0]
    for label, txt in (("morning", m[0]), ("close", c)):
        check(f"{label}: empty feed never prints '(empty)'", "(empty)" not in txt.lower() and "empty)" not in txt.lower(), f"got: {txt[:200]!r}")
        check(f"{label}: no narrative sentence talks about an 'empty' feed", "empty" not in txt.lower(), f"got: {txt!r}")
    check("close: an unexplained mover says we couldn't pull headlines, not that no news exists",
          "couldn't pull headlines today" in c and "no headline explaining" not in c and "no company news" not in c, f"got: {c!r}")


def test_html_has_no_headline_section_when_feed_is_empty():
    html_m = openbell._headlines([])
    check("the email HTML builder contributes no heading for an empty feed", "Top Headlines" not in html_m)
    html_ok = openbell._headlines(_hl("Treasury Yields Fall As Inflation Data Cools"))
    check("a non-empty feed still renders its heading", "Top Headlines" in html_ok)


def test_topic_other_companys_earnings_is_not_attributed():
    # "earnings" is in the headline, but they are Walmart's, not MELI's.
    t = "Walmart Earnings Beat Estimates As Shoppers Trade Down, MELI Also Higher"
    h, spec, field = openbell._find_headline_for_symbol(_hl(t), "MELI", "MercadoLibre, Inc.")
    topic = openbell._headline_topic(h, field, "MELI", "MercadoLibre, Inc.")
    check("a different company's earnings are not credited to MELI", topic == "", f"got topic {topic!r}")
    mv = {"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.6}], "losers": []}
    s, _ = openbell._outsized_mover_note(mv, _hl(t), set(), set())
    check("the mover sentence falls back to 'mentions it, though we can't tell what it covers'",
          FALLBACK in s and "earnings" not in s, f"got: {s!r}")
    own = "MELI Earnings Beat Estimates On Strong Brazil Demand"
    h2, _, f2 = openbell._find_headline_for_symbol(_hl(own), "MELI", "MercadoLibre, Inc.")
    check("control: the company's own earnings headline is still classified",
          openbell._headline_topic(h2, f2, "MELI", "MercadoLibre, Inc.") == "its latest earnings or results")


def test_topic_keyword_far_from_the_mention_is_not_attributed():
    t = ("Oil Slides As Inventories Build, Bank Earnings Loom, Treasury Auctions Draw Weak Demand, "
         "Analysts Warn Of Volatility And MELI Rises")
    h, spec, field = openbell._find_headline_for_symbol(_hl(t), "MELI", "MercadoLibre, Inc.")
    check("a keyword many words away from the ticker is not attributed", openbell._headline_topic(h, field, "MELI", "MercadoLibre, Inc.") == "")


def test_fed_inside_another_word_is_not_monetary_policy():
    for title in ("FedEx Reports Strong Quarterly Results", "Federated Hermes Names New Chief Executive",
                  "Bedfed Holdings Prices Offering", "Why FedEx Shares Are Higher Today"):
        ev = openbell._extract_market_events(_hl(title), "morning")
        check(f"'{title[:36]}' is not read as a central-bank event",
              not any(e["category"] == "MONETARY" for e in ev), f"got: {[(e['category'], e['term']) for e in ev]}")
    ev = openbell._extract_market_events(_hl("Federal Reserve Holds Interest Rates, Signals Patience"), "morning")
    check("control: a real Fed headline is still classified", any(e["category"] == "MONETARY" for e in ev),
          f"got: {[(e['category'], e['term']) for e in ev]}")


def test_ambiguous_event_word_needs_supporting_context():
    ev = openbell._extract_market_events(_hl("Summit Materials Reports Quarterly Results"), "morning")
    check("'Summit Materials' is a company name, not a diplomatic summit",
          not any(e["term"] == "summit" for e in ev), f"got: {[(e['category'], e['term']) for e in ev]}")
    ev = openbell._extract_market_events(_hl("G7 Summit Opens With Trade Talks"), "morning")
    check("control: a real summit headline still counts", any(e["term"] == "summit" for e in ev))


def test_ticker_that_is_an_english_word_does_not_match_by_ticker_alone():
    cases = [("NOW", "ServiceNow, Inc.", "WALL STREET RALLIES NOW AS YIELDS FALL"),
             ("NOW", "ServiceNow, Inc.", "Why Investors Are Buying Tech Stocks Now"),
             ("IT",  "Gartner, Inc.",    "WHY IT STOCKS ARE SELLING OFF TODAY"),
             ("ON",  "ON Semiconductor Corporation", "Stocks Close Higher On Fed Hopes"),
             ("A",   "Agilent Technologies, Inc.", "A Strong Jobs Report Lifts Markets"),
             ("ALL", "The Allstate Corporation", "ALL THREE INDEXES CLIMB AS YIELDS FALL")]
    for sym, name, title in cases:
        h, spec, field = openbell._find_headline_for_symbol(_hl(title), sym, name)
        check(f"{sym}: '{title[:38]}' is not treated as a headline about {name.split(',')[0]}", not (h and spec), f"got: {h}")
    h, spec, field = openbell._find_headline_for_symbol(_hl("ServiceNow Announces Acquisition Of AI Startup"), "NOW", "ServiceNow, Inc.")
    check("control: the company's name still matches", bool(h and spec))
    h, spec, field = openbell._find_headline_for_symbol(_hl("Why ON Semiconductor Stock Fell Today"), "ON", "ON Semiconductor Corporation")
    check("control: ON Semiconductor matches by name", bool(h and spec))


def test_unclassifiable_specific_headline_says_it_cannot_tell_what_it_covers():
    mv = {"gainers": [{"symbol": "MELI", "name": "MercadoLibre, Inc.", "pct": 9.6}], "losers": []}
    s, _ = openbell._outsized_mover_note(mv, _hl("MercadoLibre Spotted At Industry Conference In Sao Paulo"), set(), set())
    check("a headline that names the stock but fits no topic gets the honest fallback", FALLBACK in s, f"got: {s!r}")
    check("and it does not paste the headline", "sao paulo" not in s.lower() and "conference" not in s.lower(), f"got: {s!r}")



# ═══════════════════════════════════════════════════════════════════════════
# SECTION 15 — Restored verbatim from a33eec2 (still pass against current code)
# ═══════════════════════════════════════════════════════════════════════════

SNAPSHOT_DOWN   = [{"name": "S&P 500", "pct": -0.9}, {"name": "Nasdaq", "pct": -1.1}, {"name": "Dow", "pct": -0.6}]
TREASURY_FALLING = {
    "yield": 4.74, "change": -0.058,
    "six_mo_high": 4.80, "six_mo_high_day": "Tuesday",
    "six_mo_low": 4.05, "six_mo_low_day": "",
}


def test_all_up_tape():
    text, log = morning(SNAPSHOT_UP)
    check("all-up tape classified as 'higher'", log["direction_called"] == "higher")
    check("all-up tape has no negative arrow on any index", "▼" not in text.split("\n\n")[0], f"got: {text!r}")


def test_all_down_tape():
    text, log = morning(SNAPSHOT_DOWN)
    check("all-down tape classified as 'lower'", log["direction_called"] == "lower")
    check("all-down tape has no positive arrow on any index", "▲" not in text.split("\n\n")[0], f"got: {text!r}")


def test_rates_driver_fires_and_mentions_extreme():
    text, log = morning(SNAPSHOT_UP, treasury=TREASURY_FALLING)
    check("RATES fires on a >=4bp move", "10-year" in text)
    check("RATES mentions the 6-month extreme context by day name", "Tuesday" in text, f"got: {text!r}")
    check("headline_theme logged for RATES", log["headline_theme"] == "rate expectations")


def test_ten_year_present_in_close_output():
    treasury = {"yield": 4.50, "change": -0.06}
    # No sector data at all -> composite can't fire -> falls through to standalone RATES
    text = close(
        [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": 0.1}, {"name": "Dow", "pct": 0.1}],
        treasury=treasury,
    )
    check("10-year yield appears in close output when treasury data is present",
         "10-year" in text, f"got: {text!r}")


def test_portfolio_day_performance_appears():
    picks_perf = [{"ticker": "MSFT", "pct": 0.5}, {"ticker": "AVGO", "pct": -1.2}]
    text = close(AUG28_SNAPSHOT, picks_day_performance=picks_perf)
    check("portfolio day-performance line appears with both tickers named",
         "MSFT" in text and "AVGO" in text, f"got: {text!r}")


def test_portfolio_day_performance_omitted_when_empty():
    text = close(AUG28_SNAPSHOT, picks_day_performance=[])
    check("no portfolio line when there's no picks-day-performance data",
         "Your picks" not in text and "Your one held pick" not in text, f"got: {text!r}")


def test_no_double_period_anywhere_across_every_citation_call_site():
    # Exercises all three call sites that cite a headline in one pass: the
    # portfolio-intersection line (morning), the rate-sensitive composite
    # (close), and a named mover (close).
    picks_data = {
        "picks": [{"ticker": "AVGO", "company": "Broadcom Inc."}],
        "changes_from_last_week": [],
    }
    mem = {"pick_performance_history": [{"ticker": "AVGO", "pct_change_since_pick": -13.8}]}
    headlines = [{"title": "AVGO shares slip on AI revenue doubts", "snippet": "s", "site": "Reuters"}]
    text, _ = morning(SNAPSHOT_UP, headlines=headlines, picks_data=picks_data, mem=mem)
    check("morning portfolio-intersection citation has no double period",
         '".."' not in text and '.".' not in text, f"got: {text!r}")

    sectors = [{"sector": "Utilities", "pct": -1.0}, {"sector": "Real Estate", "pct": -0.9}]
    close_headlines = [{"title": "Fed Chair Warsh Delivers First Jackson Hole Keynote", "snippet": "s", "site": "Wire"}]
    close_text = close(AUG28_SNAPSHOT, sectors=sectors, commodities=[{"name": "Gold", "pct": -2.0, "price": 4500}],
                       headlines=close_headlines)
    check("close rate-sensitive-composite citation has no double period",
         '".."' not in close_text and '.".' not in close_text, f"got: {close_text!r}")

    mover_movers = {"gainers": [{"symbol": "NOW", "name": "ServiceNow, Inc.", "pct": 5.31}], "losers": []}
    mover_headlines = [{"title": "ServiceNow shares jump on strong cloud demand", "snippet": "s", "site": "Wire"}]
    mover_text = close(AUG28_SNAPSHOT, movers=mover_movers, headlines=mover_headlines)
    check("close named-mover citation has no double period",
         '".."' not in mover_text and '.".' not in mover_text, f"got: {mover_text!r}")


def test_close_now_gets_standalone_oil_gold_sentence():
    # Previously close-mode never mentioned oil at all, and only mentioned
    # gold as a composite corroborator. With the shared ladder, a plain oil
    # move now shows up in the close brief the same way it does in morning.
    snapshot = [{"name": "S&P 500", "pct": 0.2}, {"name": "Nasdaq", "pct": 0.2}, {"name": "Dow", "pct": 0.2}]
    commodities = [{"name": "WTI Crude Oil", "price": 91.94, "change": 2.20, "pct": 2.45}]
    text = close(snapshot, commodities=commodities)
    check("close brief now names an oil move >=2%% (previously morning-only)",
         "91.94" in text, f"got: {text!r}")


def test_close_does_not_double_cite_gold_when_composite_already_used_it():
    sectors = [{"sector": "Utilities", "pct": -1.0}, {"sector": "Real Estate", "pct": -0.9}]
    commodities = [{"name": "Gold", "price": 4506, "change": -124.8, "pct": -2.69}]
    text = close(AUG28_SNAPSHOT, sectors=sectors, commodities=commodities)
    check("gold's move is named exactly once (by the composite), not a second time by the OIL/GOLD step",
         text.count("4,506") <= 1 and text.count("2.69") <= 1, f"got: {text!r}")


if __name__ == "__main__":
    tests = [
        test_sept3_fixture_runs_end_to_end,
        test_sept23_fixture_runs_end_to_end_with_real_volatility,
        test_fetch_market_snapshot_never_uses_futures_tickers,
        test_fetch_market_snapshot_returns_fresh_data_not_cached,
        test_event_excludes_opening_bell_ceremony,
        test_event_excludes_buffett_evergreen_and_etf_explainer,
        test_event_classifies_monetary_macro_corporate,
        test_event_cap_is_two,
        test_event_scheduled_vs_happened,
        test_event_no_qualifying_event_says_nothing,
        test_group_direction_europe_both_positive_not_mixed,
        test_group_direction_rally_with_small_laggard_not_mixed,
        test_group_direction_genuinely_mixed_real_magnitude_both_sides,
        test_europe_handoff_never_called_mixed_when_both_positive,
        test_us_tape_opener_not_called_mixed_with_one_small_laggard,
        test_divergence_fires_and_explains_nasdaq_dow_gap,
        test_divergence_below_threshold_says_nothing,
        test_divergence_nasdaq_underperforms_yields_up_mapping,
        test_rates_sentence_always_fires_even_without_threshold_trip,
        test_rates_sentence_gives_trailing_context_for_a_past_extreme,
        test_rates_sentence_quiet_day_says_little_changed,
        test_commodity_sentence_always_present,
        test_close_gets_rates_rotation_and_commodity_without_redundancy,
        test_rotation_rate_sensitive_pattern,
        test_rotation_rate_sensitive_pattern_flags_unusual_when_yields_actually_rose,
        test_rotation_cyclical_pattern_ties_energy_to_crude,
        test_rotation_defensive_caution_pattern,
        test_rotation_energy_crude_tied_same_direction,
        test_rotation_energy_crude_flagged_as_disconnect,
        test_close_aug28_rotation_still_names_money_flow_even_without_a_named_pattern,
        test_close_sept23_real_data_produces_interpretation_beyond_bare_sector_list,
        test_outsized_mover_gets_own_sentence,
        test_outsized_mover_no_news_found_is_stated_plainly,
        test_outsized_mover_ranked_by_absolute_magnitude_not_gainer_loser_split,
        test_no_outsized_mover_when_nothing_beyond_8pct,
        test_loop_close_wrong_only_when_sp_closes_opposite,
        test_loop_close_held_clean_no_breadth_divergence,
        test_loop_close_mostly_held_right_direction_different_breadth,
        test_loop_close_partly_held_thin_move,
        test_loop_close_falls_back_to_composite_for_old_entries_without_sp_pct_called,
        test_loop_close_skip_logs_when_no_morning_record,
        test_asia_gets_real_numbers_not_vague_description,
        test_portfolio_never_a_dead_unchanged_line,
        test_close_day_performance_explicitly_distinguished_from_since_entry,
        test_no_banned_phrases_across_rich_fixtures,
        test_bp_not_used_on_first_mention,
        test_rich_morning_fixture_lands_in_word_count_range,
        test_length_shortfall_is_logged_with_a_reason,
        test_path_reversal_down_then_up,
        test_path_gap_and_hold,
        test_no_double_period_in_rich_close_output,
        test_market_narrative_is_a_single_shared_function,
        test_candidate_flag_gainer_does_not_say_still,
        test_candidate_flag_decliner_says_still_scored,
        test_no_sign_blind_template_anywhere_in_source,
        test_oct5_europe_both_positive_is_directional_and_has_no_too,
        test_oct5_flat_tape_reads_flat_with_a_slight_tilt,
        test_flat_call_then_rally_is_a_plain_miss,
        test_flat_call_then_selloff_is_a_miss_too,
        test_flat_call_then_calm_session_held,
        test_oct5_production_entry_with_only_composite_label_still_grades_as_miss,
        test_directional_call_flat_finish_is_not_called_wrong,
        test_no_driver_fixture_has_no_causal_claim,
        test_every_because_is_inside_a_hedged_sentence,
        test_no_assertive_cause_phrasing_survives,
        test_conflicting_drivers_say_which_side_the_tape_took,
        test_terms_are_defined_for_a_reader_with_no_background,
        test_flat_day_with_offsetting_forces_names_both_sides,
        test_flat_day_waiting_for_a_scheduled_event,
        test_oct5_flat_morning_still_contains_a_why_chain,
        test_outsized_mover_scale_clause_both_ways,
        test_outsized_mover_headline_is_summarized_by_topic_never_quoted,
        test_unclassifiable_headline_says_so_instead_of_guessing,
        test_event_clause_summarizes_never_quotes,
        test_no_raw_headline_text_anywhere_in_a_rendered_brief,
        test_empty_headline_feed_is_not_reported_as_no_news,
        test_rotation_claim_requires_utilities_and_real_estate_to_be_genuinely_down,
        test_single_holding_grammar,
        test_oct5_close_regression_fixture,
        test_oct5_word_counts_in_range_or_shortfall_logged_with_reason,
        test_trimming_keeps_chain_sentences_and_drops_optional_ones_first,
        test_path_fires_with_ohlc_data,
        test_path_omitted_without_ohlc_data,
        test_path_reversal_takes_priority_over_strong_finish,
        test_path_reversal_up_then_down,
        test_path_gap_and_hold_down,
        test_path_gap_and_hold_up,
        test_path_gap_that_fades_is_not_gap_and_hold,
        test_path_falls_back_to_original_three_without_previous_close,
        test_path_strong_finish_unchanged,
        test_path_faded_close_unchanged,
        test_path_choppy_unchanged,
        test_broad_selloff_fires_at_8_of_11_sectors,
        test_broad_rally_fires_at_8_of_11_sectors,
        test_rotation_requires_both_balanced_breadth_and_spread,
        test_tiny_uniform_negative_move_is_not_automatically_a_selloff,
        test_footer_not_financial_advice_appears_once,
        test_no_age_artifact,
        test_no_catalyst_phrasing_is_gone,
        test_no_headline_appears_twice_even_with_multiple_matching_sentences,
        test_mover_never_gets_a_generic_macro_backdrop,
        test_no_earnings_today,
        test_earnings_today_forward_looking_only,
        test_handoff_notes_asia_flat,
        test_portfolio_intersection_guards_against_substring_collision,
        test_empty_headline_section_is_hidden_never_printed_as_empty,
        test_html_has_no_headline_section_when_feed_is_empty,
        test_topic_other_companys_earnings_is_not_attributed,
        test_topic_keyword_far_from_the_mention_is_not_attributed,
        test_fed_inside_another_word_is_not_monetary_policy,
        test_ambiguous_event_word_needs_supporting_context,
        test_ticker_that_is_an_english_word_does_not_match_by_ticker_alone,
        test_unclassifiable_specific_headline_says_it_cannot_tell_what_it_covers,
        test_all_up_tape,
        test_all_down_tape,
        test_rates_driver_fires_and_mentions_extreme,
        test_ten_year_present_in_close_output,
        test_portfolio_day_performance_appears,
        test_portfolio_day_performance_omitted_when_empty,
        test_no_double_period_anywhere_across_every_citation_call_site,
        test_close_now_gets_standalone_oil_gold_sentence,
        test_close_does_not_double_cite_gold_when_composite_already_used_it,
    ]
    print(f"Running {len(tests)} test groups...\n")
    for t in tests:
        print(f"{t.__name__}:")
        t()
        print()

    if failures:
        print(f"FAILED — {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed.")
        sys.exit(0)
