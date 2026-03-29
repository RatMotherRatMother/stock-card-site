from flask import Flask, render_template, request, redirect, url_for, jsonify
import yfinance as yf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time
import json
import os
import pandas as pd
from datetime import datetime

app = Flask(__name__)

# ===========================================================================
# JSON-FILE-BACKED CACHE
# ===========================================================================
#
# Overview
# --------
# Rather than auto-refreshing from yfinance every 10 minutes on user
# requests, the app now reads pre-built JSON files that are written once
# per day by refresh_cache.py (scheduled externally via cron / Task
# Scheduler).  This eliminates per-request yfinance latency for all
# symbols that were pre-warmed.
#
# File layout
# -----------
#   data/tracker_cache.json    — { "AAPL": { "info": {...}, "fetched_at": "..." }, ... }
#   data/collection_cache.json — { "AAPL": { "info": {...},
#                                             "financials":    { split-orient DataFrame },
#                                             "balance_sheet": { split-orient DataFrame },
#                                             "cashflow":      { split-orient DataFrame },
#                                             "fetched_at": "..." }, ... }
#
# DataFrames are stored in pandas "split" orientation so they round-trip
# perfectly:  df → df.to_dict("split") → pd.DataFrame(**d)
#
# Lookup order for get_cached_ticker(symbol)
# ------------------------------------------
#   1. In-memory _ticker_cache  (process lifetime, avoids repeat disk reads)
#   2. data/collection_cache.json  (written by refresh_cache.py)
#   3. data/tracker_cache.json     (written by refresh_cache.py; info-only)
#   4. Live yfinance fetch          (fallback for symbols not in any JSON file)
#
# To switch to Redis or a database later, only this section and
# get_cached_ticker() need to change — all consumers are untouched.
# ===========================================================================

# Paths to the two JSON cache files
# If the CACHE_DIR env var is set (e.g. "/data" on Render with a
# persistent disk), use that. Otherwise fall back to the local
# "data/" folder so development works without any changes.
DATA_DIR               = os.environ.get(
    "CACHE_DIR",
    os.path.join(os.path.dirname(__file__), "data")
)
TRACKER_CACHE_PATH     = os.path.join(DATA_DIR, "tracker_cache.json")
COLLECTION_CACHE_PATH  = os.path.join(DATA_DIR, "collection_cache.json")

# In-process memory cache — populated lazily from JSON or yfinance.
# No TTL: entries live for the duration of the server process.
# refresh_cache.py is the sole mechanism for updating stale data.
_ticker_cache: dict = {}

# ── JSON file helpers ────────────────────────────────────────────────────────

