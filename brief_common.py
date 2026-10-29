"""
brief_common.py — pure helpers shared by the private/public briefs and the watchlist brief.

No side effects on import: it does not call load_dotenv(), read any file, or import openbell /
pippy_mcp, so a module that only needs these (the public watchlist) never touches .env or any
private data. openbell.py imports every name here, so `openbell._foo` keeps working.
Moved verbatim from openbell.py; behavior is unchanged.
"""
import re

GREEN  = "#16a34a"


RED    = "#dc2626"


GRAY   = "#6b7280"


def _pct_color(pct) -> str:
    try:
        return GREEN if float(pct) >= 0 else RED
    except Exception:
        return GRAY


def _arrow(pct) -> str:
    try:
        return "▲" if float(pct) >= 0 else "▼"
    except Exception:
        return "—"


def _fmt(pct) -> str:
    try:
        v = float(pct)
        return f"{_arrow(v)} {abs(v):.2f}%"
    except Exception:
        return str(pct) if pct else "—"


# Reused across every section's <td> (via _section()) and by the two secondary
# table columns hidden on very narrow screens (watchlist headline snippet,
# movers' company-name column) — progressive disclosure instead of letting a
# 4-column row squeeze its percentage value off the edge at 320px.
_RESPONSIVE_STYLE_BLOCK = """<style>
  @media screen and (max-width: 480px) {
    .pippy-section-pad { padding: 20px 16px !important; }
    .pippy-header-pad  { padding: 22px 16px !important; }
    .pippy-footer-pad  { padding: 16px 16px !important; }
    .pippy-hide-narrow { display: none !important; mso-hide: all; }
  }
  /* UNVERIFIED — this dark-mode rule has never been checked in a real dark
     inbox on a real phone, only in the browser pane's prefers-color-scheme
     emulation. That emulation can only prove the CSS itself does what it
     says; it can't prove a light card reads as intentional (vs. broken)
     inside an actual dark Gmail/Mail app on a device. Do not treat this as
     confirmed until that check happens.
     Explicit light-mode opt-in for the card itself (color-scheme meta tells
     well-behaved clients we handle both modes; this pins the white card so
     its existing text-color contrast, and the red/green arrows, hold up
     even when the surrounding client chrome is dark). Gmail's app-level
     dark mode uses its own image/color heuristic rather than
     prefers-color-scheme, so this belt-and-suspenders rule is aimed at
     Apple Mail / Outlook.com / other standards-following dark modes —
     it will not reliably reach Gmail's own inversion. */
  @media (prefers-color-scheme: dark) {
    .pippy-outer-bg  { background: #0b0f19 !important; }
    .pippy-card      { background: #ffffff !important; }
  }
</style>"""


_PRIVATE_FOOTER = """<tr><td class="pippy-footer-pad" style="padding:20px 32px;border-top:1px solid #e5e7eb;background:#f9fafb">
    <p style="margin:0;font-size:11px;color:#9ca3af">Pippy's Brief &mdash; automated daily market briefing. Not financial advice.</p>
  </td></tr>"""


def _wrap(body: str, title: str, subtitle: str, footer_html: str = None) -> str:
    # footer_html is for the public edition only; the default is the private footer, unchanged.
    footer = _PRIVATE_FOOTER if footer_html is None else footer_html
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supports-color-scheme" content="light dark">
{_RESPONSIVE_STYLE_BLOCK}
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table class="pippy-outer-bg" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0">
<tr><td align="center">
<table class="pippy-card" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">

  <!-- HEADER -->
  <tr><td class="pippy-header-pad" style="background:#111827;padding:28px 32px">
    <p style="margin:0 0 4px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af">{subtitle}</p>
    <h1 style="margin:0;font-size:22px;font-weight:700;color:#ffffff;line-height:1.2">{title}</h1>
  </td></tr>

  <!-- BODY -->
  {body}

  <!-- FOOTER -->
  {footer}

