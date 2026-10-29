"""
test_desks.py — the desk framework: concurrency, per-desk timeout, failure isolation, the no-advice
guard, runtime logging, and the public/private split. Stand-in desks only (sleep / raise / hang);
no network, no files, no live clock dependence beyond short real sleeps (wall-clock timing is the
thing under test).

Run: python3 test_desks.py
"""
import ast
import builtins
import os
import subprocess
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desks import framework as fw  # noqa: E402
from desks import registry  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


def desk(name, fetch=None, build=None, private=False, timeout=5.0):
    mod = types.SimpleNamespace(NAME=name, PRIVATE=private, TIMEOUT=timeout,
                                fetch=fetch or (lambda: {"x": 1}),
                                build_section=build or (lambda facts, mode: f"<p>{name} {mode}</p>"))
    return fw.Desk(mod)


def sleeper(name, secs, **kw):
    def f():
        time.sleep(secs)
        return {"x": 1}
    return desk(name, fetch=f, **kw)


def run(desks, mode="close", **kw):
    log = []
    t0 = time.monotonic()
    results, run_ = fw.run_desks(desks, mode, log=log.append, **kw)
    return results, log, time.monotonic() - t0


def test_desks_run_concurrently_and_runtimes_are_logged():
    results, log, wall = run([sleeper("a", 0.4), sleeper("b", 0.4), sleeper("c", 0.4)])
    check("three 0.4s desks finish in well under the 1.2s a serial run needs", wall < 0.85, f"{wall:.2f}s")
    check("all three produced sections, in registry order", [r.name for r in results] == ["a", "b", "c"] and all(r.ok for r in results))
    check("each desk's runtime is measured", all(0.35 < r.runtime < 0.8 for r in results), str([r.runtime for r in results]))
    check("each desk logs '[DESK-<name>] ok <seconds>'", all(any(l.startswith(f"[DESK-{n}] ok 0.") for l in log) for n in "abc"), str(log))
    check("a summary line gives the total desk runtime and how long the brief actually waited",
          any(l.startswith("[DESKS] close: 3 of 3 sections; desks ran 1.") and "the brief waited 0." in l for l in log), str(log))


def test_desk_time_overlaps_the_callers_own_work():
    log = []
    r = fw.start_desks([sleeper("a", 0.5)], "morning", log=log.append)
    time.sleep(0.6)                                   # the pipeline's own fetching
    t0 = time.monotonic()
    results = r.collect()
    waited = time.monotonic() - t0
    check("start_desks returns immediately and the desk worked during the caller's sleep", waited < 0.15 and results[0].ok, f"waited {waited:.2f}s")
    check("and the summary line reports that the brief waited ~0s", any("the brief waited 0.0s" in l for l in log), str(log))
    t0 = time.monotonic()
    fw.start_desks([sleeper("a", 0.3)], "morning", log=log.append).collect()
    check("collect() right away waits only for the desk's own remaining time", 0.25 < time.monotonic() - t0 < 0.7)


def test_a_slow_desk_is_cut_off_at_its_own_timeout_and_the_rest_still_arrive():
    results, log, wall = run([sleeper("fast", 0.05), sleeper("slow", 3.0, timeout=0.3), sleeper("also_fast", 0.05)])
    by = {r.name: r for r in results}
    check("the slow desk is marked timeout and has no section", by["slow"].status == "timeout" and by["slow"].html is None)
    check("the timeout is logged with its limit", "[DESK-slow] timed out after 0.3s — section omitted" in log, str(log))
    check("the other desks are unaffected", by["fast"].ok and by["also_fast"].ok)
    check("the whole run took about the timeout, not the 3s the slow desk wanted", wall < 1.0, f"{wall:.2f}s")
    check("sections() returns only the working desks, in order", fw.DeskRun.sections(results) == ["<p>fast close</p>", "<p>also_fast close</p>"])


def test_each_desk_has_its_own_timeout():
    results, log, _ = run([sleeper("short", 0.5, timeout=0.2), sleeper("long", 0.5, timeout=3.0)])
    check("0.2s limit cuts a 0.5s desk; a 3s limit lets the same work finish",
          [r.status for r in results] == ["timeout", "ok"], str(results))


def test_failures_are_isolated_and_logged():
    def boom():
        raise RuntimeError("yahoo said no")

    def bad_build(f, m):
        raise ValueError("template broke")
    results, log, _ = run([desk("ok1"), desk("boom", fetch=boom), desk("badbuild", build=bad_build), desk("ok2")])
    st = {r.name: r.status for r in results}
    check("raising fetch / raising build_section are errors; neighbours are fine", st == {"ok1": "ok", "boom": "error", "badbuild": "error", "ok2": "ok"}, str(st))
    check("the error is logged with its type and message", any(l.startswith("[DESK-boom] error: RuntimeError: yahoo said no — section omitted") for l in log), str(log))
    check("so is the build failure", any("[DESK-badbuild] error: ValueError: template broke" in l for l in log))
    check("two sections still come out", len(fw.DeskRun.sections(results)) == 2)


