"""
Ruliad Capital Management Systems – Indian Markets
Flask backend: serves live NSE prices via yfinance with background refresh.
"""

from flask import Flask, jsonify, render_template, request, make_response
import secrets
import sqlite3
import yfinance as yf
import pandas as pd
import threading
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------------------------------------------------------------------------
# BASIC AUTH  — set a password via env var LAUNCHPAD_PASSWORD before starting
# e.g.  $env:LAUNCHPAD_PASSWORD="mysecret" ; python app.py
# Leave unset / empty to disable auth (safe for localhost-only use)
# ---------------------------------------------------------------------------
_AUTH_PASSWORD = os.environ.get("LAUNCHPAD_PASSWORD", "").strip()
_AUTH_USER     = os.environ.get("LAUNCHPAD_USER", "launchpad").strip()

def _check_auth(username: str, password: str) -> bool:
    """Constant-time compare to avoid timing attacks."""
    if not _AUTH_PASSWORD:
        return True          # no password set — open access
    ok_user = secrets.compare_digest(username.encode(), _AUTH_USER.encode())
    ok_pass = secrets.compare_digest(password.encode(), _AUTH_PASSWORD.encode())
    return ok_user and ok_pass

@app.before_request
def _global_auth():
    if not _AUTH_PASSWORD:
        return
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        resp = make_response("Authentication required", 401)
        resp.headers["WWW-Authenticate"] = 'Basic realm="Launchpad"'
        return resp

# ---------------------------------------------------------------------------
# SECTOR → SHEET MAPPING  (all 26 sheets, 3 pages of 4×3 = 9 panels each,
#                          last page has 8)
# ---------------------------------------------------------------------------
PANEL_CONFIG = [
    # Page 1  (indices 0-8)
    ("Brokerages",               "Sheet 1"),
    ("PSU Banks",                "Sheet2"),
    ("Capital Markets",          "Sheet 3"),
    ("Insurance & Wealth",       "Sheet 4"),
    ("NBFC & Housing Finance",   "Sheet 5"),
    ("Private Banks",            "Sheet 6"),
    ("Information Technology",   "Sheet 7"),
    ("Consumer & FMCG",          "Sheet 8"),
    ("Diversified / Conglom.",   "Sheet 9"),
    # Page 2  (indices 9-17)
    ("Pharmaceuticals",          " Sheet 10"),
    ("Automobiles",              "Sheet 11"),
    ("Industrials & Materials",  "Sheet 12"),
    ("Oil & Gas",                "Sheet 13"),
    ("Power & Energy",           "Sheet 14"),
    ("Auto Components",          "Sheet 15"),
    ("Cement",                   "Sheet 16"),
    ("Media & Entertainment",    "Sheet 17"),
    ("Real Estate",              "Sheet 18"),
    # Page 3  (indices 18-25)
    ("Telecom",                  "Sheet 19"),
    ("Metals & Mining",          "Sheet 20"),
    ("Paper & Packaging",        "Sheet 21"),
    ("Logistics & Shipping",     "Sheet 22"),
    ("Agrochem & Fertilisers",   "Sheet 23"),
    ("Speciality Chemicals",     "Sheet 24"),
    ("Hotels & Hospitality",     "Sheet 25"),
    ("Textiles",                 "Sheet 26"),
]

# ---------------------------------------------------------------------------
# SECTOR / SYMBOL DATABASE  (SQLite)
# All sector panel stock lists are stored in launchpad.db.
# On first run the database is auto-populated from sheet_data.json if present.
# ---------------------------------------------------------------------------
DB_FILE = "launchpad.db"


