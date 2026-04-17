"""
Ruliad Capital Management Systems – Indian Markets
Flask backend: serves live NSE prices via yfinance with background refresh.

Entry point: creates the Flask app, initialises shared state from the
database, applies saved customisations, registers route blueprints, and
starts background threads.
"""

import json
import os
import secrets
import threading

from flask import Flask, make_response, request

import state
from core.db import _init_db, _load_panels_from_db, _load_dead_symbols_from_db
from core.customizations import _load_customs, _save_customs, _save_sheet_data
from core.fetcher import background_updater, mktcap_updater

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
        return True
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
# DATABASE INIT & PANEL LOADING
# ---------------------------------------------------------------------------
_init_db()
_db_panels = _load_panels_from_db()

# raw_sheet_data: {panel_id: [{symbol, name}]} — used by _save_sheet_data / migration
state.raw_sheet_data.update({p["panel_id"]: p["stocks"] for p in _db_panels})
_persisted_dead = _load_dead_symbols_from_db()
if _persisted_dead:
    print(f"[DB] Loaded {len(_persisted_dead)} persisted dead symbols — skipping on fetch")
state.dead_symbols.update(_persisted_dead)

# Build panels + deduplicated master symbol list — driven entirely by the database
_seen_global: set = set()
for _db_panel in _db_panels:
    _panel_stocks = []
    _seen_panel: set = set()
    for s in _db_panel["stocks"]:
        sym = s["symbol"]
        if sym not in _seen_panel:
            _seen_panel.add(sym)
            _panel_stocks.append({"symbol": sym, "name": s.get("name", sym)})
        if sym not in _seen_global:
            _seen_global.add(sym)
            state.all_symbols.append(sym)
    state.panels.append({
        "sector": _db_panel["sector_name"],
        "stocks": _panel_stocks,
        "id":     _db_panel["panel_id"],
    })

print(f"[INIT] {len(state.panels)} panels, {len(state.all_symbols)} unique symbols")

# ---------------------------------------------------------------------------
# APPLY SAVED CUSTOMISATIONS — stable-ID based
# ---------------------------------------------------------------------------
_init_customs = _load_customs()

# ── MIGRATION: handle old positional-index format keys alongside stable IDs ──
_orig_ids     = [p["id"] for p in state.panels]
_numeric_keys = [k for k in _init_customs
                 if k.lstrip("-").isdigit() and k not in ("__page_count__",)]
if _numeric_keys:
    if "__order__" not in _init_customs:
        print("[INIT] Old positional customisations (no __order__) — clearing")
        _init_customs = {}
    else:
        print(f"[INIT] Migrating {len(_numeric_keys)} positional customisations to stable IDs")
        for _nk in _numeric_keys:
            _n = int(_nk)
            if 0 <= _n < len(_orig_ids):
                _sid  = _orig_ids[_n]
                _old  = _init_customs.pop(_nk)
                _dest = _init_customs.setdefault(_sid, {"added": [], "removed": []})
                _have = {s["symbol"] for s in _dest.get("added", [])}
                for _s in _old.get("added", []):
                    if _s["symbol"] not in _have:
                        _dest.setdefault("added", []).append(_s)
                _hrem = set(_dest.get("removed", []))
                for _sym in _old.get("removed", []):
                    if _sym not in _hrem:
                        _dest.setdefault("removed", []).append(_sym)
                if not _dest.get("sector_name") and _old.get("sector_name"):
                    _dest["sector_name"] = _old["sector_name"]
            else:
                _init_customs.pop(_nk, None)
        _save_customs(_init_customs)
        print("[INIT] Migration complete")

# ── Migration: bake historical removed/added from user_customizations.json
# into the database so it is the permanent single source of truth.
# IMPORTANT: we update raw_sheet_data and save to disk, but do NOT modify
# _init_customs in memory — Pass 1 below still needs the original values.
_migrated_pids = []
for _panel in state.panels:
    _pid     = _panel["id"]
    _cust    = _init_customs.get(_pid, {})
    _added   = list(_cust.get("added", []))
    _removed = set(_cust.get("removed", []))
    if _pid not in state.raw_sheet_data:
        continue
    if not (_added or _removed):
        continue
    if _removed:
        state.raw_sheet_data[_pid] = [
            s for s in state.raw_sheet_data[_pid] if s["symbol"] not in _removed
        ]
    for _as in _added:
        if not any(s["symbol"] == _as["symbol"] for s in state.raw_sheet_data[_pid]):
            state.raw_sheet_data[_pid].append(
                {"symbol": _as["symbol"], "name": _as.get("name", _as["symbol"])}
            )
    _migrated_pids.append(_pid)
