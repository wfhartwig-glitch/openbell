"""
test_market_narrative.py — Regression tests for the shared market-narrative
ladder (_build_market_narrative in openbell.py), which generates BOTH the
"What's Going On" morning summary (mode="morning") and the "What Happened
Today" close summary (mode="close").

This file replaces test_morning_summary.py and test_close_summary.py, which
tested two separate functions (_build_morning_summary / _build_close_summary)
before those were consolidated into one shared-ladder function with RATES,
OIL/GOLD, and BREADTH rules living once instead of forked into two copies.
All checks from both original files are preserved here, translated to the
new call signature; nothing was dropped.

Covers, by section:
  1. Morning: the reported bug (a headline selected purely by recency/rank
     that contradicts the tape it's supposedly explaining) + acceptance
     criteria render-correctly scenarios.
  2. Close: "Markets sold off today" on a rotation day, the same headline
     pasted 3 times, "no sector-specific catalyst identified" spam, the Aug
     28 regression fixture, and a rate-sensitive-composite case.
  3. PATH — all five categories (reversal and gap-and-hold added in this
     pass, on top of the original strong-finish/faded-close/choppy three).
  4. _cite_headline's no-self-terminating-period contract, directly and via
     every call site that renders one.
  5. The one deliberate behavior change from consolidation: close now gets
     a standalone OIL/GOLD sentence (previously morning-only), skipping gold
     if the RATE-SENSITIVE COMPOSITE already cited it.

Usage:
    python3.11 test_market_narrative.py
"""

import sys
import os
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


# ── Fixtures ────────────────────────────────────────────────────────────────

SNAPSHOT_UP     = [{"name": "S&P 500", "pct": 0.44}, {"name": "Nasdaq", "pct": 0.45}, {"name": "Dow", "pct": 0.62}]
SNAPSHOT_DOWN   = [{"name": "S&P 500", "pct": -0.9}, {"name": "Nasdaq", "pct": -1.1}, {"name": "Dow", "pct": -0.6}]
SNAPSHOT_MIXED  = [{"name": "S&P 500", "pct": 0.3}, {"name": "Nasdaq", "pct": -0.4}, {"name": "Dow", "pct": 0.1}]
SNAPSHOT_QUIET  = [{"name": "S&P 500", "pct": 0.05}, {"name": "Nasdaq", "pct": 0.03}, {"name": "Dow", "pct": 0.02}]

TREASURY_FALLING = {
    "yield": 4.74, "change": -0.058,
    "six_mo_high": 4.80, "six_mo_high_day": "Tuesday",
    "six_mo_low": 4.05, "six_mo_low_day": "",
}
COMMODITIES = [
    {"name": "WTI Crude Oil", "price": 91.94, "change": 2.20, "pct": 2.45},
    {"name": "Gold", "price": 4527, "change": 50.0, "pct": 1.12},
]
GLOBAL_INDICES = [
    {"name": "FTSE 100", "session": "Europe", "pct": 0.85},
    {"name": "DAX", "session": "Europe", "pct": 0.52},
    {"name": "Nikkei 225", "session": "Asia (overnight)", "pct": 0.05},
    {"name": "Hang Seng", "session": "Asia (overnight)", "pct": -0.10},
]

