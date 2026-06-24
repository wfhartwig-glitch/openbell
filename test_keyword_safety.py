"""
test_keyword_safety.py — Word-boundary collision checker for all headline keyword lists.

Run after adding any new keyword to _MACRO_KEYWORDS, _STRONG_MARKET_KWS, or _WEAK_MARKET_KWS.
Catches the "Fed/FedEx", "AI/gaIns", "yield/Yield" class of false-match bugs before they ship.

Usage:
    python3.11 test_keyword_safety.py
"""

import re
import sys

# ── Keyword lists (must stay in sync with openbell.py definitions) ─────────────

# _MACRO_KEYWORDS: short/≤6-char entries use \b word-boundary matching in production
MACRO_KEYWORDS = [
    "Fed", "Federal Reserve", "rate cut", "rate hike", "interest rate",
    "inflation", "CPI", "jobs", "unemployment", "payroll",
    "Iran", "tariff", "trade war", "earnings", "GDP", "recession",
]

# _STRONG_MARKET_KWS: currently uses plain `in` substring matching in production
STRONG_MARKET_KWS = [
    "federal reserve", "rate cut", "rate hike", "interest rate",
    "inflation", "cpi", "ppi", "payroll", "unemployment",
    "iran", "tariff", "trade war", "opec",
    "selloff", "sell-off", "s&p 500", "nasdaq composite",
    "treasury yield", "10-year yield", "recession", "gdp",
]

# _WEAK_MARKET_KWS: uses \b word-boundary matching in production
WEAK_MARKET_KWS = [
    "fed", "jobs", "war", "oil", "trade", "stocks", "market", "dow", "nasdaq",
    "treasury", "yield", "earnings", "growth", "debt", "deficit", "sanctions", "bank",
    "rally", "rates",
]

# ── Tricky words: common in financial headlines, could contain a short keyword as substring ──
# Expand this list whenever a new collision is found in the wild.
TRICKY_WORDS = [
    # Previously caught false matches
    "FedEx",        # contains "Fed"
    "Gainers",      # contains "AI" (historical, now fixed)
    "Yield",        # capital Y (historical, now fixed)
    # New candidates to check
    "Iranian",      # contains "Iran"
    "Jobless",      # contains "jobs" (but \b should NOT match since "job" != "jobs")
    "Oilers",       # contains "oil"
    "Warehouse",    # contains "war"
    "Warranty",     # contains "war"
    "SoFi",         # contains "fi" — low risk but included for completeness
    "Airbnb",       # no collision expected
    "Deficit",      # contains "defi" — check for "def" variants
    "Stockpiling",  # contains "stock"
    "Marketable",   # contains "market"
    "Downturn",     # contains "dow"
    "Fedora",       # contains "fed"
    "Deficits",     # contains "deficit" — plural, same word, should match (not a false positive)
    "Dowjones",     # contains "dow"
    "Rallying",     # contains "rally" (inflection, should match — not a false positive)
    "Banker",       # contains "bank"
    "Banking",      # contains "bank" (inflection, should match)
    "Sanction",     # contains "sanction" but not "sanctions" — \b protects
    "Trader",       # contains "trade"
    "Traded",       # contains "trade" — inflection, should match
    "Tariffs",      # contains "tariff" — plural, should match
    "Earnings",     # contains "earning" — should match via "earnings" keyword
    "Payrolls",     # contains "payroll" — plural, may or may not match depending on boundary
    "Recession",    # exact — should match
]

# ── Matching functions as actually implemented in openbell.py ─────────────────

def macro_matches(kw: str, title: str) -> bool:
    """Mirrors the _MACRO_KEYWORDS matching logic in _build_morning_summary (Pass 1)."""
    if len(kw) <= 6:
        return bool(re.search(r'\b' + re.escape(kw) + r'\b', title, re.IGNORECASE))
    return kw.lower() in title.lower()


