"""Blueprint: main data routes — index page, /api/panels, /api/refresh."""

import time
import threading
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, make_response, render_template

import state
from core.customizations import _load_customs
from core.fetcher import update_all_prices

bp = Blueprint("data", __name__)


@bp.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.route("/api/panels")
def api_panels():
    with state.cache_lock:
        cache_copy = dict(state.price_cache)
        idx_copy   = dict(state.indices_cache)
        fetch_age  = round(time.time() - state.last_fetch_time) if state.last_fetch_time else None
        good_age   = round(time.time() - state.last_good_fetch_time) if state.last_good_fetch_time else None
        IST_good   = (
            datetime.fromtimestamp(
                state.last_good_fetch_time,
                tz=timezone(timedelta(hours=5, minutes=30))
            ).strftime("%H:%M:%S")
            if state.last_good_fetch_time else None
        )

    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)

    with state.mktcap_lock:
        mktcap_copy = dict(state.mktcap_cache)

    result = []
    for i, panel in enumerate(state.panels):
        stocks_out = []
        for s in panel["stocks"]:
            sym  = s["symbol"]
            data = cache_copy.get(sym)
            mc   = mktcap_copy.get(sym)
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
        result.append({
            "sector": panel["sector"],
            "stocks": stocks_out,
            "id":     panel["id"],
            "page":   panel.get("page", i // state.PAGE_SIZE),
            "height": panel.get("height", 1),
            "mode":   panel.get("mode", "nse"),
        })

    _customs_snap = _load_customs()
    _max_used_pg  = max(
        (p.get("page", j // state.PAGE_SIZE) for j, p in enumerate(state.panels)),
        default=0
    )
    _page_count = max(_max_used_pg + 1, _customs_snap.get("__page_count__", 0))
    return jsonify({
        "panels":       result,
        "indices":      idx_copy,
        "timestamp":    now.strftime("%H:%M:%S IST"),
        "date":         now.strftime("%d %b %Y"),
        "fetch_age":    fetch_age,
        "good_age":     good_age,
        "last_good_ts": IST_good,
        "next_refresh": max(0, state.FETCH_INTERVAL - (fetch_age or state.FETCH_INTERVAL)),
        "page_count":   _page_count,
        "page_names":   _customs_snap.get("__page_names__", {}),
    })


@bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force an immediate price refresh (runs in background)."""
    t = threading.Thread(target=update_all_prices, daemon=True)
    t.start()
    return jsonify({"status": "refresh triggered"})
