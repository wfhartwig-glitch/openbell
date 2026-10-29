"""
public_edition.py — the PUBLIC edition of Pippy's Brief (morning and close).

Separate from the private brief that goes to the owner's inbox: this module never
fetches, reads or renders anything about holdings, weekly picks, the daily scan's
candidates or the personal watchlist. It builds a standalone HTML file in a local
output folder. It does NOT send email, and it writes no state files.

Usage:
    python3 public_edition.py                      # both editions -> ./public_out/
    python3 public_edition.py --mode morning --out some/dir
    python3 public_edition.py --unsubscribe-url "<ESP merge tag>" --address "<postal address>"
    python3 public_edition.py --require-footer-values     # production: refuse to build with placeholders

How holdings/picks are kept out (belt and braces):
  1. Structural — the builders are called with NO picks, NO picks-day-performance, NO scan
     candidates, NO watchlist, and a memory dict reduced to `briefing_history` only (which
     LOOP-CLOSE and the recurring-theme note need). The private-only sentence builders
     therefore have nothing to render.
  2. Guard — the finished page is scanned for private-edition language ("your", "since
     entry", candidates, picks, watchlist, holdings…) and generation FAILS rather than
     writing a file that contains any.

The data sources themselves are unchanged and shared with the private edition. See the
note on `fetch_top_movers` / the earnings calendar in the audit: their universe is the
tracked-ticker list, which overlaps the owner's holdings.
"""
import argparse
import asyncio
import html as _html
import json
import os
import re
import sys

import openbell
from desks import framework as desk_framework, registry as desk_registry

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

UNSUBSCRIBE_PLACEHOLDER = "[[UNSUBSCRIBE_URL]]"
ADDRESS_PLACEHOLDER = "[[MAILING_ADDRESS]]"

# Footer values (unsubscribe link, postal address) are supplied by the ESP / operator, never hardcoded.
# Resolution order, first non-empty wins:  CLI flag  >  environment  >  config file  >  placeholder.
CONFIG_FILE = os.path.join(PROJECT_DIR, "public_edition.config.json")   # gitignored; see the .example.json
ENV_UNSUBSCRIBE = "PUBLIC_UNSUBSCRIBE_URL"
ENV_ADDRESS = "PUBLIC_MAILING_ADDRESS"
_CONFIG_KEYS = {"unsubscribe_url", "mailing_address"}

PUBLIC_FOOTER_TEXT = ("Automated daily summary of market data. Explanations are hedged readings of price "
                      "movements, not confirmed causes. Not investment advice. Sourced from public market "
                      "data and news headlines.")

# Files this tool must never write to (it only ever writes the HTML it generates).
_STATE_FILES = {"pippy_memory.json", "send_log.json", "picks_cache.json", "case_study_history.json",
                "daily_scan_cache.json", "pippy_history.json"}


class PublicEditionLeak(Exception):
    """Raised when private-edition language is found in a public page."""


# ── Leak guard ────────────────────────────────────────────────────────────────

# Language that belongs only to the private edition. Pippy-authored text only: third-party
# headline titles and company names ("Celsius Holdings, Inc.") may legitimately contain
# these words, so headlines are excluded and the looser pattern is used outside the narrative.
_PRIVATE_WORDS_COMMON = [
    r"\byour\b", r"\bsince entry\b", r"\bcandidates?\b", r"\bpicks?\b", r"\bwatchlist\b",
    r"\bportfolio\b", r"\brotated this week\b", r"\bnew candidate\b",
]
_NARRATIVE_ONLY = [r"\bholdings?\b"]
# Generic English uses of "holding" in the narrative that are not about a portfolio.
_NARRATIVE_ALLOWED = ("holding back", "safer holdings")


def _visible_text(html_str: str) -> str:
    t = re.sub(r"<style.*?</style>", " ", html_str, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _html.unescape(t))


