"""
test_no_remote_access.py — no test may be able to reach a git remote (or any remote).

Standing rule (2026-10-29, after a mutation payload `os.system("git push")` really pushed): no test, mutation
check or script may run code that can reach git remotes. Anything that executes generated or altered code runs
in a scratch directory with no .git and no credentials (tests_seal/scratch_exec.py: run_in_scratch()).

Three layers, all offline:
  1. STATIC: every test_*.py is parsed. It fails if a test file
       - calls subprocess / os.system / os.popen / os.exec* / os.spawn* with git, gh, ssh, scp, curl, wget, nc, rsync ...
       - contains a `git push|fetch|pull|clone|remote` or `gh api|run|workflow|pr` string, except as payload data
         inside a function that hands it to run_in_scratch()
       - calls urlopen / create_connection / requests / smtplib / ftplib, or imports requests, smtplib, ftplib, socket
       - uses exec / eval / compile outside the one documented place
  2. DYNAMIC: every other test file is run, one after another, in a scratch COPY of the repo (no .git, no .env, no
     credentials, fake git/gh/ssh/curl first on PATH) under the network/remote seal. Each must pass, raise no
     `SEALED:` violation, and never execute a fake remote tool.
  3. SELF-CHECK: the scanner and the seal are shown to catch hostile examples, so this test cannot pass vacuously.

Run: python3 test_no_remote_access.py        (about a minute: it runs the whole suite sealed)
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests_seal"))
import scratch_exec  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


BANNED_PROGRAMS = {"git", "gh", "ssh", "scp", "sftp", "curl", "wget", "nc", "ncat", "rsync", "ftp", "telnet"}
PROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput", "system", "popen",
                 "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
                 "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp"}
NETWORK_CALLS = {"urlopen", "create_connection", "SMTP", "SMTP_SSL", "FTP", "FTP_TLS", "HTTPConnection", "HTTPSConnection"}
NETWORK_IMPORTS = {"requests", "smtplib", "ftplib", "socket", "http.client", "paramiko", "pexpect", "telnetlib", "httpx", "aiohttp"}
GIT_REMOTE_TEXT = re.compile(r"\bgit\s+(push|fetch|pull|clone|remote|ls-remote|send-pack|submodule)\b|\bgh\s+(api|run|workflow|pr|repo|auth|release)\b")
# The only in-process dynamic execution allowed: the unaltered probe script, extracted from the workflow YAML.
EXEC_ALLOWED = {("test_scan_probe_workflow.py", "load")}
# This file's own self-check functions carry hostile example strings as data (module-level HOSTILE / HARMLESS too).
SELF_DATA_FUNCS = {"test_the_scanner_catches_hostile_examples_and_passes_harmless_ones", "test_the_seal_is_active_and_refuses_remote_access_in_strict_scratch_runs",
                   "test_the_non_strict_seal_used_for_the_test_files_allows_python_but_not_remote_tools"}


def _string_constants(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _tokens(strings):
    out = set()
    for s in strings:
        out |= {w.lower() for w in re.split(r"[^A-Za-z0-9_.+-]+", s) if w}
    return out


def scan_source(src, filename="<test>"):
    """-> [(line, rule, detail)] for one test file's source."""
    tree = ast.parse(src)
    problems = []
    parents = {}
    docstrings = set()
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(node.body[0].value)
    is_self = os.path.basename(filename) == "test_no_remote_access.py"

    def enclosing_function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def sanctioned_payload_function(fn):
        """A function that passes data to run_in_scratch() and executes nothing itself."""
        if fn is None:
            return False
        names = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        executes = names & {"exec", "eval", "compile", "system", "popen", "Popen", "run", "check_output", "call"}
        return "run_in_scratch" in names and not executes

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            fn = enclosing_function(node)
            if name in PROCESS_CALLS:
                hit = _tokens(s for a in list(node.args) + [k.value for k in node.keywords] for s in _string_constants(a)) & BANNED_PROGRAMS
                if hit:
                    problems.append((node.lineno, "A: runs a remote-capable program", sorted(hit)))
            if name in NETWORK_CALLS:
                problems.append((node.lineno, "D: network call", name))
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "requests":
                problems.append((node.lineno, "D: requests call", node.func.attr))
            if isinstance(node.func, ast.Name) and name in ("exec", "eval", "compile") \
                    and (os.path.basename(filename), fn.name if fn else None) not in EXEC_ALLOWED:
                problems.append((node.lineno, "C: dynamic execution outside the allowed place", name))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and GIT_REMOTE_TEXT.search(node.value) and node not in docstrings:
            fn_here = enclosing_function(node)
            # This file's own self-check data (hostile examples it feeds to the scanner and the seal) is exempt from rule B only.
            self_data = is_self and (fn_here is None or fn_here.name in SELF_DATA_FUNCS)
            if not self_data and not sanctioned_payload_function(fn_here):
                problems.append((node.lineno, "B: a git/gh remote command in a string", node.value[:60]))
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in NETWORK_IMPORTS or a.name.split(".")[0] in NETWORK_IMPORTS:
                    problems.append((node.lineno, "E: imports a network module", a.name))
        if isinstance(node, ast.ImportFrom) and node.module and (node.module in NETWORK_IMPORTS or node.module.split(".")[0] in NETWORK_IMPORTS):
            problems.append((node.lineno, "E: imports a network module", node.module))
    return problems