if _migrated_pids:
    _save_sheet_data()
    _disk_customs = json.loads(json.dumps(_init_customs))
    for _pid in _migrated_pids:
        if _pid in _disk_customs:
            _disk_customs[_pid]["removed"] = []
            _disk_customs[_pid]["added"]   = []
    _save_customs(_disk_customs)
    print(f"[INIT] Migrated historical customisations into database ({len(_migrated_pids)} panels)")

# Pass 1: apply per-panel customisations to DB-sourced panels
for _panel in state.panels:
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
                if _sym not in state.global_symbols:
                    state.global_symbols.append(_sym)
            else:
                if _sym not in state.all_symbols:
                    state.all_symbols.append(_sym)
                if _pmode == "bse":
                    state.bse_override.add(_sym)

# Pass 2: reconstruct user-created panels from saved order
_existing_ids = {p["id"] for p in state.panels}
_order = _init_customs.get("__order__", [])
for _pid in _order:
    if _pid not in _existing_ids:
        _uc = _init_customs.get(_pid, {})
        if _uc.get("user_created"):
            _removed = set(_uc.get("removed", []))
            _stocks  = [s for s in _uc.get("added", [])
                        if s.get("symbol") and s["symbol"] not in _removed]
            _mode = _uc.get("mode", "nse")
            state.panels.append({
                "sector": _uc.get("sector_name", "Custom Sector"),
                "stocks": _stocks,
                "id":     _pid,
                "mode":   _mode,
            })
            _sym_list = state.global_symbols if _mode == "global" else state.all_symbols
            for _s in _stocks:
                if _s["symbol"] not in _sym_list:
                    _sym_list.append(_s["symbol"])
                if _mode == "bse":
                    state.bse_override.add(_s["symbol"])
            _existing_ids.add(_pid)

# Pass 3: restore panel display order
if _order:
    _id_map   = {p["id"]: p for p in state.panels}
    _ordered  = [_id_map[_pid] for _pid in _order if _pid in _id_map]
    _in_order = set(_order)
    for _p in state.panels:
        if _p["id"] not in _in_order:
            _ordered.append(_p)
    state.panels[:] = _ordered

# Pass 4: restore explicit page assignments and heights
_page_map   = _init_customs.get("__pages__", {})
_height_map = _init_customs.get("__heights__", {})
for _i, _panel in enumerate(state.panels):
    _panel["page"]   = _page_map.get(_panel["id"], _i // state.PAGE_SIZE)
    _panel["height"] = _height_map.get(_panel["id"], 1)

# Rebuild symbol fetch lists from the final panel state.
# Must happen AFTER all customisation passes so removed symbols are never fetched.
state.all_symbols.clear()
state.global_symbols.clear()
state.bse_override.clear()
_seen_syms: set = set()
for _panel in state.panels:
    _pmode = _panel.get("mode", "nse")
    for _s in _panel["stocks"]:
        _sym = _s["symbol"]
        if _sym not in _seen_syms:
            _seen_syms.add(_sym)
            if _pmode == "global":
                state.global_symbols.append(_sym)
            else:
                state.all_symbols.append(_sym)
                if _pmode == "bse":
                    state.bse_override.add(_sym)

del _init_customs

# ---------------------------------------------------------------------------
# REGISTER BLUEPRINTS
# ---------------------------------------------------------------------------
from routes.data   import bp as _data_bp    # noqa: E402
from routes.panels import bp as _panels_bp  # noqa: E402
from routes.pages  import bp as _pages_bp   # noqa: E402

app.register_blueprint(_data_bp)
app.register_blueprint(_panels_bp)
app.register_blueprint(_pages_bp)

# ---------------------------------------------------------------------------
# START BACKGROUND THREADS
# ---------------------------------------------------------------------------
_updater_thread = threading.Thread(target=background_updater, daemon=True)
_updater_thread.start()

_mktcap_thread = threading.Thread(target=mktcap_updater, daemon=True)
_mktcap_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