def find_private_language(narrative_text: str, other_html: str) -> list:
    """Returns the offending matches (empty list = clean)."""
    hits = []
    nar = narrative_text
    for allowed in _NARRATIVE_ALLOWED:
        nar = re.sub(re.escape(allowed), " ", nar, flags=re.I)
    for pat in _PRIVATE_WORDS_COMMON + _NARRATIVE_ONLY:
        for m in re.finditer(pat, nar, flags=re.I):
            hits.append(("narrative", m.group(0)))
    other = _visible_text(other_html)
    for pat in _PRIVATE_WORDS_COMMON + [r"\bHolding\s*·"]:
        for m in re.finditer(pat, other, flags=re.I):
            hits.append(("page", m.group(0)))
    return hits


# ── Page assembly ─────────────────────────────────────────────────────────────

def resolve_footer_values(unsubscribe_url=None, address=None, config_path=None, environ=None) -> tuple:
    """(unsubscribe_url, mailing_address) after applying the precedence above. Values are used
    exactly as given (an ESP merge tag such as {{...}} or *|...|* passes through untouched).
    A malformed or misspelled config file raises instead of silently falling back."""
    environ = os.environ if environ is None else environ
    cfg = {}
    path = config_path or CONFIG_FILE
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            raise ValueError(f"{path}: not valid JSON ({e})")
        if not isinstance(cfg, dict) or set(cfg) - _CONFIG_KEYS - {"_comment"}:
            raise ValueError(f"{path}: expected only the keys {sorted(_CONFIG_KEYS)}, got {sorted(cfg) if isinstance(cfg, dict) else type(cfg).__name__}")

    def pick(flag, env_name, key, placeholder):
        for v in (flag, environ.get(env_name), cfg.get(key)):
            if isinstance(v, str) and v.strip():
                return v.strip()
        return placeholder

    return (pick(unsubscribe_url, ENV_UNSUBSCRIBE, "unsubscribe_url", UNSUBSCRIBE_PLACEHOLDER),
            pick(address, ENV_ADDRESS, "mailing_address", ADDRESS_PLACEHOLDER))


def footer_is_complete(unsubscribe_url: str, address: str) -> bool:
    return UNSUBSCRIBE_PLACEHOLDER not in unsubscribe_url and ADDRESS_PLACEHOLDER not in address


def _public_footer(unsubscribe_url: str, address: str) -> str:
    href = unsubscribe_url.replace('"', "&quot;")          # only guard the attribute; the value is otherwise verbatim
    addr = address.replace("\r\n", "\n").replace("\n", "<br>")
    return f"""<tr><td class="pippy-footer-pad" style="padding:20px 32px;border-top:1px solid #e5e7eb;background:#f9fafb">
    <p style="margin:0 0 10px;font-size:12px;line-height:1.5;color:#6b7280">{PUBLIC_FOOTER_TEXT}</p>
    <p style="margin:0 0 4px;font-size:12px;line-height:1.5;color:#6b7280"><a href="{href}" style="color:#6b7280;text-decoration:underline">Unsubscribe</a></p>
    <p style="margin:0;font-size:12px;line-height:1.5;color:#6b7280">{addr}</p>
  </td></tr>"""


def _narrative_paragraphs(section_html: str) -> str:
    """The narrative's text, for the strict guard."""
    return _visible_text(section_html)


def _finish(body_parts: list, narrative_section: str, headlines_section: str, title: str, subtitle: str,
            unsubscribe_url: str, address: str) -> str:
    authored = "".join(body_parts)   # everything Pippy wrote or formatted, minus headlines
    hits = find_private_language(_narrative_paragraphs(narrative_section), authored + _public_footer("", ""))
    if hits:
        raise PublicEditionLeak(f"private-edition language in public page: {hits}")
    body = narrative_section + "".join(body_parts) + headlines_section
    return openbell._wrap(body, title, subtitle, footer_html=_public_footer(unsubscribe_url, address))


