"""
watchlist_brief.py — a facts-only watchlist section for the PUBLIC product.

Input: tickers a subscriber chose (plain strings — nothing else is accepted: no shares, no
dollar amounts, no cost basis). Output: a short HTML section per edition (morning / close).
No website, database or sender here; this module only validates, plans fetches, fetches,
and renders.

For each ticker it states: last price, the day's change, how that compares with the S&P 500
and with the ticker's sector, and whether a same-day headline names it (topic summary only —
a headline is never pasted or quoted). Explanations are hedged readings, never causes.

Safety properties (each is tested):
  * Never reads the owner's private data. It does not import pippy_mcp, does not call the
    memory/picks tools and opens no files.
  * Facts only: an advice-language guard (buy/sell/hold, thesis, target, upside, score,
    rating, since entry, ...) fails generation if any such word is in what this module wrote.
  * At most MAX_TICKERS tickers per subscriber; malformed tickers are rejected with a reason;
    unknown ones are reported, not guessed at.
  * Fetch cost does not scale with subscribers: each unique ticker is fetched once per run and
    the result is shared (see plan_fetch / FetchPlan.describe()).

Timing: data is the last COMPLETED daily bar. Run the morning edition before the open (it
reports the last session) and the close edition after the close (it reports today).
"""
import datetime
import html as _html
import re
from dataclasses import dataclass, field

import openbell

MAX_TICKERS = 15
SP500_SYMBOL = "^GSPC"
FOOTER_LINE = ("A watchlist summary of public market data for the tickers you chose. "
               "Not investment advice.")

# "In line with" band, in percentage points. A judgment call: small enough that a 2-point gap
# is clearly a difference, large enough that ordinary noise between a stock and an index of a
# few hundred names isn't narrated as a divergence.
IN_LINE_PP = 0.5
# A headline counts as "today's" if it was published within this many hours of the run.
NEWS_MAX_AGE_HOURS = 24

# Mirrors pippy_mcp.SECTOR_ETFS (a test asserts they match; this module does not import pippy_mcp).
SECTOR_ETFS = {
    "Technology": "XLK", "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV",
    "Industrials": "XLI", "Consumer Discret.": "XLY", "Consumer Staples": "XLP",
    "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU", "Comm. Services": "XLC",
}
# Yahoo's sector names -> the names used for SECTOR_ETFS. Anything else (ETFs, funds) gets no sector.
YAHOO_SECTOR_TO_OURS = {
    "Technology": "Technology", "Financial Services": "Financials", "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discret.", "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy", "Industrials": "Industrials", "Basic Materials": "Materials",
    "Real Estate": "Real Estate", "Utilities": "Utilities", "Communication Services": "Comm. Services",
}

# The shared summarizer words analyst actions as upgrades/downgrades/price targets — rating
# language that does not belong here. Neutral wording instead.
_TOPIC_OVERRIDES = {
    "an analyst upgrade": "analyst commentary",
    "an analyst downgrade": "analyst commentary",
    "an analyst price-target change": "analyst commentary",
}

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


# ── Validation ────────────────────────────────────────────────────────────────

class WatchlistError(ValueError):
    pass


class TooManyTickers(WatchlistError):
    pass


class AdviceLanguageError(Exception):
    """Raised when advice language is found in a generated watchlist section."""


@dataclass
class Rejected:
    raw: str
    reason: str


@dataclass
class Watchlist:
    tickers: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def validate_watchlist(raw) -> Watchlist:
    """Normalise a subscriber's list. Takes tickers only: anything that isn't a plain symbol
    string (a number of shares, a dollar amount, 'AAPL:10', a dict) is rejected, never parsed.
    Raises TooManyTickers beyond MAX_TICKERS distinct valid tickers (never silently truncates)."""
    if not isinstance(raw, (list, tuple)):
        raise WatchlistError("tickers must be given as a list of ticker symbols")
    wl = Watchlist()
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            wl.rejected.append(Rejected(repr(item)[:40], "only ticker symbols are accepted"))
            continue
        sym = item.strip().upper().replace(".", "-")        # BRK.B -> BRK-B (Yahoo's spelling)
        if not sym:
            wl.rejected.append(Rejected(item, "empty"))
        elif not _TICKER_RE.match(sym):
            wl.rejected.append(Rejected(item[:40], "not a valid ticker symbol"))
        elif sym not in seen:
            seen.add(sym)
            wl.tickers.append(sym)
    if len(wl.tickers) > MAX_TICKERS:
        raise TooManyTickers(f"a watchlist can hold at most {MAX_TICKERS} tickers; got {len(wl.tickers)}")
    return wl


