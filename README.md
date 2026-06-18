# OpenBell

Three automated emails, every day. Morning briefing before the open, closing
summary after the bell, and deep dives on weekends and holidays. Powered by
Claude + yfinance.

---

## Email Schedule

| Time (Central) | Day | Email |
|---|---|---|
| 8:30 AM CT | Weekdays | ☀️ Morning Briefing — futures, headlines, What to Watch, calendar, watchlist, monthly picks |
| 3:00 PM CT | Weekdays | 📊 Market Close Summary — closing prices, Today's Story, top movers, sectors, watchlist EOD |
| 8:30 AM CT | Weekends & holidays | 📚 Deep Dive — Claude-written deep dive on a company, sector, macro theme, or market topic |

Market holidays (NYSE) are hardcoded for 2025 and 2026. On a holiday weekday
the close job is skipped and the morning job sends a Deep Dive instead.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# then edit .env with your keys (see sections below)

# 3. Test one email right now
python openbell.py --now morning    # send a Morning Briefing immediately
python openbell.py --now close      # send a Market Close Summary immediately
python openbell.py --now deepdive   # send a Deep Dive immediately
python openbell.py --now auto       # use schedule logic (market open? → morning, else → deep dive)

# 4. Run the scheduler (stays running, fires at 09:30 weekdays)
python openbell.py
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
| `NEWS_API_KEY` | Your NewsAPI key (optional but recommended) |

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

| Section | Source |
|---|---|
| Futures / closing prices | `yfinance` — live index or futures tickers |
| Top headlines | NewsAPI (preferred) or yfinance fallback |
| Sector performance | `yfinance` — XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLB, XLRE, XLU, XLC |
| All narrative writing | Claude `claude-sonnet-4-6` via tool-use agentic loop |
| Monthly picks | `yfinance` fundamentals scan, cached in `picks_cache.json` |
| Deep dive topics | Claude chooses from 7 categories, rotated via `deep_dive_history` |

---

## Running locally

```bash
# Send one email right now
python openbell.py morning
python openbell.py close
python openbell.py deepdive   # skips automatically if market is open

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
