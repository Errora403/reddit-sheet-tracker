# reddit-sheet-tracker

A small, read-only analytics script that:
1) polls a subreddit for new posts and logs metadata into Google Sheets, and
2) snapshots each post's score and comment count once per day for 7 days.

## What data is stored
- post id, title, permalink, author, created_utc, inserted_utc
- initial score/comments (when first logged)
- day1..day7 score/comments snapshots

Optionally (disabled by default), it can store `selftext` (post body) truncated.

## Setup

### 1) Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
