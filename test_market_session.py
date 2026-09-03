"""
test_market_session.py — Regression tests for market-open/closed classification
(_classify_market_session in pippy_mcp.py), using frozen/injected ET timestamps
rather than live clock reads.

Written after a real bug report: a dry-run at 15:15 UTC was narrated as "should
be open" based on a mis-converted local time, and the actual open/closed logic
was wrongly dismissed as "a quirk." Root cause turned out to be twofold:
  1. openbell.py's own "started at ... UTC" print was naive local time
     mislabeled as UTC (fixed separately, in openbell.py's run()).
  2. _classify_market_session itself was correct for the 9:30-4:00 window, but
     had NO early-close support at all (day-after-Thanksgiving, Christmas Eve,
     July 3rd) — a real, confirmed gap, fixed here alongside making the whole
     function frozen-timestamp-testable instead of only reading datetime.now().

Covers, per the required test matrix:
  - weekday during regular hours -> open
  - weekday before 9:30 ET / after 4:00 PM ET -> closed
  - weekend -> closed
  - a known NYSE holiday -> closed
  - an early-close day (day after Thanksgiving) -> open before 1PM, closed after
  - both sides of the March and November DST boundaries

Usage:
    python3.11 test_market_session.py
"""

import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pippy_mcp  # noqa: E402
import pytz

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


ET = pytz.timezone("America/New_York")


def et(year, month, day, hour, minute=0):
    """Build a tz-aware America/New_York datetime via pytz's real tz database
    (not a hardcoded UTC offset) — the DST rules for the given date decide
    whether this resolves to EST or EDT."""
    return ET.localize(datetime.datetime(year, month, day, hour, minute))


# ── Weekday, regular hours ───────────────────────────────────────────────────

def test_weekday_regular_hours_is_open():
    # 2026-09-03 is a Thursday, not a holiday.
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 11, 0))
    check("weekday 11:00 AM ET is open", result["open"] is True, f"got: {result!r}")


def test_weekday_just_after_open_is_open():
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 9, 30))
    check("exactly 9:30 AM ET is open (inclusive boundary)", result["open"] is True, f"got: {result!r}")


def test_weekday_just_before_close_is_open():
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 15, 59))
    check("3:59 PM ET is still open", result["open"] is True, f"got: {result!r}")


# ── Weekday, outside regular hours ───────────────────────────────────────────

def test_weekday_before_open_is_closed():
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 9, 0))
    check("9:00 AM ET (before 9:30 open) is closed", result["open"] is False, f"got: {result!r}")
    check("reason mentions it's before the open", "before" in result["reason"].lower(), f"got: {result!r}")


def test_weekday_after_close_is_closed():
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 16, 30))
    check("4:30 PM ET (after 4:00 close) is closed", result["open"] is False, f"got: {result!r}")


def test_weekday_exactly_at_close_is_closed():
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 16, 0))
    check("exactly 4:00 PM ET is closed (exclusive boundary)", result["open"] is False, f"got: {result!r}")


# ── Weekend ───────────────────────────────────────────────────────────────────

def test_weekend_is_closed():
    # 2026-09-05 is a Saturday.
    result = pippy_mcp._classify_market_session(et(2026, 9, 5, 11, 0))
    check("Saturday during would-be trading hours is closed", result["open"] is False, f"got: {result!r}")
    check("reason is 'weekend'", result["reason"] == "weekend", f"got: {result!r}")


# ── Known holiday ─────────────────────────────────────────────────────────────

def test_known_holiday_is_closed():
    # 2026-01-01 (New Year's Day) is a Thursday, listed in NYSE_HOLIDAYS.
    result = pippy_mcp._classify_market_session(et(2026, 1, 1, 11, 0))
    check("New Year's Day 2026 is closed", result["open"] is False, f"got: {result!r}")
    check("reason is 'NYSE holiday'", result["reason"] == "NYSE holiday", f"got: {result!r}")


# ── Early close (the gap this pass fixes) ────────────────────────────────────

def test_early_close_day_open_before_cutoff():
    # 2026-11-27 (day after Thanksgiving) is a Friday, early close at 1:00 PM ET.
    result = pippy_mcp._classify_market_session(et(2026, 11, 27, 11, 0))
    check("early-close day at 11:00 AM ET is open", result["open"] is True, f"got: {result!r}")
    check("reason mentions the early close", "early close" in result["reason"].lower(), f"got: {result!r}")


def test_early_close_day_closed_after_cutoff():
    result = pippy_mcp._classify_market_session(et(2026, 11, 27, 13, 30))
    check("early-close day at 1:30 PM ET is closed (would be open on a normal day)",
         result["open"] is False, f"got: {result!r}")
    check("reason mentions the early close", "early close" in result["reason"].lower(), f"got: {result!r}")


def test_early_close_day_closed_exactly_at_cutoff():
    result = pippy_mcp._classify_market_session(et(2026, 11, 27, 13, 0))
    check("exactly 1:00 PM ET on an early-close day is closed (exclusive boundary)",
         result["open"] is False, f"got: {result!r}")


