"""
Desk framework: run specialist desks concurrently, each with its own timeout and failure isolation.

Guarantees (each is tested):
  * A desk that raises, times out, returns nothing, or produces advice language is OMITTED and
    logged as [DESK-<name>] <reason>. The brief still sends.
  * Desks run in daemon threads, so a hung network call can never keep the process alive.
  * start_desks() returns immediately; the caller does its own work and calls collect() later, so
    desk time overlaps the rest of the pipeline. collect() never waits past a desk's own deadline.
  * Runtimes are logged per desk plus a summary line.
  * Public runs exclude PRIVATE desks. Desks take no arguments: they cannot be handed picks,
    holdings or memory.
No LLM and no network access in this module itself.
"""
import html as _html
import re
import sys
import threading
import time

import brief_common as bc

DEFAULT_TIMEOUT = 25.0


class DeskSkipped(Exception):
    """Raised by a desk's fetch() when it deliberately does nothing (e.g. a missing optional setting)."""


class DeskGuardError(Exception):
    """A desk's output contained advice language."""


class Desk:
    def __init__(self, module=None, *, name=None, fetch=None, build_section=None, private=None, timeout=None):
        self.name = name or getattr(module, "NAME")
        self.fetch = fetch or getattr(module, "fetch")
        self.build_section = build_section or getattr(module, "build_section")
        self.private = bool(getattr(module, "PRIVATE", False) if private is None else private)
        self.timeout = float(timeout if timeout is not None else getattr(module, "TIMEOUT", DEFAULT_TIMEOUT))


class DeskResult:
    def __init__(self, name, status, html=None, runtime=0.0, detail=""):
        self.name, self.status, self.html, self.runtime, self.detail = name, status, html, runtime, detail

    @property
    def ok(self):
        return self.status == "ok"

    def __repr__(self):
        return f"DeskResult({self.name!r}, {self.status!r}, {self.runtime:.2f}s)"


def visible_text(html_str: str) -> str:
    t = re.sub(r"<style.*?</style>", " ", html_str or "", flags=re.S)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", t)))


def check_no_advice(html_str: str) -> None:
    hits = bc.find_advice_language(visible_text(html_str))
    if hits:
        raise DeskGuardError(f"advice language {sorted(set(hits))}")


def select(desks, include_private: bool):
    """Public editions pass include_private=False: private desks are never even started."""
    return [d for d in desks if include_private or not d.private]


class _Slot:
    def __init__(self, desk, started):
        self.desk, self.started, self.done = desk, started, threading.Event()
        self.outcome = None            # (status, html, detail, runtime)
        self.abandoned = False


def _work(slot: _Slot, mode: str) -> None:
    d, t0 = slot.desk, time.monotonic()
    try:
        facts = d.fetch()
        html_out = d.build_section(facts, mode) if facts else None
        if html_out:
            check_no_advice(html_out)
            out = ("ok", html_out, "")
        else:
            out = ("empty", None, "nothing to show")
    except DeskSkipped as e:
        out = ("skipped", None, str(e))
    except DeskGuardError as e:
        out = ("blocked", None, str(e))
    except Exception as e:                                   # noqa: BLE001 — isolation is the point
        out = ("error", None, f"{type(e).__name__}: {e}")
    if not slot.abandoned:
        slot.outcome = out + (time.monotonic() - t0,)
    slot.done.set()


class DeskRun:
    def __init__(self, slots, mode, log):
        self.slots, self.mode, self.log = slots, mode, log
        self.t0 = time.monotonic()

    def collect(self) -> list:
        """Wait (never past each desk's own deadline), log, and return DeskResults in registry order."""
        results = []
        t_collect = time.monotonic()
        for s in self.slots:
            remaining = s.started + s.desk.timeout - time.monotonic()
            finished = s.done.wait(max(0.0, remaining))
            name = s.desk.name
            if not finished:
                s.abandoned = True
                r = DeskResult(name, "timeout", None, s.desk.timeout, f"timed out after {s.desk.timeout:g}s")
                self.log(f"[DESK-{name}] {r.detail} — section omitted")
            else:
                status, html_out, detail, rt = s.outcome
                r = DeskResult(name, status, html_out, rt, detail)
                if status == "ok":
                    self.log(f"[DESK-{name}] ok {rt:.1f}s")
                elif status == "skipped":
                    self.log(f"[DESK-{name}] skipped{': ' + detail if detail else ''}")
                elif status == "empty":
                    self.log(f"[DESK-{name}] empty — section omitted ({rt:.1f}s)")
                elif status == "blocked":
                    self.log(f"[DESK-{name}] blocked by the no-advice guard ({detail}) — section omitted")
                else:
                    self.log(f"[DESK-{name}] {status}: {detail} — section omitted ({rt:.1f}s)")
            results.append(r)
        waited = time.monotonic() - t_collect          # the time the brief actually spent waiting on desks
        total = sum(r.runtime for r in results)
        ok = sum(r.ok for r in results)
        self.log(f"[DESKS] {self.mode}: {ok} of {len(results)} sections; desks ran {total:.1f}s in total, "
                 f"the brief waited {waited:.1f}s for them")
        return results

    @staticmethod
    def sections(results) -> list:
        return [r.html for r in results if r.ok and r.html]


def start_desks(desks, mode: str, include_private: bool = False, log=None) -> DeskRun:
    """Start every selected desk now, each in its own daemon thread, and return at once."""
    log = log or (lambda m: print(m, flush=True))
    slots = []
    now = time.monotonic()
    for d in select(desks, include_private):
        slot = _Slot(d, now)
        threading.Thread(target=_work, args=(slot, mode), name=f"desk-{d.name}", daemon=True).start()
        slots.append(slot)
    return DeskRun(slots, mode, log)


def run_desks(desks, mode: str, include_private: bool = False, log=None):
    """Convenience: start, then collect immediately. Returns (results, run)."""
    run = start_desks(desks, mode, include_private, log)
    return run.collect(), run