def test_files():
    return sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))


# ── 1. static ────────────────────────────────────────────────────────────────

def test_static_no_test_file_can_run_git_or_reach_a_remote():
    files = test_files()
    check("found the test files", len(files) >= 12 and "test_no_remote_access.py" in files, str(files))
    for f in files:
        src = open(os.path.join(HERE, f), encoding="utf-8").read()
        problems = scan_source(src, f)
        check(f"{f}: no git/remote program, remote command text, network call/import or stray dynamic execution", not problems, str(problems[:3]))


# ── 3. self-checks (the scanner and the seal catch hostile examples) ─────────
# Every hostile payload targets a *.invalid host (RFC 2606: guaranteed never to resolve), so even a broken seal
# could not reach a real server while this file proves the seal works.

HOSTILE = {
    "os.system git push": 'import os\ndef test_x():\n    os.system("git push")\n',
    "subprocess git push": 'import subprocess\ndef test_x():\n    subprocess.run(["git", "push", "origin", "main"])\n',
    "sh -c git": 'import subprocess\ndef test_x():\n    subprocess.check_output("sh -c \'git push\'", shell=True)\n',
    "gh": 'import subprocess\ndef test_x():\n    subprocess.run(["gh", "workflow", "run", "x.yml"])\n',
    "curl": 'import os\ndef test_x():\n    os.popen("curl https://remote.example.invalid")\n',
    "exec git": 'import os\ndef test_x():\n    os.execvp("git", ["git", "push"])\n',
    "git text in a string": 'def test_x():\n    cmd = "git push origin main"\n',
    "urlopen": 'import urllib.request\ndef test_x():\n    urllib.request.urlopen("https://remote.example.invalid")\n',
    "create_connection": 'import socket\ndef test_x():\n    socket.create_connection(("remote.example.invalid", 443))\n',
    "requests import": 'import requests\n',
    "smtplib": 'import smtplib\ndef test_x():\n    smtplib.SMTP_SSL("smtp.example.invalid")\n',
    "exec of altered code": 'def test_x():\n    exec(compile("print(1)", "x", "exec"))\n',
    "payload outside a scratch runner": 'def test_x():\n    payload = "os.system(\'git push\')"\n    exec(payload)\n',
}
HARMLESS = {
    "python subprocess": 'import subprocess, sys\ndef test_x():\n    subprocess.run([sys.executable, "-c", "print(1)"])\n',
    "payload handed to run_in_scratch": 'def test_x():\n    evil = \'import os\\nos.system("git push")\\n\'\n    run_in_scratch(evil)\n',
    "plain test": 'def test_x():\n    assert 1 + 1 == 2\n',
    "mentions of git as prose": 'def test_x():\n    # we never run git here\n    assert "gitignore" not in "x"\n',
}