AUG28_SNAPSHOT = [
    {"name": "Dow", "pct": -0.01},
    {"name": "S&P 500", "pct": -0.24},
    {"name": "Nasdaq", "pct": -0.52},
]
AUG28_SECTORS = [
    {"sector": "Comm. Services", "pct": 1.24},
    {"sector": "Consumer Discret.", "pct": 1.06},
    {"sector": "Financials", "pct": 0.4},
    {"sector": "Health Care", "pct": 0.2},
    {"sector": "Consumer Staples", "pct": 0.1},
    {"sector": "Energy", "pct": -0.3},
    {"sector": "Materials", "pct": -0.5},
    {"sector": "Real Estate", "pct": -0.8},
    {"sector": "Technology", "pct": -1.05},
    {"sector": "Industrials", "pct": -1.06},
    {"sector": "Utilities", "pct": -1.13},
]
AUG28_COMMODITIES = [{"name": "Gold", "price": 4506, "change": -124.8, "pct": -2.69}]
AUG28_MOVERS = {
    "gainers": [{"symbol": "NOW", "name": "ServiceNow, Inc.", "pct": 5.31}],
    "losers":  [{"symbol": "HIMS", "name": "Hims & Hers Health, Inc.", "pct": -8.13},
                {"symbol": "VRT", "name": "Vertiv Holdings", "pct": -4.39}],
}
AUG28_HEADLINES = [
    {"title": "Fed Chair Warsh Delivers First Jackson Hole Keynote, Signals Hawkish Tilt",
     "snippet": "Warsh struck a hawkish tone in his debut Jackson Hole address as Fed chair.",
     "site": "Reuters"},
]
AUG28_SCAN_CANDIDATES = [{"ticker": "VRT", "score": 97}]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Morning (mode="morning")
# ═══════════════════════════════════════════════════════════════════════════

# ── 1a. The reported bug: a "mixed" headline cited under a confidently ──────
#       higher tape. This must never happen again.

def test_sentiment_gate_rejects_contradicting_headline():
    headlines = [
        {"title": "Equity Futures Mixed Pre-Bell Thursday",
         "snippet": "Futures traded without clear direction ahead of the bell.",
         "site": "MT Newswires"},
    ]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check(
        "sentiment gate rejects a 'mixed' headline under a confidently higher tape",
        "Mixed Pre-Bell" not in text,
        f"got: {text!r}",
    )
    check(
        "direction_called is 'higher' (tape itself, not the headline)",
        log["direction_called"] == "higher",
    )


def test_sentiment_gate_rejects_bearish_headline_under_higher_tape():
    headlines = [
        {"title": "Stocks Plunge as Selloff Deepens", "snippet": "A broad selloff hit markets.", "site": "Wire"},
    ]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check(
        "sentiment gate rejects a bearish headline under a higher tape",
        "Plunge" not in text and "Selloff" not in text,
        f"got: {text!r}",
    )


def test_sentiment_gate_allows_agreeing_headline():
    headlines = [
        {"title": "Stocks Rally as Investors Cheer Earnings Beats", "snippet": "A broad rally lifted major indices.", "site": "Wire"},
    ]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check(
        "sentiment gate allows a bullish headline under a higher tape",
        "Rally" in text,
        f"got: {text!r}",
    )


def test_sentiment_gate_allows_neutral_unclassifiable_headline():
    headlines = [
        {"title": "Federal Reserve schedules policy meeting for next month", "snippet": "The Fed set its next meeting date.", "site": "Wire"},
    ]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check(
        "sentiment gate allows a neutral/unclassifiable but market-relevant headline",
        "Federal Reserve schedules" in text,
        f"got: {text!r}",
    )


# ── 1b. Mixed tape (indices disagreeing) ─────────────────────────────────────

def test_mixed_tape_renders_correctly():
    text, log = morning(SNAPSHOT_MIXED)
    check("mixed tape classified as 'mixed'", log["direction_called"] == "mixed")
    check(
        "mixed tape does not use a confident directional phrase",
        not any(w in text for w in ("firm and broad", "solidly positive", "under pressure", "soft —")),
        f"got: {text!r}",
    )
    check("mixed tape still renders both index numbers", "▲" in text and "▼" in text, f"got: {text!r}")


# ── 1c. All-up / all-down tape ────────────────────────────────────────────────

def test_all_up_tape():
    text, log = morning(SNAPSHOT_UP)
    check("all-up tape classified as 'higher'", log["direction_called"] == "higher")
    check("all-up tape has no negative arrow on any index", "▼" not in text.split("\n\n")[0], f"got: {text!r}")