def test_empty_and_skipped_desks_are_logged_and_omitted():
    def skip():
        raise fw.DeskSkipped("SEC_USER_AGENT is not set")
    results, log, _ = run([desk("nothing", fetch=lambda: {}), desk("none_html", build=lambda f, m: None), desk("sec", fetch=skip)])
    check("empty facts / no html -> status empty", [r.status for r in results[:2]] == ["empty", "empty"])
    check("'[DESK-nothing] empty — section omitted' is logged", any(l.startswith("[DESK-nothing] empty — section omitted") for l in log), str(log))
    check("a deliberately skipped desk logs '[DESK-sec] skipped' with the reason", "[DESK-sec] skipped: SEC_USER_AGENT is not set" in log, str(log))
    check("skipped is not an error and has no section", results[2].status == "skipped" and results[2].html is None)


def test_the_no_advice_guard_blocks_a_desk_that_slips():
    for bad in ("Analysts rate it a buy", "A price target of 400", "Strong upside here", "The thesis is intact", "Hold the position",
                "A sell-off is coming", "Its rating is strong", "Score 145"):
        results, log, _ = run([desk("risky", build=lambda f, m, b=bad: f"<p>{b}</p>")])
        check(f"blocked: '{bad}'", results[0].status == "blocked" and results[0].html is None, str(results))
    check("the block is logged loudly", any(l.startswith("[DESK-risky] blocked by the no-advice guard") for l in log), str(log))
    results, _, _ = run([desk("clean", build=lambda f, m: "<p>The S&amp;P 500 is up 1.2% over a week.</p>")])
    check("a clean desk passes the same guard", results[0].ok)


def test_a_hung_desk_cannot_keep_the_process_alive():
    code = ("import sys, time, types; sys.path.insert(0, %r)\n"
            "from desks import framework as fw\n"
            "m = types.SimpleNamespace(NAME='hung', PRIVATE=False, TIMEOUT=0.3, fetch=lambda: time.sleep(60), build_section=lambda f, m: '<p>x</p>')\n"
            "fw.run_desks([fw.Desk(m)], 'close', log=print)\n" % os.path.dirname(os.path.abspath(__file__)))
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    check("a desk stuck in a 60s call does not stop the interpreter exiting", r.returncode == 0 and time.monotonic() - t0 < 5, f"{time.monotonic() - t0:.1f}s {r.stderr[-200:]}")
    check("and the timeout was logged", "[DESK-hung] timed out after 0.3s" in r.stdout, r.stdout)


def test_a_late_finisher_cannot_change_a_result_already_reported():
    results, log, _ = run([sleeper("late", 0.5, timeout=0.15)])
    time.sleep(0.5)
    check("a desk that finishes after its timeout stays 'timeout'", results[0].status == "timeout" and results[0].html is None)


def test_private_desks_are_never_started_in_a_public_run():
    started = []
    spy = desk("mine", fetch=lambda: started.append(1) or {"x": 1}, private=True)
    pub = desk("market")
    results, log, _ = run([spy, pub], include_private=False)
    time.sleep(0.1)
    check("the private desk's fetch is never called in a public run", started == [], str(started))
    check("only the market-wide desk appears", [r.name for r in results] == ["market"])
    results, _, _ = run([spy, pub], include_private=True)
    check("a private run includes both", [r.name for r in results] == ["mine", "market"] and started == [1])
    check("the Trends desk is public", all(not d.private for d in registry.ALL) and fw.select(registry.ALL, False) == registry.ALL)


def test_desks_take_no_arguments_so_they_cannot_be_handed_private_data():
    import inspect
    check("fetch() takes no required arguments", all(not [p for p in inspect.signature(d.fetch).parameters.values()
                                                       if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)] for d in registry.ALL))
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "desks", "framework.py")).read()
    imported = {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    check("the framework imports nothing that reaches memory, picks or the MCP server",
          not {m for m in imported if m and ("pippy" in m or "openbell" in m or m.startswith("mcp"))}, str(imported))
    opened = []
    real = builtins.open
    builtins.open = lambda f, *a, **k: (opened.append(str(f)), real(f, *a, **k))[1]
    try:
        run([desk("a"), desk("b")])
    finally:
        builtins.open = real
    check("running desks opens no files", opened == [], str(opened))


def test_registry_is_well_formed():
    names = [d.name for d in registry.ALL]
    check("desk names are unique, lowercase ids", len(names) == len(set(names)) and all(n.islower() and n.isidentifier() for n in names), str(names))
    check("every registered desk has a positive timeout of at most 25s", all(0 < d.timeout <= 25 for d in registry.ALL))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
