"""
test_scan_probe_workflow.py — pins the safety properties of .github/workflows/scan_probe.yml.

The probe is a one-off measurement. This test fails if the workflow could ever send email, write state,
run on its own, reach another host through its own code, or start inside a brief's send window.
It parses the YAML, inspects the inline script's AST, and exercises the script's functions with fakes:
no network, no files outside a temp dir, no live clock.

Needs PyYAML (test-time only; the workflows do not run tests).   Run: python3 test_scan_probe_workflow.py
"""
import ast
import builtins
import datetime
import json
import os
import re
import sys
import tempfile
import types
import urllib.error

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, ".github", "workflows", "scan_probe.yml")
RAW = open(PATH, encoding="utf-8").read()
DOC = yaml.safe_load(RAW)
JOB = DOC["jobs"]["probe"]
SCRIPT = JOB["steps"][0]["run"]
TREE = ast.parse(SCRIPT)

# SAFETY OF THE TEST ITSELF. This file executes the probe's script (and, when mutation-testing, deliberately
# broken variants of it). On Oct 29 a variant containing os.system("git push") really pushed from the repo.
# So: (1) run in an empty temp directory, (2) point git at nothing so any git command fails, and (3) give the
# executed script a sealed `os` that raises on process or file-mutation calls (see _SealedOS / load()).
os.chdir(tempfile.mkdtemp(prefix="probe-test-"))
os.environ["GIT_DIR"] = os.path.join(os.getcwd(), "no-such-git-dir")
os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.getcwd())

# (4) from here on, importing a process / file-copy / mail module raises, wherever the import statement is
# (module level OR inside a function). Everything this test file needs has already been imported above.
_real_import = builtins.__import__
_BANNED_IMPORTS = {"subprocess", "shutil", "smtplib", "ctypes", "ftplib", "telnetlib", "pty"}


def _guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in _BANNED_IMPORTS:
        raise AssertionError(f"sealed: the probe script tried to import {name}")
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _guarded_import

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


_REFUSED = {"system", "popen", "remove", "unlink", "rename", "replace", "makedirs", "mkdir", "rmdir", "removedirs", "chmod", "chown",
            "symlink", "link", "truncate", "execv", "execve", "execvp", "execl", "execlp", "spawnv", "spawnl", "fork", "forkpty", "kill", "killpg",
            "putenv", "unsetenv", "chdir", "startfile", "posix_spawn"}


