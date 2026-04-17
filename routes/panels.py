"""Blueprint: all panel and symbol management routes."""

from flask import Blueprint, jsonify, request

import state
from core.db import (
    _db_add_stock, _db_remove_stock, _db_rename_stock, _db_rename_panel, _db_move_stock,
    _db_create_panel, _db_delete_panel, _db_update_display_orders,
    _db_set_panel_page, _db_set_panel_height, _db_next_panel_id,
)
from core.fetcher import fetch_symbols_batch, fetch_bse_batch, fetch_global_batch

bp = Blueprint("panels", __name__)


@bp.route("/api/panel/<int:pi>/add", methods=["POST"])
def api_add_ticker(pi: int):
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    # Auto-detect explicit exchange suffix typed by user (e.g. RELIANCE.NS or 500325.BO)
    mode   = state.panels[pi].get("mode", "nse")
    is_bse = False
    if symbol.endswith(".BO"):
        symbol = symbol[:-3]
        is_bse = True
    elif symbol.endswith(".NS"):
        symbol = symbol[:-3]
        is_bse = False
    elif mode == "bse":
        is_bse = True

    existing = {s["symbol"] for s in state.panels[pi]["stocks"]}
    if symbol in existing:
        return jsonify({"error": f"{symbol} is already in this panel"}), 409

    with state.cache_lock:
        cached = dict(state.price_cache.get(symbol, {}))
    if not cached:
        if mode == "global":
            result = fetch_global_batch([symbol])
        elif is_bse:
            result = fetch_bse_batch([symbol])
            if not result:
                result = fetch_symbols_batch([symbol])
                if result:
                    is_bse = False
        else:
            result = fetch_symbols_batch([symbol])
            if not result:
                result = fetch_bse_batch([symbol])
                if result:
                    is_bse = True
        if result:
            with state.cache_lock:
                state.price_cache.update(result)
            cached = result.get(symbol, {})

    new_stock = {"symbol": symbol, "name": symbol}

    # Write to DB first — if this fails the route returns an error and state
    # is never touched, so DB and in-memory state remain consistent.
    _db_add_stock(state.panels[pi]["id"], symbol, symbol)

    state.panels[pi]["stocks"].append(new_stock)
    with state.panels_lock:
        if mode == "global":
            if symbol not in state.global_symbols:
                state.global_symbols.append(symbol)
        else:
            if symbol not in state.all_symbols:
                state.all_symbols.append(symbol)
            if is_bse:
                state.bse_override.add(symbol)
            else:
                state.bse_override.discard(symbol)

    pdata = cached
    return jsonify({
        "status":     "ok",
        "symbol":     symbol,
        "price":      pdata.get("price") if pdata else None,
        "change_pct": pdata.get("change_pct") if pdata else None,
    })


@bp.route("/api/panel/<int:pi>/rename", methods=["POST"])
def api_rename_panel(pi: int):
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        return jsonify({"error": "Invalid name"}), 400
    _db_rename_panel(state.panels[pi]["id"], name)
    state.panels[pi]["sector"] = name
    return jsonify({"status": "ok", "sector": name})


@bp.route("/api/panel/<int:pi>/remove", methods=["POST"])
def api_remove_ticker(pi: int):
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "No symbol provided"}), 400
    _db_remove_stock(state.panels[pi]["id"], symbol)
    state.panels[pi]["stocks"] = [s for s in state.panels[pi]["stocks"] if s["symbol"] != symbol]
    return jsonify({"status": "ok", "removed": symbol})


@bp.route("/api/panel/<int:pi>/edit", methods=["POST"])
def api_edit_ticker(pi: int):
    """Rename a ticker symbol in-place (preserves position in the stock list)."""
    if pi < 0 or pi >= len(state.panels):
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
    existing = {s["symbol"] for s in state.panels[pi]["stocks"]}
    if old_symbol not in existing:
        return jsonify({"error": f"{old_symbol} not found in this panel"}), 404
    if new_symbol in existing:
        return jsonify({"error": f"{new_symbol} is already in this panel"}), 409

    mode = state.panels[pi].get("mode", "nse")

    _db_rename_stock(state.panels[pi]["id"], old_symbol, new_symbol)

    for s in state.panels[pi]["stocks"]:
        if s["symbol"] == old_symbol:
            s["symbol"] = new_symbol
            s["name"]   = new_symbol
            break

    with state.panels_lock:
        sym_list = state.global_symbols if mode == "global" else state.all_symbols
        if old_symbol in sym_list and new_symbol not in sym_list:
            sym_list[sym_list.index(old_symbol)] = new_symbol
        elif new_symbol not in sym_list:
            sym_list.append(new_symbol)

        if old_symbol in state.bse_override:
            state.bse_override.discard(old_symbol)
            if mode == "bse":
                state.bse_override.add(new_symbol)

    if mode == "global":
        fetcher = fetch_global_batch
    elif mode == "bse":
        fetcher = fetch_bse_batch
    else:
        fetcher = fetch_symbols_batch
    result = fetcher([new_symbol])
    if result:
        with state.cache_lock:
            state.price_cache.update(result)
    pdata = state.price_cache.get(new_symbol, {})
    return jsonify({
        "status":     "ok",
        "old_symbol": old_symbol,
        "symbol":     new_symbol,
        "price":      pdata.get("price"),
        "change_pct": pdata.get("change_pct"),
    })