def test_all_down_tape():
    text, log = morning(SNAPSHOT_DOWN)
    check("all-down tape classified as 'lower'", log["direction_called"] == "lower")
    check("all-down tape has no positive arrow on any index", "▲" not in text.split("\n\n")[0], f"got: {text!r}")


# ── 1d. No drivers tripping thresholds ───────────────────────────────────────

def test_no_drivers_trip_on_quiet_data():
    text, log = morning(SNAPSHOT_QUIET)
    check("quiet data with no headlines falls back to an honest 'no catalyst' statement",
         "No single catalyst" in text, f"got: {text!r}")
    check("quiet tiny move uses modest phrasing, not an overstated one",
         "quietly" in text or "narrowly" in text, f"got: {text!r}")


def test_rates_driver_fires_and_mentions_extreme():
    text, log = morning(SNAPSHOT_UP, treasury=TREASURY_FALLING)
    check("RATES fires on a >=4bp move", "10-year" in text)
    check("RATES mentions the 6-month extreme context by day name", "Tuesday" in text, f"got: {text!r}")
    check("headline_theme logged for RATES", log["headline_theme"] == "rate expectations")


def test_rates_does_not_fire_under_4bp():
    treasury_small = {"yield": 4.50, "change": -0.02}
    text, log = morning(SNAPSHOT_UP, treasury=treasury_small)
    check("RATES does not fire for a <4bp move", "10-year" not in text, f"got: {text!r}")


def test_oil_and_gold_combine_into_one_sentence_when_both_trip():
    text, log = morning(SNAPSHOT_UP, commodities=COMMODITIES)
    check("OIL fires at >=2%% move", "91.94" in text)
    check("GOLD fires at >=1%% move", "4,527" in text)
    check("OIL+GOLD combine into one connector sentence", "cut the other way" in text or "Working against" in text
         or "other side of the ledger" in text, f"got: {text!r}")


def test_breadth_qualifier_fires_on_large_gap():
    snapshot_breadth = [{"name": "S&P 500", "pct": 0.4}, {"name": "Nasdaq", "pct": 0.1}, {"name": "Dow", "pct": 0.6}]
    text, log = morning(snapshot_breadth)
    check("BREADTH qualifier fires when Dow beats Nasdaq by >=30bp",
         "cyclicals" in text, f"got: {text!r}")


def test_handoff_notes_asia_flat():
    global_flat_asia = [
        {"name": "FTSE 100", "session": "Europe", "pct": 0.5},
        {"name": "Nikkei 225", "session": "Asia (overnight)", "pct": 0.05},
        {"name": "Hang Seng", "session": "Asia (overnight)", "pct": -0.05},
    ]
    text, log = morning(SNAPSHOT_UP, global_indices=global_flat_asia)
    check("HANDOFF notes Asia was flat (<0.15% all majors)", "Asia was flat" in text, f"got: {text!r}")


# ── 1e. No earnings today ────────────────────────────────────────────────────

def test_no_earnings_today():
    text, log = morning(SNAPSHOT_UP)
    check("no 'Today:' sentence when there's no earnings today", "Today:" not in text, f"got: {text!r}")


def test_earnings_today_forward_looking_only():
    earnings = [{"symbol": "LULU", "date": datetime.date.today().isoformat(), "eps_estimated": 1.80}]
    text, log = morning(SNAPSHOT_UP, earnings=earnings)
    check("earnings sentence appears", "LULU reports" in text, f"got: {text!r}")
    check("earnings sentence is forward-looking, not phrased as a cause",
         "because" not in text.lower() and "due to" not in text.lower())


# ── 1f. Portfolio intersection ───────────────────────────────────────────────

def test_no_portfolio_intersection_when_no_headline_matches():
    picks_data = {"picks": [{"ticker": "AAPL", "company": "Apple Inc."}], "changes_from_last_week": []}
    headlines = [{"title": "Federal Reserve holds rates steady", "snippet": "No change to rates.", "site": "Wire"}]
    text, log = morning(SNAPSHOT_UP, headlines=headlines, picks_data=picks_data)
    check("no 'one to watch' line when no held ticker appears in headlines",
         "one to watch" not in text, f"got: {text!r}")
    check("plain 'unchanged' picks sentence still renders", "Your 1 picks are unchanged." in text, f"got: {text!r}")