def _db_connect():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def _init_db() -> None:
    """Create the panel_stocks table. Migrate from sheet_data.json if the DB is empty."""
    con = _db_connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS panel_stocks (
            panel_id TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            name     TEXT,
            PRIMARY KEY (panel_id, symbol)
        )
    """)
    con.commit()
    if con.execute("SELECT COUNT(*) FROM panel_stocks").fetchone()[0] == 0:
        if os.path.exists("sheet_data.json"):
            with open("sheet_data.json") as _fh:
                _jdata = json.load(_fh)
            _rows = []
            for _pid, _stocks in _jdata.items():
                for _s in _stocks:
                    _rows.append((_pid, _s["symbol"], _s.get("name", _s["symbol"])))
            con.executemany(
                "INSERT OR IGNORE INTO panel_stocks (panel_id, symbol, name) VALUES (?,?,?)",
                _rows
            )
            con.commit()
            print(f"[DB] Migrated sheet_data.json → {DB_FILE} ({len(_rows)} rows)")
        else:
            print("[DB] WARNING: launchpad.db is empty and no sheet_data.json found.")
    con.close()


def _load_sheet_data_from_db() -> dict:
    """Return all panel stocks as {panel_id: [{symbol, name}, ...]}."""
    con = _db_connect()
    rows = con.execute(
        "SELECT panel_id, symbol, name FROM panel_stocks ORDER BY rowid"
    ).fetchall()
    con.close()
    data: dict = {}
    for panel_id, symbol, name in rows:
        data.setdefault(panel_id, []).append({"symbol": symbol, "name": name or symbol})
    return data


_init_db()
raw_sheet_data = _load_sheet_data_from_db()

# Build panels + deduplicated master symbol list
panels = []
all_symbols:    list[str] = []   # ALL non-global symbols (NSE + BSE)
global_symbols: list[str] = []   # raw yfinance tickers: ^DJI, GC=F, CL=F …
bse_symbols:    list[str] = []   # DEPRECATED: kept only to not break old startup paths
bse_override:   set[str]  = set()  # symbols to fetch via .BO (subset of all_symbols)
dead_symbols:   dict      = {}     # symbol -> consecutive miss count; excluded after threshold
DEAD_THRESHOLD  = 3               # skip after this many consecutive total misses
seen_global: set[str] = set()

PAGE_SIZE = 12  # must match frontend PAGE_SIZE constant

for sector_name, sheet_key in PANEL_CONFIG:
    stocks = raw_sheet_data.get(sheet_key, [])
    panel_stocks = []
    seen_panel: set[str] = set()
    _sheet_dirty = False
    for s in stocks:
        sym = s["symbol"]
        # Strip any exchange suffix stored in sheet_data.json by mistake
        clean = sym
        if clean.upper().endswith(".BO"):  clean = clean[:-3]; _sheet_dirty = True
        elif clean.upper().endswith(".NS"): clean = clean[:-3]; _sheet_dirty = True
        if clean != sym:
            s["symbol"] = clean
            sym = clean
        if sym not in seen_panel:
            seen_panel.add(sym)
            panel_stocks.append({"symbol": sym, "name": s.get("name", sym)})
        if sym not in seen_global:
            seen_global.add(sym)
            all_symbols.append(sym)
    if _sheet_dirty:
        _save_sheet_data()
        print(f"[INIT] Stripped exchange suffixes from {sheet_key}")
    panels.append({"sector": sector_name, "stocks": panel_stocks, "id": sheet_key.strip()})

print(f"[INIT] {len(panels)} panels, {len(all_symbols)} unique symbols")


# ---------------------------------------------------------------------------
# PERSISTENCE: user customisations (add / remove tickers per panel)
# ---------------------------------------------------------------------------
CUSTOMIZATIONS_FILE = "user_customizations.json"


def _load_customs() -> dict:
    if os.path.exists(CUSTOMIZATIONS_FILE):
        try:
            with open(CUSTOMIZATIONS_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def _save_customs(data: dict) -> None:
    with open(CUSTOMIZATIONS_FILE, "w") as fh:
        json.dump(data, fh, indent=2)


def _save_sheet_data() -> None:
    """Persist raw_sheet_data back to the SQLite database."""
    con = _db_connect()
    con.execute("DELETE FROM panel_stocks")
    rows = []
    for panel_id, stocks in raw_sheet_data.items():
        for s in stocks:
            rows.append((panel_id, s["symbol"], s.get("name", s["symbol"])))
    con.executemany(
        "INSERT INTO panel_stocks (panel_id, symbol, name) VALUES (?,?,?)",
        rows
    )
    con.commit()
    con.close()


def _save_order() -> None:
    """Persist panel display order and page assignments using stable IDs."""
    customs = _load_customs()
    customs["__order__"] = [p["id"] for p in panels]
    customs["__pages__"] = {p["id"]: p.get("page", 0) for p in panels}
    _save_customs(customs)


# ---------------------------------------------------------------------------
# Apply saved customisations at startup — stable-ID based
# ---------------------------------------------------------------------------
_init_customs = _load_customs()

# ── MIGRATION: handle old positional-index format keys alongside stable IDs ──
_orig_ids    = [sk.strip() for _, sk in PANEL_CONFIG]   # original panel order mapping
_numeric_keys = [k for k in _init_customs
                 if k.lstrip("-").isdigit() and k not in ("__page_count__",)]
if _numeric_keys:
    if "__order__" not in _init_customs:
        # Pure old format — cannot reliable reorder, clear
        print("[INIT] Old positional customisations (no __order__) — clearing")
        _init_customs = {}
    else:
        # Mixed: numeric keys alongside stable IDs → migrate to stable IDs
        print(f"[INIT] Migrating {len(_numeric_keys)} positional customisations to stable IDs")
        for _nk in _numeric_keys:
            _n = int(_nk)
            if 0 <= _n < len(_orig_ids):
                _sid  = _orig_ids[_n]
                _old  = _init_customs.pop(_nk)
                _dest = _init_customs.setdefault(_sid, {"added": [], "removed": []})
                # merge added stocks (no dupes)
                _have = {s["symbol"] for s in _dest.get("added", [])}
                for _s in _old.get("added", []):
                    if _s["symbol"] not in _have:
                        _dest.setdefault("added", []).append(_s)
                # merge removed symbols
                _hrem = set(_dest.get("removed", []))
                for _sym in _old.get("removed", []):
                    if _sym not in _hrem:
                        _dest.setdefault("removed", []).append(_sym)
                # merge sector_name if not already set by stable key
                if not _dest.get("sector_name") and _old.get("sector_name"):
                    _dest["sector_name"] = _old["sector_name"]
            else:
                _init_customs.pop(_nk, None)   # out-of-range (e.g. user-created under old system)
        _save_customs(_init_customs)
        print("[INIT] Migration complete")

# ── Migration: bake historical removed/added from user_customizations.json
# into the database so it is the permanent single source of truth.
# IMPORTANT: we update raw_sheet_data and save to disk, but do NOT modify
# _init_customs in memory — Pass 1 below still needs the original values
# to correctly update the already-built in-memory panels list this run.
_sheet_changed = False
_migrated_pids = []
for _panel in panels:
    _pid   = _panel["id"]
    _cust  = _init_customs.get(_pid, {})
    _added   = list(_cust.get("added", []))
    _removed = set(_cust.get("removed", []))
    if _pid not in raw_sheet_data:
        continue  # user-created panel — not in database
    if not (_added or _removed):
        continue
    if _removed:
        _before = len(raw_sheet_data[_pid])
        raw_sheet_data[_pid] = [s for s in raw_sheet_data[_pid] if s["symbol"] not in _removed]
        if len(raw_sheet_data[_pid]) != _before:
            _sheet_changed = True
    for _as in _added:
        if not any(s["symbol"] == _as["symbol"] for s in raw_sheet_data[_pid]):
            raw_sheet_data[_pid].append({"symbol": _as["symbol"], "name": _as.get("name", _as["symbol"])})
            _sheet_changed = True
    _migrated_pids.append(_pid)
if _migrated_pids:
    _save_sheet_data()
    # Write updated customs to disk with cleared added/removed for migrated panels
    # (use a fresh load so we don't disturb the _init_customs dict used by Pass 1)
    import json as _json
    _disk_customs = _json.loads(_json.dumps(_init_customs))  # deep copy
    for _pid in _migrated_pids:
        if _pid in _disk_customs:
            _disk_customs[_pid]["removed"] = []
            _disk_customs[_pid]["added"]   = []
    _save_customs(_disk_customs)
    print(f"[INIT] Migrated historical customisations into sheet_data.json ({len(_migrated_pids)} panels)")

# Pass 1: apply per-panel customisations to Excel-derived panels by stable id
for _panel in panels:
    _cust = _init_customs.get(_panel["id"], {})
    if _cust.get("sector_name"):
        _panel["sector"] = _cust["sector_name"]
    if _cust.get("mode"):
        _panel["mode"] = _cust["mode"]
    _removed = set(_cust.get("removed", []))
    _panel["stocks"] = [s for s in _panel["stocks"] if s["symbol"] not in _removed]
    _existing = {s["symbol"] for s in _panel["stocks"]}
    _pmode = _panel.get("mode", "nse")
    for _s in _cust.get("added", []):
        if _s["symbol"] not in _existing:
            _panel["stocks"].append(_s)
            _sym = _s["symbol"]
            if _pmode == "global":
                if _sym not in global_symbols: global_symbols.append(_sym)
            else:
                if _sym not in all_symbols: all_symbols.append(_sym)
                # Mark as BSE-override if panel is BSE mode
                if _pmode == "bse":
                    bse_override.add(_sym)

# Pass 2: reconstruct user-created panels from saved order
_existing_ids = {p["id"] for p in panels}
_order = _init_customs.get("__order__", [])
for _pid in _order:
    if _pid not in _existing_ids:
        _uc = _init_customs.get(_pid, {})
        if _uc.get("user_created"):
            _removed = set(_uc.get("removed", []))
            _stocks  = [s for s in _uc.get("added", [])
                        if s.get("symbol") and s["symbol"] not in _removed]
            _mode = _uc.get("mode", "nse")
            panels.append({"sector": _uc.get("sector_name", "Custom Sector"),
                           "stocks": _stocks, "id": _pid, "mode": _mode})
            if _mode == "global":
                _sym_list = global_symbols
            else:
                _sym_list = all_symbols
            for _s in _stocks:
                if _s["symbol"] not in _sym_list:
                    _sym_list.append(_s["symbol"])
                if _mode == "bse":
                    bse_override.add(_s["symbol"])
            _existing_ids.add(_pid)

# Pass 3: restore panel display order
if _order:
    _id_map  = {p["id"]: p for p in panels}
    _ordered = [_id_map[_pid] for _pid in _order if _pid in _id_map]
    _in_order = set(_order)
    for _p in panels:
        if _p["id"] not in _in_order:
            _ordered.append(_p)
    panels[:] = _ordered

# Pass 4: restore explicit page assignments (enables arbitrary page placement)
_page_map   = _init_customs.get("__pages__", {})
_height_map = _init_customs.get("__heights__", {})
for _i, _panel in enumerate(panels):
    _panel["page"]   = _page_map.get(_panel["id"], _i // PAGE_SIZE)
    _panel["height"] = _height_map.get(_panel["id"], 1)

# Rebuild symbol fetch lists from the final panel state.
# This must happen AFTER all customisation passes so deleted symbols are
# never added to all_symbols and never fetched in the background thread.
all_symbols.clear()
global_symbols.clear()
bse_override.clear()
_seen_syms: set[str] = set()
for _panel in panels:
    _pmode = _panel.get("mode", "nse")
    for _s in _panel["stocks"]:
        _sym = _s["symbol"]
        if _sym not in _seen_syms:
            _seen_syms.add(_sym)
            if _pmode == "global":
                global_symbols.append(_sym)
            else:
                all_symbols.append(_sym)
                if _pmode == "bse":
                    bse_override.add(_sym)

del _init_customs


# ---------------------------------------------------------------------------
# PRICE CACHE  (+ disk persistence for instant reload)
# ---------------------------------------------------------------------------
PRICE_CACHE_FILE = "price_cache.json"

price_cache: dict = {}
indices_cache: dict = {}


def _load_price_cache() -> None:
    """Load previously saved prices from disk into memory cache."""
    if os.path.exists(PRICE_CACHE_FILE):
        try:
            with open(PRICE_CACHE_FILE) as fh:
                loaded = json.load(fh)
            with cache_lock:
                price_cache.update(loaded.get("prices", {}))
                indices_cache.update(loaded.get("indices", {}))
            print(f"[INIT] Loaded {len(price_cache)} cached prices from disk")
        except Exception as e:
            print(f"[WARN] Could not load price cache: {e}")


def _save_price_cache() -> None:
    """Persist current price cache to disk."""
    try:
        with cache_lock:
            snapshot = {"prices": dict(price_cache), "indices": dict(indices_cache)}
        with open(PRICE_CACHE_FILE, "w") as fh:
            json.dump(snapshot, fh)
    except Exception as e:
        print(f"[WARN] Could not save price cache: {e}")
cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# MARKET CAP CACHE  (fetched once at startup then every 6 hours)
# ---------------------------------------------------------------------------
MKTCAP_CACHE_FILE = "mktcap_cache.json"
mktcap_cache: dict = {}          # sym -> market_cap (INR)
mktcap_lock  = threading.Lock()


def _load_mktcap_cache() -> None:
    if os.path.exists(MKTCAP_CACHE_FILE):
        try:
            with open(MKTCAP_CACHE_FILE) as fh:
                loaded = json.load(fh)
            with mktcap_lock:
                mktcap_cache.update(loaded)
            print(f"[INIT] Loaded {len(mktcap_cache)} cached market caps from disk")
        except Exception as e:
            print(f"[WARN] Could not load mktcap cache: {e}")


def _save_mktcap_cache() -> None:
    try:
        with mktcap_lock:
            snapshot = dict(mktcap_cache)
        with open(MKTCAP_CACHE_FILE, "w") as fh:
            json.dump(snapshot, fh)
    except Exception as e:
        print(f"[WARN] Could not save mktcap cache: {e}")


def _fetch_one_mktcap(sym: str):
    """Fetch market cap for one symbol via yfinance fast_info."""
    try:
        mc = yf.Ticker(sym + ".NS").fast_info.market_cap
        return sym, (mc if mc and mc > 0 else None)
    except Exception:
        return sym, None


def fetch_mktcap_all() -> None:
    """Fetch market cap using a thread pool to parallelise fast_info calls.
    8 workers × ~0.6s per call → 600 symbols in ~45 seconds.
    """
    t0  = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching market caps for {len(all_symbols)} symbols...")
    new_mc: dict = {}
    hit = 0
    with ThreadPoolExecutor(max_workers=8) as exe:
        futures = {exe.submit(_fetch_one_mktcap, sym): sym for sym in all_symbols}
        for fut in as_completed(futures):
            try:
                sym, mc = fut.result(timeout=20)
                if mc:
                    new_mc[sym] = mc
                    hit += 1
            except Exception:
                pass
    with mktcap_lock:
        mktcap_cache.update(new_mc)
    _save_mktcap_cache()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Market caps fetched: {hit}/{len(all_symbols)} in {round(time.time()-t0,1)}s")


def mktcap_updater() -> None:
    _load_mktcap_cache()
    fetch_mktcap_all()
    while True:
        time.sleep(6 * 3600)   # refresh every 6 hours
        fetch_mktcap_all()


_mktcap_thread = threading.Thread(target=mktcap_updater, daemon=True)
_mktcap_thread.start()
last_fetch_time: float = 0
last_good_fetch_time: float = 0   # only updated when ≥1 price was actually received
FETCH_INTERVAL = 30          # seconds between background refreshes
BATCH_SIZE     = 50          # symbols per outer batch loop (rate-limit pacing)


def fetch_symbols_batch(symbols: list[str]) -> dict:
    """Download latest price + volume for NSE symbols.
    Uses 2-day hourly data so the last bar is the true last-traded price,
    avoiding the multi-hour EOD propagation delay in yfinance daily closes.
    yf.download handles Yahoo session/crumb internally and is rate-limit safe.
    """
    result: dict = {}
    if not symbols:
        return result

    tickers_list = [s + ".NS" for s in symbols]
    try:
        df = yf.download(
            tickers_list,
            period="2d",
            interval="1h",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if df.empty:
            return result

        if isinstance(df.columns, pd.MultiIndex):
            close_df = df["Close"]
            vol_df   = df["Volume"]
        else:
            close_df = df[["Close"]].rename(columns={"Close": tickers_list[0]})
            vol_df   = df[["Volume"]].rename(columns={"Volume": tickers_list[0]})

        for ns_sym, sym in zip(tickers_list, symbols):
            try:
                if ns_sym not in close_df.columns:
                    continue
                closes = close_df[ns_sym].dropna()
                if closes.empty:
                    continue
                price = float(closes.iloc[-1])
                # Previous day's last close for change% calculation
                last_date   = closes.index[-1].date()
                prev_closes = closes[closes.index.date < last_date]
                prev = float(prev_closes.iloc[-1]) if not prev_closes.empty else price
                chg  = price - prev
                pct  = (chg / prev * 100) if prev > 0 else 0.0
                # Today's total volume = sum of today's hourly bars
                if ns_sym in vol_df.columns:
                    today_vols = vol_df[ns_sym][vol_df[ns_sym].index.date == last_date]
                    vol = int(today_vols.sum()) if not today_vols.empty else 0
                else:
                    vol = 0
                result[sym] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                    "volume":     vol,
                }
            except Exception:
                pass

    except Exception as e:
        print(f"[WARN] batch fetch error ({len(symbols)} syms): {e}")

    return result


def fetch_bse_batch(symbols: list[str]) -> dict:
    """Fetch current price for BSE symbols using fast_info (per-ticker, avoids batch rate limits).
    Falls back to history() if fast_info has no last_price.
    """
    result: dict = {}
    if not symbols:
        return result

    for sym in symbols:
        ticker_str = sym + ".BO"
        try:
            fi    = yf.Ticker(ticker_str).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            if price is None or price != price:   # None or NaN → try history fallback
                raise ValueError("no last_price in fast_info")
            prev  = prev if (prev and prev == prev) else price
            chg   = price - prev
            pct   = (chg / prev * 100) if prev > 0 else 0.0
            vol   = getattr(fi, "three_month_average_volume", None) or 0
            result[sym] = {
                "price":      round(price, 2),
                "change":     round(chg,   2),
                "change_pct": round(pct,   2),
                "volume":     int(vol),
            }
        except Exception:
            # Fallback: use history() for symbols where fast_info gives no price
            try:
                hist = yf.Ticker(ticker_str).history(period="5d", interval="1d", auto_adjust=True)
                if hist.empty:
                    continue
                closes = hist["Close"].dropna()
                vols   = hist["Volume"].dropna()
                if closes.empty:
                    continue
                price = float(closes.iloc[-1])
                prev  = float(closes.iloc[-2]) if len(closes) >= 2 else price
                chg   = price - prev
                pct   = (chg / prev * 100) if prev > 0 else 0.0
                vol   = int(vols.iloc[-1]) if not vols.empty else 0
                result[sym] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                    "volume":     vol,
                }
            except Exception:
                pass
        time.sleep(0.12)   # gentle per-symbol delay to avoid BSE rate limits

    return result


def fetch_global_batch(symbols: list[str]) -> dict:
    """Download latest daily close + volume for non-NSE symbols (indices, futures, etc.).
    Uses the ticker as-is — no .NS suffix appended.
    """
    result: dict = {}
    if not symbols:
        return result
    tickers = symbols if len(symbols) > 1 else symbols[0]
    try:
        df = yf.download(
            tickers,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if df.empty:
            return result
        if isinstance(df.columns, pd.MultiIndex):
            close_df = df["Close"]
            vol_df   = df["Volume"]
        else:
            sym0 = symbols[0]
            close_df = df[["Close"]].rename(columns={"Close": sym0})
            vol_df   = df[["Volume"]].rename(columns={"Volume": sym0})
        for sym in symbols:
            try:
                if sym not in close_df.columns:
                    continue
                closes = close_df[sym].dropna()
                vols   = vol_df[sym].dropna() if sym in vol_df.columns else pd.Series(dtype=float)
                if closes.empty:
                    continue
                price = float(closes.iloc[-1])
                prev  = float(closes.iloc[-2]) if len(closes) >= 2 else price
                chg   = price - prev
                pct   = (chg / prev * 100) if prev > 0 else 0.0
                vol   = int(vols.iloc[-1]) if not vols.empty else 0
                result[sym] = {"price": round(price, 2), "change": round(chg, 2),
                               "change_pct": round(pct, 2), "volume": vol}
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] global batch fetch error ({len(symbols)} syms): {e}")
    return result


def fetch_indices() -> dict:
    """Fetch NIFTY 50, SENSEX, BANK NIFTY using fast_info."""
    result: dict = {}
    index_map = {"^NSEI": "NIFTY 50", "^BSESN": "SENSEX", "^NSEBANK": "BANK NIFTY"}
    for ticker_str, label in index_map.items():
        try:
            fi    = yf.Ticker(ticker_str).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            if price and prev and prev > 0:
                chg = price - prev
                pct = chg / prev * 100
                result[label] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                }
        except Exception:
            pass
    return result


def update_all_prices() -> None:
    global last_fetch_time, last_good_fetch_time
    t0 = time.time()
    ts = datetime.now().strftime("%H:%M:%S")
    # Split all_symbols into BSE-override and NSE buckets for this run
    bse_direct  = [s for s in all_symbols if s in bse_override]
    nse_symbols = [s for s in all_symbols if s not in bse_override]
    print(f"[{ts}] Refreshing {len(nse_symbols)} NSE + {len(bse_direct)} BSE + {len(global_symbols)} global symbols ...")

    new_prices: dict = {}

    # NSE batch fetch with automatic BSE fallback for misses
    for i in range(0, len(nse_symbols), BATCH_SIZE):
        batch  = [s for s in nse_symbols[i: i + BATCH_SIZE] if dead_symbols.get(s, 0) < DEAD_THRESHOLD]
        if not batch: continue
        result = fetch_symbols_batch(batch)
        new_prices.update(result)
        missed = [s for s in batch if s not in result]
        if missed:
            bse_retry = fetch_bse_batch(missed)
            if bse_retry:
                new_prices.update(bse_retry)
                # Promote these to bse_override so next cycle fetches them directly
                for s in bse_retry:
                    bse_override.add(s)
                    dead_symbols.pop(s, None)
                print(f"[INFO] BSE fallback resolved {list(bse_retry.keys())}")
            # Track symbols that failed both NSE and BSE
            still_missed = [s for s in missed if s not in new_prices]
            for s in still_missed:
                dead_symbols[s] = dead_symbols.get(s, 0) + 1
                if dead_symbols[s] == DEAD_THRESHOLD:
                    print(f"[WARN] Marking {s} as dead after {DEAD_THRESHOLD} consecutive misses")
        # Reset miss count for anything that resolved
        for s in result:
            dead_symbols.pop(s, None)
        time.sleep(0.3)

    # BSE-override symbols: fetch directly via .BO with NSE fallback for misses
    for i in range(0, len(bse_direct), BATCH_SIZE):
        batch  = [s for s in bse_direct[i: i + BATCH_SIZE] if dead_symbols.get(s, 0) < DEAD_THRESHOLD]
        if not batch: continue
        result = fetch_bse_batch(batch)
        new_prices.update(result)
        missed = [s for s in batch if s not in result]
        if missed:
            nse_retry = fetch_symbols_batch(missed)
            if nse_retry:
                new_prices.update(nse_retry)
                # Demote from bse_override if NSE actually works
                for s in nse_retry:
                    bse_override.discard(s)
                    dead_symbols.pop(s, None)
                print(f"[INFO] NSE fallback resolved {list(nse_retry.keys())}")
            still_missed = [s for s in missed if s not in new_prices]
            for s in still_missed:
                dead_symbols[s] = dead_symbols.get(s, 0) + 1
                if dead_symbols[s] == DEAD_THRESHOLD:
                    print(f"[WARN] Marking {s} as dead after {DEAD_THRESHOLD} consecutive misses")
        for s in result:
            dead_symbols.pop(s, None)
        time.sleep(0.3)

    # Fetch non-NSE global symbols (commodities, foreign indices, futures)
    if global_symbols:
        for i in range(0, len(global_symbols), BATCH_SIZE):
            batch  = global_symbols[i: i + BATCH_SIZE]
            result = fetch_global_batch(batch)
            new_prices.update(result)
            time.sleep(0.2)

    new_indices = fetch_indices()

    with cache_lock:
        price_cache.update(new_prices)
        indices_cache.update(new_indices)
        last_fetch_time = time.time()
        if new_prices:          # only mark a good fetch when we actually got data
            last_good_fetch_time = last_fetch_time

    _save_price_cache()   # persist to disk for instant reload on next restart

    elapsed = round(time.time() - t0, 1)
    status  = f"Updated {len(new_prices)} prices" if new_prices else "NO DATA (network/rate-limit?)"
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {status} in {elapsed}s")


def background_updater() -> None:
    """Runs forever: initial fetch then refresh every FETCH_INTERVAL seconds."""
    _load_price_cache()      # <-- immediately serve stale-but-valid prices from last run
    global last_fetch_time
    # Mark last_fetch_time as "old" so UI shows stale indicator, not null
    with cache_lock:
        if price_cache and last_fetch_time == 0:
            last_fetch_time = time.time() - FETCH_INTERVAL  # treat as just-expired
            last_good_fetch_time = last_fetch_time
    update_all_prices()
    while True:
        time.sleep(FETCH_INTERVAL)
        update_all_prices()


# Start background thread immediately on import (use_reloader=False required)
_updater_thread = threading.Thread(target=background_updater, daemon=True)
_updater_thread.start()


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/portfolio")
def portfolio_page():
    resp = make_response(render_template("portfolio.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/panels")
def api_panels():
    with cache_lock:
        cache_copy   = dict(price_cache)
        idx_copy     = dict(indices_cache)
        fetch_age    = round(time.time() - last_fetch_time) if last_fetch_time else None
        good_age     = round(time.time() - last_good_fetch_time) if last_good_fetch_time else None
        IST_good     = (datetime.fromtimestamp(last_good_fetch_time, tz=timezone(timedelta(hours=5,minutes=30)))
                        .strftime("%H:%M:%S") if last_good_fetch_time else None)

    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)

    with mktcap_lock:
        mktcap_copy = dict(mktcap_cache)

    result = []
    for i, panel in enumerate(panels):
        stocks_out = []
        for s in panel["stocks"]:
            sym  = s["symbol"]
            data = cache_copy.get(sym)
            mc   = mktcap_copy.get(sym)   # market cap in INR (None if not yet fetched)
            if data:
                stocks_out.append({
                    "symbol":     sym,
                    "name":       s["name"],
                    "price":      data["price"],
                    "change":     data["change"],
                    "change_pct": data["change_pct"],
                    "volume":     data.get("volume", 0),
                    "market_cap": mc,
                })
            else:
                stocks_out.append({
                    "symbol":     sym,
                    "name":       s["name"],
                    "price":      None,
                    "change":     None,
                    "change_pct": None,
                    "volume":     None,
                    "market_cap": mc,
                })
        result.append({"sector": panel["sector"], "stocks": stocks_out,
                       "id":     panel["id"],
                       "page":   panel.get("page", i // PAGE_SIZE),
                       "height": panel.get("height", 1),
                       "mode":   panel.get("mode", "nse")})

    _customs_snap = _load_customs()
    _max_used_pg  = max((p.get("page", j // PAGE_SIZE) for j, p in enumerate(panels)), default=0)
    _page_count   = max(_max_used_pg + 1, _customs_snap.get("__page_count__", 0))
    return jsonify({
        "panels":       result,
        "indices":      idx_copy,
        "timestamp":    now.strftime("%H:%M:%S IST"),
        "date":         now.strftime("%d %b %Y"),
        "fetch_age":    fetch_age,
        "good_age":     good_age,
        "last_good_ts": IST_good,
        "next_refresh": max(0, FETCH_INTERVAL - (fetch_age or FETCH_INTERVAL)),
        "page_count":   _page_count,
        "page_names":   _customs_snap.get("__page_names__", {}),
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force an immediate price refresh (runs in background)."""
    t = threading.Thread(target=update_all_prices, daemon=True)
    t.start()
    return jsonify({"status": "refresh triggered"})