# ── Fetch plan ────────────────────────────────────────────────────────────────

@dataclass
class FetchPlan:
    tickers: list            # unique tickers across all subscribers, sorted
    history_symbols: list    # tickers + the S&P 500 + the 11 sector ETFs, fetched in ONE batched request
    subscribers: int
    requested: int           # sum of list lengths (what per-subscriber fetching would pay for)

    @property
    def calls(self) -> dict:
        u = len(self.tickers)
        return {"history (1 batched request)": 1 if u else 0, "profile (name, sector)": u, "news search": u}

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def naive_calls(self) -> int:
        return (1 if self.requested else 0) * self.subscribers + 2 * self.requested

    def describe(self) -> str:
        lines = [f"Fetch plan: {self.subscribers} subscriber(s), {self.requested} tickers requested, "
                 f"{len(self.tickers)} unique."]
        lines.append(f"  history: 1 batched request for {len(self.history_symbols)} symbols "
                     f"({len(self.tickers)} tickers + {SP500_SYMBOL} + {len(SECTOR_ETFS)} sector ETFs), shared by everyone")
        lines.append(f"  profile: {len(self.tickers)} request(s), one per unique ticker")
        lines.append(f"  news:    {len(self.tickers)} request(s), one per unique ticker")
        lines.append(f"  total {self.total_calls} request(s) vs {self.naive_calls} if every subscriber fetched their own list")
        lines.append("  (tickers with no price history are skipped for profile/news, so actual calls can be fewer)")
        return "\n".join(lines)


def plan_fetch(watchlists: dict) -> FetchPlan:
    unique = sorted({t for wl in watchlists.values() for t in wl.tickers})
    syms = unique + [SP500_SYMBOL] + list(SECTOR_ETFS.values()) if unique else []
    return FetchPlan(unique, syms, len(watchlists), sum(len(wl.tickers) for wl in watchlists.values()))


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class TickerData:
    symbol: str
    known: bool = False
    name: str = ""
    sector: str = None        # one of SECTOR_ETFS' names, or None
    price: float = None
    pct: float = None
    as_of: datetime.date = None
    news: list = field(default_factory=list)   # [{"title","snippet","site"}], already age-filtered
    news_ok: bool = True


@dataclass
class MarketData:
    tickers: dict = field(default_factory=dict)       # symbol -> TickerData
    sp_pct: float = None
    sector_pct: dict = field(default_factory=dict)    # our sector name -> pct
    as_of: datetime.date = None


class DataUnavailable(Exception):
    pass


def _pct_from_closes(closes: list):
    """closes: [(date, close), ...] oldest first. -> (as_of, last, pct) or None if < 2 usable bars."""
    pts = [(d, c) for d, c in closes if isinstance(c, (int, float)) and c == c and c > 0]
    if len(pts) < 2:
        return None
    (_, prev), (d, last) = pts[-2], pts[-1]
    return d, last, (last - prev) / prev * 100