def _load_json_cache(path: str) -> dict:
    """Read a JSON cache file and return its contents as a dict.
    Returns {} if the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _df_from_json(d) -> pd.DataFrame | None:
    """
    Reconstruct a pandas DataFrame from a dict serialised with
    df.to_dict('split') by refresh_cache.py.

    refresh_cache.py converts Timestamp column labels to ISO strings
    (e.g. "2024-03-31T00:00:00") so they survive JSON serialisation.
    This function converts them back to pd.Timestamp objects before
    building the DataFrame, so that fetch_historical_metrics() can
    look up columns the same way it does with live yfinance data.

    Without this conversion the column lookup
        fin_years = {str(col.year): col for col in fin.columns}
    produces string keys that never match the Timestamp objects that
    yfinance normally returns, causing KeyErrors that Flask catches
    and returns as an HTML 500 page — which the browser sees instead
    of JSON, producing the "Unexpected token '<'" fetch error.

    Returns None if the input is None or malformed.
    """
    if d is None:
        return None
    try:
        # Work on a shallow copy so we don't mutate the cached dict
        d = dict(d)
        # Convert ISO-string column labels back to pd.Timestamp,
        # reversing what refresh_cache._df_to_json() serialised them as.
        converted = []
        for col in d.get("columns", []):
            try:
                converted.append(pd.Timestamp(col))
            except Exception:
                converted.append(col)   # leave as-is if not a date string
        d["columns"] = converted
        return pd.DataFrame(**d)
    except Exception:
        return None


def _load_symbol_from_json(symbol: str) -> dict | None:
    """
    Try to find `symbol` in the JSON cache files.

    Prefers collection_cache.json (has all four data sources).
    Falls back to tracker_cache.json (info-only).

    Returns a dict with keys: info, financials, balance_sheet, cashflow
    — or None if the symbol is not found in either file.
    """
    # 1. Try the richer collection cache first
    col_cache = _load_json_cache(COLLECTION_CACHE_PATH)
    if symbol in col_cache:
        entry = col_cache[symbol]
        print(f"[CACHE] JSON-HIT (collection) for {symbol}"
              f"  fetched_at={entry.get('fetched_at', '?')}")
        return {
            "info":          entry.get("info", {}),
            "financials":    _df_from_json(entry.get("financials")),
            "balance_sheet": _df_from_json(entry.get("balance_sheet")),
            "cashflow":      _df_from_json(entry.get("cashflow")),
        }

    # 2. Fall back to the tracker cache (info only — DataFrames will be None)
    trk_cache = _load_json_cache(TRACKER_CACHE_PATH)
    if symbol in trk_cache:
        entry = trk_cache[symbol]
        print(f"[CACHE] JSON-HIT (tracker) for {symbol}"
              f"  fetched_at={entry.get('fetched_at', '?')}")
        return {
            "info":          entry.get("info", {}),
            "financials":    None,
            "balance_sheet": None,
            "cashflow":      None,
        }

    return None  # symbol not in any JSON file


# ── _CachedTicker wrapper ────────────────────────────────────────────────────

class _CachedTicker:
    """
    Thin wrapper exposing the same attributes as yf.Ticker
    (info, financials, balance_sheet, cashflow) regardless of whether
    the data came from the JSON cache or a live yfinance fetch.
    """
    def __init__(self, symbol: str):
        self.symbol        = symbol
        entry              = _ticker_cache[symbol]
        self.info          = entry["info"]
        self.financials    = entry["financials"]
        self.balance_sheet = entry["balance_sheet"]
        self.cashflow      = entry["cashflow"]


# ── Bulk JSON pre-load ───────────────────────────────────────────────────────

def warm_ticker_cache_from_json() -> int:
    """Load every symbol from the JSON cache files into _ticker_cache in one
    pass at startup.  Avoids reading the whole JSON file N times (once per
    symbol) on the first page load after a cold server start."""
    loaded = 0
    col_cache = _load_json_cache(COLLECTION_CACHE_PATH)
    for symbol, entry in col_cache.items():
        if symbol in _ticker_cache:
            continue
        _ticker_cache[symbol] = {
            "info":          entry.get("info", {}),
            "financials":    _df_from_json(entry.get("financials")),
            "balance_sheet": _df_from_json(entry.get("balance_sheet")),
            "cashflow":      _df_from_json(entry.get("cashflow")),
        }
        loaded += 1
    trk_cache = _load_json_cache(TRACKER_CACHE_PATH)
    for symbol, entry in trk_cache.items():
        if symbol in _ticker_cache:
            continue
        _ticker_cache[symbol] = {
            "info":          entry.get("info", {}),
            "financials":    None,
            "balance_sheet": None,
            "cashflow":      None,
        }
        loaded += 1
    if loaded:
        print(f"[CACHE] warm_ticker_cache_from_json: loaded {loaded} symbols")
    return loaded


# ── Main public accessor ─────────────────────────────────────────────────────

def get_cached_ticker(symbol: str) -> _CachedTicker:
    """
    Return a _CachedTicker for `symbol` using a three-level lookup:

      Level 1 — in-memory _ticker_cache
        Populated during this server process; avoids repeated disk reads.

      Level 2 — JSON cache files  (data/collection_cache.json first,
                                    then data/tracker_cache.json)
        Written once per day by refresh_cache.py.  If found, the entry
        is promoted into _ticker_cache so subsequent requests skip disk.

      Level 3 — live yfinance fetch
        Only reached if the symbol is absent from both JSON files.
        Result is stored in _ticker_cache for the remainder of this
        process lifetime (no TTL expiry on the in-memory layer).

    Callers should wrap in try/except — yfinance can raise on bad symbols.
    """
    # Level 1: in-memory hit
    if symbol in _ticker_cache:
        print(f"[CACHE] MEM-HIT for {symbol}")
        return _CachedTicker(symbol)

    # Level 2: JSON file hit
    from_json = _load_symbol_from_json(symbol)
    if from_json is not None:
        _ticker_cache[symbol] = from_json          # promote to memory
        return _CachedTicker(symbol)

    # Level 3: live yfinance fetch (symbol not pre-warmed)
    print(f"[CACHE] MISS for {symbol} — fetching live from yfinance")
    ticker = yf.Ticker(symbol)
    _ticker_cache[symbol] = {
        "info":          ticker.info,
        "financials":    ticker.financials,
        "balance_sheet": ticker.balance_sheet,
        "cashflow":      ticker.cashflow,
    }
    return _CachedTicker(symbol)


# ===========================================================================
# CACHE MANAGEMENT HELPERS
# ===========================================================================

def invalidate_cache(symbol: str) -> None:
    """Remove a symbol from the in-memory cache.
    The next request will re-read from JSON (or yfinance if not in JSON)."""
    _ticker_cache.pop(symbol, None)


def cache_stats() -> dict:
    """
    Return a summary of the current in-memory cache state and the
    modification times of the two JSON cache files.
    """
    def _file_mtime(path):
        try:
            ts = os.path.getmtime(path)
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            return "missing"

    def _file_symbols(path):
        return list(_load_json_cache(path).keys())

    return {
        "memory": {sym: {"loaded": True} for sym in _ticker_cache},
        "json_files": {
            "tracker_cache":    {
                "path":         TRACKER_CACHE_PATH,
                "last_written": _file_mtime(TRACKER_CACHE_PATH),
                "symbols":      _file_symbols(TRACKER_CACHE_PATH),
            },
            "collection_cache": {
                "path":         COLLECTION_CACHE_PATH,
                "last_written": _file_mtime(COLLECTION_CACHE_PATH),
                "symbols":      _file_symbols(COLLECTION_CACHE_PATH),
            },
        },
    }


# --------------------------
# Google Sheets Setup
# --------------------------
SPREADSHEET_KEY = "1L5rFbJXp77MA_BaoX9wBwPszfg8QtY_ihyOaN6TSUpg"

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# Load credentials from environment variable (production / Render)
# or fall back to a local JSON file that is gitignored (local dev only).
_creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
if _creds_b64:
    import base64, json as _json
    _creds_dict = _json.loads(base64.b64decode(_creds_b64.strip()).decode("utf-8"))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(_creds_dict, scope)
else:
    # Local development fallback — file must exist locally but is never committed
    _local_keyfile = os.environ.get(
        "GOOGLE_KEYFILE", "centered-being-489415-j5-d615d43fa816.json"
    )
    creds = ServiceAccountCredentials.from_json_keyfile_name(_local_keyfile, scope)

client = gspread.authorize(creds)
sheet                  = client.open_by_key(SPREADSHEET_KEY).sheet1
sheet_industries       = client.open_by_key(SPREADSHEET_KEY).worksheet("industries")
sheet_strategic_groups = client.open_by_key(SPREADSHEET_KEY).worksheet("strategic_groups")

# --------------------------
# Formatting Helpers
# --------------------------
def format_large_number(val):
    if val is None:
        return "N/A"
    try:
        return f"{int(val):,}"
    except (ValueError, TypeError):
        return "N/A"

def df_value(df, *row_names):
    if df is None or df.empty:
        return None
    for name in row_names:
        if name in df.index:
            try:
                val = df.loc[name].iloc[0]
                if val is not None and str(val) != "nan":
                    return float(val)
            except (IndexError, ValueError, TypeError):
                continue
    return None

# --------------------------
# Google Sheets Helpers
# --------------------------
def get_col_index(header_name):
    headers = sheet.row_values(1)
    try:
        return headers.index(header_name) + 1
    except ValueError:
        print(f"[ERROR] Header '{header_name}' not found. Available: {headers}")
        return None

def update_sheet_cell(symbol, header_name, value):
    col = get_col_index(header_name)
    if col is None:
        return
    try:
        cell = sheet.find(symbol)
        if cell:
            sheet.update_cell(cell.row, col, value)
        else:
            print(f"[ERROR] Symbol '{symbol}' not found in sheet.")
    except Exception as e:
        print(f"[ERROR] update_sheet_cell({symbol!r}, {header_name!r}): {e}")

def update_stage(symbol, new_stage):
    update_sheet_cell(symbol, "Stage", new_stage)

def update_alert_price(symbol, new_price):
    update_sheet_cell(symbol, "Price Alert", new_price)

# --------------------------
# Shared Sheet Reader  (TTL-cached)
# --------------------------
_rows_cache: list        = []
_rows_cache_time: float  = 0.0
ROWS_CACHE_TTL: int      = 60     # seconds before re-fetching from Sheets

def get_all_rows() -> list:
    """Return all non-empty rows from the sheet, cached for ROWS_CACHE_TTL
    seconds so the Sheets API is called at most once per minute."""
    global _rows_cache, _rows_cache_time
    import time as _time
    now = _time.monotonic()
    if _rows_cache and (now - _rows_cache_time) < ROWS_CACHE_TTL:
        return _rows_cache
    _rows_cache = [
        r for r in sheet.get_all_records()
        if str(r.get("Symbol", "")).strip() != ""
    ]
    _rows_cache_time = now
    return _rows_cache

def invalidate_rows_cache() -> None:
    """Force the next get_all_rows() call to re-fetch from Google Sheets.
    Call after any write that changes the sheet (stage move, alert update)."""
    global _rows_cache_time
    _rows_cache_time = 0.0

# --------------------------
# Industries Sheet Reader  (TTL-cached)
# --------------------------
_industries_cache: list       = []
_industries_cache_time: float = 0.0

def get_all_industries() -> list:
    """Return all non-empty rows from the industries worksheet, cached for
    ROWS_CACHE_TTL seconds. Each row is a dict with keys matching the
    sheet column headers: industry, image_url, supplier_power, buyer_power,
    new_entrants, substitutes, rivalry."""
    global _industries_cache, _industries_cache_time
    import time as _time
    now = _time.monotonic()
    if _industries_cache and (now - _industries_cache_time) < ROWS_CACHE_TTL:
        return _industries_cache
    _industries_cache = [
        r for r in sheet_industries.get_all_records()
        if str(r.get("industry", "")).strip() != ""
    ]
    _industries_cache_time = now
    return _industries_cache

def invalidate_industries_cache() -> None:
    """Force the next get_all_industries() call to re-fetch from Google Sheets.
    Call after any write that updates an industry rating."""
    global _industries_cache_time
    _industries_cache_time = 0.0

# --------------------------
# Strategic Groups Sheet Reader  (TTL-cached)
# --------------------------
_groups_cache: list       = []
_groups_cache_time: float = 0.0

def get_all_strategic_groups() -> list:
    """Return all non-empty rows from the strategic_groups worksheet, cached
    for ROWS_CACHE_TTL seconds. Each row is a dict with keys: group,
    parent_industry, image_url, customer_overlap, differentiation,
    number_and_size, strategy_overlap."""
    global _groups_cache, _groups_cache_time
    import time as _time
    now = _time.monotonic()
    if _groups_cache and (now - _groups_cache_time) < ROWS_CACHE_TTL:
        return _groups_cache
    _groups_cache = [
        r for r in sheet_strategic_groups.get_all_records()
        if str(r.get("group", "")).strip() != ""
    ]
    _groups_cache_time = now
    return _groups_cache

def invalidate_groups_cache() -> None:
    """Force the next get_all_strategic_groups() call to re-fetch.
    Call after any write that updates a strategic group rating."""
    global _groups_cache_time
    _groups_cache_time = 0.0

# --------------------------
# Analysis JSON helpers  (/data/analysis.json)
# --------------------------
ANALYSIS_PATH = os.path.join(DATA_DIR, "analysis.json")

def _load_analysis() -> dict:
    """Load analysis data from disk. Returns {} if file missing or unreadable.
    The file is created automatically on the first save — no manual setup needed."""
    try:
        with open(ANALYSIS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_analysis(data: dict) -> None:
    """Atomically write analysis dict to disk.
    Creates /data/ directory if it does not exist (e.g. first run locally)."""
    os.makedirs(os.path.dirname(ANALYSIS_PATH), exist_ok=True)
    tmp = ANALYSIS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ANALYSIS_PATH)


# --------------------------
# Main Watchlist Builder (Tracker page)
# --------------------------
def get_watchlist():
    rows = get_all_rows()
    stocks = []

    for row in rows:
        symbol          = str(row.get("Symbol", "")).strip()
        stage           = str(row.get("Stage",  "")).strip()
        alert_price_raw = row.get("Price Alert")

        alert_price = None
        if alert_price_raw not in (None, ""):
            try:
                alert_price = float(alert_price_raw)
            except (ValueError, TypeError):
                alert_price = None

        raw_price = raw_market_cap = pe_ratio = None
        current_assets = total_debt = total_liab = None
        debt_ratio = assets_to_liabilities = roic = None
        gross_profit = revenue = sg_expense = gross_margin = sg_percent = None
        gross_margin_display = sg_display = "N/A"
        earnings_yield = raw_earnings_yield = None
        price = None
        color = "black"

        # Block 1: price / market cap / PE
        # Uses the cache — no direct yf.Ticker() call here.
        try:
            ticker         = get_cached_ticker(symbol)
            info           = ticker.info
            raw_price      = info.get("currentPrice")
            raw_market_cap = info.get("marketCap")
            pe_ratio       = info.get("trailingPE")
            price = f"${raw_price:,.2f}" if raw_price is not None else None
        except Exception as e:
            print(f"[ERROR] ticker.info for {symbol}: {e}")

        # Color: frozen — do not modify
        if raw_price is not None and alert_price is not None and alert_price > 0:
            ratio = float(raw_price) / float(alert_price)
            if ratio <= 0.80:
                color = "green"
            elif ratio <= 1.10:
                color = "orange"
            else:
                color = "red"

        # Block 2: balance sheet
        try:
            bs = ticker.balance_sheet
            current_assets = df_value(bs, "Current Assets", "CurrentAssets",
                                          "Total Current Assets")
            total_debt     = df_value(bs, "Total Debt", "TotalDebt",
                                          "Long Term Debt And Capital Lease Obligation")
            total_liab     = df_value(bs, "Total Liabilities Net Minority Interest",
                                          "Total Liabilities", "TotalLiab")
            debt_ratio            = round(current_assets / total_debt, 2) \
                                    if current_assets and total_debt else None
            assets_to_liabilities = round(current_assets / total_liab, 2) \
                                    if current_assets and total_liab else None
        except Exception as e:
            print(f"[ERROR] balance_sheet for {symbol}: {e}")

        # Block 3: ROIC
        try:
            fin = ticker.financials
            bs  = ticker.balance_sheet
            op_income        = df_value(fin, "Operating Income", "EBIT")
            pretax_inc       = df_value(fin, "Pretax Income", "Income Before Tax",
                                            "Pre Tax Income")
            tax_expense      = df_value(fin, "Tax Provision", "Income Tax Expense",
                                            "Provision For Tax")
            invested_capital = df_value(bs, "Invested Capital")
            if all(v is not None for v in
                   [op_income, pretax_inc, tax_expense, invested_capital]):
                tax_rate = (tax_expense / pretax_inc) if pretax_inc != 0 else 0
                nopat    = op_income * (1 - tax_rate)
                if invested_capital != 0:
                    roic = f"{(nopat / invested_capital) * 100:.2f}%"
        except Exception as e:
            print(f"[ERROR] ROIC for {symbol}: {e}")

        # Block 4: Gross Margin & SG%
        try:
            fin = ticker.financials
            gross_profit = df_value(fin, "Gross Profit", "GrossProfit")
            revenue      = df_value(fin, "Total Revenue", "Revenue", "Sales")
            if gross_profit is not None and revenue not in (0, None):
                gross_margin = round(gross_profit / revenue, 4)
                gross_margin_display = f"{gross_margin * 100:.2f}%"
            sg_expense = df_value(
                fin,
                "Selling General And Administration",
                "Selling General Administrative",
                "Selling General And Administrative Expense",
                "Selling General and Administrative",
                "Selling General & Administrative",
                "SG&A Expense",
                "SGA",
            )
            if sg_expense is not None and gross_profit not in (0, None):
                sg_percent = round(sg_expense / gross_profit, 4)
                sg_display = f"{sg_percent * 100:.2f}%"
        except Exception as e:
            print(f"[ERROR] Gross Margin / SG% for {symbol}: {e}")

        # Block 5: Earnings Yield
        try:
            fin = ticker.financials
            pretax_inc_yield = df_value(fin, "Pretax Income", "Income Before Tax",
                                            "Pre Tax Income")
            diluted_shares   = df_value(fin, "Diluted Average Shares",
                                            "Average Dilution Earnings",
                                            "Diluted Shares")
            if (pretax_inc_yield is not None
                    and diluted_shares not in (None, 0)
                    and raw_price not in (None, 0)):
                eps_pretax         = pretax_inc_yield / diluted_shares
                raw_earnings_yield = eps_pretax / raw_price
                earnings_yield     = f"{raw_earnings_yield * 100:.2f}%"
        except Exception as e:
            print(f"[ERROR] Earnings Yield for {symbol}: {e}")

        stocks.append({
            "symbol":                symbol,
            "price":                 price,
            "market_cap":            format_large_number(raw_market_cap),
            "pe_ratio":              round(pe_ratio, 2) if pe_ratio else "N/A",
            "current_assets":        format_large_number(current_assets),
            "total_debt":            format_large_number(total_debt),
            "total_liabilities":     format_large_number(total_liab),
            "debt_ratio":            debt_ratio            if debt_ratio            is not None else "N/A",
            "assets_to_liabilities": assets_to_liabilities if assets_to_liabilities is not None else "N/A",
            "roic":                  roic                  if roic                  is not None else "N/A",
            "gross_margin":          gross_margin_display,
            "sg_percent":            sg_display,
            "earnings_yield":        earnings_yield        if earnings_yield         is not None else "N/A",
            "stage":                 stage,
            "alert_price":           alert_price,
            "color":                 color,
            "_raw_market_cap":            raw_market_cap,
            "_raw_pe_ratio":              pe_ratio,
            "_raw_current_assets":        current_assets,
            "_raw_total_debt":            total_debt,
            "_raw_total_liabilities":     total_liab,
            "_raw_debt_ratio":            debt_ratio,
            "_raw_assets_to_liabilities": assets_to_liabilities,
            "_raw_gross_margin":          gross_margin,
            "_raw_sg_percent":            sg_percent,
            "_raw_earnings_yield":        raw_earnings_yield,
        })

    return stocks


# ===========================================================================
# TRACKER ROUTES
# ===========================================================================

SORT_KEY_MAP = {
    "market_cap":            "_raw_market_cap",
    "pe_ratio":              "_raw_pe_ratio",
    "current_assets":        "_raw_current_assets",
    "total_debt":            "_raw_total_debt",
    "total_liabilities":     "_raw_total_liabilities",
    "debt_ratio":            "_raw_debt_ratio",
    "assets_to_liabilities": "_raw_assets_to_liabilities",
    "gross_margin":          "_raw_gross_margin",
    "sg_percent":            "_raw_sg_percent",
    "earnings_yield":        "_raw_earnings_yield",
}

# Pre-load all JSON-cached symbols into memory at startup so the first
# tracker/collection page load is fast even on a cold server start.
warm_ticker_cache_from_json()


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/tracker", methods=["GET", "POST"])
def tracker():
    if request.method == "POST":
        symbol      = request.form.get("symbol", "").strip()
        new_stage   = request.form.get("stage", "").strip()
        alert_price = request.form.get("alert_price", "").strip()
        if symbol and new_stage:
            update_stage(symbol, new_stage)
        if symbol and alert_price != "":
            update_alert_price(symbol, alert_price)
        invalidate_rows_cache()   # force fresh Sheets read on next load
        return redirect(url_for("tracker"))

    selected_stage = request.args.get("stage", "")
    sort_by        = request.args.get("sort_by", "")
    sort_order     = request.args.get("sort_order", "asc")

    all_stocks = get_watchlist()

    # Count stocks per stage across the full list (used by the deck filter strip)
    stage_counts = {"": len(all_stocks)}
    for s in all_stocks:
        key = s.get("stage", "")
        stage_counts[key] = stage_counts.get(key, 0) + 1

    stocks = all_stocks
    if selected_stage:
        stocks = [s for s in stocks if s["stage"] == selected_stage]

    if sort_by and sort_by in SORT_KEY_MAP:
        raw_key = SORT_KEY_MAP[sort_by]
        reverse = sort_order == "desc"
        def sort_key(s):
            val = s.get(raw_key)
            if val is None:
                return (1, 0)
            return (0, float(val))
        stocks.sort(key=sort_key, reverse=reverse)

    return render_template(
        "tracker.html",
        stocks=stocks,
        selected_stage=selected_stage,
        sort_by=sort_by,
        sort_order=sort_order,
        stage_counts=stage_counts,
    )


# ===========================================================================
# COLLECTION ROUTES
# ===========================================================================

# All valid stages — update this list if you add new stages
STAGES = ["graveyard", "0.5", "1", "2", "3"]

@app.route("/collection")
def collection():
    return render_template("collection.html", stages=STAGES)


@app.route("/api/symbols")
def api_symbols():
    stage = request.args.get("stage", "").strip()
    rows  = get_all_rows()
    symbols = [
        str(r.get("Symbol", "")).strip()
        for r in rows
        if str(r.get("Stage", "")).strip() == stage
        and str(r.get("Symbol", "")).strip() != ""
    ]
    return jsonify({"stage": stage, "symbols": symbols})


# ---------------------------------------------------------------------------
# yfinance — historical metrics for the Collection page left table
# ---------------------------------------------------------------------------

def fetch_historical_metrics(symbol, years=4):
    """
    Fetch up to `years` years of annual financial data via yfinance and
    calculate historical metrics for the left table on the Collection page.

    Internally fetches years+1 columns so that the oldest displayed year
    always has a prior-year value available for YoY % change calculations.

    Returns:
    {
        "years":  ["2024", "2023", "2022", "2021"],   # newest first, length = years
        "ratios": [
            {"label": "D/E (Liabilities / Equity)", "values": ["2.34", ...]},
            ...
        ],
        "cagr": [
            {"label": "Revenue CAGR (4yr)", "value": "8.23%"},
            ...
        ]
    }

    ── Adding a new per-year metric ────────────────────────────────────────
    1. Extract the raw field in the "Pre-extract" block using get().
    2. Append one dict to `metric_definitions`.

    ── Adding a new CAGR metric ────────────────────────────────────────────
    1. The raw parallel list already exists (or add it in Pre-extract).
    2. Append one dict to `cagr_definitions`.
    ─────────────────────────────────────────────────────────────────────────
    """
    # Uses the JSON-backed cache — avoids a live yfinance fetch if
    # refresh_cache.py has already pre-warmed this symbol today.
    try:
        ticker = get_cached_ticker(symbol)
        fin    = ticker.financials      # income statement — columns = dates newest→oldest
        bs     = ticker.balance_sheet   # balance sheet    — columns = dates newest→oldest
        cf     = ticker.cashflow        # cash flow statement
    except Exception as e:
        print(f"[ERROR] yfinance fetch for {symbol}: {e}")
        return None

    if fin is None or fin.empty or bs is None or bs.empty:
        print(f"[WARN] Empty financials/balance sheet for {symbol}")
        return None

    # Align columns by fiscal year ─────────────────────────────────────
    # Fetch years+1 so YoY change for the oldest displayed year is computable.
    # The extra (oldest) year is used only for prior-year comparisons and CAGR
    # — it is NOT included in the "years" list sent to the frontend.
    fin_years = {str(col.year): col for col in fin.columns}
    bs_years  = {str(col.year): col for col in bs.columns}
    cf_years  = {str(col.year): col for col in cf.columns} if cf is not None and not cf.empty else {}
    all_common = sorted([y for y in fin_years if y in bs_years], reverse=True)[:years + 1]

    if not all_common:
        print(f"[WARN] No overlapping fiscal years for {symbol}")
        return None

    # Display years = most recent `years` entries; prior_year = last entry
    display_years = all_common[:years]
    n             = len(display_years)
    has_prior     = len(all_common) > n   # True when a prior year is available

    fin_cols_all = [fin_years[y] for y in all_common]
    bs_cols_all  = [bs_years[y]  for y in all_common]
    fin_cols     = fin_cols_all[:n]       # display years only
    bs_cols      = bs_cols_all[:n]

    # ── Safe field extractor ──────────────────────────────────────────────
    def get(df, col, *row_names):
        """
        Return float for the first matching row name in df at column col.
        Returns None if no row matches or the value is NaN/None.
        """
        for name in row_names:
            if name in df.index:
                try:
                    val = df.loc[name, col]
                    if val is not None and str(val) not in ("nan", "None", ""):
                        return float(val)
                except Exception:
                    pass
        return None

    # ── Pre-extract raw parallel lists ───────────────────────────────────
    # Each list has n entries (display years).
    # _all variants have n+1 entries (display + prior year) for YoY/CAGR.

    # Income statement — display years
    revenue      = [get(fin, fin_cols[i], "Total Revenue", "TotalRevenue")                    for i in range(n)]
    gross_profit = [get(fin, fin_cols[i], "Gross Profit", "GrossProfit")                      for i in range(n)]
    op_income    = [get(fin, fin_cols[i], "Operating Income", "EBIT", "OperatingIncome")       for i in range(n)]
    net_income   = [get(fin, fin_cols[i], "Net Income", "NetIncome")                           for i in range(n)]
    pretax_inc   = [get(fin, fin_cols[i], "Pretax Income", "Income Before Tax",
                                          "Pre Tax Income", "PreTaxIncome")                    for i in range(n)]
    tax_expense  = [get(fin, fin_cols[i], "Tax Provision", "Income Tax Expense",
                                          "Provision For Tax", "TaxProvision")                 for i in range(n)]
    sga          = [get(fin, fin_cols[i], "Selling General And Administration",
                                          "Selling General Administrative",
                                          "Selling General And Administrative Expense",
                                          "Selling General and Administrative",
                                          "Selling General & Administrative",
                                          "SG&A Expense", "SGA")                              for i in range(n)]
    diluted_shares = [get(fin, fin_cols[i], "Diluted Average Shares",
                                            "Average Dilution Earnings", "Diluted Shares")     for i in range(n)]

    # Balance sheet — display years
    total_assets    = [get(bs, bs_cols[i], "Total Assets", "TotalAssets")                     for i in range(n)]
    total_liab      = [get(bs, bs_cols[i], "Total Liabilities Net Minority Interest",
                                           "Total Liabilities", "TotalLiab")                  for i in range(n)]
    equity          = [get(bs, bs_cols[i], "Stockholders Equity", "Total Stockholder Equity",
                                           "Stockholders' Equity", "Common Stock Equity",
                                           "TotalStockholderEquity")                          for i in range(n)]
    inv_capital     = [get(bs, bs_cols[i], "Invested Capital", "InvestedCapital")             for i in range(n)]
    retained_earn   = [get(bs, bs_cols[i], "Retained Earnings", "RetainedEarnings",
                                           "Accumulated Deficit")                             for i in range(n)]

    # Cash flow statement — display years (cf may be missing for some tickers)
    cf_cols = [cf_years[y] for y in display_years if y in cf_years]
    # Pad with None for any year not present in cf
    cf_col_map = {y: cf_years[y] for y in display_years if y in cf_years}

    def get_cf(i):
        """Return cf column for display year i, or None if unavailable."""
        return cf_col_map.get(display_years[i])

    operating_cf  = [get(cf, get_cf(i), "Operating Cash Flow", "Cash From Operating Activities",
                                         "Total Cash From Operating Activities",
                                         "CashFlowFromContinuingOperatingActivities")
                     if get_cf(i) is not None else None                                       for i in range(n)]
    capex         = [get(cf, get_cf(i), "Capital Expenditure", "Capital Expenditures",
                                         "Purchase Of Property Plant And Equipment",
                                         "PurchaseOfPPE", "CapitalExpenditures")
                     if get_cf(i) is not None else None                                       for i in range(n)]
    acquisitions  = [get(cf, get_cf(i), "Acquisition Of Business", "AcquisitionOfBusiness",
                                         "Acquisitions Net", "Business Acquisitions And Disposals")
                     if get_cf(i) is not None else None                                       for i in range(n)]

    # Extended lists including the prior year (index n = oldest prior year)
    net_income_all  = net_income  + [get(fin, fin_cols_all[n], "Net Income", "NetIncome")
                                     if has_prior else None]
    pretax_inc_all  = pretax_inc  + [get(fin, fin_cols_all[n], "Pretax Income",
                                         "Income Before Tax", "Pre Tax Income", "PreTaxIncome")
                                     if has_prior else None]
    revenue_all     = revenue     + [get(fin, fin_cols_all[n], "Total Revenue", "TotalRevenue")
                                     if has_prior else None]
    op_income_all   = op_income   + [get(fin, fin_cols_all[n], "Operating Income", "EBIT",
                                         "OperatingIncome")
                                     if has_prior else None]
    equity_all      = equity      + [get(bs, bs_cols_all[n],   "Stockholders Equity",
                                         "Total Stockholder Equity", "Stockholders' Equity",
                                         "Common Stock Equity", "TotalStockholderEquity")
                                     if has_prior else None]
    retained_all    = retained_earn + [get(bs, bs_cols_all[n], "Retained Earnings",
                                           "RetainedEarnings", "Accumulated Deficit")
                                       if has_prior else None]
    diluted_all     = diluted_shares + [get(fin, fin_cols_all[n], "Diluted Average Shares",
                                            "Average Dilution Earnings", "Diluted Shares")
                                        if has_prior else None]

    # ── Formatting helpers ────────────────────────────────────────────────
    def pct(val):
        """0.1234  →  '12.34%'"""
        return f"{val * 100:.2f}%" if val is not None else "N/A"

    def pct_change(val):
        """
        Like pct() but prefixes positive values with '+' to make
        direction obvious at a glance: '+12.34%' / '-5.67%'
        """
        if val is None:
            return "N/A"
        sign = "+" if val >= 0 else ""
        return f"{sign}{val * 100:.2f}%"

    def ratio(val):
        """2.3456  →  '2.35'"""
        return f"{val:.2f}" if val is not None else "N/A"

    def currency(val):
        """Raw dollar value  →  '$1.23B' or '$456.7M'"""
        if val is None:
            return "N/A"
        b = val / 1_000_000_000
        return f"${b:.2f}B" if abs(b) >= 1 else f"${val / 1_000_000:.1f}M"

    def safe_div(a, b):
        """Return a/b, or None if either operand is None or b is zero."""
        if a is None or b in (None, 0.0, 0):
            return None
        return a / b

    def yoy(series_all, i):
        """
        YoY % change for display index i.
        series_all[i]   = current year value
        series_all[i+1] = prior year value (one index older in newest-first list)
        Returns None if either value is missing or prior is zero.
        """
        curr  = series_all[i]
        prior = series_all[i + 1] if i + 1 < len(series_all) else None
        return safe_div(curr - prior, abs(prior)) if curr is not None and prior is not None else None

    def cagr(beginning, ending, periods):
        """
        Standard CAGR = (ending / beginning)^(1/periods) - 1.
        Returns None if inputs are invalid (None, zero, or negative beginning).
        Handles negative-to-positive or negative-to-negative gracefully.
        """
        if beginning is None or ending is None or periods <= 0:
            return None
        if beginning == 0:
            return None
        # CAGR is mathematically undefined when beginning is negative
        if beginning < 0:
            return None
        try:
            return (ending / beginning) ** (1 / periods) - 1
        except Exception:
            return None

    def nopat(i):
        """NOPAT = Operating Income × (1 − effective tax rate)"""
        rate = safe_div(tax_expense[i], pretax_inc[i])
        if op_income[i] is None or rate is None:
            return None
        return op_income[i] * (1 - rate)

    # ── CAGR inputs: newest = index 0, oldest displayed = index n-1 ──────
    cagr_periods = n  # number of years between oldest and newest displayed

    def eps(idx_list, i):
        """EPS = Net Income / Diluted Shares for extended list index i."""
        return safe_div(idx_list[0][i], idx_list[1][i])

    # ── Metric definitions (per-year table rows) ──────────────────────────
    # To add a new per-year metric: append one dict with "label" + "values".
    # ─────────────────────────────────────────────────────────────────────
    metric_definitions = [

        # ── Balance sheet ratios ──────────────────────────────────────────
        {
            "label":  "D/E (Liabilities / Equity)",
            "values": [ratio(safe_div(total_liab[i], equity[i])) for i in range(n)],
        },
        {
            "label":  "D/A (Liabilities / Assets)",
            "values": [ratio(safe_div(total_liab[i], total_assets[i])) for i in range(n)],
        },

        # ── Margins ───────────────────────────────────────────────────────
        {
            "label":  "Gross Margin",
            "values": [pct(safe_div(gross_profit[i], revenue[i])) for i in range(n)],
        },
        {
            "label":  "Earnings % of Revenue",
            "values": [pct(safe_div(net_income[i], revenue[i])) for i in range(n)],
        },
        {
            "label":  "SGA % of Gross Profit",
            "values": [pct(safe_div(sga[i], gross_profit[i])) for i in range(n)],
        },

        # ── NOPAT & ROIC ──────────────────────────────────────────────────
        {
            "label":  "NOPAT",
            "values": [currency(nopat(i)) for i in range(n)],
        },
        {
            "label":  "ROIC",
            "values": [pct(safe_div(nopat(i), inv_capital[i])) for i in range(n)],
        },

        # ── Return metrics ────────────────────────────────────────────────
        {
            "label":  "ROA",
            "values": [pct(safe_div(net_income[i], total_assets[i])) for i in range(n)],
        },
        {
            "label":  "ROE",
            "values": [pct(safe_div(net_income[i], equity[i])) for i in range(n)],
        },

        # ── Year-over-year % changes ──────────────────────────────────────
        # i+1 in *_all is the prior year; "N/A" for the oldest year if no
        # prior data exists beyond what yfinance returns.
        {
            "label":  "Net Income YoY %",
            "values": [pct_change(yoy(net_income_all, i)) for i in range(n)],
        },
        {
            "label":  "Pre-Tax Income YoY %",
            "values": [pct_change(yoy(pretax_inc_all, i)) for i in range(n)],
        },

        # ── Cash flow metrics ─────────────────────────────────────────────
        {
            # FCF = Operating CF + CapEx + Acquisitions
            # CapEx is typically negative in the cash flow statement, so
            # adding it (rather than subtracting) is intentional.
            "label":  "Free Cash Flow (FCF)",
            "values": [
                currency(
                    (operating_cf[i] or 0) + (capex[i] or 0) + (acquisitions[i] or 0)
                    if operating_cf[i] is not None else None
                )
                for i in range(n)
            ],
        },
        {
            # Negative capex / positive net income = % of earnings consumed by capex
            "label":  "CapEx % of Net Income",
            "values": [pct(safe_div(capex[i], net_income[i])) for i in range(n)],
        },

        # ← add future per-year metrics here
    ]

    # ── CAGR definitions (single-value summary rows) ──────────────────────
    # Each dict has "label" and "value" (singular — one number, not per year).
    # newest = index 0, oldest displayed = index n-1 in the parallel lists.
    # ─────────────────────────────────────────────────────────────────────
    eps_newest = safe_div(net_income_all[0],   diluted_all[0])
    eps_oldest = safe_div(net_income_all[n-1], diluted_all[n-1])

    cagr_definitions = [
        {
            "label": f"Revenue CAGR ({cagr_periods}yr)",
            "value": pct(cagr(revenue_all[n-1], revenue_all[0], cagr_periods)),
        },
        {
            "label": f"EPS CAGR ({cagr_periods}yr)",
            "value": pct(cagr(eps_oldest, eps_newest, cagr_periods)),
        },
        {
            "label": f"Earnings (Net Income) CAGR ({cagr_periods}yr)",
            "value": pct(cagr(net_income_all[n-1], net_income_all[0], cagr_periods)),
        },
        {
            "label": f"Book Value (Equity) CAGR ({cagr_periods}yr)",
            "value": pct(cagr(equity_all[n-1], equity_all[0], cagr_periods)),
        },
        {
            "label": f"Operating Income CAGR ({cagr_periods}yr)",
            "value": pct(cagr(op_income_all[n-1], op_income_all[0], cagr_periods)),
        },
        {
            "label": f"Retained Earnings CAGR ({cagr_periods}yr)",
            "value": pct(cagr(retained_all[n-1], retained_all[0], cagr_periods)),
        },

        # ← add future CAGR metrics here
    ]

    # ── Chart data — raw values (frontend will display in thousands) ─────
    def to_raw(val):
        """Return raw dollar value as float, or None."""
        return float(val) if val is not None else None

    def fcf_raw(i):
        """FCF = Operating CF + CapEx + Acquisitions (raw dollars)."""
        if operating_cf[i] is None:
            return None
        return to_raw((operating_cf[i] or 0) + (capex[i] or 0) + (acquisitions[i] or 0))

    # Each chart: label shown as title, values list aligned to display_years.
    # To add a new chart: append one dict with "label" and "values".
    chart_definitions = [
        {"label": "Revenue",          "values": [to_raw(revenue[i])       for i in range(n)]},
        {"label": "Gross Profit",     "values": [to_raw(gross_profit[i])  for i in range(n)]},
        {"label": "Net Income",       "values": [to_raw(net_income[i])    for i in range(n)]},
        {"label": "Retained Earnings","values": [to_raw(retained_earn[i]) for i in range(n)]},
        {"label": "Book Value",       "values": [to_raw(equity[i])        for i in range(n)]},
        {"label": "Free Cash Flow",   "values": [fcf_raw(i)               for i in range(n)]},
        # ← add future charts here
    ]

    return {
        "years":  display_years,
        "ratios": metric_definitions,
        "cagr":   cagr_definitions,
        "charts": chart_definitions,
    }


@app.route("/api/stock_data")
def api_stock_data():
    """
    JSON endpoint: returns data for a single symbol to populate the
    two tables on the Collection page.

    Query param:  ?symbol=AAPL
    Response:
    {
        "symbol": "AAPL",
        "left_table": {
            "years":  ["2024", "2023", "2022", "2021"],
            "ratios": [{"label": "...", "values": [...]}, ...]
        },
        "right_table": {"Symbol": "AAPL", "Stage": "2", ...}
    }

    All exceptions are caught and returned as JSON so the browser
    never receives an HTML Flask error page (which would cause the
    "Unexpected token '<'" SyntaxError on the frontend).
    """
    try:
        symbol = request.args.get("symbol", "").strip().upper()
        if not symbol:
            return jsonify({"error": "No symbol provided"}), 400

        # Left table: 4-year historical metrics via yfinance / JSON cache
        left_table = fetch_historical_metrics(symbol)
        if left_table is None:
            left_table = {
                "years":  [],
                "ratios": [],
                "error":  "Could not fetch financial data — symbol may be invalid or cache unavailable",
            }

        # Right table: current snapshot — uses cache entry already populated
        # by fetch_historical_metrics() above, so no second network call.
        right_table = {"Symbol": symbol, "Stage": "—"}
        try:
            ticker = get_cached_ticker(symbol)
            info   = ticker.info

            raw_price      = info.get("currentPrice")
            raw_market_cap = info.get("marketCap")
            pe_ratio       = info.get("trailingPE")

            right_table["Current Price"] = f"${raw_price:,.2f}" if raw_price else "N/A"
            right_table["Market Cap"]    = format_large_number(raw_market_cap)
            right_table["P/E Ratio"]     = round(pe_ratio, 2) if pe_ratio else "N/A"
        except Exception as e:
            print(f"[ERROR] yfinance info for {symbol}: {e}")

        try:
            rows  = get_all_rows()
            match = next(
                (r for r in rows if str(r.get("Symbol", "")).strip().upper() == symbol),
                None,
            )
            if match:
                right_table["Stage"] = str(match.get("Stage", "—")).strip()
        except Exception as e:
            print(f"[ERROR] sheet lookup for {symbol}: {e}")

    except Exception as e:
        # Safety net: any unhandled exception returns JSON, never HTML.
        # This prevents the browser from receiving a Flask error page,
        # which would cause "Unexpected token '<'" on the frontend.
        import traceback
        print(f"[ERROR] api_stock_data unhandled exception: {e}")
        print(traceback.format_exc())
        return jsonify({"error": f"Internal server error: {e}"}), 500

    return jsonify({
        "symbol":      symbol,
        "left_table":  left_table,
        "right_table": right_table,
        "charts":      left_table.get("charts", []),
        "years":       left_table.get("years",  []),
    })


@app.route("/api/move_symbol", methods=["POST"])
def api_move_symbol():
    """
    POST {"symbol": "AAPL", "new_stage": "2"}
    Moves the symbol to the target stage in Google Sheets.
    """
    data      = request.get_json(force=True)
    symbol    = str(data.get("symbol",    "")).strip().upper()
    new_stage = str(data.get("new_stage", "")).strip()

    if not symbol or not new_stage:
        return jsonify({"ok": False, "error": "symbol and new_stage are required"}), 400
    if new_stage not in STAGES:
        return jsonify({"ok": False, "error": f"'{new_stage}' is not a valid stage"}), 400

    try:
        update_stage(symbol, new_stage)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "symbol": symbol, "new_stage": new_stage})


# ===========================================================================
# INDUSTRY TAROT ROUTES
# ===========================================================================

@app.route("/industry")
def industry():
    """Render the Industry Tarot page."""
    return render_template("industry.html")


@app.route("/api/industries")
def api_industries():
    """
    Return the full list of industries with all columns.
    GET /api/industries
    Response: { "industries": [ { "industry": "Trucking", "image_url": "...",
                                   "supplier_power": 3, ... }, ... ] }
    Rating fields are returned as integers (or None if blank in the sheet).
    """
    try:
        rows = get_all_industries()
        rating_fields = [
            "supplier_power", "buyer_power", "new_entrants",
            "substitutes", "rivalry"
        ]
        result = []
        for r in rows:
            entry = {
                "industry":  str(r.get("industry", "")).strip(),
                "image_url": str(r.get("image_url", "")).strip(),
            }
            for field in rating_fields:
                raw = r.get(field, "")
                try:
                    entry[field] = int(raw) if str(raw).strip() != "" else None
                except (ValueError, TypeError):
                    entry[field] = None
            result.append(entry)
        return jsonify({"industries": result})
    except Exception as e:
        print(f"[ERROR] api_industries: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategic_groups")
def api_strategic_groups():
    """
    Return strategic groups, optionally filtered by parent industry.
    GET /api/strategic_groups                  → all groups
    GET /api/strategic_groups?industry=Trucking → only Trucking groups
    Response: { "groups": [ { "group": "LTL", "parent_industry": "Trucking",
                               "image_url": "...", "customer_overlap": 3, ... } ] }
    """
    try:
        industry_filter = request.args.get("industry", "").strip()
        rows = get_all_strategic_groups()
        rating_fields = [
            "customer_overlap", "differentiation",
            "number_and_size", "strategy_overlap"
        ]
        result = []
        for r in rows:
            parent = str(r.get("parent_industry", "")).strip()
            if industry_filter and parent != industry_filter:
                continue
            entry = {
                "group":           str(r.get("group", "")).strip(),
                "parent_industry": parent,
                "image_url":       str(r.get("image_url", "")).strip(),
            }
            for field in rating_fields:
                raw = r.get(field, "")
                try:
                    entry[field] = int(raw) if str(raw).strip() != "" else None
                except (ValueError, TypeError):
                    entry[field] = None
            result.append(entry)
        return jsonify({"groups": result})
    except Exception as e:
        print(f"[ERROR] api_strategic_groups: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/industry_rating", methods=["POST"])
def api_industry_rating():
    """
    Update a single rating cell in the industries worksheet.
    POST JSON: { "industry": "Trucking", "field": "supplier_power", "value": 4 }
    Finds the matching row by the industry name and updates that column.
    Invalidates the industries cache so the next read is fresh.
    """
    data     = request.get_json(silent=True) or {}
    industry = str(data.get("industry", "")).strip()
    field    = str(data.get("field",    "")).strip()
    value    = data.get("value")

    allowed_fields = [
        "supplier_power", "buyer_power", "new_entrants",
        "substitutes", "rivalry"
    ]
    if not industry or field not in allowed_fields:
        return jsonify({"ok": False, "error": "industry and valid field required"}), 400
    try:
        value = int(value)
        if not 1 <= value <= 5:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "value must be an integer 1–5"}), 400

    try:
        # Find the column index for this field in the industries sheet
        headers = sheet_industries.row_values(1)
        if field not in headers:
            return jsonify({"ok": False, "error": f"Column '{field}' not found in sheet"}), 400
        col_idx = headers.index(field) + 1  # gspread is 1-indexed

        # Find the row for this industry
        cell = sheet_industries.find(industry)
        if cell is None:
            return jsonify({"ok": False, "error": f"Industry '{industry}' not found in sheet"}), 404

        sheet_industries.update_cell(cell.row, col_idx, value)
        invalidate_industries_cache()
        return jsonify({"ok": True, "industry": industry, "field": field, "value": value})
    except Exception as e:
        print(f"[ERROR] api_industry_rating: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/group_rating", methods=["POST"])
def api_group_rating():
    """
    Update a single rating cell in the strategic_groups worksheet.
    POST JSON: { "group": "LTL", "field": "customer_overlap", "value": 3 }
    Finds the matching row by the group name and updates that column.
    Invalidates the groups cache so the next read is fresh.
    """
    data  = request.get_json(silent=True) or {}
    group = str(data.get("group", "")).strip()
    field = str(data.get("field", "")).strip()
    value = data.get("value")

    allowed_fields = [
        "customer_overlap", "differentiation",
        "number_and_size", "strategy_overlap"
    ]
    if not group or field not in allowed_fields:
        return jsonify({"ok": False, "error": "group and valid field required"}), 400
    try:
        value = int(value)
        if not 1 <= value <= 5:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "value must be an integer 1–5"}), 400

    try:
        headers = sheet_strategic_groups.row_values(1)
        if field not in headers:
            return jsonify({"ok": False, "error": f"Column '{field}' not found in sheet"}), 400
        col_idx = headers.index(field) + 1

        cell = sheet_strategic_groups.find(group)
        if cell is None:
            return jsonify({"ok": False, "error": f"Group '{group}' not found in sheet"}), 404

        sheet_strategic_groups.update_cell(cell.row, col_idx, value)
        invalidate_groups_cache()
        return jsonify({"ok": True, "group": group, "field": field, "value": value})
    except Exception as e:
        print(f"[ERROR] api_group_rating: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analysis/stock/<symbol>", methods=["GET"])
def api_get_stock_analysis(symbol):
    """
    Return the full analysis entry for a stock symbol.
    GET /api/analysis/stock/AAPL
    Response: { "symbol": "AAPL", "data": { "assigned_industry": "...", ... } }
    Returns an empty data dict if no analysis has been saved yet.
    """
    symbol = symbol.strip().upper()
    analysis = _load_analysis()
    return jsonify({"symbol": symbol, "data": analysis.get(symbol, {})})


@app.route("/api/analysis/stock/<symbol>", methods=["POST"])
def api_post_stock_analysis(symbol):
    """
    Save (merge) analysis data for a stock symbol.
    POST /api/analysis/stock/AAPL
    Body: any subset of the analysis fields, e.g.:
      { "assigned_industry": "Trucking" }
      { "firm_strategy": "differentiation" }
      { "firm_profitability": { "mobility_barriers": 3 } }
    Nested dicts (like firm_profitability) are merged at one level deep
    so a partial update does not wipe sibling keys.
    """
    symbol = symbol.strip().upper()
    updates = request.get_json(silent=True) or {}
    if not updates:
        return jsonify({"ok": False, "error": "no data provided"}), 400

    analysis = _load_analysis()
    if symbol not in analysis:
        analysis[symbol] = {}

    for key, val in updates.items():
        if isinstance(val, dict) and isinstance(analysis[symbol].get(key), dict):
            # Merge nested dicts one level deep (e.g. firm_profitability)
            analysis[symbol][key].update(val)
        else:
            analysis[symbol][key] = val

    _save_analysis(analysis)
    return jsonify({"ok": True, "symbol": symbol, "data": analysis[symbol]})


# --------------------------
# Debug Routes
# --------------------------
@app.route("/debug/yf/<symbol>")
def debug_symbol(symbol):
    """
    Shows all yfinance row names and their most recent values.
    Uses the cache — data will be fetched from yfinance only if stale.
    Visit: /debug/yf/AAPL
    """
    symbol = symbol.upper().strip()
    output = [f"<h2>yfinance Debug: {symbol}</h2><pre>"]
    try:
        ticker = get_cached_ticker(symbol)
        output.append("=== Income Statement (financials) ===\n")
        fin = ticker.financials
        for name in fin.index:
            output.append(f"  {name!r:60s} = {fin.loc[name].iloc[0]}\n")
        output.append("\n=== Balance Sheet ===\n")
        bs = ticker.balance_sheet
        for name in bs.index:
            output.append(f"  {name!r:60s} = {bs.loc[name].iloc[0]}\n")
    except Exception as e:
        output.append(f"Error: {e}\n")
    output.append("</pre>")
    return "".join(output)


@app.route("/debug/cache")
def debug_cache():
    """
    Shows the state of both JSON cache files and the in-memory cache.
    Visit: /debug/cache
    """
    stats = cache_stats()
    mem   = stats["memory"]
    files = stats["json_files"]

    def _sym_list(symbols):
        if not symbols:
            return "<em style='color:#6a5c48'>none</em>"
        return ", ".join(f"<strong>{s}</strong>" for s in sorted(symbols))

    trk  = files["tracker_cache"]
    col  = files["collection_cache"]

    mem_rows = "".join(
        f"<tr><td><strong>{sym}</strong></td><td>✓ loaded</td>"
        f"<td><a href='/debug/cache/invalidate/{sym}'>evict</a></td></tr>"
        for sym in sorted(mem)
    ) or "<tr><td colspan='3'><em style='color:#6a5c48'>empty</em></td></tr>"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Cache Debug</title>
    <link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
<div class="container">
    <nav>
        <a href="/tracker">Tracker</a>
        <a href="/collection">Collection</a>
        <a href="/industry">Industry</a>
    </nav>
    <h1>Cache Status</h1>

    <div class="panel" style="margin-bottom:16px;">
        <h3 style="margin-bottom:10px;">JSON Cache Files</h3>
        <table>
        <thead><tr><th>File</th><th>Last Written</th><th>Symbols Cached</th></tr></thead>
        <tbody>
            <tr>
                <td><code>data/tracker_cache.json</code></td>
                <td>{trk['last_written']}</td>
                <td>{_sym_list(trk['symbols'])}</td>
            </tr>
            <tr>
                <td><code>data/collection_cache.json</code></td>
                <td>{col['last_written']}</td>
                <td>{_sym_list(col['symbols'])}</td>
            </tr>
        </tbody>
        </table>
        <p style="margin-top:10px; font-size:0.8rem; color:#6a5c48;">
            JSON files are written by <code>refresh_cache.py</code> (run daily at 02:00).
            &nbsp;|&nbsp; <a href="/debug/cache/invalidate/ALL">Evict all from memory</a>
        </p>
    </div>

    <div class="panel">
        <h3 style="margin-bottom:10px;">In-Memory Cache  ({len(mem)} symbol(s))</h3>
        <p style="font-size:0.8rem; color:#6a5c48; margin-bottom:10px;">
            Evicting a symbol forces the next request to re-read from the JSON file
            (or fetch live from yfinance if absent from JSON).
        </p>
        <table>
        <thead><tr><th>Symbol</th><th>Status</th><th>Action</th></tr></thead>
        <tbody>{mem_rows}</tbody>
        </table>
    </div>

    <p style="font-size:0.8rem; color:#6a5c48; margin-top:8px;">
        Page auto-refreshes every 30s. &nbsp;|&nbsp; <a href="/debug/cache">Refresh now</a>
    </p>
</div>
<script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>"""
    return html