def test_normal_day_still_open_at_time_that_would_be_after_an_early_close():
    # Same wall-clock time (1:30 PM ET) on a day WITHOUT an early close must
    # still be open — proves the early-close check doesn't leak into other days.
    result = pippy_mcp._classify_market_session(et(2026, 9, 3, 13, 30))
    check("1:30 PM ET on a normal (non-early-close) day is open", result["open"] is True, f"got: {result!r}")


# ── DST boundaries — both sides, both directions ─────────────────────────────
# US DST in 2026: begins Sun March 8 (spring forward), ends Sun Nov 1 (fall back).

def test_dst_before_march_boundary_is_est():
    # 2026-03-05 (Thursday, before the March 8 transition) -> EST, UTC-5.
    now = et(2026, 3, 5, 11, 0)
    check("early March is EST (UTC-5) before the DST transition",
         now.utcoffset() == datetime.timedelta(hours=-5), f"got offset: {now.utcoffset()}")
    result = pippy_mcp._classify_market_session(now)
    check("weekday 11:00 AM ET is open even in EST", result["open"] is True, f"got: {result!r}")


def test_dst_after_march_boundary_is_edt():
    # 2026-03-10 (Tuesday, after the March 8 transition) -> EDT, UTC-4.
    now = et(2026, 3, 10, 11, 0)
    check("mid-March is EDT (UTC-4) after the DST transition",
         now.utcoffset() == datetime.timedelta(hours=-4), f"got offset: {now.utcoffset()}")
    result = pippy_mcp._classify_market_session(now)
    check("weekday 11:00 AM ET is open in EDT too", result["open"] is True, f"got: {result!r}")
    check("the open/close window itself doesn't shift with DST (still 9:30/4:00 ET)",
         pippy_mcp._classify_market_session(et(2026, 3, 10, 16, 0))["open"] is False)


def test_dst_before_november_boundary_is_edt():
    # 2026-10-30 (Friday, before the Nov 1 transition) -> still EDT, UTC-4.
    now = et(2026, 10, 30, 11, 0)
    check("late October is still EDT (UTC-4) before the fall-back transition",
         now.utcoffset() == datetime.timedelta(hours=-4), f"got offset: {now.utcoffset()}")
    result = pippy_mcp._classify_market_session(now)
    check("weekday 11:00 AM ET is open before fall-back", result["open"] is True, f"got: {result!r}")


def test_dst_after_november_boundary_is_est():
    # 2026-11-03 (Tuesday, after the Nov 1 transition) -> EST, UTC-5.
    now = et(2026, 11, 3, 11, 0)
    check("early November is EST (UTC-5) after the fall-back transition",
         now.utcoffset() == datetime.timedelta(hours=-5), f"got offset: {now.utcoffset()}")
    result = pippy_mcp._classify_market_session(now)
    check("weekday 11:00 AM ET is open after fall-back", result["open"] is True, f"got: {result!r}")


def test_dst_offset_is_derived_from_tzdb_not_hardcoded():
    # If the offset were hardcoded (e.g. always -5), this would fail on one side.
    before = et(2026, 3, 5, 11, 0).utcoffset()
    after  = et(2026, 3, 10, 11, 0).utcoffset()
    check("the UTC offset actually changes across the DST boundary (proves it's tz-db-derived, not hardcoded)",
         before != after, f"before={before}, after={after}")


# ── _is_market_open_now_fallback wrapper also accepts injected timestamps ───

def test_fallback_wrapper_accepts_injected_timestamp():
    check("_is_market_open_now_fallback(frozen_open_time) is True",
         pippy_mcp._is_market_open_now_fallback(et(2026, 9, 3, 11, 0)) is True)
    check("_is_market_open_now_fallback(frozen_closed_time) is False",
         pippy_mcp._is_market_open_now_fallback(et(2026, 9, 3, 20, 0)) is False)


# ── Run everything ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_weekday_regular_hours_is_open,
        test_weekday_just_after_open_is_open,
        test_weekday_just_before_close_is_open,
        test_weekday_before_open_is_closed,
        test_weekday_after_close_is_closed,
        test_weekday_exactly_at_close_is_closed,
        test_weekend_is_closed,
        test_known_holiday_is_closed,
        test_early_close_day_open_before_cutoff,
        test_early_close_day_closed_after_cutoff,
        test_early_close_day_closed_exactly_at_cutoff,
        test_normal_day_still_open_at_time_that_would_be_after_an_early_close,
        test_dst_before_march_boundary_is_est,
        test_dst_after_march_boundary_is_edt,
        test_dst_before_november_boundary_is_edt,
        test_dst_after_november_boundary_is_est,
        test_dst_offset_is_derived_from_tzdb_not_hardcoded,
        test_fallback_wrapper_accepts_injected_timestamp,
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
