"""Load/save user customisations, panel sheet data (SQLite), and panel order."""

import os
import json

import state
from core.db import _db_connect

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
    for panel_id, stocks in state.raw_sheet_data.items():
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
    customs["__order__"] = [p["id"] for p in state.panels]
    customs["__pages__"] = {p["id"]: p.get("page", 0) for p in state.panels}
    _save_customs(customs)