def test_portfolio_intersection_picks_worst_holding_among_matches():
    picks_data = {
        "picks": [
            {"ticker": "AVGO", "company": "Broadcom Inc."},
            {"ticker": "MSFT", "company": "Microsoft Corporation"},
        ],
        "changes_from_last_week": [],
    }
    mem = {"pick_performance_history": [
        {"ticker": "AVGO", "pct_change_since_pick": -13.8},
        {"ticker": "MSFT", "pct_change_since_pick": 3.2},
    ]}
    headlines = [
        {"title": "AVGO shares slip on AI revenue doubts", "snippet": "Broadcom faces analyst skepticism.", "site": "Reuters"},
        {"title": "Microsoft Corporation unveils new Copilot features", "snippet": "Microsoft announced updates.", "site": "Wire"},
    ]
    text, log = morning(SNAPSHOT_UP, headlines=headlines, picks_data=picks_data, mem=mem)
    check("worst holding (AVGO, -13.8%) is featured over the better one (MSFT, +3.2%)",
         "AVGO is the one to watch" in text, f"got: {text!r}")
    check("no double-period after the quoted headline citation",
         '".."' not in text and '.".' not in text, f"got: {text!r}")


def test_portfolio_intersection_guards_against_substring_collision():
    picks_data = {"picks": [{"ticker": "APP", "company": "AppLovin Corporation"}], "changes_from_last_week": []}
    mem = {"pick_performance_history": [{"ticker": "APP", "pct_change_since_pick": 5.0}]}
    headlines = [{"title": "Best new app store releases this week", "snippet": "A roundup of new mobile app titles.", "site": "TechCrunch"}]
    text, log = morning(SNAPSHOT_UP, headlines=headlines, picks_data=picks_data, mem=mem)
    check("ticker APP does not false-match generic 'app store' text",
         "one to watch" not in text, f"got: {text!r}")


# ── 1g. Misc fixed bugs ───────────────────────────────────────────────────────

def test_no_age_artifact():
    headlines = [{"title": "Federal Reserve holds rates steady", "snippet": "No change.", "site": "Wire", "age_hrs": 0}]
    text, log = morning(SNAPSHOT_UP, headlines=headlines)
    check("no '(0h ago)' timestamp artifact anywhere in the output", "h ago)" not in text, f"got: {text!r}")


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


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Close (mode="close")
# ═══════════════════════════════════════════════════════════════════════════

# ── 2a. The reported bug: rotation misclassified as a sell-off ──────────────

def test_aug28_rotation_regression_fixture():
    text = close(
        AUG28_SNAPSHOT, movers=AUG28_MOVERS, sectors=AUG28_SECTORS, commodities=AUG28_COMMODITIES,
        headlines=AUG28_HEADLINES, scan_candidates=AUG28_SCAN_CANDIDATES,
        mem={"briefing_history": []},
    )
    # Note: "gold sold off 2.69%" legitimately appears later in the composite
    # note — check specifically for the old buggy market-tone phrase, not any
    # occurrence of "sold off" (which is correct when describing gold itself).
    check("does NOT say 'Markets sold off' despite all 3 indices being negative",
         "Markets sold off" not in text, f"got: {text!r}")
    check("correctly identifies this as a rotation", "rotation" in text.lower(), f"got: {text!r}")
    check("names what money left (Utilities/Industrials/Technology)",
         all(s in text for s in ("Utilities", "Industrials", "Technology")), f"got: {text!r}")
    check("names what money moved into (Comm. Services / Consumer Discret.)",
         "Comm. Services" in text and "Consumer Discret." in text, f"got: {text!r}")
    check("Jackson Hole headline appears exactly once, not three times",
         text.count("Jackson Hole") == 1, f"count={text.count('Jackson Hole')}, got: {text!r}")
    check("no 'no sector-specific catalyst identified' spam anywhere",
         "no sector-specific catalyst" not in text and "no single catalyst" not in text, f"got: {text!r}")
    check("rate-sensitive composite correctly reads gold + lagging Utilities/RE as a hawkish repricing",
         "hawkish rate repricing" in text, f"got: {text!r}")
    check("both movers named factually (HIMS worst, NOW/ServiceNow best)",
         "HIMS" in text and "NOW" in text, f"got: {text!r}")
    check("candidate cross-reference names VRT and its score",
         "VRT" in text and "97" in text, f"got: {text!r}")
    check("no double-terminal punctuation anywhere", '".".' not in text and '"..' not in text, f"got: {text!r}")