@app.route("/api/panel/<int:pi>/add", methods=["POST"])
def api_add_ticker(pi: int):
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    # Auto-detect explicit exchange suffix typed by user (e.g. RELIANCE.NS or 500325.BO)
    # Override mode resolution based on the suffix so any panel accepts either.
    mode = panels[pi].get("mode", "nse")
    is_bse = False
    if symbol.endswith(".BO"):
        symbol = symbol[:-3]   # store without suffix
        is_bse = True
    elif symbol.endswith(".NS"):
        symbol = symbol[:-3]   # store without suffix
        is_bse = False
    elif mode == "bse":
        is_bse = True

    existing = {s["symbol"] for s in panels[pi]["stocks"]}
    if symbol in existing:
        return jsonify({"error": f"{symbol} is already in this panel"}), 409
    # Check cache first (may already be fetched by background thread)
    with cache_lock:
        cached = dict(price_cache.get(symbol, {}))
    if not cached:
        # Try primary exchange first, then fallback to the other
        if mode == "global":
            result = fetch_global_batch([symbol])
        elif is_bse:
            result = fetch_bse_batch([symbol])
            if not result:
                result = fetch_symbols_batch([symbol])  # NSE fallback
                if result: is_bse = False  # NSE worked, don't mark as BSE
        else:
            result = fetch_symbols_batch([symbol])
            if not result:
                result = fetch_bse_batch([symbol])   # BSE fallback
                if result: is_bse = True
        if result:
            with cache_lock:
                price_cache.update(result)
            cached = result.get(symbol, {})
    new_stock = {"symbol": symbol, "name": symbol}
    panels[pi]["stocks"].append(new_stock)
    # All non-global symbols go into all_symbols; BSE-detected ones also go into bse_override
    if mode == "global":
        if symbol not in global_symbols:
            global_symbols.append(symbol)
    else:
        if symbol not in all_symbols:
            all_symbols.append(symbol)
        if is_bse:
            bse_override.add(symbol)
        else:
            bse_override.discard(symbol)
    pk = panels[pi]["id"]   # stable id — survives panel reordering
    if pk in raw_sheet_data:
        # Built-in panel: sheet_data.json is the single source of truth
        if not any(s["symbol"] == symbol for s in raw_sheet_data[pk]):
            raw_sheet_data[pk].append({"symbol": symbol, "name": symbol})
            _save_sheet_data()
    else:
        # User-created panel: persist via user_customizations.json
        customs = _load_customs()
        customs.setdefault(pk, {"added": [], "removed": []})
        if not any(s["symbol"] == symbol for s in customs[pk].get("added", [])):
            customs[pk].setdefault("added", []).append(new_stock)
        customs[pk]["removed"] = [s for s in customs[pk].get("removed", []) if s != symbol]
        _save_customs(customs)
    pdata = cached
    return jsonify({
        "status":     "ok",
        "symbol":     symbol,
        "price":      pdata.get("price") if pdata else None,
        "change_pct": pdata.get("change_pct") if pdata else None,
    })