@app.route("/debug/cache/invalidate/<symbol>")
def debug_cache_invalidate(symbol):
    """
    Evict a symbol (or ALL symbols) from the in-memory cache.
    The next request will re-read from the JSON file, or fetch live
    from yfinance if the symbol isn't in either JSON file.

    Visit: /debug/cache/invalidate/AAPL   — evict one symbol
    Visit: /debug/cache/invalidate/ALL    — evict entire memory cache
    """
    if symbol.upper() == "ALL":
        cleared = list(_ticker_cache.keys())
        _ticker_cache.clear()
        msg = (f"Evicted entire memory cache "
               f"({len(cleared)} symbol(s): {', '.join(cleared) or 'none'}). "
               f"Next requests will re-read from JSON files.")
    else:
        symbol = symbol.upper().strip()
        if symbol in _ticker_cache:
            invalidate_cache(symbol)
            msg = (f"<strong>{symbol}</strong> evicted from memory. "
                   f"Next request will re-read from JSON (or fetch live if absent).")
        else:
            msg = f"<strong>{symbol}</strong> was not in the memory cache — nothing to do."

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8"><title>Cache Evicted</title>
    <link rel="stylesheet" href="/static/css/style.css">
    <meta http-equiv="refresh" content="2;url=/debug/cache">
