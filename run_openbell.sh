#!/bin/bash
# Run the OpenBell briefing via Claude Code (uses your Max subscription tokens).

# Skip weekends
DAY=$(date +%u)
if [ "$DAY" -ge 6 ]; then
  echo "[OpenBell] Weekend — skipping."
  exit 0
fi

TODAY=$(date '+%A, %B %d, %Y')

cd "/Users/williamhartwig/Stock News" || exit 1

claude -p "You are OpenBell, a pre-market stock market briefing agent. Today is ${TODAY}. Run the following steps in order: 1) Call get_futures, get_headlines, get_economic_events, and get_market_movers. 2) Call get_monthly_picks. 3) Write a concise 3-5 sentence what_to_watch paragraph in plain English based on the data. 4) Call send_briefing_email with all the data and your what_to_watch paragraph. Confirm when the email has been sent."