def _public_mem(mem) -> dict:
    """Only what LOOP-CLOSE and the recurring-theme note read. No pick performance history,
    no flagged tickers, no email counters."""
    mem = mem if isinstance(mem, dict) else {}
    return {"briefing_history": mem.get("briefing_history", [])}


def _public_movers(movers: dict) -> str:
    """Top Movers for the public edition. The data source ranks a fixed list of tracked stocks, not the
    whole market, so the heading says so (the private edition keeps its original label). Hidden when empty."""
    gainers, losers = movers.get("gainers", []) or [], movers.get("losers", []) or []
    if not gainers and not losers:
        return ""
    section = openbell._movers(gainers, losers)
    n = movers.get("universe_size")
    if movers.get("source") == "FMP":
        return section                                       # market-wide source: "Top Movers" is accurate
    label = (f"Among {n} widely followed stocks we track" if isinstance(n, int) and n > 0
             else "Among the widely followed stocks we track")
    assert section.count(">Top Movers<") == 1, "openbell._movers heading changed; update _public_movers"
    return section.replace(">Top Movers<", f">{label}<", 1)


def build_public_morning(snap_list, hl_list, global_list, comm_list, tsy, earnings_list, mem,
                         unsubscribe_url=UNSUBSCRIBE_PLACEHOLDER, address=ADDRESS_PLACEHOLDER, desk_sections=()):
    today_str = openbell._today_ct().strftime("%A, %B %d")
    narrative, _ = openbell._morning_summary_html(
        snap_list, hl_list, {}, comm_list, tsy, _public_mem(mem), earnings_list, [], global_list)
    parts = [openbell._indices(snap_list),
             openbell._global_indices(global_list),
             openbell._commodities_and_yields(comm_list, tsy)] + list(desk_sections)
    html = _finish(parts, narrative, openbell._headlines(hl_list),
                   f"Morning Briefing &nbsp; {today_str}", "Pippy's Brief ☀️", unsubscribe_url, address)
    return f"Pippy's Brief — {today_str} Morning Briefing", html


def build_public_close(snap_list, movers, sectors_list, comm_list, tsy, earnings_list, hl_list, mem,
                       unsubscribe_url=UNSUBSCRIBE_PLACEHOLDER, address=ADDRESS_PLACEHOLDER, desk_sections=()):
    today_str = openbell._today_ct().strftime("%A, %B %d")
    movers = movers if isinstance(movers, dict) else {}
    narrative = openbell._close_summary_html(
        snap_list, movers, sectors_list, comm_list, treasury=tsy, earnings=earnings_list,
        headlines=hl_list, picks_day_performance=None, scan_candidates=None, mem=_public_mem(mem),
        second_mover=True)
    parts = [openbell._indices(snap_list),
             _public_movers(movers),
             openbell._sectors(sectors_list),
             openbell._commodities_and_yields(comm_list, tsy)] + list(desk_sections)
    html = _finish(parts, narrative, openbell._headlines(hl_list),
                   f"Market Close &nbsp; {today_str}", "Pippy's Brief 📊", unsubscribe_url, address)
    return f"Pippy's Brief — {today_str} Market Close", html


# ── Fetch (read-only MCP tools only) and write ────────────────────────────────

