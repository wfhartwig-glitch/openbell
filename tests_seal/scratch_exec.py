"""
Run generated or altered code SAFELY: in a fresh scratch directory with no .git, no .env and no credentials, in a
child Python process that has the network/remote seal (tests_seal/sitecustomize.py) in STRICT mode (no
subprocess at all), with fake `git`/`gh`/`ssh`/`curl` programs first on PATH. A fake that is ever executed
writes a marker file; a test asserts the marker is absent, so a seal failure is detected and still harmless.

Standing rule: any test that needs to execute code it generated or altered must use run_in_scratch().
"""
import os
import subprocess
import sys
import tempfile

SEAL_DIR = os.path.dirname(os.path.abspath(__file__))
FAKE_PROGRAMS = ("git", "gh", "ssh", "scp", "curl", "wget", "nc", "rsync")
CREDENTIAL_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT", "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GMAIL_APP_PASSWORD",
                   "GMAIL_ADDRESS", "NEWS_API_KEY", "FMP_API_KEY", "SEC_USER_AGENT", "TO_EMAIL", "ANTHROPIC_API_KEY")


def scrubbed_env(root, strict=False, extra=None):
    """Environment for a sealed child: no credentials, a throwaway HOME, git pointed at nothing, fake remote
    tools first on PATH, the seal on PYTHONPATH. Returns (env, marker_dir)."""
    bindir, markers = os.path.join(root, "bin"), os.path.join(root, "markers")
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(markers, exist_ok=True)
    for prog in FAKE_PROGRAMS:
        path = os.path.join(bindir, prog)
        with open(path, "w") as fh:
            fh.write(f"#!/bin/sh\necho \"$0 $@\" >> '{markers}/{prog}.executed'\nexit 99\n")
        os.chmod(path, 0o755)
    keep = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "TZ", "TMPDIR")}
    env = dict(keep)
    env.update({
        "PATH": bindir + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "HOME": os.path.join(root, "home"),
        "GIT_DIR": os.path.join(root, "no-such-git-dir"),
        "GIT_CEILING_DIRECTORIES": root,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "PYTHONPATH": SEAL_DIR, "PYTHONDONTWRITEBYTECODE": "1", "SEAL_STRICT": "1" if strict else "0",
    })
    os.makedirs(env["HOME"], exist_ok=True)
    for var in CREDENTIAL_VARS:
        env.pop(var, None)
    env.update(extra or {})
    return env, markers


def run_in_scratch(source: str, files: dict = None, timeout: int = 60):
    """Execute `source` (a Python program) in a scratch dir. `files` = {relative name: text} written there first.
    -> dict(returncode, stdout, stderr, executed_fakes=[...], sealed=bool)."""
    with tempfile.TemporaryDirectory(prefix="scratch-exec-") as root:
        work = os.path.join(root, "work")
        os.makedirs(work)
        for name, text in (files or {}).items():
            with open(os.path.join(work, name), "w") as fh:
                fh.write(text)
        with open(os.path.join(work, "program.py"), "w") as fh:
            fh.write(source)
        env, markers = scrubbed_env(root, strict=True)
        r = subprocess.run([sys.executable, "program.py"], cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
        executed = sorted(os.listdir(markers))
        assert not os.path.exists(os.path.join(work, ".git")), "a scratch dir must never contain .git"
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr, "executed_fakes": executed,
                "sealed": "SEALED:" in r.stderr, "workdir_files": sorted(os.listdir(work))}
