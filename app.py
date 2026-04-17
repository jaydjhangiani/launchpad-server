"""
Ruliad Capital Management Systems – Indian Markets
Flask backend: serves live NSE prices via yfinance with background refresh.

Entry point: creates the Flask app, initialises all shared state from
NeonDB, registers route blueprints, and starts background threads.
No local customisation files — NeonDB is the single source of truth.
"""

import sys
import threading

from flask import Flask
from flask_cors import CORS

import state
from core.db import _init_db, _load_panels_from_db, _load_dead_symbols_from_db
from core.fetcher import background_updater, mktcap_updater

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
CORS(app, origins=["http://localhost:5173", "http://localhost:3000"])


# ---------------------------------------------------------------------------
# DATABASE INIT & PANEL LOADING
# ---------------------------------------------------------------------------
try:
    _init_db()
    _db_panels = _load_panels_from_db()
except Exception as _e:
    print(f"[FATAL] Cannot connect to database: {_e}", file=sys.stderr)
    print("[FATAL] Check DATABASE_URL in .env and ensure NeonDB is reachable.", file=sys.stderr)
    sys.exit(1)

_persisted_dead = _load_dead_symbols_from_db()
if _persisted_dead:
    print(f"[DB] Loaded {len(_persisted_dead)} persisted dead symbols — skipping on fetch")
state.dead_symbols.update(_persisted_dead)

# Build panels + symbol lists entirely from NeonDB
_seen_syms: set = set()
for _db_panel in _db_panels:
    _panel_stocks = []
    _seen_panel: set = set()
    _pmode = _db_panel.get("mode", "nse")
    for s in _db_panel["stocks"]:
        sym = s["symbol"]
        if sym not in _seen_panel:
            _seen_panel.add(sym)
            _panel_stocks.append({"symbol": sym, "name": s.get("name", sym)})
        if sym not in _seen_syms:
            _seen_syms.add(sym)
            if _pmode == "global":
                state.global_symbols.append(sym)
            else:
                state.all_symbols.append(sym)
                if _pmode == "bse":
                    state.bse_override.add(sym)
    state.panels.append({
        "sector": _db_panel["sector_name"],
        "stocks": _panel_stocks,
        "id":     _db_panel["panel_id"],
        "mode":   _pmode,
        "page":   _db_panel.get("page", 0),
        "height": _db_panel.get("height", 1),
    })

print(f"[INIT] {len(state.panels)} panels, {len(state.all_symbols)} NSE/BSE + {len(state.global_symbols)} global symbols")

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