async def _fetch_and_build(session, mode, unsubscribe_url, address):
    call = openbell.call
    # Public edition: only non-private desks are even started.
    desk_run = desk_framework.start_desks(desk_registry.ALL, mode, include_private=False)
    if mode == "morning":
        snap, glob, comm, tsy_raw, hl, earn, mem = await asyncio.gather(
            call(session, "fetch_market_snapshot"), call(session, "fetch_global_indices"),
            call(session, "fetch_commodities"), call(session, "fetch_treasury_yield"),
            call(session, "fetch_top_headlines"), call(session, "fetch_earnings_calendar"),
            call(session, "load_memory"))
        tsy = tsy_raw if tsy_raw.get("yield") else {}
        openbell._log_pct_mismatches(comm)
        return build_public_morning(snap.get("data", []), hl.get("headlines", []), glob.get("indices", []),
                                    comm.get("commodities", []), tsy, earn.get("earnings", []), mem,
                                    unsubscribe_url, address, desk_sections=desk_run.sections(desk_run.collect()))
    snap, sect, movers, comm, tsy_raw, hl, earn, mem = await asyncio.gather(
        call(session, "fetch_market_snapshot"), call(session, "fetch_sector_performance"),
        call(session, "fetch_top_movers"), call(session, "fetch_commodities"),
        call(session, "fetch_treasury_yield"), call(session, "fetch_top_headlines"),
        call(session, "fetch_earnings_calendar"), call(session, "load_memory"))
    tsy = tsy_raw if tsy_raw.get("yield") else {}
    openbell._log_pct_mismatches(comm, movers)
    return build_public_close(snap.get("data", []), movers, sect.get("sectors", []), comm.get("commodities", []),
                              tsy, earn.get("earnings", []), hl.get("headlines", []), mem,
                              unsubscribe_url, address, desk_sections=desk_run.sections(desk_run.collect()))


def write_html(out_dir: str, name: str, html: str) -> str:
    if name in _STATE_FILES or not name.endswith(".html"):
        raise ValueError(f"refusing to write {name!r}")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


async def _run(modes, out_dir, unsubscribe_url, address):
    # unsubscribe_url / address are already resolved (see resolve_footer_values)
    server_params = openbell.StdioServerParameters(
        command=sys.executable, args=[os.path.join(PROJECT_DIR, "pippy_mcp.py")], env=dict(os.environ))
    iso = openbell._today_ct_iso()
    async with openbell.stdio_client(server_params) as (read, write):
        async with openbell.ClientSession(read, write) as session:
            await session.initialize()
            for mode in modes:
                subject, html = await _fetch_and_build(session, mode, unsubscribe_url, address)
                path = write_html(out_dir, f"public_{mode}_{iso}.html", html)
                print(f"[public:{mode}] {subject}\n  -> {path} ({len(html)} chars)")
                if not footer_is_complete(unsubscribe_url, address):
                    print("  NOTE: unsubscribe/address are still placeholders — the ESP must fill them before sending.")


def main():
    ap = argparse.ArgumentParser(description="Build the public edition HTML (never sends, never writes state).")
    ap.add_argument("--mode", choices=["morning", "close", "both"], default="both")
    ap.add_argument("--out", default=os.path.join(PROJECT_DIR, "public_out"))
    ap.add_argument("--unsubscribe-url", default=None,
                    help=f"ESP merge tag or URL (else ${ENV_UNSUBSCRIBE}, else the config file, else a placeholder)")
    ap.add_argument("--address", default=None,
                    help=f"postal address or merge tag (else ${ENV_ADDRESS}, else the config file, else a placeholder)")
    ap.add_argument("--config", default=None, help=f"JSON config with unsubscribe_url / mailing_address (default {CONFIG_FILE})")
    ap.add_argument("--verbose", action="store_true", help="log every per-symbol detail instead of one summary line")
    ap.add_argument("--require-footer-values", action="store_true",
                    help="fail instead of writing a page whose unsubscribe/address are still placeholders")
    a = ap.parse_args()
    if a.verbose:
        os.environ["PIPPY_VERBOSE"] = "1"
    url, addr = resolve_footer_values(a.unsubscribe_url, a.address, a.config)
    if a.require_footer_values and not footer_is_complete(url, addr):
        sys.exit("refusing to build: unsubscribe URL and/or mailing address are still placeholders "
                 "(set them via flags, environment, or the config file)")
    modes = ["morning", "close"] if a.mode == "both" else [a.mode]
    asyncio.run(_run(modes, a.out, url, addr))


if __name__ == "__main__":
    main()
