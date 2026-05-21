# Flight Deal Monitor ✈

Automated round-trip flight deal tracker: **IAD → Anywhere in the US** for ≤ $50/person.

Runs every 6 hours via GitHub Actions and emails you when a deal is found.

## Features

- Uses **Aviasales Data API** (free, no credit card)
- Searches 1–4 weeks and 1–3 months out
- Minimum 2-night stay enforced
- `[Weekend]` tag on Thu/Fri → Sun/Mon trips
- Deduplication — no repeat emails for the same deal within 24 hours
- Zero dependencies — pure Python stdlib
- `--test` flag to verify your email setup

## Setup

### 1. Get your Aviasales API token

1. Sign up at [travelpayouts.com](https://www.travelpayouts.com)
2. Go to **Developers → API** → copy your token

### 2. Get a Gmail App Password

1. Enable 2-Factor Authentication on your Google account
2. Go to **Google Account → Security → 2-Step Verification → App passwords**
3. Create a new app password → copy the 16-character code

### 3. Push to GitHub and add Secrets

```bash
cd ~/flight-monitor
git init
git add .
git commit -m "init"
gh repo create flight-monitor --private --source=. --push
```

Then go to your repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `AVIASALES_TOKEN` | From travelpayouts.com → Developers |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char app password from Google |
| `NOTIFY_EMAIL` | Email to receive alerts (can be same as above) |

### 4. Test it

```bash
# Test email locally
export AVIASALES_TOKEN=your_token
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASSWORD=xxxx_xxxx_xxxx_xxxx
export NOTIFY_EMAIL=you@gmail.com

python flight_monitor.py --test   # sends a dummy deal email
python flight_monitor.py          # runs a real search

# Or trigger manually on GitHub:
# Actions → Flight Deal Monitor → Run workflow
```

## How it works

1. Calls [Aviasales Data API](https://www.travelpayouts.com/developers/api) for latest cached prices from IAD
2. Filters for: US destinations, ≤ $50/pp, ≥ 2 nights, within 1-week to 3-month window
3. Tags weekend-friendly trips (`[Weekend]` in subject)
4. Sends one email per deal (batches if > 3 at once)
5. Commits a `seen_deals.json` cache so you don't get duplicate emails