# ── 2b. Sector-breadth-gated classification ──────────────────────────────────

def test_tiny_uniform_negative_move_is_not_automatically_a_selloff():
    snapshot = [{"name": "S&P 500", "pct": -0.05}, {"name": "Nasdaq", "pct": -0.03}, {"name": "Dow", "pct": -0.02}]
    sectors = [{"sector": f"Up{i}", "pct": 0.1} for i in range(5)] + [{"sector": f"Down{i}", "pct": -0.1} for i in range(6)]
    text = close(snapshot, sectors=sectors)
    check("tiny uniform-negative move does not trigger 'sold off'", "sold off" not in text, f"got: {text!r}")


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


# ── 2c. Headline dedup ────────────────────────────────────────────────────────

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


def test_no_catalyst_phrasing_is_gone():
    text = close(AUG28_SNAPSHOT, sectors=AUG28_SECTORS)
    check("'no sector-specific catalyst identified' phrasing is gone entirely",
         "no sector-specific catalyst identified" not in text, f"got: {text!r}")
    check("'no clear single catalyst' mover-fallback phrasing is gone entirely",
         "no clear single catalyst" not in text, f"got: {text!r}")


def test_mover_never_gets_a_generic_macro_backdrop():
    movers = {"gainers": [], "losers": [{"symbol": "ZZZ", "name": "Nothing Corp", "pct": -6.0}]}
    text = close(
        [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": 0.1}, {"name": "Dow", "pct": 0.1}],
        movers=movers, headlines=AUG28_HEADLINES,
    )
    check("mover with no specific headline gets no borrowed macro 'backdrop' citation",
         "broader market backdrop" not in text and "Jackson Hole" not in text, f"got: {text!r}")
    check("mover still states its move factually", "ZZZ" in text and "6.00%" in text, f"got: {text!r}")


# ── 2d. RATE-SENSITIVE COMPOSITE ──────────────────────────────────────────────

def test_rate_sensitive_composite_fires_on_utilities_and_re_lag_plus_gold():
    sp_pct = -0.2
    sectors = [{"sector": "Utilities", "pct": -1.0}, {"sector": "Real Estate", "pct": -0.9},
               {"sector": "Technology", "pct": 0.1}]
    note = openbell._rate_sensitive_composite_note(sp_pct, sectors, gold_pct=-2.0, treasury_chg=None)
    check("composite fires when Utilities+RE both lag S&P by >=0.5pp and gold moved >=1.5%%",
         "hawkish rate repricing" in note, f"got: {note!r}")
    check("composite is phrased as a reading, not an asserted cause",
         "caused" not in note.lower() and "because" not in note.lower(), f"got: {note!r}")


def test_rate_sensitive_composite_does_not_fire_without_lag():
    sp_pct = -0.2
    sectors = [{"sector": "Utilities", "pct": -0.1}, {"sector": "Real Estate", "pct": -0.9}]
    note = openbell._rate_sensitive_composite_note(sp_pct, sectors, gold_pct=-2.0, treasury_chg=None)
    check("composite does not fire when only one of Utilities/RE lags",
         note == "", f"got: {note!r}")


def test_rate_sensitive_composite_does_not_fire_without_gold_or_yield_move():
    sp_pct = -0.2
    sectors = [{"sector": "Utilities", "pct": -1.0}, {"sector": "Real Estate", "pct": -0.9}]
    note = openbell._rate_sensitive_composite_note(sp_pct, sectors, gold_pct=0.3, treasury_chg=0.01)
    check("composite does not fire without gold>=1.5%% or |10y|>=4bp corroborating",
         note == "", f"got: {note!r}")


def test_rate_sensitive_composite_names_correlated_headline_without_asserting_cause():
    sp_pct = -0.2
    sectors = [{"sector": "Utilities", "pct": -1.0}, {"sector": "Real Estate", "pct": -0.9}]
    headlines = [{"title": "Fed Chair Warsh Delivers First Jackson Hole Keynote", "snippet": "s", "site": "Wire"}]
    note = openbell._rate_sensitive_composite_note(sp_pct, sectors, gold_pct=-2.0, treasury_chg=None,
                                                   headlines=headlines, used_headlines=set())
    check("names the correlated headline as 'on a day when', not as a cause",
         "on a day when" in note and "Jackson Hole" in note, f"got: {note!r}")
    check("does not claim the headline caused the move", "caused" not in note.lower(), f"got: {note!r}")


# ── 2e. Data gap fixes ────────────────────────────────────────────────────────

def test_ten_year_present_in_close_output():
    treasury = {"yield": 4.50, "change": -0.06}
    # No sector data at all -> composite can't fire -> falls through to standalone RATES
    text = close(
        [{"name": "S&P 500", "pct": 0.1}, {"name": "Nasdaq", "pct": 0.1}, {"name": "Dow", "pct": 0.1}],
        treasury=treasury,
    )
    check("10-year yield appears in close output when treasury data is present",
         "10-year" in text, f"got: {text!r}")


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


# ── 2f. Portfolio day-performance ─────────────────────────────────────────────

def test_portfolio_day_performance_appears():
    picks_perf = [{"ticker": "MSFT", "pct": 0.5}, {"ticker": "AVGO", "pct": -1.2}]
    text = close(AUG28_SNAPSHOT, picks_day_performance=picks_perf)
    check("portfolio day-performance line appears with both tickers named",
         "MSFT" in text and "AVGO" in text, f"got: {text!r}")


def test_portfolio_day_performance_omitted_when_empty():
    text = close(AUG28_SNAPSHOT, picks_day_performance=[])
    check("no portfolio line when there's no picks-day-performance data",
         "Your picks" not in text and "Your one held pick" not in text, f"got: {text!r}")


# ── 2g. LOOP-CLOSE ─────────────────────────────────────────────────────────────

def test_loop_close_reports_when_call_missed():
    today_iso = datetime.date.today().isoformat()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "direction_called": "higher"}]}
    text = close(AUG28_SNAPSHOT, mem=mem)
    check("LOOP-CLOSE reports a missed morning call", "we called the tape higher" in text, f"got: {text!r}")
    check("LOOP-CLOSE states the actual outcome", "closed lower instead" in text, f"got: {text!r}")