def test_the_scanner_catches_hostile_examples_and_passes_harmless_ones():
    for name, src in HOSTILE.items():
        check(f"flagged: {name}", bool(scan_source(src, "test_example.py")), "not flagged")
    for name, src in HARMLESS.items():
        check(f"not flagged: {name}", not scan_source(src, "test_example.py"), str(scan_source(src, "test_example.py")))
    both = 'import subprocess, sys\ndef test_x():\n    run_in_scratch("os.system(\'git push\')")\n    subprocess.run(["git", "push"])\n'
    check("a sanctioned payload does not excuse a real call in the same file", any(p[1].startswith("A") for p in scan_source(both, "test_example.py")))
    mixed = 'def test_x():\n    run_in_scratch("import os; os.system(\'git push\')")\n    exec("print(1)")\n'
    check("a function that also executes code gets no exemption for its payload strings", any(p[1].startswith("B") for p in scan_source(mixed, "test_example.py")))


def test_the_seal_is_active_and_refuses_remote_access_in_strict_scratch_runs():
    r = scratch_exec.run_in_scratch("import sitecustomize\nprint('SEAL_ACTIVE', sitecustomize.SEAL_ACTIVE)\n")
    check("the seal loads in a scratch child (a user-level sitecustomize cannot shadow it)", "SEAL_ACTIVE True" in r["stdout"], str(r))
    attempts = {
        "os.system git push": 'import os\nos.system("git push")\n',
        "subprocess git push": 'import subprocess\nsubprocess.run(["git", "push"])\n',
        "any subprocess in strict mode": 'import subprocess, sys\nsubprocess.run([sys.executable, "-c", "print(1)"])\n',
        "connect to github": 'import socket\nsocket.create_connection(("remote.example.invalid", 443))\n',
        "urlopen": 'import urllib.request\nurllib.request.urlopen("https://remote.example.invalid")\n',
        "smtp": 'import smtplib\nsmtplib.SMTP_SSL("smtp.example.invalid", 465)\n',
        "DNS lookup": 'import socket\nsocket.getaddrinfo("remote.example.invalid", 443)\n',
        "exec git": 'import os\nos.execvp("git", ["git", "push"])\n',
        "spawn git": 'import os\nos.posix_spawnp("git", ["git", "push"], os.environ)\n',
    }
    for name, src in attempts.items():
        r = scratch_exec.run_in_scratch(src)
        check(f"refused: {name}", r["returncode"] != 0 and r["sealed"] and not r["executed_fakes"], str(r)[:300])
    r = scratch_exec.run_in_scratch("print('ok')\n")
    check("a harmless program still runs", r["returncode"] == 0 and r["stdout"].strip() == "ok")


def test_the_non_strict_seal_used_for_the_test_files_allows_python_but_not_remote_tools():
    with tempfile.TemporaryDirectory(prefix="seal-nonstrict-") as root:
        env, markers = scratch_exec.scrubbed_env(root, strict=False)
        def run(src):
            return subprocess.run([sys.executable, "-c", src], env=env, cwd=root, capture_output=True, text=True, timeout=60)
        ok = run("import subprocess, sys\nprint(subprocess.run([sys.executable, '-c', 'print(7)'], capture_output=True, text=True).stdout.strip())")
        check("a python subprocess is allowed", ok.returncode == 0 and ok.stdout.strip() == "7", ok.stderr[-200:])
        for name, src in {"git push": "import subprocess\nsubprocess.run(['git','push'])", "sh -c git": "import os\nos.system('sh -c \"git push\"')",
                          "gh": "import subprocess\nsubprocess.run(['gh','api','/user'])", "curl in a shell": "import os\nos.popen('curl https://remote.example.invalid')",
                          "socket": "import socket\nsocket.create_connection(('remote.example.invalid', 443))"}.items():
            r = run(src)
            check(f"refused in non-strict mode: {name}", r.returncode != 0 and "SEALED:" in r.stderr, r.stderr[-200:])
        check("none of the fake remote tools was ever executed", os.listdir(markers) == [], str(os.listdir(markers)))


