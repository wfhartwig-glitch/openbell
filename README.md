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

## How it works

| Section | Source |
|---|---|
| Futures snapshot | `yfinance` — ES=F, NQ=F, YM=F |
| Top headlines | NewsAPI `/top-headlines?category=business` |
| Economic events | MarketWatch calendar link (static note) |
| What to Watch | Claude `claude-sonnet-4-6` |
| Monthly picks | Claude `claude-sonnet-4-6`, cached in `picks_cache.json` |

**Monthly picks caching:** On the 1st of each month the script calls Claude
to generate 5 stock picks and saves them to `picks_cache.json` keyed by
`YYYY-MM`. Every other day it reads straight from the cache — no extra API
call. Delete the cache entry for the current month to force a refresh.

---

## Running as a background service (macOS)

Keep the scheduler alive after you close your terminal with `nohup`:

```bash
nohup python openbell.py > openbell.log 2>&1 &
echo $! > openbell.pid   # save PID to stop it later
```

To stop it:
```bash
kill $(cat openbell.pid)
```

Alternatively, add a launchd plist in `~/Library/LaunchAgents/` to have macOS
start it automatically at login — see Apple's launchd documentation.

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