@app.route("/api/panel/<int:pi>/rename", methods=["POST"])
def api_rename_panel(pi: int):
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        return jsonify({"error": "Invalid name"}), 400
    panels[pi]["sector"] = name
    customs = _load_customs()
    pk = panels[pi]["id"]   # stable id
    customs.setdefault(pk, {"added": [], "removed": []})
    customs[pk]["sector_name"] = name
    _save_customs(customs)
    return jsonify({"status": "ok", "sector": name})


@app.route("/api/panel/<int:pi>/remove", methods=["POST"])
def api_remove_ticker(pi: int):
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "No symbol provided"}), 400
    panels[pi]["stocks"] = [s for s in panels[pi]["stocks"] if s["symbol"] != symbol]
    pk = panels[pi]["id"]   # stable id
    if pk in raw_sheet_data:
        # Built-in panel: sheet_data.json is the single source of truth
        raw_sheet_data[pk] = [s for s in raw_sheet_data[pk] if s["symbol"] != symbol]
        _save_sheet_data()
    else:
        # User-created panel: persist via user_customizations.json
        customs = _load_customs()
        customs.setdefault(pk, {"added": [], "removed": []})
        if symbol not in customs[pk].get("removed", []):
            customs[pk].setdefault("removed", []).append(symbol)
        customs[pk]["added"] = [s for s in customs[pk].get("added", []) if s["symbol"] != symbol]
        _save_customs(customs)
    return jsonify({"status": "ok", "removed": symbol})


@app.route("/api/panel/<int:pi>/edit", methods=["POST"])
def api_edit_ticker(pi: int):
    """Rename a ticker symbol in-place (preserves position in the stock list)."""
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body       = request.get_json(force=True, silent=True) or {}
    old_symbol = (body.get("old_symbol") or "").strip().upper()
    new_symbol = (body.get("new_symbol") or "").strip().upper()
    if not old_symbol or not new_symbol:
        return jsonify({"error": "Both old_symbol and new_symbol are required"}), 400
    if len(new_symbol) > 20:
        return jsonify({"error": "Symbol too long"}), 400
    if old_symbol == new_symbol:
        return jsonify({"status": "ok", "symbol": new_symbol})
    existing = {s["symbol"] for s in panels[pi]["stocks"]}
    if old_symbol not in existing:
        return jsonify({"error": f"{old_symbol} not found in this panel"}), 404
    if new_symbol in existing:
        return jsonify({"error": f"{new_symbol} is already in this panel"}), 409
    # Rename in-place so position is preserved
    for s in panels[pi]["stocks"]:
        if s["symbol"] == old_symbol:
            s["symbol"] = new_symbol
            s["name"]   = new_symbol
            break
    # Track in the correct refresh list
    mode = panels[pi].get("mode", "nse")
    if mode == "global":
        sym_list = global_symbols
    else:
        sym_list = all_symbols
    if old_symbol in sym_list and new_symbol not in sym_list:
        sym_list[sym_list.index(old_symbol)] = new_symbol
    elif new_symbol not in sym_list:
        sym_list.append(new_symbol)
    # Carry over bse_override if old symbol had it; new symbol inherits panel mode
    if old_symbol in bse_override:
        bse_override.discard(old_symbol)
        if mode == "bse":
            bse_override.add(new_symbol)
    pk = panels[pi]["id"]
    if pk in raw_sheet_data:
        # Built-in panel: sheet_data.json is the single source of truth
        for s in raw_sheet_data[pk]:
            if s["symbol"] == old_symbol:
                s["symbol"] = new_symbol
                s["name"]   = new_symbol
        if not any(s["symbol"] == new_symbol for s in raw_sheet_data[pk]):
            raw_sheet_data[pk].append({"symbol": new_symbol, "name": new_symbol})
        raw_sheet_data[pk] = [s for s in raw_sheet_data[pk] if s["symbol"] != old_symbol or s["symbol"] == new_symbol]
        _save_sheet_data()
    else:
        # User-created panel: persist via user_customizations.json
        customs = _load_customs()
        customs.setdefault(pk, {"added": [], "removed": []})
        for s in customs[pk].get("added", []):
            if s["symbol"] == old_symbol:
                s["symbol"] = new_symbol
                s["name"]   = new_symbol
        if not any(s["symbol"] == new_symbol for s in customs[pk].get("added", [])):
            customs[pk].setdefault("added", []).append({"symbol": new_symbol, "name": new_symbol})
        if old_symbol not in customs[pk].get("removed", []):
            customs[pk].setdefault("removed", []).append(old_symbol)
        customs[pk]["removed"] = [s for s in customs[pk]["removed"] if s != new_symbol]
        _save_customs(customs)
    # Best-effort immediate fetch of new symbol
    if mode == "global":
        fetcher = fetch_global_batch
    elif mode == "bse":
        fetcher = fetch_bse_batch
    else:
        fetcher = fetch_symbols_batch
    result  = fetcher([new_symbol])
    if result:
        with cache_lock:
            price_cache.update(result)
    pdata = price_cache.get(new_symbol, {})
    return jsonify({
        "status":     "ok",
        "old_symbol": old_symbol,
        "symbol":     new_symbol,
        "price":      pdata.get("price"),
        "change_pct": pdata.get("change_pct"),
    })