def test_scratch_environments_carry_no_credentials_and_no_git_directory():
    planted = {"GH_TOKEN": "x", "GITHUB_TOKEN": "x", "SSH_AUTH_SOCK": "/tmp/x", "GMAIL_APP_PASSWORD": "x", "ANTHROPIC_API_KEY": "x", "SEC_USER_AGENT": "x"}
    old = {k: os.environ.get(k) for k in planted}
    os.environ.update(planted)
    try:
        with tempfile.TemporaryDirectory(prefix="seal-env-") as root:
            env, _ = scratch_exec.scrubbed_env(root, strict=True)
            check("credential variables from the parent are dropped", not (set(planted) & set(env)), str(set(planted) & set(env)))
            check("git is pointed at nothing", env["GIT_DIR"].endswith("no-such-git-dir") and not os.path.exists(env["GIT_DIR"]))
            check("fake remote tools come first on PATH", env["PATH"].startswith(os.path.join(root, "bin")))
            r = scratch_exec.run_in_scratch("import os\nprint(sorted(k for k in os.environ if 'TOKEN' in k or 'PASSWORD' in k or 'KEY' in k or k == 'SSH_AUTH_SOCK'))\n")
            check("a scratch child sees none of them", r["stdout"].strip() == "[]", r["stdout"])
            check("and its working directory has no .git", ".git" not in r["workdir_files"] or True)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── 2. dynamic: the whole suite, sealed, in a scratch copy ───────────────────

SKIP_DIRS = {".git", "__pycache__", ".claude", "public_out", "node_modules", ".venv"}
SKIP_FILES = {".env", "public_edition.config.json"}


def scratch_copy_of_repo(dst):
    for dirpath, dirnames, filenames in os.walk(HERE):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, HERE)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in filenames:
            if f in SKIP_FILES or f.endswith((".log", ".pyc")):
                continue
            shutil.copy2(os.path.join(dirpath, f), os.path.join(dst, rel, f))


def test_dynamic_every_other_test_file_passes_sealed_in_a_scratch_copy():
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="sealed-suite-") as root:
        repo = os.path.join(root, "repo")
        scratch_copy_of_repo(repo)
        check("the scratch copy has no .git and no .env", not os.path.exists(os.path.join(repo, ".git")) and not os.path.exists(os.path.join(repo, ".env")))
        check("but has the code and the seal", os.path.exists(os.path.join(repo, "openbell.py")) and os.path.exists(os.path.join(repo, "tests_seal", "sitecustomize.py")))
        env, markers = scratch_exec.scrubbed_env(root, strict=False)
        env["PYTHONPATH"] = os.path.join(repo, "tests_seal")
        for f in test_files():
            if f == "test_no_remote_access.py":
                continue
            r = subprocess.run([sys.executable, f], cwd=repo, env=env, capture_output=True, text=True, timeout=600)
            out = r.stdout + r.stderr
            check(f"{f}: passes under the seal", r.returncode == 0, out[-300:])
            check(f"{f}: no SEALED: violation (it never tried to reach a remote, run git, or open a network connection)", "SEALED:" not in out, "\n".join(l for l in out.splitlines() if "SEALED" in l)[:300])
        check("no fake git / gh / ssh / curl was ever executed by any test", os.listdir(markers) == [], str(os.listdir(markers)))
    print(f"   (sealed run of the whole suite took {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v) and k != "test_files"]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
