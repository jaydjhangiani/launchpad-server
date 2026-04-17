"""
Ruliad Capital Management Systems – Indian Markets
Flask backend: serves live NSE prices via yfinance with background refresh.

Entry point: creates the Flask app, initialises shared state from the
database, applies saved customisations, registers route blueprints, and
starts background threads.
"""

import threading

from flask import Flask

import state
from core.db import _init_db, _load_panels_from_db, _load_dead_symbols_from_db
from core.customizations import _load_customs
from core.fetcher import background_updater, mktcap_updater

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ---------------------------------------------------------------------------
# DATABASE INIT & PANEL LOADING
# ---------------------------------------------------------------------------
_init_db()
_db_panels = _load_panels_from_db()

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
# APPLY SAVED CUSTOMISATIONS
# ---------------------------------------------------------------------------
_init_customs = _load_customs()

# Pass 1: apply per-panel customisations to DB-sourced panels.
# For DB panels the database is the sole source of truth for stock lists —
# only sector_name and mode overrides are read from user_customizations.json.
# removed/added from user_customizations.json apply only to user-created
# panels (identified by the "_uc_" id prefix), handled in Pass 2.
for _panel in state.panels:
    _cust = _init_customs.get(_panel["id"], {})
    if _cust.get("sector_name"):
        _panel["sector"] = _cust["sector_name"]
    if _cust.get("mode"):
        _panel["mode"] = _cust["mode"]

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