def test_loop_close_confirms_when_call_held():
    today_iso = datetime.date.today().isoformat()
    mem = {"briefing_history": [{"date": today_iso, "type": "morning", "direction_called": "lower"}]}
    text = close(AUG28_SNAPSHOT, mem=mem)
    check("LOOP-CLOSE confirms a held morning call", "held through the close" in text, f"got: {text!r}")


# test_loop_close_logs_explicit_skip_for_missing_morning_record is run directly
# in __main__ below (needs stdout capture; this project has no pytest fixtures).


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — PATH: all five categories (reversal, gap-and-hold added this pass)
# ═══════════════════════════════════════════════════════════════════════════

def test_path_reversal_down_then_up():
    # Opened -0.5% vs yesterday, closed +0.3% vs yesterday -> crossed intraday.
    snapshot = [{"name": "S&P 500", "pct": 0.3, "previous_close": 100.0, "open": 99.5,
                 "price": 100.3, "day_high": 100.4, "day_low": 99.3}]
    text = openbell._path_note(snapshot)
    check("reversal (down->up) is named and described as opening red, closing green",
         "clawed back to close green" in text, f"got: {text!r}")


def test_path_reversal_up_then_down():
    snapshot = [{"name": "S&P 500", "pct": -0.3, "previous_close": 100.0, "open": 100.5,
                 "price": 99.7, "day_high": 100.6, "day_low": 99.6}]
    text = openbell._path_note(snapshot)
    check("reversal (up->down) is named and described as opening green, closing red",
         "slid to close red" in text, f"got: {text!r}")