def fetch_market_data(plan: FetchPlan, client, now: datetime.datetime) -> MarketData:
    """Execute the plan through `client` (history/info/news). `now` is injected (tz-aware UTC)
    so the headline-age filter never reads the live clock in tests."""
    md = MarketData()
    if not plan.tickers:
        return md
    try:
        hist = client.history(plan.history_symbols)
    except Exception as e:
        raise DataUnavailable(f"price history unavailable: {e}")
    sp = _pct_from_closes(hist.get(SP500_SYMBOL, []))
    if sp:
        md.sp_pct, md.as_of = sp[2], sp[0]
    for sector, etf in SECTOR_ETFS.items():
        r = _pct_from_closes(hist.get(etf, []))
        if r:
            md.sector_pct[sector] = r[2]
    for sym in plan.tickers:
        td = TickerData(sym)
        r = _pct_from_closes(hist.get(sym, []))
        if r:
            td.known, td.as_of, td.price, td.pct = True, r[0], r[1], r[2]
            try:
                info = client.info(sym) or {}
                td.name = info.get("name") or sym
                td.sector = YAHOO_SECTOR_TO_OURS.get(info.get("sector") or "")
            except Exception:
                td.name = sym
            try:
                cutoff = now - datetime.timedelta(hours=NEWS_MAX_AGE_HOURS)
                td.news = [n for n in (client.news(sym) or [])
                           if isinstance(n.get("published"), datetime.datetime) and n["published"] >= cutoff]
            except Exception:
                td.news, td.news_ok = [], False
        md.tickers[sym] = td
    return md


class YFinanceClient:
    """Default client (read-only, public data). Exactly one batched price request for all symbols."""

    def history(self, symbols):
        import yfinance as yf
        df = yf.download(list(symbols), period="10d", interval="1d", auto_adjust=False, progress=False,
                         group_by="ticker", threads=False)
        out = {}
        for s in symbols:
            try:
                col = df[s]["Close"].dropna()
                out[s] = [(i.date(), float(v)) for i, v in col.items()]
            except Exception:
                out[s] = []
        return out

    def info(self, symbol):
        import yfinance as yf
        i = yf.Ticker(symbol).info or {}
        return {"name": i.get("shortName") or i.get("longName"), "sector": i.get("sector")}

    def news(self, symbol):
        """Search first: it kept working on the days Yahoo's per-ticker news endpoint (Ticker.news)
        404'd and returned []. If Search itself fails, fall back to Ticker.news. Because Ticker.news
        swallows its 404 into [], an empty backup after a Search failure is reported as a FAILURE
        (the caller then says "couldn't pull headlines"), never as "no news"."""
        import sys
        import yfinance as yf
        try:
            return [self._from_search(it) for it in (yf.Search(symbol, news_count=8).news or [])]
        except Exception as e:
            print(f"[watchlist] Search news failed for {symbol} ({type(e).__name__}); trying Ticker.news", file=sys.stderr)
        items = yf.Ticker(symbol).news or []
        if not items:
            raise RuntimeError(f"news unavailable for {symbol}: Search failed and Ticker.news returned nothing")
        return [self._from_ticker_news(it) for it in items]

    @staticmethod
    def _from_search(it):
        ep = it.get("providerPublishTime")
        return {"title": it.get("title", ""), "snippet": "", "site": it.get("publisher", ""),
                "published": (datetime.datetime.fromtimestamp(ep, tz=datetime.timezone.utc)
                              if isinstance(ep, (int, float)) else None)}

    @staticmethod
    def _from_ticker_news(it):
        c = it.get("content", {}) or {}
        raw = c.get("pubDate") or c.get("displayTime") or ""
        try:
            pub = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pub = None
        return {"title": c.get("title") or it.get("title", ""), "snippet": c.get("summary", "") or "",
                "site": (c.get("provider") or {}).get("displayName", ""), "published": pub}


# ── Headline matching (reuses the shared summarizer, plus a stricter name check) ──────

def _whole(pattern: str, text: str, flags=0) -> bool:
    """Whole-word match that also refuses hyphenated compounds ("Visa-Free", "Apple-picking"):
    a miss only under-claims a mention, which is the safe direction."""
    return bool(re.search(r"(?<![A-Za-z0-9-])" + re.escape(pattern) + r"(?![A-Za-z0-9])(?!-[A-Za-z])", text, flags))


def _names_ticker(h: dict, symbol: str, name: str) -> bool:
    """The shared matcher accepts a company name as a bare SUBSTRING ("Apple" inside "Pineapple",
    "Visa" inside "Advisable"). Require whole-word matches here. Tickers of one or two letters,
    and tickers that are ordinary English words, count only via the company name."""
    text = f"{h.get('title', '') or ''} {h.get('snippet', '') or ''}"
    short = openbell._company_short_name(name)
    if short and len(short) >= 3 and _whole(short, text, re.I):
        return True
    ambiguous = len(symbol) <= 2 or symbol in openbell._COMMON_WORD_TICKERS
    return (not ambiguous) and _whole(symbol, text)      # case-sensitive: "app" is not APP


