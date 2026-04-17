"""Blueprint: page management routes — add, delete, rename, reorder."""

from flask import Blueprint, jsonify, request

import state
from core.customizations import _load_customs, _save_customs

bp = Blueprint("pages", __name__)


@bp.route("/api/page/add", methods=["POST"])
def api_add_page():
    """Increment stored page count to allow empty pages."""
    customs  = _load_customs()
    max_used = max(
        (p.get("page", j // state.PAGE_SIZE) for j, p in enumerate(state.panels)), default=0
    )
    current  = max(max_used + 1, customs.get("__page_count__", 0))
    customs["__page_count__"] = current + 1
    _save_customs(customs)
    return jsonify({"status": "ok", "page_count": customs["__page_count__"]})


@bp.route("/api/page/<int:pg>/delete", methods=["POST"])
def api_delete_page(pg: int):
    """Delete a page only if no panels are assigned to it."""
    occupied = [p for p in state.panels if p.get("page", 0) == pg]
    if occupied:
        return jsonify({"error": f"Page has {len(occupied)} sector(s) on it — move them first"}), 409
    customs = _load_customs()
    current  = customs.get("__page_count__", 0)
    page_map = customs.get("__pages__", {})
    for p in state.panels:
        old_pg = p.get("page", 0)
        if old_pg > pg:
            p["page"] = old_pg - 1
            page_map[p["id"]] = old_pg - 1
    customs["__pages__"]      = page_map
    customs["__page_count__"] = max(0, current - 1)
    _save_customs(customs)
    max_used = max((p.get("page", 0) for p in state.panels), default=0) if state.panels else 0
    return jsonify({"status": "ok", "page_count": max(max_used + 1, customs["__page_count__"])})


@bp.route("/api/page/<int:pg>/rename", methods=["POST"])
def api_rename_page(pg: int):
    """Set a custom name for a page tab."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if len(name) > 40:
        return jsonify({"error": "Name too long"}), 400
    customs    = _load_customs()
    page_names = customs.setdefault("__page_names__", {})
    if name:
        page_names[str(pg)] = name
    else:
        page_names.pop(str(pg), None)
    _save_customs(customs)
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
    customs = _load_customs()
    page_map = customs.get("__pages__", {})
    customs["__pages__"] = {pid: remap.get(pg, pg) for pid, pg in page_map.items()}
    old_names = customs.get("__page_names__", {})
    customs["__page_names__"] = {
        str(remap[int(k)]): v for k, v in old_names.items() if int(k) in remap
    }
    _save_customs(customs)
    return jsonify({"status": "ok", "remap": remap})