def test_path_reversal_takes_priority_over_strong_finish():
    # Also closes near the day's own high (>=85% of day's range) -- reversal
    # must win anyway since it's checked first.
    snapshot = [{"name": "S&P 500", "pct": 0.3, "previous_close": 100.0, "open": 99.5,
                 "price": 100.3, "day_high": 100.32, "day_low": 99.4}]
    text = openbell._path_note(snapshot)
    check("reversal outranks strong-finish when both conditions are technically true",
         "reversal" in text, f"got: {text!r}")


def test_path_gap_and_hold_up():
    # Opened +0.8% vs yesterday, closed +0.6% vs yesterday (held >=half the gap, same direction).
    snapshot = [{"name": "S&P 500", "pct": 0.6, "previous_close": 100.0, "open": 100.8,
                 "price": 100.6, "day_high": 100.9, "day_low": 100.5}]
    text = openbell._path_note(snapshot)
    check("gap-and-hold (higher) is named", "gapped higher at the open and held it" in text, f"got: {text!r}")


def test_path_gap_and_hold_down():
    snapshot = [{"name": "S&P 500", "pct": -0.6, "previous_close": 100.0, "open": 99.2,
                 "price": 99.4, "day_high": 99.5, "day_low": 99.1}]
    text = openbell._path_note(snapshot)
    check("gap-and-hold (lower) is named", "gapped lower at the open and held it" in text, f"got: {text!r}")


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


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — _cite_headline: fixed at the source (Priority 4)
# ═══════════════════════════════════════════════════════════════════════════

def test_cite_headline_never_self_terminates():
    short = {"title": "Fed holds rates steady", "snippet": "s"}
    long_text = " ".join(["word"] * 30)
    long_h = {"title": long_text, "snippet": "s"}
    short_result = openbell._cite_headline(short, "title")
    long_result  = openbell._cite_headline(long_h, "title")
    check("short (quoted) citation never ends with its own period",
         not short_result.endswith("."), f"got: {short_result!r}")
    check("long (truncated) citation never ends with a period either",
         not long_result.endswith("."), f"got: {long_result!r}")
    check("short citation is still a properly closed quote",
         short_result.startswith('"') and short_result.endswith('"'), f"got: {short_result!r}")


def test_cite_headline_strips_a_source_period_before_requoting():
    # A source title/snippet that itself ends in "." must not produce "..\"" —
    # the trailing period is stripped before quoting, not after.
    h = {"title": "Fed holds rates steady.", "snippet": "s"}
    result = openbell._cite_headline(h, "title")
    check("no double period even when the source text itself ends in '.'",
         ".." not in result, f"got: {result!r}")


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


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — Consolidation behavior (Priority 2): shared OIL/GOLD in close
# ═══════════════════════════════════════════════════════════════════════════

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


def test_market_narrative_is_a_single_shared_function():
    check("the old two-function split is gone — only _build_market_narrative exists",
         not hasattr(openbell, "_build_morning_summary") and not hasattr(openbell, "_build_close_summary"))
    check("_build_market_narrative exists and is callable",
         callable(getattr(openbell, "_build_market_narrative", None)))