@bp.route("/api/panel/new", methods=["POST"])
def api_new_panel():
    """Create a brand-new empty sector panel."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        return jsonify({"error": "Name must be 1-60 characters"}), 400
    mode = (body.get("mode") or "nse").strip().lower()
    if mode not in ("nse", "global", "bse"):
        mode = "nse"
    target_pg = (
        max((p.get("page", j // state.PAGE_SIZE) for j, p in enumerate(state.panels)), default=0)
        if state.panels else 0
    )
    panel_id      = _db_next_panel_id()
    display_order = len(state.panels)
    _db_create_panel(panel_id, name, mode, display_order, target_pg)
    new_panel = {"sector": name, "stocks": [], "id": panel_id, "page": target_pg, "mode": mode, "height": 1}
    state.panels.append(new_panel)
    return jsonify({"status": "ok", "id": panel_id, "index": len(state.panels) - 1,
                    "sector": name, "page": target_pg})


@bp.route("/api/panel/<int:pi>/delete", methods=["POST"])
def api_delete_panel(pi: int):
    """Permanently delete a panel (any mode)."""
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    panel = state.panels[pi]   # peek before pop
    pid   = panel["id"]

    # DB delete first (FK cascade removes panel_stocks automatically)
    _db_delete_panel(pid)
    state.panels.pop(pi)

    still_used: set = set()
    for p in state.panels:
        for s in p.get("stocks", []):
            still_used.add(s["symbol"])

    with state.panels_lock:
        if panel.get("mode") == "global":
            state.global_symbols[:] = [s for s in state.global_symbols if s in still_used]
        else:
            state.all_symbols[:] = [s for s in state.all_symbols if s in still_used]
            for s in list(state.bse_override):
                if s not in still_used:
                    state.bse_override.discard(s)
    return jsonify({"status": "ok", "deleted": pid})


@bp.route("/api/panel/swap", methods=["POST"])
def api_swap_panels():
    """Swap two panels by index (for drag-and-drop reordering)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        a, b = int(body.get("a", -1)), int(body.get("b", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid indices"}), 400
    if not (0 <= a < len(state.panels)) or not (0 <= b < len(state.panels)) or a == b:
        return jsonify({"error": "Invalid panel indices"}), 400
    state.panels[a], state.panels[b] = state.panels[b], state.panels[a]
    _db_update_display_orders([p["id"] for p in state.panels])
    return jsonify({"status": "ok", "swapped": [a, b]})


@bp.route("/api/symbol/move", methods=["POST"])
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
    if not (0 <= from_pi < len(state.panels)) or not (0 <= to_pi < len(state.panels)) or from_pi == to_pi:
        return jsonify({"error": "Invalid panel indices"}), 400

    src   = state.panels[from_pi]
    dst   = state.panels[to_pi]
    stock = next((s for s in src["stocks"] if s["symbol"] == symbol), None)
    if not stock:
        return jsonify({"error": f"{symbol} not in source panel"}), 404
    if any(s["symbol"] == symbol for s in dst["stocks"]):
        return jsonify({"error": f"{symbol} already in destination panel"}), 409

    src["stocks"] = [s for s in src["stocks"] if s["symbol"] != symbol]
    dst["stocks"].append(stock)

    src_pk = src["id"]
    dst_pk = dst["id"]
    name   = stock.get("name", symbol)
    _db_move_stock(src_pk, dst_pk, symbol, name)
    return jsonify({"status": "ok", "symbol": symbol, "from": from_pi, "to": to_pi})


@bp.route("/api/panel/move", methods=["POST"])
def api_move_panel():
    """Move a panel from index `from` to index `to`, shifting panels in between."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        frm = int(body.get("from", -1))
        to  = int(body.get("to",   -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid indices"}), 400
    if not (0 <= frm < len(state.panels)) or not (0 <= to <= len(state.panels)) or frm == to:
        return jsonify({"error": "Invalid panel indices"}), 400
    panel = state.panels.pop(frm)
    state.panels.insert(to, panel)
    _db_update_display_orders([p["id"] for p in state.panels])
    return jsonify({"status": "ok", "from": frm, "to": to})


@bp.route("/api/panel/<int:pi>/setpage", methods=["POST"])
def api_set_panel_page(pi: int):
    """Assign a panel to an explicit display page."""
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    try:
        page = int(body.get("page", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid page"}), 400
    if page < 0:
        return jsonify({"error": "Page must be >= 0"}), 400
    state.panels[pi]["page"] = page
    _db_set_panel_page(state.panels[pi]["id"], page)
    return jsonify({"status": "ok", "page": page})


@bp.route("/api/panel/<int:pi>/setheight", methods=["POST"])
def api_set_panel_height(pi: int):
    """Set the row-span height of a panel (1-4 units)."""
    if pi < 0 or pi >= len(state.panels):
        return jsonify({"error": "Invalid panel index"}), 400
    body = request.get_json(force=True, silent=True) or {}
    try:
        height = int(body.get("height", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid height"}), 400
    height = max(1, min(4, height))
    state.panels[pi]["height"] = height
    _db_set_panel_height(state.panels[pi]["id"], height)
    return jsonify({"status": "ok", "height": height})
