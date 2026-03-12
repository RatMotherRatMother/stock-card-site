#!/usr/bin/env python3
"""
refresh_cache.py
================================================================================
Daily cache pre-warmer for the Stock Tracker / Collection Flask app.

Run this script once per day (e.g. at 02:00 AM) to fetch fresh financial data
from yfinance for every symbol in the Google Sheet and write it to two JSON
cache files:

    data/tracker_cache.json    — used by the Tracker page (info dict only)
    data/collection_cache.json — used by the Collection page (info + all
                                  financial statements as DataFrames)

The Flask app reads these files on startup (and on each cache miss) so that
user page loads never have to wait for a live yfinance network call.

Usage
-----
    python refresh_cache.py [--dry-run]

    --dry-run   Print which symbols would be fetched but do not write files.

Scheduling
----------
See the SCHEDULING INSTRUCTIONS section at the bottom of this file.
================================================================================
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import gspread
import yfinance as yf
from oauth2client.service_account import ServiceAccountCredentials

# ---------------------------------------------------------------------------
# Config — must match app.py
# ---------------------------------------------------------------------------
SPREADSHEET_KEY = "1L5rFbJXp77MA_BaoX9wBwPszfg8QtY_ihyOaN6TSUpg"
JSON_KEYFILE    = "centered-being-489415-j5-d615d43fa816.json"

SCRIPT_DIR             = os.path.dirname(os.path.abspath(__file__))
DATA_DIR               = os.path.join(SCRIPT_DIR, "data")
TRACKER_CACHE_PATH     = os.path.join(DATA_DIR, "tracker_cache.json")
COLLECTION_CACHE_PATH  = os.path.join(DATA_DIR, "collection_cache.json")

# Seconds to pause between yfinance requests to avoid rate-limiting.
# Increase if you see HTTP 429 / Too Many Requests errors.
REQUEST_DELAY_SECONDS  = 2

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(JSON_KEYFILE, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_KEY).sheet1


def get_all_symbols(sheet) -> list[str]:
    """Return every non-empty symbol from the Google Sheet."""
    records = sheet.get_all_records()
    return [
        str(r.get("Symbol", "")).strip().upper()
        for r in records
        if str(r.get("Symbol", "")).strip() != ""
    ]


# ---------------------------------------------------------------------------
# DataFrame serialisation helpers
# ---------------------------------------------------------------------------

def _df_to_json(df) -> dict | None:
    """
    Serialise a pandas DataFrame to a plain dict using the 'split'
    orientation so it round-trips perfectly via pd.DataFrame(**d).

    Returns None if the DataFrame is None or empty.
    Column labels are converted to strings so JSON can handle
    Timestamp objects (yfinance date columns).
    """
    if df is None or df.empty:
        return None
    try:
        d = df.to_dict("split")
        # Convert Timestamp column keys → ISO strings
        d["columns"] = [
            c.isoformat() if hasattr(c, "isoformat") else str(c)
            for c in d["columns"]
        ]
        # Convert index values if needed (row names are usually strings)
        d["index"] = [str(i) for i in d["index"]]
        # Replace NaN with None so JSON serialisation works
        d["data"] = [
            [None if (v != v) else v for v in row]   # NaN != NaN
            for row in d["data"]
        ]
        return d
    except Exception as e:
        print(f"    [WARN] Could not serialise DataFrame: {e}")
        return None


# ---------------------------------------------------------------------------
# Per-symbol fetch
# ---------------------------------------------------------------------------

def fetch_symbol(symbol: str) -> dict | None:
    """
    Fetch all four yfinance data sources for one symbol.

    Returns a dict:
      {
        "info":          { ... },          # ticker.info (plain dict — JSON-safe)
        "financials":    { split-df },     # income statement
        "balance_sheet": { split-df },     # balance sheet
        "cashflow":      { split-df },     # cash flow statement
        "fetched_at":    "2025-01-15 02:00:00",
      }

    Returns None if the fetch fails entirely.
    """
    try:
        ticker = yf.Ticker(symbol)

        # ticker.info is already a plain dict — no conversion needed.
        # Sanitise any non-JSON-serialisable values (rare but possible).
        raw_info = ticker.info or {}
        info = {}
        for k, v in raw_info.items():
            try:
                json.dumps(v)          # test-serialise
                info[k] = v
            except (TypeError, ValueError):
                info[k] = str(v)       # coerce to string as fallback

        return {
            "info":          info,
            "financials":    _df_to_json(ticker.financials),
            "balance_sheet": _df_to_json(ticker.balance_sheet),
            "cashflow":      _df_to_json(ticker.cashflow),
            "fetched_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        print(f"    [ERROR] yfinance fetch failed for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: dict) -> None:
    """
    Write `data` as JSON to `path` atomically using a temp file.
    This prevents the Flask app from reading a half-written file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)   # atomic on POSIX; near-atomic on Windows


# ---------------------------------------------------------------------------
# Main refresh logic
# ---------------------------------------------------------------------------