def headline_sentence(td: TickerData) -> str:
    """One hedged, never-quoting sentence about whether today's headlines name this ticker."""
    if not td.news_ok:
        return "We couldn't pull headlines today, so we can't say whether it is in the news."
    for h in td.news:
        found, specific, field_ = openbell._find_headline_for_symbol([h], td.symbol, td.name, sector="")
        if not (found and specific and _names_ticker(h, td.symbol, td.name)):
            continue
        topic = openbell._headline_topic(h, field_, td.symbol, td.name)
        topic = _TOPIC_OVERRIDES.get(topic, topic)
        if topic:
            return (f"A same-day headline covers {topic}; the data can't show whether that "
                    f"had anything to do with the move.")
        return "A same-day headline mentions it, though we can't tell what it covers."
    return "No same-day headline we found mentions it."


# ── Sentences ─────────────────────────────────────────────────────────────────

def _vs(pct: float, ref: float, ref_label: str) -> tuple:
    """(kind, clause) where kind is 'in', 'ahead' or 'behind'."""
    diff = pct - ref
    words = openbell._pct_words(ref)
    if abs(diff) < IN_LINE_PP:
        return "in", f"in line with {ref_label} ({words})"
    return ("ahead", f"ahead of {ref_label} ({words})") if diff > 0 else ("behind", f"behind {ref_label} ({words})")


def relative_sentences(td: TickerData, data: MarketData) -> str:
    m_kind = m_clause = s_kind = s_clause = None
    if data.sp_pct is not None:
        m_kind, m_clause = _vs(td.pct, data.sp_pct, "the S&P 500")
    sec_pct = data.sector_pct.get(td.sector) if td.sector else None
    if sec_pct is not None:
        s_kind, s_clause = _vs(td.pct, sec_pct, f"its sector, {td.sector}")
    if m_kind and s_kind:
        first = f"That was {m_clause} and {s_clause}."
    elif m_kind:
        first = f"That was {m_clause}. We have no sector comparison for it."
    elif s_kind:
        first = f"That was {s_clause}."
    else:
        return "We couldn't pull the S&P 500 or sector moves to compare it with."
    if m_kind == "in" and s_kind in (None, "in"):
        read = "That suggests it mostly moved with the market, though the data can't show why."
    elif s_kind == "in":
        read = "That suggests the move was shared by its sector rather than the whole market, though the data can't show why."
    elif m_kind == "in":
        read = "That suggests it followed the broader market more than its own sector, though the data can't show why."
    else:
        read = "That suggests something specific to the stock may have mattered, though the data can't say what."
    return f"{first} {read}"


def _when(edition: str, td: TickerData) -> str:
    return "today" if edition == "close" else f"in the last session ({td.as_of:%a %b %d})"


def ticker_text(td: TickerData, data: MarketData, edition: str) -> str:
    if not td.known:
        return f"We couldn't find market data for {td.symbol}, so it is left out of this summary."
    name = openbell._company_short_name(td.name) or td.symbol
    verb = "closed at" if edition == "close" else "last closed at"
    head = f"{name} {verb} ${td.price:,.2f}, {openbell._pct_words(td.pct)} {_when(edition, td)}."
    return " ".join([head, relative_sentences(td, data), headline_sentence(td)])


# ── Advice-language guard ─────────────────────────────────────────────────────

_ADVICE_PATTERNS = [
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
    return [m.group(0) for p in _ADVICE_PATTERNS for m in re.finditer(p, t, flags=re.I)]


def _visible(html_str: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html_str)))


# ── HTML ──────────────────────────────────────────────────────────────────────

