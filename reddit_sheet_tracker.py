#!/usr/bin/env python3
"""
Track new posts from a subreddit into Google Sheets, then snapshot score/comments once per day for 7 days.

Recommended usage:
- Run `poll` every few minutes (cron) to capture new posts.
- Run `daily` once per day (cron) to record day-1..day-7 snapshots.

Secrets are read from environment variables (see .env.example).
"""

from __future__ import annotations

import os
import sys
import json
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

import praw
import gspread
from google.oauth2.service_account import Credentials

try:
    # Optional: makes local dev easier; ignored if not installed
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# ---------------------------
# Helpers
# ---------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def to_iso_z(dt: datetime) -> str:
    # Example: 2026-01-15T06:45:00Z
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso_z(s: str) -> datetime:
    # expects "YYYY-MM-DDTHH:MM:SSZ"
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return int(v)

def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

def safe_str(x: Any) -> str:
    return "" if x is None else str(x)

def shorten(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


# ---------------------------
# Config
# ---------------------------

SUBREDDIT = require_env("SUBREDDIT")

REDDIT_CLIENT_ID = require_env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = require_env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = require_env("REDDIT_USER_AGENT")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  # recommended
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")  # fallback
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Sheet1")

POST_FETCH_LIMIT = env_int("POST_FETCH_LIMIT", 50)

STORE_BODY = env_bool("STORE_BODY", default=False)
BODY_MAX_CHARS = env_int("BODY_MAX_CHARS", 800)

# How many days of daily snapshots
TRACK_DAYS = env_int("TRACK_DAYS", 7)

# ---------------------------
# Google Sheets
# ---------------------------

def make_gspread_client() -> gspread.Client:
    """
    Supports either:
    - GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/credentials.json (recommended for local dev), OR
    - GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}' (stringified JSON)
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    json_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

    if json_str:
        info = json.loads(json_str)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif json_file:
        creds = Credentials.from_service_account_file(json_file, scopes=scopes)
    else:
        raise RuntimeError(
            "Missing Google creds. Set GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    return gspread.authorize(creds)

def open_worksheet(gc: gspread.Client) -> gspread.Worksheet:
    if SPREADSHEET_ID:
        sh = gc.open_by_key(SPREADSHEET_ID)
    elif SPREADSHEET_NAME:
        sh = gc.open(SPREADSHEET_NAME)
    else:
        raise RuntimeError("Set SPREADSHEET_ID (preferred) or SPREADSHEET_NAME.")

    return sh.worksheet(WORKSHEET_NAME)

def ensure_header(ws: gspread.Worksheet) -> None:
    # If sheet is empty, add header row.
    values = ws.get_all_values()
    if values:
        return

    # Columns:
    # A Post ID
    # B Subreddit
    # C Title
    # D Author
    # E Permalink
    # F Created UTC
    # G Inserted UTC (when we first logged it)
    # H Is Self Post
    # I Body (optional)
    # J Initial Score
    # K Initial Comments
    # L.. (Day1 Score, Day1 Comments, ... Day7 Score, Day7 Comments)
    header = [
        "post_id",
        "subreddit",
        "title",
        "author",
        "permalink",
        "created_utc",
        "inserted_utc",
        "is_self",
        "body",
        "initial_score",
        "initial_comments",
    ]

    for d in range(1, TRACK_DAYS + 1):
        header += [f"day{d}_score", f"day{d}_comments"]

    header += ["last_checked_utc", "status"]  # status: active/done/removed/deleted/error
    ws.append_row(header)


# ---------------------------
# Reddit
# ---------------------------

def make_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )

def submission_permalink(sub) -> str:
    # Always store full URL
    return "https://www.reddit.com" + sub.permalink


# ---------------------------
# Sheet logic
# ---------------------------

def get_existing_post_ids(ws: gspread.Worksheet) -> set:
    # Reads whole first column; OK for moderate sheets.
    # If you expect huge sheets, switch to local state tracking.
    col = ws.col_values(1)  # includes header
    return set([x for x in col[1:] if x])

def append_post_row(ws: gspread.Worksheet, sub) -> None:
    inserted = utc_now()
    created = datetime.fromtimestamp(sub.created_utc, tz=timezone.utc)

    author = safe_str(sub.author)
    body = ""
    if STORE_BODY:
        body = shorten(safe_str(getattr(sub, "selftext", "")), BODY_MAX_CHARS)

    row = [
        sub.id,
        SUBREDDIT,
        sub.title,
        author,
        submission_permalink(sub),
        to_iso_z(created),
        to_iso_z(inserted),
        "TRUE" if sub.is_self else "FALSE",
        body,
        str(sub.score),
        str(sub.num_comments),
    ]

    # day1..dayN empty initially
    for _ in range(TRACK_DAYS):
        row += ["", ""]

    row += ["", "active"]

    ws.append_row(row, value_input_option="RAW")


def find_row_index_by_post_id(ws: gspread.Worksheet, post_id: str) -> Optional[int]:
    # Returns 1-based row index in sheet, or None
    try:
        cell = ws.find(post_id)
        # Ensure it's in column A (post_id)
        if cell.col == 1:
            return cell.row
        return None
    except Exception:
        return None

def read_row(ws: gspread.Worksheet, row_idx: int) -> List[str]:
    # Return full row (as list of strings)
    return ws.row_values(row_idx)

def update_cells(ws: gspread.Worksheet, row_idx: int, updates: Dict[int, str]) -> None:
    """
    updates: {col_index_1_based: value}
    """
    if not updates:
        return
    cells = []
    for col, val in updates.items():
        cells.append(gspread.Cell(row=row_idx, col=col, value=val))
    ws.update_cells(cells, value_input_option="RAW")


# ---------------------------
# Commands
# ---------------------------

def cmd_poll() -> None:
    """
    Fetch newest posts and append ones not already in the sheet.
    """
    reddit = make_reddit()
    gc = make_gspread_client()
    ws = open_worksheet(gc)
    ensure_header(ws)

    existing = get_existing_post_ids(ws)

    sr = reddit.subreddit(SUBREDDIT)
    new_posts = list(sr.new(limit=POST_FETCH_LIMIT))

    added = 0
    for sub in new_posts:
        if sub.id in existing:
            continue
        append_post_row(ws, sub)
        added += 1

    print(f"[poll] Added {added} new post(s).")

def cmd_daily() -> None:
    """
    Once per day: for each active post, write score/comments into the correct day slot (1..TRACK_DAYS).
    Marks done after day TRACK_DAYS is filled.
    """
    reddit = make_reddit()
    gc = make_gspread_client()
    ws = open_worksheet(gc)
    ensure_header(ws)

    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        print("[daily] No rows yet.")
        return

    header = all_values[0]
    rows = all_values[1:]  # excludes header
    today = date.today()  # local machine date; script stores UTC timestamps though

    # Map header names to columns (1-based)
    col_index = {name: i + 1 for i, name in enumerate(header)}

    inserted_col = col_index["inserted_utc"]
    status_col = col_index["status"]
    last_checked_col = col_index["last_checked_utc"]

    updated_count = 0
    done_count = 0

    for offset, row in enumerate(rows):
        row_idx = offset + 2  # because header is row 1
        status = (row[status_col - 1] if len(row) >= status_col else "").strip().lower()
        if status not in {"active", ""}:
            continue

        post_id = row[col_index["post_id"] - 1]
        inserted_str = row[inserted_col - 1] if len(row) >= inserted_col else ""
        if not inserted_str:
            continue

        inserted_dt = parse_iso_z(inserted_str)
        # Determine which day slot to fill:
        # Day 1 is the first daily run after insertion date; we use calendar day difference.
        days_since = (today - inserted_dt.date()).days
        day_index = days_since  # 0 means same calendar date as insertion
        # We only record day1..dayN, so require at least 1 day since insertion date
        slot = day_index
        if slot < 1:
            # too soon; skip until next day
            continue
        if slot > TRACK_DAYS:
            # past tracking window; mark done if not already
            update_cells(ws, row_idx, {
                status_col: "done",
                last_checked_col: to_iso_z(utc_now()),
            })
            done_count += 1
            continue

        # Determine target columns
        score_col_name = f"day{slot}_score"
        comm_col_name = f"day{slot}_comments"
        if score_col_name not in col_index or comm_col_name not in col_index:
            continue

        score_col = col_index[score_col_name]
        comm_col = col_index[comm_col_name]

        # If already filled, don't overwrite
        existing_score = row[score_col - 1] if len(row) >= score_col else ""
        existing_comm = row[comm_col - 1] if len(row) >= comm_col else ""
        if (existing_score or "").strip() and (existing_comm or "").strip():
            continue

        try:
            subm = reddit.submission(id=post_id)
            score = str(subm.score)
            comms = str(subm.num_comments)

            # If removed/deleted, PRAW often still returns numbers; you can optionally detect:
            # removed_by_category can exist, but is not always present.
            updates = {
                score_col: score,
                comm_col: comms,
                last_checked_col: to_iso_z(utc_now()),
                status_col: "active",
            }
            update_cells(ws, row_idx, updates)
            updated_count += 1

        except Exception as e:
            update_cells(ws, row_idx, {
                last_checked_col: to_iso_z(utc_now()),
                status_col: f"error: {type(e).__name__}",
            })

    print(f"[daily] Updated {updated_count} post(s); marked done {done_count} post(s).")

def cmd_init_sheet() -> None:
    gc = make_gspread_client()
    ws = open_worksheet(gc)
    ensure_header(ws)
    print("[init-sheet] Header ensured.")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python reddit_sheet_tracker.py [init-sheet|poll|daily]")
        sys.exit(1)

    cmd = sys.argv[1].strip().lower()
    if cmd == "init-sheet":
        cmd_init_sheet()
    elif cmd == "poll":
        cmd_poll()
    elif cmd == "daily":
        cmd_daily()
    else:
        print("Unknown command:", cmd)
        sys.exit(1)

if __name__ == "__main__":
    main()
