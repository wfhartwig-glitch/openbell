# Pippy's Brief

Four automated emails. Morning briefing before the open, closing summary
after the bell, and a standalone business-history Case Study that fires on
its own schedule, fully decoupled from market status.
Powered by FMP (Financial Modeling Prep). Zero AI API cost.

---

## Email Schedule

| Time (Central) | Day | Email |
|---|---|---|
| 8:30 AM CT | Weekdays | ☀️ Morning Briefing — snapshot, headlines, calendar, watchlist, unified Stock Picks (skips on non-trading days) |
| 3:00 PM CT | Weekdays | 📊 Market Close Summary — closing prices, top movers, sectors, watchlist EOD, headlines (skips on non-trading days) |
| 12:00 PM CT | Weekdays | 🧠 Case Study — business-history narrative (crises, founders, rivalries, product launches), zero market data, fires unconditionally |
| 8:30 AM CT | Weekends | 🧠 Case Study — same content engine, weekend schedule |

The Case Study rotates through a hand-curated library of ~40 stories with dedupe logic (won't repeat a topic used in the last ~30 sends).

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# then edit .env with your keys (see sections below)

# 3. Test one email right now (add --dry-run to preview without sending)
python openbell.py morning      # Morning Briefing (skips on non-trading days)
python openbell.py close        # Market Close Summary (skips on non-trading days)
python openbell.py casestudy    # Case Study (fires unconditionally, any day)
```

---

## API keys & credentials

### 1. Gmail App Password

You need a **Gmail App Password** — this is different from your regular Gmail
password and is safe to put in a `.env` file (it can only send mail, not read
it or change your account).

Steps:
1. Go to your Google Account → **Security**
2. Turn on **2-Step Verification** if not already on
3. Go to **App passwords**: https://myaccount.google.com/apppasswords
4. Select app: *Mail*, device: *Other* → type "OpenBell" → click **Generate**
5. Copy the 16-character password into `.env` as `GMAIL_APP_PASSWORD`

Set `GMAIL_ADDRESS` to the Gmail address you're sending from, and `TO_EMAIL`
to wherever you want the briefing delivered (can be the same address).

---

### 2. NewsAPI key (free)

1. Register at https://newsapi.org/register
2. Copy your API key into `.env` as `NEWS_API_KEY`

Free tier: 100 requests/day, US business headlines — more than enough for
one briefing per weekday.

---

### 3. Anthropic API key

1. Sign in at https://console.anthropic.com
2. Go to **Settings → API Keys** → **Create Key**
3. Copy the key (starts with `sk-ant-`) into `.env` as `ANTHROPIC_API_KEY`

OpenBell makes **two** Claude API calls per day (one for "What to Watch",
one for monthly picks on the 1st). Usage cost is well under $0.01/day on
Sonnet.

---

## API Keys Required

| Key | Where to get it | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Yes |
| `GMAIL_ADDRESS` | Your Gmail address | Yes |
| `GMAIL_APP_PASSWORD` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) | Yes |
| `TO_EMAIL` | Your recipient email | Yes |
| `FMP_API_KEY` | [financialmodelingprep.com](https://financialmodelingprep.com) — free tier | Yes |
| `NEWS_API_KEY` | [newsapi.org](https://newsapi.org/register) — free tier | Optional fallback |

---

## GitHub Actions Setup

Everything runs automatically in the cloud — no local machine needed.

1. Go to **[github.com/wfhartwig-glitch/openbell/settings/secrets/actions](https://github.com/wfhartwig-glitch/openbell/settings/secrets/actions)**
2. Click **New repository secret** and add each of these:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-…`) |
| `GMAIL_ADDRESS` | Gmail address you're sending from |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password |
| `TO_EMAIL` | Where to deliver the emails |
| `FMP_API_KEY` | Your FMP API key (free at financialmodelingprep.com) |
| `NEWS_API_KEY` | Your NewsAPI key (optional — FMP headlines used first) |

3. Go to the **Actions** tab and manually trigger `OpenBell Morning Briefing` once to confirm it works end to end.

After that, GitHub Actions fires on schedule — Pippy wakes up, fetches live data, writes the email, sends it, saves what it learned, and commits `pippy_memory.json` back to the repo. Every run makes it smarter.

---

## Architecture

```
GitHub Actions (cron scheduler)
        ↓
  openbell.py (CLI entrypoint)
        ↓
  Pippy agentic loop (Anthropic tool-use API)
        ↓  calls tools from →  pippy_mcp.py
        ↓
  Gmail SMTP (sends email)
        ↓
  pippy_memory.json committed back to repo
```

**Pippy** is a fully autonomous agent. It decides which tools to call, in what order, based on the email type. It reads its own memory, fetches live data, writes the HTML email, sends it, and saves what it learned — all without being told step by step.

**pippy_mcp.py** exposes all 13 tools two ways:
- As plain Python functions imported by `openbell.py` for cloud runs
- As a FastMCP server for the local `pippy.py` terminal agent

**pippy_memory.json** is committed to this repo and is Pippy's persistent brain. Terminal sessions and cloud email runs share the same memory.

---

## How it works

| Section | Primary Source | Fallback |
|---|---|---|
| Market snapshot / indices | FMP `/quotes/index` | yfinance |
| Stock data | FMP `/quote/{ticker}` | yfinance |
| Top headlines | FMP `/stock_news` | NewsAPI → yfinance |
| Sector performance | FMP `/sectors-performance` | yfinance sector ETFs |
| Top movers | FMP `/stock_market/gainers` + `/losers` | yfinance scan |
| Economic calendar | FMP `/economic_calendar` | yfinance earnings |
| Earnings calendar | FMP `/earning_calendar` | — |
| Pre-market data | FMP `/pre-post-market/{ticker}` | yfinance |
| Market open status | FMP `/is-the-market-open` | hardcoded holiday list |
| All narrative writing | Claude `claude-sonnet-4-6` via tool-use agentic loop | — |
| Monthly picks | `yfinance` fundamentals scan, cached in `picks_cache.json` | — |
| Case Study topics | Hand-curated library in `case_studies.py`, rotated via `case_study_history.json` | — |

---

## Weekly Stock Picks

Pippy reviews and updates his 5 stock picks every Monday morning. Picks are not changed for novelty — they're kept if the thesis still holds and replaced only when something has genuinely shifted (sharp price breakdown, analyst downgrade, broken technical level). Pippy tracks the performance of every pick over time and uses that track record to improve future selections — leaning toward sectors and risk profiles that have been working, away from those that haven't.

Each pick carries:
- `weeks_held` — how many weeks it has survived review
- `pct_change_since_pick` — total return since first picked
- `note` — "Holding — thesis intact" or "New pick — reason" or "Replaced X — reason it was dropped"

This builds a real track record over time instead of resetting fresh every week.

---

## Running locally

```bash
# Send one email right now
python openbell.py morning     # skips automatically on non-trading days
python openbell.py close       # skips automatically on non-trading days
python openbell.py casestudy   # fires unconditionally, any day

# Start the terminal agent
python pippy.py
```

---

## File overview

```
openbell.py        — main script
.env               — your credentials (never commit this)
.env.example       — credential template
picks_cache.json   — cached monthly stock picks
requirements.txt   — Python dependencies
README.md          — this file
```
