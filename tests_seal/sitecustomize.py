"""
Seal for test processes. Python imports `sitecustomize` automatically at start-up when this directory is on
PYTHONPATH, and child processes inherit PYTHONPATH, so every Python process a sealed test starts is sealed too.

What it enforces (each refusal prints a line starting `SEALED:` to stderr and raises SealViolation):
  * no network: socket connect / create_connection / getaddrinfo / gethostbyname refuse everything except
    loopback and unix sockets (that also stops git over ssh/https, smtp, http, DNS)
  * no remote-capable programs: subprocess, os.system/popen/exec*/spawn*/posix_spawn refuse any command that
    mentions git, gh, ssh, scp, sftp, curl, wget, nc, ncat, rsync, ftp or telnet
  * in strict mode (SEAL_STRICT=1) no subprocess or exec at all, for running generated or altered code

Why it exists: on 2026-10-29 a mutation-test payload (os.system("git push")) executed from inside the repo and
really pushed. Standing rule since: no test may be able to reach a git remote.
"""
import os
import socket
import subprocess
import sys

SEAL_ACTIVE = True
BANNED_PROGRAMS = {"git", "gh", "ssh", "scp", "sftp", "curl", "wget", "nc", "ncat", "rsync", "ftp", "telnet"}
LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
STRICT = os.environ.get("SEAL_STRICT") == "1"


class SealViolation(RuntimeError):
    pass


def _deny(what):
    print(f"SEALED: {what}", file=sys.stderr, flush=True)
    raise SealViolation(what)


import re

_SPLIT = re.compile(r"[^A-Za-z0-9_.+-]+")          # quotes, spaces, ; & | ( ) $ ` / etc. all separate words


def _tokens(cmd):
    """Every word in a command, however it is quoted or chained: `sh -c "git push"` and `x && /usr/bin/git push`
    both yield 'git'."""
    if isinstance(cmd, (bytes, os.PathLike)):
        cmd = os.fsdecode(cmd)
    items = [cmd] if isinstance(cmd, str) else list(cmd)
    words = set()
    for c in items:
        c = os.fsdecode(c) if isinstance(c, (bytes, os.PathLike)) else str(c)
        words |= {w.lower() for w in _SPLIT.split(c) if w}
    return words


def _check_command(cmd, via):
    if STRICT:
        _deny(f"{via} is not allowed in strict mode: {str(cmd)[:80]}")
    hit = _tokens(cmd) & BANNED_PROGRAMS
    if hit:
        _deny(f"{via} refused, it mentions {sorted(hit)}: {str(cmd)[:80]}")


_real_popen_init = subprocess.Popen.__init__


def _popen_init(self, args, *a, **k):
    _check_command(args, "subprocess")
    return _real_popen_init(self, args, *a, **k)


subprocess.Popen.__init__ = _popen_init

_real_system = os.system


def _system(cmd):
    _check_command(cmd, "os.system")
    return _real_system(cmd)


os.system = _system


def _wrap_exec(name):
    real = getattr(os, name, None)
    if real is None:
        return

    def guarded(*args, **kwargs):
        _check_command([a for a in args if isinstance(a, (str, bytes, list, tuple, os.PathLike))], f"os.{name}")
        return real(*args, **kwargs)
    setattr(os, name, guarded)


for _n in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
           "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp"):
    _wrap_exec(_n)


def _host_ok(address):
    if isinstance(address, (str, bytes)):
        return True                                   # AF_UNIX path
    host = address[0] if address else ""
    return (host.decode() if isinstance(host, bytes) else str(host)) in LOOPBACK


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _connect(self, address):
    if not _host_ok(address):
        _deny(f"socket.connect to {address!r}")
    return _real_connect(self, address)


def _connect_ex(self, address):
    if not _host_ok(address):
        _deny(f"socket.connect_ex to {address!r}")
    return _real_connect_ex(self, address)


socket.socket.connect, socket.socket.connect_ex = _connect, _connect_ex
_real_create_connection = socket.create_connection


def _create_connection(address, *a, **k):
    if not _host_ok(address):
        _deny(f"socket.create_connection to {address!r}")
    return _real_create_connection(address, *a, **k)


socket.create_connection = _create_connection
_real_getaddrinfo = socket.getaddrinfo


def _getaddrinfo(host, *a, **k):
    if host not in LOOPBACK and host is not None:
        _deny(f"DNS lookup of {host!r}")
    return _real_getaddrinfo(host, *a, **k)


socket.getaddrinfo = _getaddrinfo
_real_gethostbyname = socket.gethostbyname


def _gethostbyname(host):
    if host not in LOOPBACK:
        _deny(f"DNS lookup of {host!r}")
    return _real_gethostbyname(host)


socket.gethostbyname = _gethostbyname