def refresh(dry_run: bool = False) -> None:
    started = datetime.now()
    print(f"{'[DRY-RUN] ' if dry_run else ''}refresh_cache.py started at {started:%Y-%m-%d %H:%M:%S}")
    print(f"  Tracker cache    -> {TRACKER_CACHE_PATH}")
    print(f"  Collection cache -> {COLLECTION_CACHE_PATH}")
    print()

    # ── Step 1: get symbol list from Google Sheet ────────────────────
    print("Connecting to Google Sheets...")
    try:
        sheet   = _get_sheet()
        symbols = get_all_symbols(sheet)
    except Exception as e:
        print(f"[FATAL] Could not read Google Sheet: {e}")
        sys.exit(1)

    if not symbols:
        print("[WARN] No symbols found in the sheet. Nothing to do.")
        return

    print(f"Found {len(symbols)} symbol(s): {', '.join(symbols)}\n")

    if dry_run:
        print("[DRY-RUN] Would fetch the symbols above and write cache files.")
        print("[DRY-RUN] No network calls or file writes performed.")
        return

    # ── Step 2: fetch each symbol ────────────────────────────────────
    tracker_cache    = {}
    collection_cache = {}

    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] Fetching {symbol}...")
        result = fetch_symbol(symbol)

        if result is None:
            print(f"    Skipped - fetch failed.")
        else:
            # Tracker cache: info only (the tracker page only uses ticker.info)
            tracker_cache[symbol] = {
                "info":       result["info"],
                "fetched_at": result["fetched_at"],
            }
            # Collection cache: all four data sources
            collection_cache[symbol] = result
            print(f"    OK  Done  (fetched_at={result['fetched_at']})")

        # Polite delay between requests
        if i < len(symbols):
            time.sleep(REQUEST_DELAY_SECONDS)

    print()

    # ── Step 3: write JSON files ─────────────────────────────────────
    if tracker_cache:
        _atomic_write(TRACKER_CACHE_PATH, tracker_cache)
        print(f"OK  Wrote tracker_cache.json   ({len(tracker_cache)} symbols)")
    else:
        print("[WARN] tracker_cache is empty - file not written.")

    if collection_cache:
        _atomic_write(COLLECTION_CACHE_PATH, collection_cache)
        print(f"OK  Wrote collection_cache.json ({len(collection_cache)} symbols)")
    else:
        print("[WARN] collection_cache is empty - file not written.")

    elapsed = (datetime.now() - started).total_seconds()
    print(f"\nDone in {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pre-warm the JSON cache files for the Stock Tracker app."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be fetched without making any network calls or writing files.",
    )
    args = parser.parse_args()
    refresh(dry_run=args.dry_run)


# ==============================================================================
# SCHEDULING INSTRUCTIONS
# ==============================================================================
#
# Run this script daily at 02:00 AM so the cache is fresh before market open.
# Replace /path/to/project with your actual project directory.
#
# ── OPTION A: Linux / macOS — cron ────────────────────────────────────────────
#
#   1. Open your crontab:
#        crontab -e
#
#   2. Add this line (adjust paths):
#        0 2 * * * /path/to/project/venv/bin/python /path/to/project/refresh_cache.py >> /path/to/project/logs/refresh_cache.log 2>&1
#
#   3. Create the logs directory if it doesn't exist:
#        mkdir -p /path/to/project/logs
#
#   Tip: verify the cron is registered with:
#        crontab -l
#
# ── OPTION B: Windows — Task Scheduler ────────────────────────────────────────
#
#   1. Open Task Scheduler → Create Basic Task
#   2. Name: "StockTrackerCacheRefresh"
#   3. Trigger: Daily at 02:00 AM
#   4. Action: Start a program
#        Program/script:  C:\path\to\project\venv\Scripts\python.exe
#        Arguments:       C:\path\to\project\refresh_cache.py
#        Start in:        C:\path\to\project\
#   5. Finish → Right-click task → Properties → check
#      "Run whether user is logged on or not" for headless execution.
#
#   To log output, wrap in a .bat file:
#        @echo off
#        C:\path\to\project\venv\Scripts\python.exe C:\path\to\project\refresh_cache.py >> C:\path\to\project\logs\refresh_cache.log 2>&1
#   Then point Task Scheduler at the .bat file.
#
# ── OPTION C: Render.com — Cron Job service ────────────────────────────────────
#
#   Render does not run cron jobs on the same instance as your web service.
#   Use a dedicated Render Cron Job service:
#
#   1. In the Render dashboard → New → Cron Job
#   2. Name:          stock-cache-refresh
#   3. Environment:   Python
#   4. Build Command: pip install -r requirements.txt
#   5. Start Command: python refresh_cache.py
#   6. Schedule:      0 2 * * *   (02:00 UTC daily)
#   7. Instance type: Free (or Starter for reliability)
#
#   Important: both the web service and the cron job must share the same
#   persistent disk (Render Disk) mounted at /data, OR write the JSON files
#   to a shared location (e.g. a mounted volume, S3, or a database).
#
#   If using a Render Disk:
#   - Attach the disk to the web service at mount path /data
#   - Set DATA_DIR in both app.py and refresh_cache.py to /data
#   - The cron job writes to /data/tracker_cache.json etc.
#   - The web service reads from the same path.
#
#   If not using a Render Disk (ephemeral filesystem):
#   - Each deploy wipes the filesystem — JSON files written by the cron
#     job on a different instance will not be visible to the web service.
#   - In this case, use an external store: write JSON to an S3 bucket or
#     a small Postgres/SQLite DB instead of local files.
#
# ── Manual run (any platform) ─────────────────────────────────────────────────
#
#   python refresh_cache.py              # fetch and write
#   python refresh_cache.py --dry-run   # preview without writing
#
# ==============================================================================