</head>
<body>
<div class="container">
    <nav>
        <a href="/tracker">Tracker</a>
        <a href="/collection">Collection</a>
        <a href="/industry">Industry</a>
    </nav>
    <h1>Memory Cache Evicted</h1>
    <div class="panel"><p style="color:#e0d8c8">{msg}</p>
    <p style="margin-top:10px; color:#6a5c48; font-size:0.82rem;">
        Redirecting to <a href="/debug/cache">/debug/cache</a> in 2s…
    </p></div>
</div>
</body>
</html>"""


# ===========================================================================
# NOTES ROUTES
# ===========================================================================

NOTES_PATH = os.path.join(
    os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), "data")),
    "notes.json"
)

def _load_notes() -> dict:
    """Load all notes from disk. Returns {} if file missing or unreadable."""
    try:
        with open(NOTES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_notes(notes: dict) -> None:
    """Atomically write notes dict to disk."""
    os.makedirs(os.path.dirname(NOTES_PATH), exist_ok=True)
    tmp = NOTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2, ensure_ascii=False)
    os.replace(tmp, NOTES_PATH)


@app.route("/api/notes", methods=["GET"])
def api_get_notes():
    """
    Return notes for any namespaced key.
    Supports stock symbols:          GET /api/notes?symbol=AAPL
    And namespaced industry keys:    GET /api/notes?symbol=industry::Trucking::supplier_power
    The ?symbol= param is the raw key used in notes.json.
    """
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    notes = _load_notes()
    return jsonify({"symbol": symbol, "notes": notes.get(symbol, [])})


@app.route("/api/notes", methods=["POST"])
def api_post_note():
    """
    Add a note for any namespaced key.
    POST JSON: { "symbol": "industry::Trucking::supplier_power", "text": "..." }
    The symbol field accepts any string key, including namespaced industry keys.
    """
    data   = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    text   = str(data.get("text", "")).strip()
    if not symbol or not text:
        return jsonify({"error": "symbol and text required"}), 400
    notes = _load_notes()
    if symbol not in notes:
        notes[symbol] = []
    from datetime import datetime as _dt
    notes[symbol].append({
        "text":      text,
        "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M"),
    })
    _save_notes(notes)
    return jsonify({"ok": True, "notes": notes[symbol]})


@app.route("/api/notes/<path:symbol>/<int:index>", methods=["DELETE"])
def api_delete_note(symbol, index):
    """
    Delete a note by index.
    DELETE /api/notes/AAPL/0
    DELETE /api/notes/industry::Trucking::supplier_power/0
    Uses <path:symbol> so that :: in the key is handled correctly.
    """
    symbol = symbol.strip()
    notes  = _load_notes()
    if symbol not in notes or index >= len(notes[symbol]) or index < 0:
        return jsonify({"error": "note not found"}), 404
    notes[symbol].pop(index)
    _save_notes(notes)
    return jsonify({"ok": True, "notes": notes[symbol]})


# ===========================================================================
# CACHE REFRESH ROUTE  (called by Render Cron Job via HTTP)
# ===========================================================================

@app.route("/run-cache-refresh")
def run_cache_refresh():
    """
    Trigger a full cache refresh from inside the web service process.
    This is called nightly by a Render Cron Job via HTTP GET so the
    refreshed JSON files are written to the same filesystem the web
    service reads from.

    Protected by a secret token set as the REFRESH_SECRET env var.
    Requests without the correct ?token= param are rejected with 403.
    """
    import threading

    expected = os.environ.get("REFRESH_SECRET", "")
    if not expected or request.args.get("token") != expected:
        return jsonify({"error": "forbidden"}), 403

    def _do_refresh():
        """Run in a background thread so the HTTP response returns immediately."""
        try:
            from refresh_cache import refresh
            refresh()
            # FIX: clear stale in-memory cache before reloading from the
            # freshly-written JSON files. Without this, warm_ticker_cache_from_json()
            # skips every symbol that is already in memory (if symbol in _ticker_cache:
            # continue), so the old prices are never replaced.
            _ticker_cache.clear()
            warm_ticker_cache_from_json()
            invalidate_rows_cache()
            print("[REFRESH] Cache refresh completed successfully.")
        except Exception as e:
            print(f"[REFRESH] Cache refresh failed: {e}")

    thread = threading.Thread(target=_do_refresh, daemon=True)
    thread.start()
    return jsonify({"status": "refresh started"}), 202


if __name__ == "__main__":
    # Local development only — Render uses Gunicorn and never reaches this block.
    # Debug mode is read from the FLASK_DEBUG env var (defaults to off).
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port  = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug)