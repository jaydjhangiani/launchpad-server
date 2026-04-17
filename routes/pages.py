"""Blueprint: page management routes — add, delete, rename, reorder. All state in NeonDB."""

from flask import Blueprint, jsonify, request

import state
from core.db import (
    _db_get_page_count, _db_set_page_count, _db_get_page_names,
    _db_set_page_name, _db_shift_pages, _db_remap_pages, _db_remap_page_names,
)

bp = Blueprint("pages", __name__)


@bp.route("/api/page/add", methods=["POST"])
def api_add_page():
    """Increment stored page count to allow empty pages."""
    max_used = max(
        (p.get("page", j // state.PAGE_SIZE) for j, p in enumerate(state.panels)), default=0
    )
    current = max(max_used + 1, _db_get_page_count())
    _db_set_page_count(current + 1)
    return jsonify({"status": "ok", "page_count": current + 1})


@bp.route("/api/page/<int:pg>/delete", methods=["POST"])
def api_delete_page(pg: int):
    """Delete a page only if no panels are assigned to it."""
    occupied = [p for p in state.panels if p.get("page", 0) == pg]
    if occupied:
        return jsonify({"error": f"Page has {len(occupied)} sector(s) on it — move them first"}), 409
    current = _db_get_page_count()
    # Shift all panel pages above pg down by 1 — both in memory and DB
    for p in state.panels:
        if p.get("page", 0) > pg:
            p["page"] -= 1
    _db_shift_pages(pg + 1, -1)
    # Remap page names: delete pg's name, shift names above pg down
    page_names = _db_get_page_names()
    _db_set_page_name(pg, "")
    for idx_str, name in sorted(page_names.items(), key=lambda x: int(x[0])):
        idx = int(idx_str)
        if idx > pg:
            _db_set_page_name(idx - 1, name)
            _db_set_page_name(idx, "")
    new_count = max(0, current - 1)
    _db_set_page_count(new_count)
    max_used = max((p.get("page", 0) for p in state.panels), default=0) if state.panels else 0
    return jsonify({"status": "ok", "page_count": max(max_used + 1, new_count)})


@bp.route("/api/page/<int:pg>/rename", methods=["POST"])
def api_rename_page(pg: int):
    """Set a custom name for a page tab."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if len(name) > 40:
        return jsonify({"error": "Name too long"}), 400
    _db_set_page_name(pg, name)
    return jsonify({"status": "ok", "page": pg, "name": name})


@bp.route("/api/page/reorder", methods=["POST"])
def api_reorder_pages():
    """Reorder pages: body = {"order": [2, 0, 1, ...]} — new index = position in list."""
    body      = request.get_json(force=True, silent=True) or {}
    order     = body.get("order", [])
    num_pages = max((p.get("page", 0) for p in state.panels), default=0) + 1
    if sorted(order) != list(range(num_pages)):
        return jsonify({"error": "order must be a permutation of all page indices"}), 400
    remap = {old: new for new, old in enumerate(order)}
    for p in state.panels:
        old_pg = p.get("page", 0)
        p["page"] = remap.get(old_pg, old_pg)
    _db_remap_pages(remap)
    _db_remap_page_names(remap)
    return jsonify({"status": "ok", "remap": remap})