# ── Run everything ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import io
    import contextlib

    tests = [
        # Section 1 — morning
        test_sentiment_gate_rejects_contradicting_headline,
        test_sentiment_gate_rejects_bearish_headline_under_higher_tape,
        test_sentiment_gate_allows_agreeing_headline,
        test_sentiment_gate_allows_neutral_unclassifiable_headline,
        test_mixed_tape_renders_correctly,
        test_all_up_tape,
        test_all_down_tape,
        test_no_drivers_trip_on_quiet_data,
        test_rates_driver_fires_and_mentions_extreme,
        test_rates_does_not_fire_under_4bp,
        test_oil_and_gold_combine_into_one_sentence_when_both_trip,
        test_breadth_qualifier_fires_on_large_gap,
        test_handoff_notes_asia_flat,
        test_no_earnings_today,
        test_earnings_today_forward_looking_only,
        test_no_portfolio_intersection_when_no_headline_matches,
        test_portfolio_intersection_picks_worst_holding_among_matches,
        test_portfolio_intersection_guards_against_substring_collision,
        test_no_age_artifact,
        test_footer_not_financial_advice_appears_once,
        # Section 2 — close
        test_aug28_rotation_regression_fixture,
        test_tiny_uniform_negative_move_is_not_automatically_a_selloff,
        test_broad_selloff_fires_at_8_of_11_sectors,
        test_broad_rally_fires_at_8_of_11_sectors,
        test_rotation_requires_both_balanced_breadth_and_spread,
        test_no_headline_appears_twice_even_with_multiple_matching_sentences,
        test_no_catalyst_phrasing_is_gone,
        test_mover_never_gets_a_generic_macro_backdrop,
        test_rate_sensitive_composite_fires_on_utilities_and_re_lag_plus_gold,
        test_rate_sensitive_composite_does_not_fire_without_lag,
        test_rate_sensitive_composite_does_not_fire_without_gold_or_yield_move,
        test_rate_sensitive_composite_names_correlated_headline_without_asserting_cause,
        test_ten_year_present_in_close_output,
        test_path_fires_with_ohlc_data,
        test_path_omitted_without_ohlc_data,
        test_portfolio_day_performance_appears,
        test_portfolio_day_performance_omitted_when_empty,
        test_loop_close_reports_when_call_missed,
        test_loop_close_confirms_when_call_held,
        # Section 3 — PATH (all 5 categories)
        test_path_reversal_down_then_up,
        test_path_reversal_up_then_down,
        test_path_reversal_takes_priority_over_strong_finish,
        test_path_gap_and_hold_up,
        test_path_gap_and_hold_down,
        test_path_gap_that_fades_is_not_gap_and_hold,
        test_path_falls_back_to_original_three_without_previous_close,
        test_path_strong_finish_unchanged,
        test_path_faded_close_unchanged,
        test_path_choppy_unchanged,
        # Section 4 — _cite_headline contract
        test_cite_headline_never_self_terminates,
        test_cite_headline_strips_a_source_period_before_requoting,
        test_no_double_period_anywhere_across_every_citation_call_site,
        # Section 5 — consolidation behavior
        test_close_now_gets_standalone_oil_gold_sentence,
        test_close_does_not_double_cite_gold_when_composite_already_used_it,
        test_market_narrative_is_a_single_shared_function,
    ]
    print(f"Running {len(tests)} test groups...\n")
    for t in tests:
        print(f"{t.__name__}:")
        t()
        print()

    # test_loop_close_logs_explicit_skip_for_missing_morning_record needs stdout
    # capture — do it standalone with a simple redirect rather than pytest's capsys.
    print("test_loop_close_logs_explicit_skip_for_missing_morning_record:")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mem = {"briefing_history": []}
        text = close(AUG28_SNAPSHOT, mem=mem)
    out = buf.getvalue()
    check("LOOP-CLOSE prints an explicit skip message for a missing morning record",
         "[LOOP-CLOSE] Skipped" in out, f"got stdout: {out!r}")
    check("no fabricated loop-close sentence appears in the actual output text",
         "we called the tape" not in text and "held through the close" not in text, f"got: {text!r}")
    print()

    if failures:
        print(f"FAILED — {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed.")
        sys.exit(0)