@app.route("/api/panel/new", methods=["POST"])
def api_new_panel():
    """Create a brand-new empty sector panel (no Excel sheet required)."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        return jsonify({"error": "Name must be 1-60 characters"}), 400
    mode = (body.get("mode") or "nse").strip().lower()
    if mode not in ("nse", "global", "bse"):
        mode = "nse"
    # Place on the last currently-used page
    target_pg = max((p.get("page", j // PAGE_SIZE) for j, p in enumerate(panels)), default=0) if panels else 0
    panel_id  = f"_uc_{int(time.time() * 1000)}"
    new_panel = {"sector": name, "stocks": [], "id": panel_id, "page": target_pg, "mode": mode}
    panels.append(new_panel)
    customs = _load_customs()
    customs[panel_id] = {"user_created": True, "sector_name": name, "mode": mode, "added": [], "removed": []}
    customs["__order__"] = [p["id"] for p in panels]
    customs.setdefault("__pages__", {})[panel_id] = target_pg
    _save_customs(customs)
    return jsonify({"status": "ok", "id": panel_id, "index": len(panels) - 1, "sector": name, "page": target_pg})


@app.route("/api/panel/<int:pi>/delete", methods=["POST"])
def api_delete_panel(pi: int):
    """Permanently delete a panel (any mode). Removes from display and customisations."""
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    panel = panels.pop(pi)
    pid   = panel["id"]
    # Clean up global_symbols / all_symbols entries that only belong to this panel
    # (heuristic: remove from tracking if no other panel uses the symbol)
    still_used: set[str] = set()
    for p in panels:
        for s in p.get("stocks", []):
            still_used.add(s["symbol"])
    if panel.get("mode") == "global":
        global global_symbols
        global_symbols = [s for s in global_symbols if s in still_used]
    else:
        global all_symbols
        all_symbols = [s for s in all_symbols if s in still_used]
        for s in list(bse_override):
            if s not in still_used:
                bse_override.discard(s)
    customs = _load_customs()
    customs.pop(pid, None)
    customs["__order__"]  = [p["id"] for p in panels]
    customs.get("__pages__",  {}).pop(pid, None)
    customs.get("__heights__", {}).pop(pid, None)
    # Recalculate page_count from remaining panels
    max_pg = max((p.get("page", 0) for p in panels), default=0) if panels else 0
    customs["__page_count__"] = max(max_pg + 1, 1)
    _save_customs(customs)
    return jsonify({"status": "ok", "deleted": pid})


@app.route("/api/panel/swap", methods=["POST"])
def api_swap_panels():
    """Swap two panels by index (for drag-and-drop reordering)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        a, b = int(body.get("a", -1)), int(body.get("b", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid indices"}), 400
    if not (0 <= a < len(panels)) or not (0 <= b < len(panels)) or a == b:
        return jsonify({"error": "Invalid panel indices"}), 400
    # Swap panels; customisations keyed by stable id — no re-keying needed
    panels[a], panels[b] = panels[b], panels[a]
    customs = _load_customs()
    customs["__order__"] = [p["id"] for p in panels]
    _save_customs(customs)
    return jsonify({"status": "ok", "swapped": [a, b]})


@app.route("/api/symbol/move", methods=["POST"])
def api_move_symbol():
    """Move a symbol from one panel to another."""
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:
        from_pi = int(body.get("from_pi", -1))
        to_pi   = int(body.get("to_pi",   -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid indices"}), 400
    if not symbol:
        return jsonify({"error": "No symbol"}), 400
    if not (0 <= from_pi < len(panels)) or not (0 <= to_pi < len(panels)) or from_pi == to_pi:
        return jsonify({"error": "Invalid panel indices"}), 400
    src = panels[from_pi]; dst = panels[to_pi]
    stock = next((s for s in src["stocks"] if s["symbol"] == symbol), None)
    if not stock:
        return jsonify({"error": f"{symbol} not in source panel"}), 404
    if any(s["symbol"] == symbol for s in dst["stocks"]):
        return jsonify({"error": f"{symbol} already in destination panel"}), 409
    # Move in-memory
    src["stocks"] = [s for s in src["stocks"] if s["symbol"] != symbol]
    dst["stocks"].append(stock)
    # Persist: remove from source sheet_data, add to dest sheet_data
    src_pk = src["id"]; dst_pk = dst["id"]
    changed = False
    if src_pk in raw_sheet_data:
        raw_sheet_data[src_pk] = [s for s in raw_sheet_data[src_pk] if s["symbol"] != symbol]
        changed = True
    if dst_pk in raw_sheet_data:
        if not any(s["symbol"] == symbol for s in raw_sheet_data[dst_pk]):
            raw_sheet_data[dst_pk].append({"symbol": symbol, "name": stock.get("name", symbol)})
            changed = True
    if changed:
        _save_sheet_data()
    # Also handle user-created panels (not in raw_sheet_data)
    if src_pk not in raw_sheet_data or dst_pk not in raw_sheet_data:
        customs = _load_customs()
        if src_pk not in raw_sheet_data:
            customs.setdefault(src_pk, {"added": [], "removed": []})
            customs[src_pk]["added"] = [s for s in customs[src_pk].get("added", []) if s["symbol"] != symbol]
            if symbol not in customs[src_pk].get("removed", []):
                customs[src_pk].setdefault("removed", []).append(symbol)
        if dst_pk not in raw_sheet_data:
            customs.setdefault(dst_pk, {"added": [], "removed": []})
            if not any(s["symbol"] == symbol for s in customs[dst_pk].get("added", [])):
                customs[dst_pk].setdefault("added", []).append({"symbol": symbol, "name": stock.get("name", symbol)})
            customs[dst_pk]["removed"] = [s for s in customs[dst_pk].get("removed", []) if s != symbol]
        _save_customs(customs)
    return jsonify({"status": "ok", "symbol": symbol, "from": from_pi, "to": to_pi})


@app.route("/api/panel/move", methods=["POST"])
def api_move_panel():
    """Move a panel from index `from` to index `to`, shifting panels in between."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        frm = int(body.get("from", -1))
        to  = int(body.get("to",   -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid indices"}), 400
    if not (0 <= frm < len(panels)) or not (0 <= to <= len(panels)) or frm == to:
        return jsonify({"error": "Invalid panel indices"}), 400
    # Move; customisations keyed by stable id — no re-keying needed
    panel = panels.pop(frm)
    panels.insert(to, panel)
    customs = _load_customs()
    customs["__order__"] = [p["id"] for p in panels]
    _save_customs(customs)
    return jsonify({"status": "ok", "from": frm, "to": to})


@app.route("/api/panel/<int:pi>/setpage", methods=["POST"])
def api_set_panel_page(pi: int):
    """Assign a panel to an explicit display page (enables arbitrary page placement)."""
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    try:
        page = int(body.get("page", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid page"}), 400
    if page < 0:
        return jsonify({"error": "Page must be >= 0"}), 400
    panels[pi]["page"] = page
    customs = _load_customs()
    customs.setdefault("__pages__", {})[panels[pi]["id"]] = page
    # Ensure page_count covers the new page
    max_used = max((p.get("page", j // PAGE_SIZE) for j, p in enumerate(panels)), default=0)
    customs["__page_count__"] = max(max_used + 1, customs.get("__page_count__", 0), page + 1)
    _save_customs(customs)
    return jsonify({"status": "ok", "page": page})


@app.route("/api/panel/<int:pi>/setheight", methods=["POST"])
def api_set_panel_height(pi: int):
    """Set the row-span height of a panel (1-4 units)."""
    if pi < 0 or pi >= len(panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    try:
        height = int(body.get("height", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid height"}), 400
    height = max(1, min(4, height))
    panels[pi]["height"] = height
    customs = _load_customs()
    customs.setdefault("__heights__", {})[panels[pi]["id"]] = height
    _save_customs(customs)
    return jsonify({"status": "ok", "height": height})


@app.route("/api/page/add", methods=["POST"])
def api_add_page():
    """Increment stored page count to allow empty pages."""
    customs = _load_customs()
    max_used = max((p.get("page", j // PAGE_SIZE) for j, p in enumerate(panels)), default=0)
    current  = max(max_used + 1, customs.get("__page_count__", 0))
    customs["__page_count__"] = current + 1
    _save_customs(customs)
    return jsonify({"status": "ok", "page_count": customs["__page_count__"]})


@app.route("/api/page/<int:pg>/delete", methods=["POST"])
def api_delete_page(pg: int):
    """Delete a page only if no panels are assigned to it."""
    occupied = [p for p in panels if p.get("page", 0) == pg]
    if occupied:
        return jsonify({"error": f"Page has {len(occupied)} sector(s) on it — move them first"}), 409
    customs = _load_customs()
    current  = customs.get("__page_count__", 0)
    # Shift panels on higher pages down by 1, update saved page map
    page_map = customs.get("__pages__", {})
    for p in panels:
        old_pg = p.get("page", 0)
        if old_pg > pg:
            p["page"] = old_pg - 1
            page_map[p["id"]] = old_pg - 1
    customs["__pages__"] = page_map
    customs["__page_count__"] = max(0, current - 1)
    _save_customs(customs)
    max_used = max((p.get("page", 0) for p in panels), default=0) if panels else 0
    return jsonify({"status": "ok", "page_count": max(max_used + 1, customs["__page_count__"])})


@app.route("/api/page/<int:pg>/rename", methods=["POST"])
def api_rename_page(pg: int):
    """Set a custom name for a page tab."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if len(name) > 40:
        return jsonify({"error": "Name too long"}), 400
    customs = _load_customs()
    page_names = customs.setdefault("__page_names__", {})
    if name:
        page_names[str(pg)] = name
    else:
        page_names.pop(str(pg), None)  # empty string = reset to default
    _save_customs(customs)
    return jsonify({"status": "ok", "page": pg, "name": name})


@app.route("/api/page/reorder", methods=["POST"])
def api_reorder_pages():
    """Reorder pages: body = {"order": [2, 0, 1, ...]} — new index = position in list."""
    body  = request.get_json(force=True, silent=True) or {}
    order = body.get("order", [])
    num_pages = max((p.get("page", 0) for p in panels), default=0) + 1
    if sorted(order) != list(range(num_pages)):
        return jsonify({"error": "order must be a permutation of all page indices"}), 400
    # Build mapping: old page index → new page index
    remap = {old: new for new, old in enumerate(order)}
    for p in panels:
        old_pg = p.get("page", 0)
        p["page"] = remap.get(old_pg, old_pg)
    customs = _load_customs()
    # Remap __pages__
    page_map = customs.get("__pages__", {})
    customs["__pages__"] = {pid: remap.get(pg, pg) for pid, pg in page_map.items()}
    # Remap __page_names__
    old_names = customs.get("__page_names__", {})
    customs["__page_names__"] = {str(remap[int(k)]): v for k, v in old_names.items() if int(k) in remap}
    _save_customs(customs)
    return jsonify({"status": "ok", "remap": remap})


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PORTFOLIO  — transaction-based, derives unrealized + realized P&L
# Format per entry:
#   {"symbol":"RELIANCE","name":"...","exchange":"nse",
#    "transactions":[{"date":"2026-01-01","type":"buy","shares":100,"price":2800},...]}
# Legacy flat format (shares/avg_cost, no transactions) is auto-migrated on read.
# ---------------------------------------------------------------------------
PORTFOLIO_FILE = "portfolio.json"
CASH_FILE      = "cash.json"
CA_FILE        = "corporate_actions.json"


# ── Corporate Actions helpers ─────────────────────────────────
# Supported types and what they mean:
#   merger / amalgamation : from_symbol → to_symbol, ratio = new shares per 1 old share
#   name_change           : from_symbol → to_symbol, ratio = 1 (same share count)
#   demerger / spinoff    : from_symbol → to_symbol (child), ratio = child shares per 1 parent share
#                           cost_allocation_pct = % of parent cost basis that goes to child
#   split / subdivision   : symbol, ratio = N  (1 old → N new, avg_cost ÷ N)
#   bonus                 : symbol, ratio = X  (X bonus shares per 1 held, avg_cost diluted)

def _load_ca() -> list:
    try:
        with open(CA_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_ca(actions: list) -> None:
    with open(CA_FILE, "w", encoding="utf-8") as fh:
        json.dump(actions, fh, indent=2, ensure_ascii=False)


def _derive_position_with_ca(entry: dict, ca_list: list) -> dict:
    """
    Average-cost method with inline split/bonus adjustments from corporate_actions.
    Mergers/demergers are handled separately at CAGR-chain level.
    """
    sym  = str(entry.get("symbol", "")).strip().upper()
    txs  = sorted(entry.get("transactions", []), key=lambda t: t.get("date", ""))

    # Intra-symbol CAs: split, subdivision, bonus only
    intra = sorted(
        [ca for ca in ca_list
         if ca.get("type", "").lower() in ("split", "subdivision", "bonus")
         and ca.get("symbol", "").strip().upper() == sym],
        key=lambda ca: ca.get("date", "")
    )

    # Unified chronological timeline
    timeline = [("tx", tx.get("date", ""), tx) for tx in txs] + \
               [("ca", ca.get("date", ""), ca) for ca in intra]
    timeline.sort(key=lambda x: x[1])

    open_shares     = 0.0
    avg_cost        = 0.0
    realized_pnl    = 0.0
    sold_cost_basis = 0.0

    for kind, _, evt in timeline:
        if kind == "tx":
            ttype = evt.get("type", "buy").lower()
            sh    = float(evt.get("shares", 0))
            pr    = float(evt.get("price",  0))
            if ttype == "buy":
                total       = open_shares * avg_cost + sh * pr
                open_shares += sh
                avg_cost    = total / open_shares if open_shares else 0.0
            elif ttype == "sell":
                realized_pnl    += (pr - avg_cost) * sh
                sold_cost_basis += avg_cost * sh
                open_shares      = max(0.0, open_shares - sh)
        else:  # CA event: split or bonus
            ca_type = evt.get("type", "").lower()
            ratio   = float(evt.get("ratio", 1))
            if ratio <= 0 or open_shares <= 0:
                continue
            if ca_type in ("split", "subdivision"):
                # 1 share → ratio shares; avg_cost ÷ ratio
                open_shares = round(open_shares * ratio, 6)
                avg_cost    = round(avg_cost    / ratio, 6) if avg_cost else 0.0
            elif ca_type == "bonus":
                # ratio bonus shares per 1 held; total invested unchanged
                new_sh      = open_shares * ratio
                total_cost  = open_shares * avg_cost
                open_shares = round(open_shares + new_sh, 6)
                avg_cost    = round(total_cost / open_shares, 6) if open_shares else 0.0

    return {
        "open_shares":     round(open_shares,     6),
        "avg_cost":        round(avg_cost,         4),
        "realized_pnl":    round(realized_pnl,     2),
        "sold_cost_basis": round(sold_cost_basis,  2),
    }


def _resolve_cagr_inception(symbol: str, ca_list: list, all_entries: list) -> str | None:
    """
    Walk backwards through merger / amalgamation / name_change / demerger chain
    to find the earliest first-buy date.  Returns date string or None.
    Handles multi-hop chains (e.g. A → B → C where C is the current symbol).
    """
    entries_by_sym = {str(e.get("symbol", "")).strip().upper(): e for e in all_entries}
    visited = set()
    cur     = symbol.strip().upper()
    earliest: str | None = None

    while cur and cur not in visited:
        visited.add(cur)
        pred_ca = next(
            (ca for ca in ca_list
             if ca.get("type", "").lower() in
                ("merger", "amalgamation", "name_change", "demerger", "spinoff", "spin-off")
             and ca.get("to_symbol", "").strip().upper() == cur),
            None
        )
        if not pred_ca:
            break
        pred_sym   = pred_ca.get("from_symbol", "").strip().upper()
        pred_entry = entries_by_sym.get(pred_sym)
        if pred_entry:
            buy_dates = [t.get("date", "") for t in pred_entry.get("transactions", [])
                         if t.get("type", "buy").lower() == "buy" and t.get("date", "")]
            if buy_dates:
                fd = min(buy_dates)
                if earliest is None or fd < earliest:
                    earliest = fd
        cur = pred_sym

    return earliest


def _load_cash() -> list:
    try:
        with open(CASH_FILE) as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_cash(txs: list) -> None:
    with open(CASH_FILE, "w") as fh:
        json.dump(txs, fh, indent=2)


def _calc_cash_balance(txs: list) -> float:
    b = 0.0
    for t in txs:
        amt = float(t.get("amount", 0))
        b += amt if t.get("type") == "deposit" else -amt
    return round(b, 2)


def _load_portfolio() -> list:
    if not os.path.exists(PORTFOLIO_FILE):
        return []
    try:
        with open(PORTFOLIO_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_portfolio(entries: list) -> None:
    with open(PORTFOLIO_FILE, "w") as fh:
        json.dump(entries, fh, indent=2)


def _migrate_entry(entry: dict) -> dict:
    """If entry is legacy flat format (shares/avg_cost), convert to transactions."""
    if "transactions" not in entry and entry.get("shares") and entry.get("avg_cost"):
        entry = dict(entry)
        entry["transactions"] = [{
            "date":   entry.get("date", "2000-01-01"),
            "type":   "buy",
            "shares": float(entry["shares"]),
            "price":  float(entry["avg_cost"]),
        }]
        entry.pop("shares", None)
        entry.pop("avg_cost", None)
    return entry


def _derive_position(entry: dict) -> dict:
    """Derive open_shares, avg_cost, realized_pnl using average-cost method."""
    txs = sorted(entry.get("transactions", []), key=lambda t: t.get("date", ""))
    open_shares      = 0.0
    avg_cost         = 0.0
    realized_pnl     = 0.0
    sold_cost_basis  = 0.0   # cost of shares that were sold, for realized %
    for tx in txs:
        ttype  = tx.get("type", "buy").lower()
        sh     = float(tx.get("shares", 0))
        pr     = float(tx.get("price",  0))
        if ttype == "buy":
            total        = open_shares * avg_cost + sh * pr
            open_shares += sh
            avg_cost     = total / open_shares if open_shares else 0.0
        elif ttype == "sell":
            realized_pnl    += (pr - avg_cost) * sh
            sold_cost_basis += avg_cost * sh
            open_shares      = max(0.0, open_shares - sh)
    return {
        "open_shares":      round(open_shares,     6),
        "avg_cost":         round(avg_cost,         4),
        "realized_pnl":     round(realized_pnl,     2),
        "sold_cost_basis":  round(sold_cost_basis,  2),
    }


@app.route("/api/portfolio")
def api_portfolio():
    raw       = _load_portfolio()
    entries   = [_migrate_entry(e) for e in raw]
    if not entries:
        return jsonify({"positions": [], "totals": {}})

    ca_list        = _load_ca()
    cache_snap     = dict(price_cache)
    total_mktval   = 0.0
    total_invested = 0.0
    total_realized = 0.0
    enriched       = []

    for entry in entries:
        sym  = str(entry.get("symbol", "")).strip().upper()
        name = entry.get("name") or sym
        txs  = entry.get("transactions", [])
        pos  = _derive_position_with_ca(entry, ca_list)
        open_shares      = pos["open_shares"]
        avg_cost         = pos["avg_cost"]
        realized_pnl     = pos["realized_pnl"]
        sold_cost_basis  = pos["sold_cost_basis"]

        pdata      = cache_snap.get(sym, {})
        ltp        = pdata.get("price")
        change_pct = pdata.get("change_pct")
        invested   = open_shares * avg_cost
        mkt_value  = (open_shares * ltp) if ltp is not None else None
        unreal_pnl = (mkt_value - invested) if mkt_value is not None else None
        unreal_pct = (unreal_pnl / invested * 100) if (unreal_pnl is not None and invested) else None
        total_pnl  = ((unreal_pnl or 0) + realized_pnl) if unreal_pnl is not None else None
        # Absolute day gain on position
        if ltp and change_pct is not None and open_shares > 0:
            day_gain_abs = round(open_shares * ltp * change_pct / (100.0 + change_pct), 2)
        else:
            day_gain_abs = None
        # Realized gain %
        realized_pct = round(realized_pnl / sold_cost_basis * 100, 2) if sold_cost_basis else None
        # Status
        status = "Open" if open_shares > 0 else "Closed"

        if mkt_value  is not None: total_mktval   += mkt_value
        total_invested += invested
        total_realized += realized_pnl

        # Build per-tx realized P&L snapshot for display
        tx_rows = []
        _sh = 0.0; _ac = 0.0
        for tx in sorted(txs, key=lambda t: t.get("date", "")):
            ttype = tx.get("type", "buy").lower()
            sh    = float(tx.get("shares", 0))
            pr    = float(tx.get("price",  0))
            tx_pnl = None
            if ttype == "buy":
                total   = _sh * _ac + sh * pr
                _sh    += sh
                _ac     = total / _sh if _sh else 0.0
            elif ttype == "sell":
                tx_pnl  = round((pr - _ac) * sh, 2)
                _sh     = max(0.0, _sh - sh)
            tx_rows.append({
                "date":   tx.get("date", ""),
                "type":   ttype,
                "shares": sh,
                "price":  pr,
                "value":  round(sh * pr, 2),
                "pnl":    tx_pnl,
            })

        # Per-stock CAGR — resolve inception through CA chain first
        today_d    = datetime.now(timezone.utc).date()
        buy_dates  = [t.get("date","") for t in txs if t.get("type","buy").lower()=="buy"  and t.get("date","")]
        sell_dates = [t.get("date","") for t in txs if t.get("type","buy").lower()=="sell" and t.get("date","")]
        first_buy  = min(buy_dates)  if buy_dates  else None
        last_sell  = max(sell_dates) if sell_dates else None
        # Walk the corporate-action chain (merger / demerger / name_change) to find
        # the true inception date; falls back to this symbol's own first buy if no chain.
        ca_inception   = _resolve_cagr_inception(sym, ca_list, entries)
        inception_date = ca_inception  # exposed in API so UI can show tooltip/marker
        cagr_start     = ca_inception or first_buy
        cagr = None
        try:
            if cagr_start:
                fbd = datetime.strptime(cagr_start, "%Y-%m-%d").date()
                if open_shares > 0 and ltp is not None and avg_cost > 0:
                    days = (today_d - fbd).days
                    if days >= 30:
                        # Use (ltp / avg_cost) — avg_cost already reflects predecessor
                        # cost basis transferred via merger tx; only inception *date* changes.
                        cagr = round(((ltp / avg_cost) ** (365.25 / days) - 1) * 100, 2)
                elif open_shares == 0 and sold_cost_basis > 0 and last_sell:
                    lsd  = datetime.strptime(last_sell, "%Y-%m-%d").date()
                    days = (lsd - fbd).days
                    if days >= 30:
                        total_ret = (sold_cost_basis + realized_pnl) / sold_cost_basis
                        cagr = -100.0 if total_ret <= 0 else round((total_ret ** (365.25 / days) - 1) * 100, 2)
        except Exception:
            cagr = None
        if cagr is not None and abs(cagr) > 9999:
            cagr = None   # data anomaly (e.g. erroneous tx price)

        enriched.append({
            "symbol":       sym,
            "name":         name,
            "open_shares":  open_shares,
            "avg_cost":     avg_cost,
            "ltp":          ltp,
            "change_pct":   change_pct,
            "invested":     round(invested,    2),
            "mkt_value":    round(mkt_value,   2) if mkt_value   is not None else None,
            "unreal_pnl":   round(unreal_pnl,  2) if unreal_pnl  is not None else None,
            "unreal_pct":   round(unreal_pct,  2) if unreal_pct  is not None else None,
            "realized_pnl":  realized_pnl,
            "realized_pct":  realized_pct,
            "day_gain_abs":  day_gain_abs,
            "status":        status,
            "needs_data":    bool(entry.get("needs_data")),
            "total_pnl":     round(total_pnl, 2) if total_pnl is not None else None,
            "cagr":          cagr,
            "inception_date": inception_date,   # non-null = CA chain used; shown as * in CAGR cell
            "transactions":  tx_rows,
        })

    total_unreal    = total_mktval - total_invested if total_mktval else None
    total_unreal_pct = (total_unreal / total_invested * 100) if (total_unreal is not None and total_invested) else None

    cash_txs     = _load_cash()
    cash_balance = _calc_cash_balance(cash_txs)
    port_total   = round(total_mktval + cash_balance, 2)
    cash_pct     = round(cash_balance / port_total * 100, 2) if port_total else None
    invested_pct = round(total_mktval  / port_total * 100, 2) if port_total else None

    # alloc% relative to full portfolio (equity + cash) — cash dilutes each stock's weight
    for row in enriched:
        mv = row["mkt_value"]
        row["alloc_pct"] = round(mv / port_total * 100, 1) if (mv is not None and port_total) else None

    # Market-value weighted CAGR (open positions with valid price only)
    _open_cagr = [(r["cagr"], r["mkt_value"]) for r in enriched
                  if r["cagr"] is not None and r["mkt_value"] is not None and r["open_shares"] > 0]
    _tot_open_mv = sum(mv for _, mv in _open_cagr)
    weighted_cagr = round(sum(c * mv for c, mv in _open_cagr) / _tot_open_mv, 2) if _tot_open_mv else None

    return jsonify({
        "positions": enriched,
        "totals": {
            "invested":        round(total_invested, 2),
            "mkt_value":       round(total_mktval,   2),
            "unreal_pnl":      round(total_unreal,   2) if total_unreal      is not None else None,
            "unreal_pct":      round(total_unreal_pct, 2) if total_unreal_pct is not None else None,
            "realized_pnl":    round(total_realized, 2),
            "total_pnl":       round((total_unreal or 0) + total_realized, 2) if total_unreal is not None else None,
            "cash_balance":    cash_balance,
            "portfolio_total": port_total,
            "cash_pct":        cash_pct,
            "invested_pct":    invested_pct,
            "weighted_cagr":   weighted_cagr,
        }
    })


@app.route("/api/portfolio/add", methods=["POST"])
def api_portfolio_add():
    """Add a transaction (buy or sell) to a symbol's ledger."""
    body     = request.get_json(force=True, silent=True) or {}
    symbol   = (body.get("symbol")   or "").strip().upper()
    exchange = (body.get("exchange") or "nse").strip().lower()
    tx_type  = (body.get("type")     or "buy").strip().lower()
    tx_date  = (body.get("date")     or "").strip()
    try:
        shares = float(body.get("shares", 0))
        price  = float(body.get("price",  0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares or price"}), 400
    if not symbol:
        return jsonify({"error": "No symbol"}), 400
    if shares <= 0:
        return jsonify({"error": "Shares must be > 0"}), 400
    if price <= 0:
        return jsonify({"error": "Price must be > 0"}), 400
    if tx_type not in ("buy", "sell"):
        return jsonify({"error": "type must be buy or sell"}), 400
    # Strip exchange suffix
    if symbol.endswith(".BO"):  symbol = symbol[:-3]
    elif symbol.endswith(".NS"): symbol = symbol[:-3]
    if not tx_date:
        from datetime import date
        tx_date = date.today().isoformat()

    entries = _load_portfolio()
    entries = [_migrate_entry(e) for e in entries]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None:
        entry = {"symbol": symbol, "name": symbol, "exchange": exchange, "transactions": []}
        entries.append(entry)
    entry["exchange"] = exchange
    entry["transactions"].append({"date": tx_date, "type": tx_type, "shares": shares, "price": price})
    _save_portfolio(entries)
    # Register for price fetching
    if symbol not in all_symbols:
        all_symbols.append(symbol)
        if exchange == "bse":
            bse_override.add(symbol)
    derived = _derive_position(entry)
    return jsonify({"status": "ok", "symbol": symbol, **derived})


@app.route("/api/portfolio/remove_tx", methods=["POST"])
def api_portfolio_remove_tx():
    """Remove a specific transaction by symbol + index."""
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:
        idx = int(body.get("tx_index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid tx_index"}), 400
    if not symbol:
        return jsonify({"error": "No symbol"}), 400
    entries = _load_portfolio()
    entries = [_migrate_entry(e) for e in entries]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None:
        return jsonify({"error": "Symbol not found"}), 404
    txs = entry.get("transactions", [])
    if idx < 0 or idx >= len(txs):
        return jsonify({"error": "tx_index out of range"}), 400
    txs.pop(idx)
    _save_portfolio(entries)
    return jsonify({"status": "ok", "symbol": symbol, "remaining_tx": len(txs)})


@app.route("/api/portfolio/edit_tx", methods=["POST"])
def api_portfolio_edit_tx():
    """Edit an existing transaction by symbol + index."""
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:
        idx = int(body.get("tx_index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid tx_index"}), 400
    tx_type = (body.get("type") or "buy").strip().lower()
    tx_date = (body.get("date") or "").strip()
    try:
        shares = float(body.get("shares", 0))
        price  = float(body.get("price",  0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares or price"}), 400
    if not symbol:
        return jsonify({"error": "No symbol"}), 400
    if shares <= 0 or price <= 0:
        return jsonify({"error": "Shares and price must be > 0"}), 400
    if tx_type not in ("buy", "sell"):
        return jsonify({"error": "type must be buy or sell"}), 400
    entries = _load_portfolio()
    entries = [_migrate_entry(e) for e in entries]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None:
        return jsonify({"error": "Symbol not found"}), 404
    txs = entry.get("transactions", [])
    if idx < 0 or idx >= len(txs):
        return jsonify({"error": "tx_index out of range"}), 400
    txs[idx] = {"date": tx_date, "type": tx_type, "shares": shares, "price": price}
    _save_portfolio(entries)
    return jsonify({"status": "ok", "symbol": symbol, "tx_index": idx})


@app.route("/api/portfolio/remove", methods=["POST"])
def api_portfolio_remove():
    """Remove an entire symbol position."""
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "No symbol"}), 400
    entries = _load_portfolio()
    new_entries = [e for e in entries if str(e.get("symbol","")).upper() != symbol]
    if len(new_entries) == len(entries):
        return jsonify({"error": "Symbol not found"}), 404
    _save_portfolio(new_entries)
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/api/portfolio/import_yahoo_csv", methods=["POST"])
def api_portfolio_import_yahoo_csv():
    """Import transactions from a Yahoo Finance portfolio CSV upload."""
    import csv, io
    f = request.files.get("csv_file")
    if f is None:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        text = f.read().decode("utf-8-sig")  # handle BOM
    except Exception:
        return jsonify({"error": "Could not decode file"}), 400

    reader    = csv.DictReader(io.StringIO(text))
    entries   = _load_portfolio()
    entries   = [_migrate_entry(e) for e in entries]
    entry_map = {e["symbol"]: e for e in entries}

    imported  = 0   # transactions imported
    skipped   = 0   # rows skipped (cash, duplicates, parse errors)
    new_syms  = []  # symbols newly added with full transaction data
    cash_imports = []  # cash deposit/withdrawal rows from $$CASH_TX
    stub_syms = []  # symbols added with NO transaction data (need manual entry)
    needs_data = [] # human-readable list of stubs for the UI message

    for row in reader:
        raw_sym = (row.get("Symbol") or "").strip()
        # Import cash transactions from $$CASH_TX rows
        if raw_sym == "$$CASH_TX":
            td_raw   = (row.get("Trade Date")      or "").strip()
            qty_raw  = (row.get("Quantity")        or "").strip()
            ctype    = (row.get("Transaction Type") or "").strip().upper()
            if td_raw and qty_raw and ctype in ("DEPOSIT", "WITHDRAWAL"):
                try:
                    amt = float(qty_raw)
                    if amt > 0:
                        td = str(td_raw).replace("/","").replace("-","")
                        cdate = f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td)==8 else td_raw
                        cash_imports.append({"date": cdate, "type": ctype.lower(), "amount": amt, "note": ""})
                except ValueError:
                    pass
            skipped += 1
            continue
        if not raw_sym:
            skipped += 1
            continue

        # Resolve symbol + exchange from suffix
        if raw_sym.endswith(".NS"):
            symbol   = raw_sym[:-3].upper()
            exchange = "nse"
        elif raw_sym.endswith(".BO"):
            symbol   = raw_sym[:-3].upper()
            exchange = "bse"
        else:
            symbol   = raw_sym.upper()
            exchange = "nse"

        trade_date_raw = (row.get("Trade Date")      or "").strip()
        qty_raw        = (row.get("Quantity")         or "").strip()
        price_raw      = (row.get("Purchase Price")   or "").strip()
        tx_type_raw    = (row.get("Transaction Type") or "").strip().lower()

        # ── Stub row: symbol present but no transaction data ─────────────────
        # (Yahoo exports watchlist / fully-closed rows without trade details)
        if not trade_date_raw and not qty_raw and not tx_type_raw:
            if symbol not in entry_map:
                entry_map[symbol] = {
                    "symbol":     symbol,
                    "name":       symbol,
                    "exchange":   exchange,
                    "transactions": [],
                    "needs_data": True,
                }
                stub_syms.append(symbol)
                needs_data.append(raw_sym)
            # If symbol already exists in portfolio, leave it untouched
            continue

        # ── Normal transaction row ────────────────────────────────────────────
        if not trade_date_raw or not qty_raw or not price_raw:
            skipped += 1
            continue

        try:
            shares = float(qty_raw)
            price  = float(price_raw)
        except ValueError:
            skipped += 1
            continue

        if shares <= 0:
            skipped += 1
            continue

        # Parse date YYYYMMDD → YYYY-MM-DD
        td = str(trade_date_raw).replace("/", "").replace("-", "")
        tx_date = f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td) == 8 else trade_date_raw

        tx_type = "sell" if tx_type_raw == "sell" else "buy"
        new_tx  = {"date": tx_date, "type": tx_type, "shares": shares, "price": price}

        if symbol not in entry_map:
            entry_map[symbol] = {
                "symbol": symbol, "name": symbol,
                "exchange": exchange, "transactions": []
            }
            new_syms.append(symbol)

        entry    = entry_map[symbol]
        existing = entry.get("transactions", [])

        # Deduplicate: skip if exact (date, type, shares, price) already exists
        dup = any(
            t.get("date") == tx_date and t.get("type") == tx_type
            and float(t.get("shares", 0)) == shares
            and float(t.get("price",  0)) == price
            for t in existing
        )
        if dup:
            skipped += 1
            continue

        existing.append(new_tx)
        imported += 1

    # Rebuild list preserving original order, then append new/stub symbols
    seen    = set()
    rebuilt = []
    for e in entries:
        s = e["symbol"]
        rebuilt.append(entry_map.get(s, e))
        seen.add(s)
    for s in new_syms + stub_syms:
        if s not in seen:
            rebuilt.append(entry_map[s])
            seen.add(s)

    # Register all new symbols for live price polling
    for s in new_syms + stub_syms:
        ex     = entry_map[s].get("exchange", "nse")
        yf_sym = f"{s}.NS" if ex == "nse" else f"{s}.BO"
        if yf_sym not in all_symbols:
            all_symbols.append(yf_sym)

    _save_portfolio(rebuilt)

    # Merge imported cash transactions (deduplicate by date+type+amount)
    if cash_imports:
        existing_cash = _load_cash()
        existing_set  = {(t["date"], t["type"], float(t["amount"])) for t in existing_cash}
        new_cash = [c for c in cash_imports
                    if (c["date"], c["type"], float(c["amount"])) not in existing_set]
        if new_cash:
            existing_cash.extend(new_cash)
            existing_cash.sort(key=lambda t: t.get("date", ""))
            _save_cash(existing_cash)

    return jsonify({
        "status":        "ok",
        "imported":      imported,
        "skipped":       skipped,
        "new_symbols":   len(new_syms),
        "stubs":         len(stub_syms),
        "needs_data":    needs_data,
        "cash_imported": len(cash_imports),
    })


@app.route("/api/cash")
def api_cash():
    txs = sorted(_load_cash(), key=lambda t: t.get("date", ""))
    return jsonify({"balance": _calc_cash_balance(txs), "transactions": txs})


@app.route("/api/cash/add", methods=["POST"])
def api_cash_add():
    body    = request.get_json(force=True, silent=True) or {}
    tx_type = (body.get("type")   or "deposit").strip().lower()
    date    = (body.get("date")   or "").strip()
    note    = str(body.get("note") or "").strip()[:200]
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400
    if tx_type not in ("deposit", "withdrawal"):
        return jsonify({"error": "type must be deposit or withdrawal"}), 400
    txs = _load_cash()
    txs.append({"date": date, "type": tx_type, "amount": amount, "note": note})
    _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


@app.route("/api/cash/remove", methods=["POST"])
def api_cash_remove():
    body = request.get_json(force=True, silent=True) or {}
    try:
        idx = int(body.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400
    txs = _load_cash()
    if idx < 0 or idx >= len(txs):
        return jsonify({"error": "Index out of range"}), 400
    txs.pop(idx)
    _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


@app.route("/api/cash/edit", methods=["POST"])
def api_cash_edit():
    body    = request.get_json(force=True, silent=True) or {}
    try:
        idx = int(body.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400
    tx_type = (body.get("type")   or "deposit").strip().lower()
    date    = (body.get("date")   or "").strip()
    note    = str(body.get("note") or "").strip()[:200]
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400
    if tx_type not in ("deposit", "withdrawal"):
        return jsonify({"error": "type must be deposit or withdrawal"}), 400
    txs = _load_cash()
    if idx < 0 or idx >= len(txs):
        return jsonify({"error": "Index out of range"}), 400
    txs[idx] = {"date": date, "type": tx_type, "amount": amount, "note": note}
    _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


# ── Corporate Actions API ─────────────────────────────────────
@app.route("/api/corporate_actions")
def api_ca_list():
    return jsonify(_load_ca())


@app.route("/api/corporate_actions/add", methods=["POST"])
def api_ca_add():
    body    = request.get_json(force=True, silent=True) or {}
    ca_type = (body.get("type") or "").strip().lower()
    valid   = {"merger","amalgamation","name_change","demerger","spinoff",
               "spin-off","split","subdivision","bonus"}
    if ca_type not in valid:
        return jsonify({"error": f"Unknown type '{ca_type}'"}), 400
    date = (body.get("date") or "").strip()
    note = str(body.get("note") or "").strip()[:300]
    rec  = {"type": ca_type, "date": date, "note": note}
    # Symbol-level CAs (split, bonus)
    if ca_type in ("split", "subdivision", "bonus"):
        sym = (body.get("symbol") or "").strip().upper()
        if not sym:
            return jsonify({"error": "symbol required"}), 400
        try:
            ratio = float(body.get("ratio", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "ratio must be a number"}), 400
        if ratio <= 0:
            return jsonify({"error": "ratio must be > 0"}), 400
        rec["symbol"] = sym
        rec["ratio"]  = ratio
    # Cross-symbol CAs (merger, demerger, name_change)
    else:
        from_sym = (body.get("from_symbol") or "").strip().upper()
        to_sym   = (body.get("to_symbol")   or "").strip().upper()
        if not from_sym or not to_sym:
            return jsonify({"error": "from_symbol and to_symbol required"}), 400
        rec["from_symbol"] = from_sym
        rec["to_symbol"]   = to_sym
        if "ratio" in body:
            try:
                rec["ratio"] = float(body["ratio"])
            except (TypeError, ValueError):
                pass
        if "cost_allocation_pct" in body:
            try:
                pct = float(body["cost_allocation_pct"])
                rec["cost_allocation_pct"] = round(max(0.0, min(100.0, pct)), 4)
            except (TypeError, ValueError):
                pass
    actions = _load_ca()
    rec["id"] = f"ca-{len(actions)+1:03d}"
    actions.append(rec)
    _save_ca(actions)
    return jsonify({"status": "ok", "action": rec})


@app.route("/api/corporate_actions/remove", methods=["POST"])
def api_ca_remove():
    body = request.get_json(force=True, silent=True) or {}
    try:
        idx = int(body.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400
    actions = _load_ca()
    if idx < 0 or idx >= len(actions):
        return jsonify({"error": "Index out of range"}), 400
    actions.pop(idx)
    _save_ca(actions)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