def _card(td: TickerData, data: MarketData, edition: str) -> str:
    sym = _html.escape(td.symbol)
    if td.known:
        name = _html.escape(openbell._company_short_name(td.name) or td.symbol)
        price = f"${td.price:,.2f}"
        pct = f'<span style="color:{openbell._pct_color(td.pct)}">{openbell._fmt(td.pct)}</span>'
    else:
        name, price, pct = "", "—", "—"
    body = _html.escape(ticker_text(td, data, edition))
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e5e7eb">
      <tr>
        <td style="padding:12px 12px 2px 0;font-size:16px;font-weight:700;color:#111827;white-space:nowrap">{sym} <span style="font-size:13px;font-weight:400;color:#6b7280">{name}</span></td>
        <td style="padding:12px 12px 2px 0;font-size:16px;color:#374151;white-space:nowrap;text-align:right">{price}</td>
        <td style="padding:12px 0 2px;font-size:16px;font-weight:700;white-space:nowrap;text-align:right;width:80px">{pct}</td>
      </tr>
      <tr><td colspan="3" style="padding:0 0 12px;font-size:16px;line-height:1.5;color:#1f2937">{body}</td></tr>
    </table>"""


def build_watchlist_section(watchlist: Watchlist, data: MarketData, edition: str) -> str:
    """The section for one subscriber. edition: 'morning' or 'close'."""
    if edition not in ("morning", "close"):
        raise ValueError("edition must be 'morning' or 'close'")
    label = "Your Watchlist — Today" if edition == "close" else "Your Watchlist — Last Session"
    cards = "".join(_card(data.tickers.get(t, TickerData(t)), data, edition) for t in watchlist.tickers)
    if not watchlist.tickers:
        cards = '<p style="margin:0;font-size:16px;color:#374151">No valid tickers on this watchlist.</p>'
    note = ""
    if watchlist.rejected:
        bits = "; ".join(f"{r.raw} ({r.reason})" for r in watchlist.rejected)
        note = (f'<p style="margin:10px 0 0;font-size:13px;color:#6b7280">Not included: {_html.escape(bits)}.</p>')
    foot = f'<p style="margin:14px 0 0;font-size:12px;line-height:1.5;color:#6b7280">{FOOTER_LINE}</p>'
    section = openbell._section(label, cards + note + foot)

    # Guard everything this module wrote; third-party strings (names, tickers, sectors) are masked.
    mask = [t for t in watchlist.tickers] + [r.raw for r in watchlist.rejected]
    for t in watchlist.tickers:
        td = data.tickers.get(t)
        if td and td.name:
            mask += [td.name, openbell._company_short_name(td.name)]
    hits = find_advice_language(_visible(section), mask)
    if hits:
        raise AdviceLanguageError(f"advice language in watchlist section: {hits}")
    return section


def build_watchlist_page(watchlist: Watchlist, data: MarketData, edition: str, title_date: str = "") -> str:
    """Standalone page for previewing a section (no unsubscribe/address; this is not the sendable email)."""
    section = build_watchlist_section(watchlist, data, edition)
    return openbell._wrap(section, f"Watchlist &nbsp; {title_date}", "Pippy's Brief", footer_html="")


# ── Many subscribers, one fetch ───────────────────────────────────────────────

@dataclass
class SubscriberResult:
    html: str = None
    error: str = None
    rejected: list = field(default_factory=list)


def render_for_subscribers(subscribers: dict, data: MarketData, edition: str) -> dict:
    out = {}
    for sid, raw in subscribers.items():
        try:
            wl = validate_watchlist(raw)
        except WatchlistError as e:
            out[sid] = SubscriberResult(error=str(e))
            continue
        out[sid] = SubscriberResult(html=build_watchlist_section(wl, data, edition), rejected=wl.rejected)
    return out


def build_for_subscribers(subscribers: dict, client, edition: str, now: datetime.datetime):
    """Validate every list, fetch the UNION of valid tickers once, render each subscriber's section.
    Returns (results, plan). A subscriber with an unusable list gets an error entry; others proceed."""
    valid = {}
    for sid, raw in subscribers.items():
        try:
            valid[sid] = validate_watchlist(raw)
        except WatchlistError:
            pass
    plan = plan_fetch(valid)
    data = fetch_market_data(plan, client, now)
    return render_for_subscribers(subscribers, data, edition), plan
