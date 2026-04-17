"""Load/save user customisations and panel order (user-created panels only)."""

import os
import json

import state

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


def _save_order() -> None:
    """Persist panel display order and page assignments using stable IDs."""
    customs = _load_customs()
    customs["__order__"] = [p["id"] for p in state.panels]
    customs["__pages__"] = {p["id"]: p.get("page", 0) for p in state.panels}
    _save_customs(customs)