</table>
</td></tr>
</table>
</body></html>"""


def _section(label: str, inner: str, bg: str = "") -> str:
    bg_style = f"background:{bg};" if bg else ""
    return f"""<tr><td class="pippy-section-pad" style="{bg_style}padding:24px 32px;border-bottom:1px solid #e5e7eb">
  <p style="margin:0 0 14px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9ca3af">{label}</p>
  {inner}
</td></tr>"""


def _pct_words(v) -> str:
    """'up 0.62%' / 'down 0.15%' / 'unchanged' — plain words instead of arrow glyphs
    for prose (the data tables keep the glyphs)."""
    try:
        v = float(v)
    except Exception:
        return "unchanged"
    if abs(v) < 0.005:
        return "unchanged"
    return f"{'up' if v >= 0 else 'down'} {abs(v):.2f}%"


# Headlines are never pasted or quoted. Zero-AI means we can't paraphrase freely, so a
# headline is reduced to the TOPIC it covers, in the brief's own words — what it is
# about ("an analyst price-target change"), never what it concludes or why a stock moved.
# First matching pattern wins; word-boundary matched like every other keyword list here.
_TICKER_HEADLINE_TOPICS = [
    (r"\b(earnings|quarterly results|revenue|profit|eps)\b", "its latest earnings or results"),
    (r"\bupgrade[sd]?\b", "an analyst upgrade"),
    (r"\bdowngrade[sd]?\b", "an analyst downgrade"),
    (r"\bprice targets?\b", "an analyst price-target change"),
    (r"\b(guidance|outlook|forecast)\b", "its financial outlook"),
    (r"\b(acquir\w+|acquisition|merger|takeover|buyout)\b", "an acquisition or merger"),
    (r"\b(lawsuit|sues?|sued|settlement|probe|investigation|antitrust)\b", "legal or regulatory action"),
    (r"\b(fda|approval|approved|recall)\b", "a regulatory decision"),
    (r"\b(launch\w*|unveil\w*|introduc\w+|new product)\b", "a product announcement"),
    (r"\b(layoffs?|job cuts|restructuring)\b", "job cuts or restructuring"),
    (r"\b(buybacks?|dividends?|stock split)\b", "a buyback, dividend or stock split"),
    (r"\b(ceo|cfo|steps? down|resigns?|appoint\w*)\b", "a leadership change"),
    (r"\b(contracts?|partnerships?|deals?|agreements?)\b", "a business deal"),
    (r"\b(slips?|slid\w*|falls?|fell|drops?|dropp\w+|plunge\w*|surges?|surged|jumps?|jumped|soars?|soared|rall\w+)\b",
     "the stock's recent move"),
]


_TOPIC_WINDOW_WORDS = 5   # a topic keyword counts only within this many words of the stock's own name


def _headline_topic(h: dict, field: str = None, symbol: str = "", company: str = "") -> str:
    """
    The topic a ticker-specific headline covers, in the brief's own words, or "" if
    none can be assigned confidently (the caller then says it can't tell what it covers).

    A keyword only counts when it sits within a few words of the stock's own ticker or
    company name — "Walmart earnings beat estimates; MELI also higher" mentions earnings,
    but they're Walmart's, so attributing "its latest earnings" to MELI would be a guess
    presented as fact. When symbol/company are given and no topic keyword is near a
    mention, the answer is "" rather than the nearest keyword anywhere in the headline.
    """
    from_name = _company_short_name(company).lower()
    sym_l = (symbol or "").lower()
    parts = [h.get("title", "") or "", h.get("snippet", "") or ""]
    if field == "snippet":
        parts.reverse()
    for text in parts:
        tl = text.lower()
        if not tl:
            continue
        tokens = [(m.start(), m.group()) for m in re.finditer(r"[a-z0-9&'.-]+", tl)]
        mention_idx = []
        if sym_l or from_name:
            for k, (_, tok) in enumerate(tokens):
                if sym_l and tok.strip(".'-") == sym_l:
                    mention_idx.append(k)
            if from_name:
                first = from_name.split()[0]
                for k, (_, tok) in enumerate(tokens):
                    if tok.strip(".'-") == first:
                        mention_idx.append(k)
            if not mention_idx:
                continue
        for pat, phrase in _TICKER_HEADLINE_TOPICS:
            for m in re.finditer(pat, tl):
                if not mention_idx:
                    return phrase
                kw_idx = sum(1 for st, _ in tokens if st < m.start())
                if any(abs(kw_idx - mi) <= _TOPIC_WINDOW_WORDS for mi in mention_idx):
                    return phrase
    return ""


# Sector-family keywords for headline-based causal matching. Word-boundary matched
# against headline title+snippet — same false-match-safe pattern as _MACRO_KEYWORDS.
# Kept as explicit plural/compound variants rather than suffix-wildcard regex —
# wildcarding a short root (e.g. "war" -> "war\w*") would reopen the exact
# false-match bug class already fixed elsewhere ("war" inside "warehouse").
# Each variant here is still a full \b-bounded word, just spelled out.
_SECTOR_HEADLINE_KEYWORDS = {
    "energy":        ["oil", "crude", "opec", "energy prices", "natural gas", "oil prices"],
    "technology":    ["chip", "chips", "chipmaker", "chipmakers", "semiconductor", "semiconductors",
                      "ai stocks", "ai", "artificial intelligence", "cloud", "cloud computing",
                      "software", "software stocks", "cybersecurity", "cyberattack", "big tech",
                      "tech selloff", "tech rally", "downgrade", "guidance cut",
                      "data center", "data centers"],
    "financial":     ["bank", "banks", "banking", "rate cut", "rate hike", "yield", "fed",
                      "lender", "lenders"],
    "health":        ["fda", "drug", "drugs", "biotech", "trial", "trials", "recall",
                      "pharma", "pharmaceutical"],
    "real estate":   ["mortgage rate", "mortgage rates", "housing", "homebuilder", "homebuilders"],
    "utilities":     ["rate cut", "rate hike", "power grid", "electricity prices"],
    "consumer":      ["retail sales", "consumer spending", "holiday sales", "retailer", "retailers"],
    "industrial":    ["manufacturing", "factory", "factories", "supply chain", "tariff", "tariffs"],
    "material":      ["commodity prices", "metals", "mining"],
    "communication": ["streaming", "advertising", "media", "telecom"],
}


def _sector_family(name: str) -> str:
    n = (name or "").lower()
    for fam in _SECTOR_HEADLINE_KEYWORDS:
        if fam in n:
            return fam
    return ""


def _find_headline_for_keywords(headlines: list, keywords: list, exclude: set = None):
    """
    Returns (headline, field) — field is "title" or "snippet", whichever actually
    contained the matching keyword. Matching against title+snippet combined but
    then citing a default field (e.g. snippet-first) can quote an unrelated part
    of the same headline object; tracking the real match location avoids that.

    exclude, if given, is a set of headline titles already cited elsewhere in the
    same summary — skipped so the same headline can't be pasted twice (or three
    times) into one output.
    """
    exclude = exclude or set()
    for h in headlines or []:
        title = h.get("title", "") or ""
        if title in exclude:
            continue
        title_l   = title.lower()
        snippet_l = (h.get("snippet", "") or "").lower()
        for kw in keywords:
            pat = r'\b' + re.escape(kw.lower()) + r'\b'
            if re.search(pat, title_l):
                return h, "title"
            if re.search(pat, snippet_l):
                return h, "snippet"
    return None, None


# Tickers that are also common English words. A bare match on these ("WALL STREET RALLIES NOW")
# is not evidence a headline is about the company, so only the company name counts.
_COMMON_WORD_TICKERS = {"NOW", "ON", "IT", "A", "ALL", "ARE", "FOR", "HAS", "ONE", "BIG", "FAST", "WELL",
                        "REAL", "TRUE", "GOOD", "LOVE", "CARE", "OPEN", "PLAY", "WORK", "TECH", "EAT"}


_CORP_SUFFIXES = {"corporation", "corp", "inc", "holdings", "holding", "co", "ltd", "plc", "company", "group"}


def _company_short_name(name: str) -> str:
    """Strip trailing corporate suffixes (\"AppLovin Corporation\" -> \"AppLovin\")
    so headline matching isn't defeated by the formal legal name."""
    tokens = [t.strip(",.") for t in (name or "").split()]
    while tokens and tokens[-1].lower().strip(".") in _CORP_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _name_in_text(name_l: str, text: str) -> bool:
    """Whole-word company-name match. A bare substring test matched "apple" inside "Pineapple" and
    "visa" inside "Advisable"; hyphenated compounds ("Visa-Free") are refused too. A miss only
    under-claims a mention, which is the safe direction."""
    return bool(name_l) and bool(re.search(
        r"(?<![a-z0-9-])" + re.escape(name_l) + r"(?![a-z0-9])(?!-[a-z])", (text or "").lower()))


def _find_headline_for_symbol(headlines: list, symbol: str, company_name: str = "", sector: str = "",
                              exclude: set = None):
    """
    Returns (headline, is_specific, field). is_specific=True means the headline
    names this exact ticker or company. If no literal match exists, falls back to
    a sector-category match (e.g. a "cybersecurity stocks" headline for a
    cybersecurity-sector mover with no ticker of its own in the text) — still
    real signal, just less specific, so the caller can phrase it honestly. field
    tracks whether the match landed in the title or snippet, so citation quotes
    the part that actually matched.

    exclude, if given, is a set of headline titles already cited elsewhere in
    the same summary — skipped so the same headline never gets pasted twice.
    """
    exclude = exclude or set()
    if symbol:
        pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')  # case-sensitive — avoids "app"/"APP" false hits
        name_l  = _company_short_name(company_name).lower()
        if symbol in _COMMON_WORD_TICKERS:
            # NOW / ON / IT are also ordinary English (and an all-caps headline defeats the
            # case-sensitivity above), so the bare ticker proves nothing: require the company name.
            pattern = re.compile(r"(?!x)x")
        for h in headlines or []:
            title, snippet = h.get("title", "") or "", h.get("snippet", "") or ""
            if title in exclude:
                continue
            if pattern.search(title) or _name_in_text(name_l, title):
                return h, True, "title"
            if pattern.search(snippet) or _name_in_text(name_l, snippet):
                return h, True, "snippet"

    fam = _sector_family(sector)
    if fam:
        h, field = _find_headline_for_keywords(headlines, _SECTOR_HEADLINE_KEYWORDS[fam], exclude=exclude)
        if h:
            return h, False, field

    return None, False, None


# Mirrors pippy_mcp.SECTOR_ETFS (test_watchlist_brief asserts they match).
SECTOR_ETFS = {
    "Technology": "XLK", "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV",
    "Industrials": "XLI", "Consumer Discret.": "XLY", "Consumer Staples": "XLP",
    "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU", "Comm. Services": "XLC",
}

# ── Advice-language guard (watchlist and every desk) ──────────────────────────
ADVICE_PATTERNS = [
    r"\bbuy(s|ing)?\b", r"\bbought\b", r"\bsell(s|ing)?\b", r"\bsold\b", r"\bhold(s|ing|ings)?\b",
    r"\bthesis\b", r"\btargets?\b", r"\bupside\b", r"\bscores?\b", r"\bratings?\b", r"\brated\b",
    r"\bsince entry\b", r"\brecommend\w*", r"\bupgrade[sd]?\b", r"\bdowngrade[sd]?\b",
    r"\boutperform\w*", r"\bunderperform\w*", r"\boverweight\b", r"\bunderweight\b", r"\byou should\b",
]


def find_advice_language(text: str, mask: list = ()) -> list:
    """Advice words in `text`. `mask` strings (company names, tickers — third-party text such as
    'Best Buy' or 'Target Corporation') are removed first so a real company name can't trip it."""
    t = text
    for m in sorted({m for m in mask if m}, key=len, reverse=True):
        t = re.sub(r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])", " ", t, flags=re.I if len(m) > 5 else 0)
    return [m.group(0) for p in ADVICE_PATTERNS for m in re.finditer(p, t, flags=re.I)]