class _SealedOS:
    """What the executed script sees as `os`: reads are fine, process and file-mutation calls raise."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name in _REFUSED:
            raise AssertionError(f"sealed: the probe script tried to use os.{name}")
        return getattr(self._real, name)


class _SealedModule:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"sealed: the probe script tried to use {self._name}.{attr}")


def load(script=None):
    """Fresh namespace with the script's functions (not run: __name__ != '__main__'). The script's os (and any
    process or socket module it imports) is replaced by a sealed stand-in after import."""
    ns = {"__name__": "probe"}
    exec(compile(script or SCRIPT, "scan_probe_inline", "exec"), ns)
    ns["os"] = _SealedOS(os)
    for mod in ("subprocess", "shutil", "socket", "smtplib", "ctypes"):
        if mod in ns:
            ns[mod] = _SealedModule(mod)
    return ns


# ── workflow shape ───────────────────────────────────────────────────────────

def test_it_can_only_be_started_by_hand():
    on = DOC.get("on", DOC.get(True))             # PyYAML reads the bare key `on` as the boolean True
    check("the only trigger is workflow_dispatch", set(on) == {"workflow_dispatch"}, str(on))
    for bad in ("schedule", "push", "pull_request", "pull_request_target", "workflow_run", "repository_dispatch", "workflow_call"):
        check(f"no '{bad}' trigger", bad not in on)
    check("workflow_dispatch takes no inputs (nothing to inject through)", not (on["workflow_dispatch"] or {}).get("inputs"))


def test_it_has_no_credentials_and_no_token_scopes():
    dumped = json.dumps(DOC)
    check("top-level permissions are empty: the job token has no scopes (a push would be refused)", DOC.get("permissions") == {}, str(DOC.get("permissions")))
    check("the job does not widen permissions", "permissions" not in JOB)
    check("no secrets are referenced anywhere (so the Gmail credentials do not exist in this job)", "secrets" not in dumped.lower())
    check("no ${{ }} expressions at all", "${{" not in dumped)
    check("no env block at any level", "env" not in DOC and "env" not in JOB and all("env" not in st for st in JOB["steps"]))
    check("no 'uses:' step: the repository is never checked out and no third-party action runs", all("uses" not in st for st in JOB["steps"]) and len(JOB["steps"]) == 1)
    check("one job, one step, a hard 6-minute cap", list(DOC["jobs"]) == ["probe"] and JOB["timeout-minutes"] <= 6, str(JOB.get("timeout-minutes")))
    check("it runs the inline script with python3", JOB["steps"][0]["shell"].startswith("python3"))


def test_the_comments_describe_the_host_restriction_honestly():
    check("states the restriction is in code, not a network firewall", "IN CODE, not a network firewall" in RAW)
    check("lists the protected windows and the suggested slots", "13:00-15:00" in RAW and "16:30-17:45" in RAW and "19:30-21:20" in RAW and "12:15 UTC" in RAW)


# ── the script: what it may import, call and write ───────────────────────────

ALLOWED_IMPORTS = {"concurrent.futures", "datetime", "json", "math", "os", "queue", "random", "re", "statistics", "sys", "threading",
                   "time", "urllib.error", "urllib.parse", "urllib.request"}


def test_the_script_is_standard_library_only_and_cannot_reach_project_code():
    imported = set()
    for n in ast.walk(TREE):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module)
    check("imports are a fixed standard-library allowlist", imported <= ALLOWED_IMPORTS, str(imported - ALLOWED_IMPORTS))
    for banned in ("subprocess", "smtplib", "socket", "shutil", "requests", "yfinance", "pippy_mcp", "openbell", "public_edition", "brief_common",
                   "watchlist_brief", "email", "ftplib", "http.client", "ctypes", "importlib", "pickle"):
        check(f"does not import {banned}", banned not in imported)
    text = SCRIPT.lower()
    for word in ("git ", "git\n", "curl", "wget", "pip install", "send_email", "smtp", "gmail", "app_password", "save_memory", "pippy_memory", "send_log", "picks_cache"):
        check(f"the script never mentions '{word.strip()}'", word not in text)


def test_the_script_writes_only_the_runner_local_step_summary():
    opens = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
    check("exactly one open() call", len(opens) == 1, str(len(opens)))
    o = opens[0]
    fn = next(f for f in ast.walk(TREE) if isinstance(f, ast.FunctionDef) and o in ast.walk(f))
    check("it is inside write_summary()", fn.name == "write_summary", fn.name)
    check("opened in append mode", len(o.args) >= 2 and isinstance(o.args[1], ast.Constant) and o.args[1].value == "a")
    src_path = ast.get_source_segment(SCRIPT, next(a for a in ast.walk(fn) if isinstance(a, ast.Assign) and any(getattr(t, "id", "") == "path" for t in a.targets)))
    check("the path is $GITHUB_STEP_SUMMARY and nothing else", "GITHUB_STEP_SUMMARY" in src_path and getattr(o.args[0], "id", "") == "path", src_path)
    dangerous = {"system", "popen", "remove", "rename", "replace", "makedirs", "mkdir", "unlink", "rmdir", "write_text", "write_bytes", "chmod", "symlink", "exec", "eval", "compile", "__import__"}
    called = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(TREE) if isinstance(n, ast.Call)}
    check("no file-system mutation, process or dynamic-execution calls", not (called & dangerous), str(called & dangerous))
    environ_reads = [ast.get_source_segment(SCRIPT, n) for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "get"
                     and "environ" in ast.get_source_segment(SCRIPT, n.func)]
    check("it reads only GITHUB_STEP_SUMMARY and RUNNER_OS from the environment",
          all(("GITHUB_STEP_SUMMARY" in e or "RUNNER_OS" in e) for e in environ_reads) and len(environ_reads) == 2, str(environ_reads))


def test_every_url_in_the_script_is_on_the_allowed_hosts():
    urls = re.findall(r"https?://[^\s\"'%/]+", SCRIPT)
    hosts = {u.split("//")[1] for u in urls}
    ns = load()
    check("every literal URL host is allowed", hosts <= set(ns["ALLOWED_HOSTS"]), str(hosts))
    check("exactly the three hosts, no wildcard", ns["ALLOWED_HOSTS"] == ("www.nasdaqtrader.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"))
    check("no plain http anywhere", "http://" not in SCRIPT)


# ── the in-code host restriction, request cap, redirects and protected windows ─

def test_the_host_check_rejects_everything_but_the_three_hosts():
    ns = load()
    ok = ["https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "https://query2.finance.yahoo.com/v7/finance/spark?symbols=A",
          "https://query1.finance.yahoo.com/v7/finance/spark?symbols=A", "https://QUERY2.finance.yahoo.com/x"]
    for u in ok:
        try:
            ns["assert_host_allowed"](u)
            good = True
        except ValueError:
            good = False
        check(f"allowed: {u[:60]}", good)
    bad = ["http://www.nasdaqtrader.com/x", "https://evil.com/", "https://www.nasdaqtrader.com.evil.com/x", "https://query2.finance.yahoo.com@evil.com/",
           "https://evil.com/?u=www.nasdaqtrader.com", "https://www.nasdaqtrader.com:8443/x", "ftp://www.nasdaqtrader.com/x", "//www.nasdaqtrader.com/x",
           "https://finance.yahoo.com/x", "https://user:pw@query1.finance.yahoo.com/x", "file:///etc/passwd", "https://api.github.com/", "https://sec.gov/"]
    for u in bad:
        try:
            ns["assert_host_allowed"](u)
            rejected = False
        except ValueError:
            rejected = True
        check(f"rejected: {u[:60]}", rejected)


def test_http_get_checks_the_host_before_anything_else_and_never_follows_redirects():
    ns = load()
    opened = []
    ns["urllib"].request.build_opener = lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("network touched"))
    try:
        ns["http_get"]("https://evil.com/")
        raised = False
    except ValueError:
        raised = True
    check("a disallowed URL raises before an opener is even built", raised and opened == [])
    req = types.SimpleNamespace(full_url="https://www.nasdaqtrader.com/x")
    try:
        ns["NoRedirect"]().redirect_request(req, None, 302, "Found", {}, "https://evil.com/")
        refused = False
    except urllib.error.HTTPError:
        refused = True
    check("a redirect (to anywhere) is refused rather than followed", refused)
    src = SCRIPT[SCRIPT.index("def http_get"):SCRIPT.index("def parse_universe")]
    check("http_get calls assert_host_allowed first and uses the no-redirect opener", src.index("assert_host_allowed(url)") < src.index("build_opener(NoRedirect)"))


def test_the_request_cap_and_deadline_are_hard():
    ns = load()
    ns["urllib"].request.build_opener = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network touched"))
    check("cap is at most 450 and the deadline at most 300s", ns["MAX_REQUESTS"] <= 450 and ns["DEADLINE_S"] <= 300)
    ns["_budget"]["requests"] = ns["MAX_REQUESTS"]
    try:
        ns["http_get"]("https://www.nasdaqtrader.com/x")
        stopped = False
    except RuntimeError:
        stopped = True
    check("at the cap, the next request is refused before any network call", stopped)
    ns["_budget"]["requests"], ns["_budget"]["t0"] = 0, ns["time"].monotonic() - 10_000
    try:
        ns["http_get"]("https://www.nasdaqtrader.com/x")
        stopped = False
    except RuntimeError:
        stopped = True
    check("past the deadline, likewise", stopped)


def test_protected_windows_cover_every_scheduled_send_with_a_margin():
    ns = load()
    blocked = lambda h, m: ns["in_blocked_window"](datetime.datetime(2026, 10, 27, h, m, tzinfo=datetime.timezone.utc))
    # real schedule (UTC): morning 13:30, case study 17:00 (weekdays), close 20:00; catch-up retriggers ~+45 min
    for name, (h, m) in {"morning": (13, 30), "case study": (17, 0), "close": (20, 0), "morning catch-up": (14, 15), "close catch-up": (20, 45)}.items():
        base = h * 60 + m
        for off in range(-30, 16):
            t = base + off
            check(f"{name}: a start {off:+d} min from its run is blocked", blocked(t // 60, t % 60), f"{t // 60}:{t % 60:02d}")
    check("the 30-minute margin is real: starts 31+ minutes before a run are blocked only if the 6-minute run would reach into the window",
          blocked(12, 55) and not blocked(12, 53) and blocked(19, 25) and not blocked(19, 24) and blocked(16, 25) and not blocked(16, 24))
    for name, (h, m) in {"pre-market slot": (12, 15), "mid-session slot": (15, 30), "after-close slot": (22, 15), "mid-session repeat": (15, 40)}.items():
        check(f"suggested {name} {h}:{m:02d} UTC is allowed", not blocked(h, m))
    check("window edges: 15:00 and 17:45 and 21:20 are open again", not blocked(15, 0) and not blocked(17, 45) and not blocked(21, 20))
    check("naive datetimes work too", ns["in_blocked_window"](datetime.datetime(2026, 10, 27, 13, 30)))


def test_main_refuses_inside_a_window_and_makes_no_request():
    ns = load()
    calls = []
    ns["http_get"] = lambda *a, **k: calls.append(a) or (200, {}, b"", 0.0)
    buf = []
    ns["say"] = buf.append
    rc = ns["main"](now=datetime.datetime(2026, 10, 27, 13, 40, tzinfo=datetime.timezone.utc))
    check("at 13:40 UTC (10 minutes into the morning run) it exits cleanly without a single request", rc == 0 and calls == [] and "REFUSING TO RUN" in buf[0], str(buf))


# ── behaviour with fakes: caps, circuit breaker, writes, verdict ─────────────

NASDAQ = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
          + "\n".join(f"{s}|{s} Inc|Q|N|N|100|N|N" for s in [f"A{c}{d}" for c in "ABCDEFGHIJ" for d in "ABCDEFGHIJ"])
          + "\nTSTX|Test Co|Q|Y|N|100|N|N\nETFX|Some ETF|Q|N|N|100|Y|N\nFile Creation Time: 1\n")
OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
         + "\n".join(f"{s}|{s} Corp|N|{s}|N|100|N|{s}" for s in [f"B{c}{d}" for c in "ABCDEFGHIJ" for d in "ABCDEFGHIJ"]) + "\nBRK.B|Berkshire|N|BRK.B|N|100|N|BRK.B\n")


def spark_body(symbols):
    return json.dumps({"spark": {"result": [{"symbol": s, "response": [{"indicators": {"quote": [{"close": [1.0, 2.0, None, 3.0]}]}}]} for s in symbols]}}).encode()


class FakeNet:
    def __init__(self, fail_from=None, code=429):
        self.calls, self.fail_from, self.code = [], fail_from, code

    def __call__(self, url, timeout=20):
        ns_assert(url)
        self.calls.append(url)
        if "nasdaqtrader" in url:
            return 200, {}, (NASDAQ if "nasdaqlisted" in url else OTHER).encode(), 0.2
        if self.fail_from is not None and len([c for c in self.calls if "spark" in c]) >= self.fail_from:
            return self.code, {"Retry-After": "30"}, b"slow down", 0.05
        syms = re.search(r"symbols=([^&]*)", url).group(1).split(",")
        return 200, {}, spark_body(syms), 0.4


def ns_assert(url):
    from urllib.parse import urlsplit
    assert urlsplit(url).hostname in ("www.nasdaqtrader.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"), url


def run_main(net, **overrides):
    ns = load()
    ns["http_get"] = net
    ns["time"] = types.SimpleNamespace(monotonic=ns["time"].monotonic, sleep=lambda s: None)
    ns["say"] = lambda *a: None
    ns.update(overrides)
    tmp = tempfile.mkdtemp()
    os.environ["GITHUB_STEP_SUMMARY"] = os.path.join(tmp, "summary.md")
    opened = []
    real = builtins.open
    builtins.open = lambda f, *a, **k: (opened.append((str(f), a[0] if a else k.get("mode", "r"))), real(f, *a, **k))[1]
    try:
        rc = ns["main"](now=datetime.datetime(2026, 10, 27, 15, 30, tzinfo=datetime.timezone.utc))
    finally:
        builtins.open = real
        out = os.environ.pop("GITHUB_STEP_SUMMARY")
    return ns, rc, opened, out


def test_a_clean_run_measures_every_phase_and_writes_only_the_summary():
    net = FakeNet()
    ns, rc, opened, summary = run_main(net)
    text = open(summary).read()
    check("exit 0", rc == 0)
    check("requests: 2 Nasdaq files + 10 per level (1, 4, 8; the 200-symbol fake universe) + 5 for the 1y range + 10 repeat",
          len(net.calls) == 2 + 30 + 5 + 10, str(len(net.calls)))
    check("never above the cap", len(net.calls) <= ns["MAX_REQUESTS"])
    check("every request went to an allowed host", all(__import__("urllib.parse").parse.urlsplit(u).hostname in ns["ALLOWED_HOSTS"] for u in net.calls))
    check("the only file opened is the step summary, in append mode", opened == [(summary, "a")], str(opened))
    check("the summary carries the levels, the 1y feasibility and a verdict", '"levels"' in text and '"range_1y"' in text and "VERDICT" in text)
    u, rows = ns["parse_universe"]({"nasdaqlisted.txt": NASDAQ, "otherlisted.txt": OTHER})
    check("parse_universe drops test issues, ETFs and BRK.B-style tickers", len(u) == 200 and "TSTX" not in u and "ETFX" not in u and "BRK.B" not in u, str(len(u)))


def test_the_circuit_breaker_stops_after_a_429_and_does_not_escalate():
    net = FakeNet(fail_from=14, code=429)               # level 1 (10 requests) clean; the 4th request of level 2 is throttled
    ns, rc, opened, summary = run_main(net)
    spark = [c for c in net.calls if "spark" in c]
    text = open(summary).read()
    report = json.loads(text.split("```json")[1].split("```")[0])
    check("it stopped issuing after the 429 (at most the other 3 workers' in-flight requests follow)", len(spark) <= 14 + 3, str(len(spark)))
    check("only concurrency 1 and 4 were measured; 8 was never attempted", sorted(report["levels"]) == ["1", "4"], str(sorted(report["levels"])))
    check("level 4 records that it stopped after a 429", report["levels"]["4"]["stopped_after_429"] is True and report["levels"]["4"]["n429"] >= 1)
    check("no 1-year range test and no repeat were run after throttling", report["range_1y"] is None and report["repeat_c4"] is None)
    check("the verdict is SAMPLED (clean at 1, throttled at 4)", "VERDICT **SAMPLED**" in text, text[-200:])


def test_a_429_at_concurrency_one_keeps_the_25_name_list():
    net = FakeNet(fail_from=3, code=429)
    ns, rc, opened, summary = run_main(net)
    text = open(summary).read()
    check("throttled even at concurrency 1 -> KEEP_25_NAMES", "VERDICT **KEEP_25_NAMES**" in text, text[-200:])


def test_a_failed_nasdaq_download_keeps_the_25_name_list_and_makes_no_yahoo_request():
    class Down(FakeNet):
        def __call__(self, url, timeout=20):
            self.calls.append(url)
            return 503, {}, b"", 0.1
    net = Down()
    ns, rc, opened, summary = run_main(net)
    check("only the two Nasdaq requests were made", len(net.calls) == 2 and all("nasdaqtrader" in c for c in net.calls), str(net.calls))
    check("verdict KEEP_25_NAMES", "KEEP_25_NAMES" in open(summary).read())


def test_the_request_cap_is_respected_end_to_end():
    ns = load()
    count = []

    class Resp:
        status, headers = 200, {}

        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout=None):
            count.append(req.full_url)
            if "nasdaqtrader" in req.full_url:
                return Resp((NASDAQ if "nasdaqlisted" in req.full_url else OTHER).encode())
            return Resp(spark_body(re.search(r"symbols=([^&]*)", req.full_url).group(1).split(",")))
    ns["urllib"].request.build_opener = lambda *a: Opener()       # the REAL http_get (host check + counter) still runs
    ns["time"] = types.SimpleNamespace(monotonic=ns["time"].monotonic, sleep=lambda s: None)
    ns["say"] = lambda *a: None
    ns["MAX_REQUESTS"] = 12
    tmp = tempfile.mkdtemp()
    os.environ["GITHUB_STEP_SUMMARY"] = os.path.join(tmp, "s.md")
    real_build = __import__("urllib.request").request.build_opener
    try:
        rc = ns["main"](now=datetime.datetime(2026, 10, 27, 15, 30, tzinfo=datetime.timezone.utc))
    finally:
        os.environ.pop("GITHUB_STEP_SUMMARY")
    check("with a cap of 12 the script still finishes and the real counter held it at 12", rc == 0 and ns["_budget"]["requests"] == 12 and len(count) == 12, f"{ns['_budget']['requests']} / {len(count)}")


def test_verdict_and_projection_arithmetic():
    ns = load()
    mk = lambda c, n=20, n429=0, n5xx=0, mean=0.5, p95=0.9, err=0: {"concurrency": c, "requests": n, "n429": n429, "n5xx": n5xx, "n_errors": err, "mean_s": mean, "p95_s": p95}
    check("298 requests at 0.5s mean (+0.1s pause) over 4 workers -> 44.7s: too slow for FULL (limit 40s)", ns["project_full_scan"](5942, mk(4)) == 44.7, str(ns["project_full_scan"](5942, mk(4))))
    check("the same latency over 8 workers -> half (22.35s)", abs(ns["project_full_scan"](5942, mk(8)) - 22.35) <= 0.051)
    check("0.2s latency over 4 workers -> about 22.35s", abs(ns["project_full_scan"](5942, mk(4, mean=0.2)) - 22.35) <= 0.051)
    v = ns["verdict"]
    check("clean, fast, projects under 40s -> FULL", v(True, mk(1), mk(4, mean=0.2), 5942)[0] == "FULL")
    check("clean but projects to 44.7s (over the 40s limit) -> SAMPLED", v(True, mk(1), mk(4), 5942)[0] == "SAMPLED")
    check("429s at 4 but not at 1 -> SAMPLED", v(True, mk(1), mk(4, n429=2, mean=0.2), 5942)[0] == "SAMPLED")
    check("one 429 in 20 (5%) at concurrency 4 is not clean", v(True, mk(1), mk(4, n429=1, mean=0.2), 5942)[0] == "SAMPLED")
    check("p95 over 3s -> SAMPLED", v(True, mk(1), mk(4, mean=0.2, p95=3.5), 5942)[0] == "SAMPLED")
    check("5xx at 4 -> SAMPLED", v(True, mk(1), mk(4, mean=0.2, n5xx=1), 5942)[0] == "SAMPLED")
    check("429 at concurrency 1 -> KEEP_25_NAMES", v(True, mk(1, n429=1), None, 5942)[0] == "KEEP_25_NAMES")
    check("Nasdaq download failed -> KEEP_25_NAMES", v(False, None, None, 0)[0] == "KEEP_25_NAMES")
    check("concurrency 4 not measured -> SAMPLED", v(True, mk(1), None, 5942)[0] == "SAMPLED")
    p = ns["percentile"]
    check("percentile helper", p([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10 and p([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5 and p([], 50) is None)


def test_the_test_harness_itself_cannot_be_used_to_run_a_git_push():
    """The Oct 29 incident, reproduced safely: a script variant that shells out to git must be stopped."""
    evil = SCRIPT.replace('say("scan probe starting', 'os.system("git push"); say("scan probe starting')
    check("the variant really contains the shell-out", 'os.system("git push")' in evil)
    ns = load(evil)
    ns["say"] = lambda *a: None
    ns["http_get"] = FakeNet()
    ns["time"] = types.SimpleNamespace(monotonic=ns["time"].monotonic, sleep=lambda s: None)
    try:
        ns["main"](now=datetime.datetime(2026, 10, 27, 15, 30, tzinfo=datetime.timezone.utc))
        stopped = False
    except AssertionError as e:
        stopped = "os.system" in str(e)
    check("os.system is refused by the sealed os, so nothing ran", stopped)
    ns = load(SCRIPT.replace('say("scan probe starting', 'import subprocess; subprocess.run(["git", "push"]); say("scan probe starting'))
    ns["say"] = lambda *a: None
    try:
        ns["main"](now=datetime.datetime(2026, 10, 27, 15, 30, tzinfo=datetime.timezone.utc))
        stopped = False
    except AssertionError as e:
        stopped = "subprocess" in str(e)
    check("an import of subprocess inside a function is refused too", stopped)
    try:
        load(SCRIPT.replace("import json\n", "import json\n          import subprocess\n" if False else "import json\nimport subprocess\n", 1))
        stopped = False
    except AssertionError as e:
        stopped = "subprocess" in str(e)
    check("and so is a module-level import", stopped)
    ns = load(SCRIPT.replace('say("scan probe starting', 'open("state.json", "w").write("x"); say("scan probe starting'))
    ns["say"] = lambda *a: None
    ns["http_get"] = FakeNet()
    ns["time"] = types.SimpleNamespace(monotonic=ns["time"].monotonic, sleep=lambda s: None)
    before = set(os.listdir("."))
    try:
        ns["main"](now=datetime.datetime(2026, 10, 27, 15, 30, tzinfo=datetime.timezone.utc))
    except Exception:
        pass
    leftover = set(os.listdir(".")) - before
    check("a stray file write lands in the throwaway temp directory, never in the repo", os.path.abspath(".").startswith(tempfile.gettempdir()) or "probe-test-" in os.getcwd(), os.getcwd())
    check("and the repo has no new state.json", not os.path.exists(os.path.join(HERE, "state.json")))
    check("git itself is pointed at nothing during the tests", os.environ.get("GIT_DIR", "").endswith("no-such-git-dir"))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
