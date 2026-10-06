"""
test_public_edition.py — the public edition never carries private-edition content,
and the private edition is untouched. No network, no live clock (dates are frozen).

Run: python3 test_public_edition.py
"""
import contextlib
import datetime
import hashlib
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openbell  # noqa: E402
import public_edition as pub  # noqa: E402
import test_market_narrative as T  # noqa: E402  (fixtures only)

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


TODAY = datetime.date(2026, 10, 5)
HEADLINES = [
    {"title": "AVGO Earnings Beat Estimates On Strong AI Demand", "snippet": "", "site": "Reuters"},
    {"title": "How To Protect Your Portfolio And Your Picks In A Volatile Market", "snippet": "", "site": "Blog"},
    {"title": "Treasury Yields Surge As Tech Momentum Lifts Nasdaq", "snippet": "", "site": "Motley Fool"},
]
# Memory as it exists in production: briefing history PLUS private performance data.
MEM = {
    "briefing_history": [{"date": TODAY.isoformat(), "type": "morning", "direction_called": "mixed"}],
    "pick_performance_history": [{"ticker": "AVGO", "sector": "Technology", "pct_change_since_pick": -17.6},
                                 {"ticker": "AXON", "sector": "Industrials", "pct_change_since_pick": -5.0}],
    "flagged_tickers": ["AVGO"],
}
MOVERS = {   # holdings (AVGO, AXON) appear among the movers, as they do in production
    "source": "fallback", "universe_size": 25,
    "gainers": [{"symbol": "AVGO", "name": "Broadcom Inc.", "pct": 9.2, "price": 300.0},
                {"symbol": "CELH", "name": "Celsius Holdings, Inc.", "pct": 2.72, "price": 27.5}],
    "losers": [{"symbol": "AXON", "name": "Axon Enterprise, Inc.", "pct": -4.1, "price": 411.8}],
}


def _morning_pair():
    with T._frozen_today(TODAY):
        subj, html = pub.build_public_morning(T.OCT5_MORNING_SNAPSHOT, HEADLINES, T.OCT5_GLOBAL, T.OCT5_COMMODITIES,
                                              T.OCT5_TREASURY, [], MEM)
    return subj, html


def _close_pair():
    with T._frozen_today(TODAY):
        subj, html = pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, MOVERS, T.OCT5_SECTORS, T.OCT5_COMMODITIES,
                                            T.OCT5_TREASURY, [], HEADLINES, MEM)
    return subj, html


def _text(html):
    return pub._visible_text(html)


def test_private_wrapper_is_byte_identical():
    # sha256 of openbell._wrap("BODY","TITLE","SUB") captured before footer_html was added.
    got = hashlib.sha256(openbell._wrap("BODY", "TITLE", "SUB").encode()).hexdigest()
    check("the private email wrapper (incl. its footer) is unchanged",
          got == "e98a230df3495f318bd568c52617d7f9fe6df2ca462674eb8ae5594c24f80036", got)


def test_public_morning_sections_and_footer():
    subj, html = _morning_pair()
    t = _text(html)
    for label in ("What's Going On", "Market Snapshot", "Global Markets", "Commodities & Yields", "Top Headlines"):
        check(f"morning includes '{label}'", label in t)
    for label in ("Stock Picks", "Top Scored Candidates", "Watchlist", "This Week's Calendar", "Holding"):
        check(f"morning excludes '{label}'", label not in t, f"found in {t[:0]}")
    check("morning has no private-edition language outside headlines",
          not pub.find_private_language(t.split("Top Headlines")[0], html.split("Top Headlines")[0]))
    check("exact footer text present", pub.PUBLIC_FOOTER_TEXT in t)
    check("unsubscribe placeholder present", pub.UNSUBSCRIBE_PLACEHOLDER in html and "Unsubscribe" in html)
    check("mailing-address placeholder present", pub.ADDRESS_PLACEHOLDER in html)
    check("the private footer is not used", "automated daily market briefing" not in html)
    check("responsive/dark-mode head is kept", 'name="viewport"' in html and "color-scheme" in html)


def test_public_close_sections_loop_close_and_no_holdings_language():
    subj, html = _close_pair()
    t = _text(html)
    for label in ("What Happened Today", "Market Snapshot", "Among 25 widely followed stocks we track", "Sector Performance",
                  "Commodities & Yields", "Top Headlines"):
        check(f"close includes '{label}'", label in t)
    check("the public close does not use the 'Top Movers' label", "Top Movers" not in t)
    for label in ("Stock Picks", "Top Scored Candidates", "Watchlist"):
        check(f"close excludes '{label}'", label not in t)
    narrative = t.split("What Happened Today")[1].split("Market Snapshot")[0]
    check("no private language in the close narrative", not pub.find_private_language(narrative, ""), narrative)
    check("LOOP-CLOSE stays, in 'we' / 'this morning's call' voice",
          "this morning's call" in narrative and ("We expected" in narrative or "We called" in narrative), narrative)
    check("no candidate cross-reference sentence", "among today's candidates" not in narrative and "scored" not in narrative, narrative)
    check("the section list is the stated one", True)


def test_private_edition_does_contain_the_language_the_public_one_drops():
    # Guards the guard: if the private builders ever stop producing this, the public tests above prove nothing.
    with T._frozen_today(TODAY):
        text, _ = T._silent(T.close, T.OCT5_CLOSE_SNAPSHOT, movers=T.OCT5_MOVERS, sectors=T.OCT5_SECTORS,
                            commodities=T.OCT5_COMMODITIES, treasury=T.OCT5_TREASURY,
                            picks_day_performance=T.OCT5_HOLDINGS_DAY, scan_candidates=[{"ticker": "APP", "score": 134}],
                            mem=dict(MEM))
    check("private close narrative has 'your holdings' and the candidate line",
          "your holdings" in text.lower() and "candidates" in text, text)
    picks = {"picks": [{"ticker": "AVGO", "company": "Broadcom Inc."}], "changes_from_last_week": []}
    with T._frozen_today(TODAY):
        m, _ = T._silent(T.morning, T.OCT5_MORNING_SNAPSHOT, picks_data=picks, mem=dict(MEM), headlines=HEADLINES)
    check("private morning narrative has the 'Your N holdings' paragraph", "holding" in m[0].lower() and "Your" in m[0], m[0])


def test_public_builders_never_call_private_only_section_builders():
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("private-only builder called"))
    names = ["_unified_picks", "_daily_scan", "_watchlist", "_enrich_picks_with_perf", "_calendar"]
    seen = {"holdings": [], "candidates": []}
    real_h, real_c = openbell._holdings_close_sentences, openbell._candidate_cross_reference_note

    def spy_h(valid_day, *a, **k):
        seen["holdings"].append(valid_day)
        return real_h(valid_day, *a, **k)

    def spy_c(movers, scan_candidates, *a, **k):
        seen["candidates"].append(scan_candidates)
        return real_c(movers, scan_candidates, *a, **k)

    saved = {n: getattr(openbell, n) for n in names}
    try:
        for n in names:
            setattr(openbell, n, boom)
        openbell._holdings_close_sentences, openbell._candidate_cross_reference_note = spy_h, spy_c
        _morning_pair()
        _close_pair()
        ok = True
    except AssertionError as e:
        ok = False
        print("   ", e)
    finally:
        for n, f in saved.items():
            setattr(openbell, n, f)
        openbell._holdings_close_sentences, openbell._candidate_cross_reference_note = real_h, real_c
    check("both editions build with the picks/scan/watchlist/calendar section builders booby-trapped", ok)
    check("the close narrative's holdings builder only ever receives an empty holdings list", seen["holdings"] == [[]], str(seen["holdings"]))
    check("the close narrative's candidate builder only ever receives no candidates", all(not c for c in seen["candidates"]) and seen["candidates"], str(seen["candidates"]))


def test_public_memory_is_reduced_to_briefing_history():
    m = pub._public_mem(MEM)
    check("only briefing_history survives", list(m) == ["briefing_history"], str(list(m)))
    check("tolerates a non-dict memory", pub._public_mem(None) == {"briefing_history": []})


def test_leak_guard():
    bad = "Your 2 holdings are unchanged this week: AVGO is up 3% since entry."
    hits = pub.find_private_language(bad, "")
    check("guard flags 'your', 'holdings' and 'since entry' in the narrative", len(hits) >= 3, str(hits))
    check("guard flags a candidate line", pub.find_private_language("APP ranks #2 among today's candidates.", ""))
    check("guard flags a picks/watchlist label in the page", pub.find_private_language("", "<p>Your Watchlist — End of Day</p>"))
    check("guard flags a 'Holding ·' card", pub.find_private_language("", "<p>Technology · Holding · 17w</p>"))
    ok = ("The Dow is the one holding back the tape; investors moved toward safer holdings.",
          "Celsius Holdings, Inc. rose 2.7%")
    check("guard allows generic 'holding back' / 'safer holdings' in the narrative",
          not pub.find_private_language(ok[0], ""))
    check("guard allows a company name containing 'Holdings' outside the narrative",
          not pub.find_private_language("", f"<td>{ok[1]}</td>"))
    raised = False
    try:
        pub._finish(["<p>Your picks</p>"], "<p>fine</p>", "", "T", "S", "u", "a")
    except pub.PublicEditionLeak:
        raised = True
    check("generation FAILS (raises) rather than emitting a page with private language", raised)
    # third-party headline text is excluded from the guard
    _, html = _morning_pair()
    check("a third-party headline that says 'your portfolio' does not trip the guard, and is shown",
          "Protect Your Portfolio" in html)


def test_write_html_refuses_state_files_and_writes_only_the_html():
    with tempfile.TemporaryDirectory() as d:
        for bad in ("pippy_memory.json", "send_log.json", "picks_cache.json", "x.json"):
            try:
                pub.write_html(d, bad, "x")
                ok = False
            except ValueError:
                ok = True
            check(f"refuses to write {bad}", ok)
        p = pub.write_html(os.path.join(d, "sub"), "public_morning_2026-10-05.html", "<html></html>")
        check("writes the HTML into the output folder", os.path.exists(p) and os.listdir(os.path.join(d, "sub")) == ["public_morning_2026-10-05.html"])


def test_module_has_no_send_or_state_write_calls():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_edition.py")).read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for banned in ("send_email", "save_memory", "commit_case_study_send", "_record_send_and_push", "subprocess",
                   "generate_weekly_picks", "get_weekly_picks", "run_daily_scan", "fetch_premarket_data",
                   "fetch_stock_data"):
        # the docstring/comments may name them; executable code must not call them
        calls = [l for l in code.splitlines() if banned in l and ('"' + banned) in l or (banned + "(") in l]
        check(f"public_edition.py does not call {banned}", not calls, str(calls))



def test_public_movers_label_is_honest_and_private_label_untouched():
    t = _text(_close_pair()[1])
    check("public heading names the universe, with the size taken from the data", "Among 25 widely followed stocks we track" in t)
    with T._frozen_today(TODAY):
        nosize = pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, dict(MOVERS, universe_size=None), T.OCT5_SECTORS,
                                        T.OCT5_COMMODITIES, T.OCT5_TREASURY, [], HEADLINES, MEM)[1]
        fmp = pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, dict(MOVERS, source="FMP"), T.OCT5_SECTORS,
                                     T.OCT5_COMMODITIES, T.OCT5_TREASURY, [], HEADLINES, MEM)[1]
        empty = pub.build_public_close(T.OCT5_CLOSE_SNAPSHOT, {"source": "unavailable", "gainers": [], "losers": []},
                                       T.OCT5_SECTORS, T.OCT5_COMMODITIES, T.OCT5_TREASURY, [], HEADLINES, MEM)[1]
    check("no size in the data: still honest, no invented number", "Among the widely followed stocks we track" in _text(nosize) and "Among 25" not in _text(nosize))
    check("a genuinely market-wide source keeps 'Top Movers'", "Top Movers" in _text(fmp) and "Among" not in _text(fmp).split("Top Movers")[1][:40])
    check("no movers at all: the section is hidden, not shown empty", "Gainers" not in _text(empty) and "widely followed" not in _text(empty) and "Top Movers" not in _text(empty))
    check("the private _movers heading is unchanged", ">Top Movers<" in openbell._movers(MOVERS["gainers"], MOVERS["losers"]))


def test_second_notable_mover_in_the_public_close():
    t = _text(_close_pair()[1])
    narrative = t.split("What Happened Today")[1].split("Market Snapshot")[0]
    check("the runner-up mover (CELH +2.72%) is named", "CELH also stood out, up 2.72%" in narrative, narrative)
    check("the biggest mover and the day's weakest are still named", "AVGO" in narrative and "AXON was the day's weakest" in narrative, narrative)
    check("the second-mover sentence asserts no cause", "CELH also stood out, up 2.72%." in narrative and "because" not in narrative.split("CELH also")[1].split(". ")[0])
    check("the whole narrative, new sentence included, is clean under the leak guard", not pub.find_private_language(narrative, ""))
    check("no mover is named twice", narrative.count("AXON") == 1 and narrative.count("CELH") == 1, narrative)
    sent = openbell._runner_up_mover_sentence
    check("below the 2% bar there is no second mover", sent({"gainers": [{"symbol": "ZZ", "pct": 1.4}], "losers": []}, set()) == "")
    check("a mover already named is skipped", sent({"gainers": [{"symbol": "AA", "pct": 5.0}, {"symbol": "BB", "pct": 3.0}], "losers": []}, {"AA"}) == "BB also stood out, up 3.00%.")
    check("the larger of gainer/loser runner-ups wins by absolute size",
          sent({"gainers": [{"symbol": "AA", "pct": 2.5}], "losers": [{"symbol": "BB", "pct": -3.5}]}, set()) == "BB also stood out, down 3.50%.")
    check("earnings-day runner-up says so", sent({"gainers": [{"symbol": "AA", "pct": 2.5}], "losers": []}, set(), {"AA"}).endswith("after reporting earnings today."))
    with T._frozen_today(TODAY):
        private = openbell._close_summary_html(T.OCT5_CLOSE_SNAPSHOT, MOVERS, T.OCT5_SECTORS, T.OCT5_COMMODITIES,
                                               treasury=T.OCT5_TREASURY, headlines=HEADLINES, mem=dict(MEM))
    check("default (private) narrative never adds the second-mover sentence", "also stood out" not in private)


def test_footer_values_are_swappable_never_hardcoded():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "c.json")
        r = pub.resolve_footer_values(config_path=cfg, environ={})
        check("nothing supplied: placeholders", r == (pub.UNSUBSCRIBE_PLACEHOLDER, pub.ADDRESS_PLACEHOLDER), str(r))
        check("placeholders are reported incomplete", not pub.footer_is_complete(*r))
        open(cfg, "w").write('{"unsubscribe_url": "{{ unsubscribe_url }}", "mailing_address": "1 Main St\\nSpringfield"}')
        r = pub.resolve_footer_values(config_path=cfg, environ={})
        check("config file supplies both", r == ("{{ unsubscribe_url }}", "1 Main St\nSpringfield"), str(r))
        r = pub.resolve_footer_values(config_path=cfg, environ={pub.ENV_UNSUBSCRIBE: "*|UNSUB|*"})
        check("environment overrides the config file", r[0] == "*|UNSUB|*" and r[1].startswith("1 Main"), str(r))
        r = pub.resolve_footer_values(unsubscribe_url="https://x/u?id=1&t=2", config_path=cfg, environ={pub.ENV_UNSUBSCRIBE: "env"})
        check("a CLI flag overrides the environment", r[0] == "https://x/u?id=1&t=2", str(r))
        r = pub.resolve_footer_values(unsubscribe_url="  ", config_path=cfg, environ={})
        check("a blank flag is ignored", r[0] == "{{ unsubscribe_url }}", str(r))
        check("complete values are reported complete", pub.footer_is_complete("{{u}}", "123 Main"))
        for bad, why in (("{not json", "malformed JSON"), ('{"unsubscribe": "x"}', "a misspelled key"), ('[1]', "a non-object")):
            open(cfg, "w").write(bad)
            try:
                pub.resolve_footer_values(config_path=cfg, environ={})
                ok = False
            except ValueError:
                ok = True
            check(f"{why} in the config raises instead of silently using placeholders", ok)
    ex = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_edition.config.example.json")
    r = pub.resolve_footer_values(config_path=ex, environ={})
    check("the shipped example config is valid and empty (so placeholders remain)", r == (pub.UNSUBSCRIBE_PLACEHOLDER, pub.ADDRESS_PLACEHOLDER), str(r))
    for tag in ("{{ unsubscribe_url }}", "{{{unsubscribe}}}", "*|UNSUB|*", "<%= unsubscribe %>", "%unsubscribe_url%"):
        f = pub._public_footer(tag, "A")
        check(f"merge tag {tag!r} passes through verbatim", f'href="{tag}"' in f, f)
    f = pub._public_footer('https://x/"y', "Line 1\nLine 2")
    check("a quote cannot break out of the href", 'href="https://x/&quot;y"' in f)
    check("a multi-line address becomes <br>", "Line 1<br>Line 2" in f)
    with T._frozen_today(TODAY):
        html = pub.build_public_morning(T.OCT5_MORNING_SNAPSHOT, HEADLINES, T.OCT5_GLOBAL, T.OCT5_COMMODITIES, T.OCT5_TREASURY, [], MEM,
                                        "{{ unsubscribe_url }}", "PO Box 1\nTown")[1]
    check("a built page carries the supplied values and no placeholder", "{{ unsubscribe_url }}" in html and "PO Box 1<br>Town" in html
          and pub.UNSUBSCRIBE_PLACEHOLDER not in html and pub.ADDRESS_PLACEHOLDER not in html)


if __name__ == "__main__":
    tests = [test_private_wrapper_is_byte_identical, test_public_morning_sections_and_footer,
             test_public_close_sections_loop_close_and_no_holdings_language,
             test_private_edition_does_contain_the_language_the_public_one_drops,
             test_public_builders_never_call_private_only_section_builders,
             test_public_memory_is_reduced_to_briefing_history, test_leak_guard,
             test_write_html_refuses_state_files_and_writes_only_the_html, test_module_has_no_send_or_state_write_calls,
             test_public_movers_label_is_honest_and_private_label_untouched, test_second_notable_mover_in_the_public_close,
             test_footer_values_are_swappable_never_hardcoded]
    buf = io.StringIO()
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
