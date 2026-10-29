"""
test_seal_not_in_production.py — the test seal (tests_seal/sitecustomize.py) must never be able to load in production.

tests_seal/sitecustomize.py refuses network access and subprocesses. Python auto-imports a module named
`sitecustomize` at start-up from any directory on its module search path, so if tests_seal ever got onto that path
in a production process (a workflow setting PYTHONPATH, an import, a .pth file, a copy at the repo root) the brief
would lose network access. This file fails if any of that becomes possible.

  1. workflows: no reference to tests_seal / sitecustomize / usercustomize / scratch_exec / SEAL_*, no PYTHONPATH,
     PYTHONSTARTUP, PYTHONHOME or PYTHONUSERBASE at any level (env blocks, run lines, comments), no $GITHUB_ENV writes
  2. production files (every .py that is not a test or the seal itself, shell scripts, requirements.txt): same words
  3. layout: the only sitecustomize.py / usercustomize.py in the repo is tests_seal/sitecustomize.py; no .pth files;
     tests_seal is not a package; no sitecustomize at the repo root
  4. runtime: production-style interpreters (`python openbell.py --help`, `python public_edition.py --help`,
     `import pippy_mcp`), started from the repo root with and without PYTHONPATH pointing at the repo, import nothing
     from tests_seal; any sitecustomize that DOES load (Homebrew ships one) comes from outside the repo
  5. self-check: every one of those rules is shown to fire on a synthetic tree that breaks it

Offline. Run: python3 test_seal_not_in_production.py
"""
import glob
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


SEAL_WORDS = ("tests_seal", "sitecustomize", "usercustomize", "scratch_exec", "SEAL_STRICT", "SEAL_ACTIVE")
PYTHON_ENV_WORDS = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE")
SKIP_DIRS = {".git", "__pycache__", ".claude", "node_modules", ".venv", "public_out"}


def _walk(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            yield os.path.join(dp, f)


def audit_static(root):
    """-> [problem strings] for the workflows, the production files and the layout under `root`."""
    problems = []
    rel = lambda p: os.path.relpath(p, root)

    # 1. workflows (raw text, so comments count too, plus the parsed env blocks)
    for wf in sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml"))):
        text = open(wf, encoding="utf-8").read()
        for w in SEAL_WORDS + PYTHON_ENV_WORDS + ("GITHUB_ENV",):
            if w in text:
                problems.append(f"workflow {rel(wf)} mentions {w}")
        doc = yaml.safe_load(text) or {}
        found = []

        def walk(n):
            if isinstance(n, dict):
                for k, v in n.items():
                    if k == "env" and isinstance(v, dict):
                        found.extend(str(x) for x in v)
                    walk(v)
            elif isinstance(n, list):
                for x in n:
                    walk(x)
        walk(doc)
        for k in found:
            if k.upper().startswith("PYTHON"):
                problems.append(f"workflow {rel(wf)} sets env {k}")

    # 2. production files
    for p in _walk(root):
        r = rel(p)
        parts = r.split(os.sep)
        if parts[0] == "tests_seal" or (parts[0].startswith("test_") and r.endswith(".py")) or parts[0] == ".github":
            continue
        base = os.path.basename(p)
        if r.endswith((".py", ".sh", ".cfg", ".toml", ".ini")) or base in ("requirements.txt", "Makefile", "Procfile", "Dockerfile"):
            text = open(p, encoding="utf-8", errors="replace").read()
            for w in SEAL_WORDS + ("PYTHONPATH", "PYTHONSTARTUP"):
                if w in text:
                    problems.append(f"production file {r} mentions {w}")

    # 3. layout
    for p in _walk(root):
        r = rel(p)
        base = os.path.basename(p)
        if base in ("sitecustomize.py", "usercustomize.py") and r != os.path.join("tests_seal", "sitecustomize.py"):
            problems.append(f"{r}: a sitecustomize/usercustomize outside tests_seal would be auto-loaded by Python")
        if base.endswith(".pth"):
            problems.append(f"{r}: a .pth file adds directories to Python's path")
    if os.path.exists(os.path.join(root, "tests_seal", "__init__.py")):
        problems.append("tests_seal/__init__.py exists: tests_seal must not be a package")
    return problems


def _clean_env(tmp, pythonpath=None):
    env = {"PATH": os.path.dirname(sys.executable) + os.pathsep + "/bin", "HOME": tmp, "TZ": "UTC",
           "GIT_DIR": os.path.join(tmp, "no-such-git-dir"), "PYTHONDONTWRITEBYTECODE": "1"}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


_REPORT = ("import sys\n"
           "print('PATH_TESTS_SEAL', [p for p in sys.path if 'tests_seal' in p])\n"
           "m = sys.modules.get('sitecustomize')\n"
           "print('SITECUSTOMIZE_FILE', getattr(m, '__file__', None))\n")


def audit_runtime(root):
    """-> [problem strings] from starting production-style interpreters inside `root`."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="seal-prod-") as tmp:
        root_real = os.path.realpath(root)
        for label, pp in (("no PYTHONPATH", None), ("PYTHONPATH = the repo root (worst case)", root)):
            env = _clean_env(tmp, pp)
            r = subprocess.run([sys.executable, "-c", _REPORT], cwd=root, env=env, capture_output=True, text=True, timeout=120)
            out = r.stdout
            if "PATH_TESTS_SEAL []" not in out:
                problems.append(f"[{label}] a tests_seal directory is on sys.path: {out.strip()[:200]}")
            for line in out.splitlines():
                if line.startswith("SITECUSTOMIZE_FILE") and "None" not in line:
                    f = os.path.realpath(line.split(" ", 1)[1])
                    if f.startswith(root_real) or "tests_seal" in f:
                        problems.append(f"[{label}] a sitecustomize from inside the repo was auto-loaded: {f}")
        env = dict(_clean_env(tmp), PYTHONPROFILEIMPORTTIME="1")
        for label, argv in (("openbell.py --help", ["openbell.py", "--help"]), ("public_edition.py --help", ["public_edition.py", "--help"]),
                            ("import pippy_mcp", ["-c", "import pippy_mcp"]), ("import desks.registry", ["-c", "import desks.registry"])):
            r = subprocess.run([sys.executable] + argv, cwd=root, env=env, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                problems.append(f"{label} did not start cleanly: {r.stderr[-200:]}")
            for w in ("tests_seal", "scratch_exec"):
                if w in r.stderr or w in r.stdout:
                    problems.append(f"{label} imported or touched {w}")
    return problems


# ── 1-4: the real repository ─────────────────────────────────────────────────

def test_no_workflow_or_production_file_references_the_seal():
    p = audit_static(HERE)
    check("no workflow, production file or packaging mechanism mentions the seal or sets a Python path variable", not p, str(p))
    wfs = sorted(os.path.basename(w) for w in glob.glob(os.path.join(HERE, ".github", "workflows", "*.y*ml")))
    check("it looked at the real workflows (morning, close, case study, probe)", {"morning_brief.yml", "close_summary.yml", "case_study.yml", "scan_probe.yml"} <= set(wfs), str(wfs))
    prod = [os.path.relpath(p_, HERE) for p_ in _walk(HERE) if p_.endswith(".py") and not os.path.basename(p_).startswith("test_") and not p_.startswith(os.path.join(HERE, "tests_seal"))]
    check("and at the production Python files (openbell, pippy_mcp, public_edition, brief_common, desks/...)",
          {"openbell.py", "pippy_mcp.py", "public_edition.py", "brief_common.py"} <= set(prod) and any(x.startswith("desks") for x in prod), str(prod))


def test_the_only_sitecustomize_is_inside_tests_seal_and_nothing_else_can_add_a_path():
    sites = [os.path.relpath(p, HERE) for p in _walk(HERE) if os.path.basename(p) in ("sitecustomize.py", "usercustomize.py")]
    check("exactly one sitecustomize.py in the repo, under tests_seal/", sites == [os.path.join("tests_seal", "sitecustomize.py")], str(sites))
    check("no .pth files", not [p for p in _walk(HERE) if p.endswith(".pth")])
    check("tests_seal is not a package (no __init__.py)", not os.path.exists(os.path.join(HERE, "tests_seal", "__init__.py")))
    check("nothing named sitecustomize at the repo root, where `python openbell.py` puts its script directory",
          not os.path.exists(os.path.join(HERE, "sitecustomize.py")) and not os.path.exists(os.path.join(HERE, "usercustomize.py")))


def test_production_style_interpreters_never_load_the_seal():
    p = audit_runtime(HERE)
    check("started from the repo root, with and without PYTHONPATH on the repo, and via the real entry points: tests_seal is never on the path, never imported, and any auto-loaded sitecustomize is not the repo's", not p, str(p))


# ── 5: self-check — each rule fires on a tree that breaks it ─────────────────

CLEAN_WORKFLOW = "name: x\non:\n  workflow_dispatch:\njobs:\n  j:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: python openbell.py morning\n"


def _tree(files):
    root = tempfile.mkdtemp(prefix="seal-tree-")
    base = {".github/workflows/w.yml": CLEAN_WORKFLOW, "openbell.py": "print('x')\n", "requirements.txt": "yfinance\n",
            "tests_seal/sitecustomize.py": "SEAL_ACTIVE = True\n", "test_x.py": "import tests_seal\n"}
    base.update(files)
    for rel, text in base.items():
        if text is None:
            continue
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
    return root


def test_the_audit_fires_on_every_way_the_seal_could_reach_production():
    check("control: a clean synthetic tree (tests may import the seal) has no problems", audit_static(_tree({})) == [], str(audit_static(_tree({}))))
    cases = {
        "a workflow env sets PYTHONPATH": {".github/workflows/w.yml": CLEAN_WORKFLOW.replace("    steps:", "    env:\n      PYTHONPATH: tests_seal\n    steps:")},
        "a workflow env sets PYTHONPATH to something else": {".github/workflows/w.yml": CLEAN_WORKFLOW.replace("    steps:", "    env:\n      PYTHONPATH: .\n    steps:")},
        "a run line exports PYTHONPATH": {".github/workflows/w.yml": CLEAN_WORKFLOW.replace("python openbell.py morning", "export PYTHONPATH=tests_seal && python openbell.py morning")},
        "a workflow comment mentions tests_seal": {".github/workflows/w.yml": "# uses tests_seal\n" + CLEAN_WORKFLOW},
        "a workflow writes to GITHUB_ENV": {".github/workflows/w.yml": CLEAN_WORKFLOW.replace("python openbell.py morning", "echo X=1 >> $GITHUB_ENV")},
        "a workflow sets PYTHONSTARTUP": {".github/workflows/w.yml": CLEAN_WORKFLOW.replace("    steps:", "    env:\n      PYTHONSTARTUP: x.py\n    steps:")},
        "openbell.py imports tests_seal": {"openbell.py": "import tests_seal\n"},
        "openbell.py imports the scratch runner": {"openbell.py": "from scratch_exec import run_in_scratch\n"},
        "a production module mentions sitecustomize": {"desks/x.py": "import sitecustomize\n"},
        "requirements mention tests_seal": {"requirements.txt": "yfinance\n./tests_seal\n"},
        "a run script sets PYTHONPATH": {"run.sh": "PYTHONPATH=tests_seal python openbell.py\n"},
        "a sitecustomize.py at the repo root": {"sitecustomize.py": "x = 1\n"},
        "a usercustomize.py anywhere": {"desks/usercustomize.py": "x = 1\n"},
        "a second sitecustomize elsewhere": {"tools/sitecustomize.py": "x = 1\n"},
        "a .pth file": {"seal.pth": "tests_seal\n"},
        "tests_seal made a package": {"tests_seal/__init__.py": ""},
    }
    for name, files in cases.items():
        p = audit_static(_tree(files))
        check(f"flagged: {name}", bool(p), "not flagged")


def test_the_runtime_audit_fires_when_the_seal_is_on_the_path_or_imported():
    clean = _tree({})
    open(os.path.join(clean, "openbell.py"), "w").write("import sys\nif '--help' in sys.argv: print('usage')\n")
    open(os.path.join(clean, "public_edition.py"), "w").write("import sys\nif '--help' in sys.argv: print('usage')\n")
    os.makedirs(os.path.join(clean, "desks"), exist_ok=True)
    open(os.path.join(clean, "desks", "__init__.py"), "w").write("")
    open(os.path.join(clean, "desks", "registry.py"), "w").write("ALL = []\n")
    open(os.path.join(clean, "pippy_mcp.py"), "w").write("X = 1\n")
    check("control: a synthetic clean tree passes the runtime audit", audit_runtime(clean) == [], str(audit_runtime(clean)))
    bad = _tree({})
    for f in ("public_edition.py", "pippy_mcp.py"):
        open(os.path.join(bad, f), "w").write("X = 1\n")
    open(os.path.join(bad, "openbell.py"), "w").write("import sys\nsys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), 'tests_seal'))\nimport sitecustomize\nprint('tests_seal loaded')\n")
    os.makedirs(os.path.join(bad, "desks"), exist_ok=True)
    open(os.path.join(bad, "desks", "__init__.py"), "w").write("")
    open(os.path.join(bad, "desks", "registry.py"), "w").write("ALL = []\n")
    p = audit_runtime(bad)
    check("an entry point that puts tests_seal on its path and prints its name is caught", bool(p), str(p))
    root_site = _tree({"sitecustomize.py": "print('repo sitecustomize loaded')\n"})
    for f in ("openbell.py", "public_edition.py", "pippy_mcp.py"):
        open(os.path.join(root_site, f), "w").write("X = 1\n")
    os.makedirs(os.path.join(root_site, "desks"), exist_ok=True)
    open(os.path.join(root_site, "desks", "__init__.py"), "w").write("")
    open(os.path.join(root_site, "desks", "registry.py"), "w").write("ALL = []\n")
    p = audit_runtime(root_site)
    check("a sitecustomize.py at the repo root IS auto-loaded when the repo is on PYTHONPATH, and the audit sees it", any("sitecustomize from inside the repo" in x for x in p), str(p))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        sys.exit(1)
    print("All checks passed.")