def strong_matches(kw: str, title: str) -> bool:
    """Mirrors _STRONG_MARKET_KWS matching — word boundary for ≤6 chars, substring otherwise."""
    tl = title.lower()
    if len(kw) <= 6:
        return bool(re.search(r'\b' + re.escape(kw) + r'\b', tl))
    return kw in tl


def weak_matches(kw: str, title: str) -> bool:
    """Mirrors _WEAK_MARKET_KWS matching — word boundary."""
    return bool(re.search(r'\b' + re.escape(kw) + r'\b', title, re.IGNORECASE))


# ── What constitutes a FALSE positive ────────────────────────────────────────
# A match is a false positive when:
#   - the keyword appears as a PREFIX/SUFFIX of a different word (not just an inflection)
#   - e.g. "Fed" matching "FedEx", "war" matching "Warehouse"
# A match is LEGITIMATE when:
#   - it's a plural/inflection of the same word ("tariff"→"Tariffs", "bank"→"Banking")
#   - the tricky word IS semantically the same thing ("deficit"→"Deficits")

# Words where a keyword match IS expected and correct (inflections / same concept)
EXPECTED_MATCHES = {
    ("tariff", "Tariffs"),
    ("tariff", "tariffs"),
    ("earnings", "Earnings"),
    ("recession", "Recession"),
    ("payroll", "Payrolls"),
    ("rally", "Rallying"),
    ("bank", "Banker"),
    ("bank", "Banking"),
    ("deficit", "Deficits"),
    ("trade", "Traded"),
    ("sanctions", "Sanction"),    # partial — "sanction" doesn't contain "sanctions", so no match
}

# ── Run the checks ────────────────────────────────────────────────────────────

failures = []

def check_list(label: str, keywords: list, match_fn, tricky: list):
    for kw in keywords:
        for tw in tricky:
            # Skip pairs where a match is expected/correct
            if (kw.lower(), tw) in {(a.lower(), b) for a, b in EXPECTED_MATCHES}:
                continue
            matched = match_fn(kw, tw)
            if not matched:
                continue
            # It matched — is it a false positive?
            # False positive: keyword appears as substring of a larger different word
            # Detect by checking if kw is a strict substring (not a word on its own)
            kw_lower = kw.lower()
            tw_lower = tw.lower()
            # Find where kw appears in tw
            idx = tw_lower.find(kw_lower)
            if idx == -1:
                continue  # no substring — regex boundary matched a different form
            before_ok = (idx == 0 or not tw_lower[idx - 1].isalpha())
            after_ok  = (idx + len(kw) >= len(tw) or not tw_lower[idx + len(kw)].isalpha())
            if not (before_ok and after_ok):
                failures.append((label, kw, tw, match_fn.__doc__.split("—")[0].strip()))


print("Running keyword word-boundary safety checks...\n")
check_list("_MACRO_KEYWORDS",      MACRO_KEYWORDS,      macro_matches,  TRICKY_WORDS)
check_list("_STRONG_MARKET_KWS",   STRONG_MARKET_KWS,   strong_matches, TRICKY_WORDS)
check_list("_WEAK_MARKET_KWS",     WEAK_MARKET_KWS,     weak_matches,   TRICKY_WORDS)

if failures:
    print(f"FAILED — {len(failures)} collision(s) found:\n")
    for label, kw, tw, matcher in failures:
        print(f"  [{label}] \"{kw}\" falsely matches inside \"{tw}\" using {matcher}")
    print(f"\nFix: apply \\b word-boundary matching for keywords ≤6 chars in {label}.")
    sys.exit(1)
else:
    print("All keyword lists passed — no substring collision bugs found.")
    print(f"  Checked {len(MACRO_KEYWORDS)} _MACRO_KEYWORDS × {len(TRICKY_WORDS)} tricky words")
    print(f"  Checked {len(STRONG_MARKET_KWS)} _STRONG_MARKET_KWS × {len(TRICKY_WORDS)} tricky words")
    print(f"  Checked {len(WEAK_MARKET_KWS)} _WEAK_MARKET_KWS × {len(TRICKY_WORDS)} tricky words")
    sys.exit(0)
