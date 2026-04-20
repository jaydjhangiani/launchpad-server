"""
Ruliad Capital Management Systems — Portfolio Server
Standalone Flask app on port 5001.
Serves portfolio.html and all portfolio/cash/corporate-actions APIs.
Fetches live prices independently via yfinance for portfolio symbols.
"""

from flask import Flask, jsonify, render_template, request, make_response
import yfinance as yf
import threading
import json
import os
from datetime import datetime, timezone, timedelta
import time
from core.capital_gains import compute_all_cg, get_tax_rate_table

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PORTFOLIO_FILE = os.path.join(BASE_DIR, "portfolio.json")
CASH_FILE      = os.path.join(BASE_DIR, "cash.json")
CA_FILE        = os.path.join(BASE_DIR, "corporate_actions.json")
PRICE_CACHE_FILE = os.path.join(BASE_DIR, "price_cache.json")

# ---------------------------------------------------------------------------
# PRICE CACHE  — read from disk (shared with launchpad) + own background fetch
# ---------------------------------------------------------------------------
_price_cache: dict = {}
_cache_lock  = threading.Lock()

def _load_price_cache_from_disk() -> None:
    """Seed from the launchpad's price_cache.json if available."""
    if os.path.exists(PRICE_CACHE_FILE):
        try:
            with open(PRICE_CACHE_FILE) as fh:
                loaded = json.load(fh)
            with _cache_lock:
                _price_cache.update(loaded.get("prices", {}))
            print(f"[PORTFOLIO] Loaded {len(_price_cache)} cached prices from disk")
        except Exception as e:
            print(f"[PORTFOLIO] Could not load price cache: {e}")


def _fetch_nse_prices(symbols: list[str]) -> dict:
    """Fetch latest prices for a batch of NSE symbols via yfinance."""
    if not symbols:
        return {}
    tickers = " ".join(f"{s}.NS" for s in symbols)
    try:
        df = yf.download(tickers, period="2d", interval="1h",
                         group_by="ticker", auto_adjust=True, progress=False)
    except Exception:
        return {}
    result = {}
    for s in symbols:
        col = f"{s}.NS"
        try:
            d = df[col] if len(symbols) > 1 else df
            if d.empty:
                continue
            closes = d["Close"].dropna()
            if len(closes) < 2:
                continue
            ltp  = round(float(closes.iloc[-1]), 2)
            prev = round(float(closes.iloc[-2]), 2)
            chg  = round((ltp - prev) / prev * 100, 2) if prev else 0.0
            result[s] = {"price": ltp, "change_pct": chg}
        except Exception:
            continue
    return result


def _fetch_bse_prices(symbols: list[str]) -> dict:
    """Fetch latest prices for BSE symbols via yfinance fast_info."""
    result = {}
    for s in symbols:
        try:
            fi = yf.Ticker(f"{s}.BO").fast_info
            ltp = fi.last_price
            if ltp:
                result[s] = {"price": round(float(ltp), 2), "change_pct": None}
        except Exception:
            continue
    return result


def _refresh_portfolio_prices() -> None:
    """Fetch prices for all symbols currently in portfolio.json."""
    entries = _load_portfolio()
    nse_syms = [e["symbol"] for e in entries if e.get("exchange", "nse") != "bse"]
    bse_syms = [e["symbol"] for e in entries if e.get("exchange", "nse") == "bse"]
    new = {}
    if nse_syms:
        new.update(_fetch_nse_prices(nse_syms))
    if bse_syms:
        new.update(_fetch_bse_prices(bse_syms))
    if new:
        with _cache_lock:
            _price_cache.update(new)
        print(f"[PORTFOLIO] Refreshed {len(new)} prices")


def _background_price_updater() -> None:
    _refresh_portfolio_prices()           # immediate fetch on start
    while True:
        time.sleep(60)                    # refresh every 60 seconds
        try:
            _refresh_portfolio_prices()
        except Exception as e:
            print(f"[PORTFOLIO] Price update error: {e}")


# ---------------------------------------------------------------------------
# CORPORATE ACTIONS helpers
# ---------------------------------------------------------------------------
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
    sym = str(entry.get("symbol", "")).strip().upper()
    txs = sorted(entry.get("transactions", []), key=lambda t: t.get("date", ""))
    intra = sorted(
        [ca for ca in ca_list
         if ca.get("type", "").lower() in ("split", "subdivision", "bonus")
         and ca.get("symbol", "").strip().upper() == sym],
        key=lambda ca: ca.get("date", "")
    )
    timeline = [("tx", tx.get("date", ""), tx) for tx in txs] + \
               [("ca", ca.get("date", ""), ca) for ca in intra]
    timeline.sort(key=lambda x: x[1])

    open_shares = 0.0; avg_cost = 0.0
    realized_pnl = 0.0; sold_cost_basis = 0.0

    for kind, _, evt in timeline:
        if kind == "tx":
            ttype = evt.get("type", "buy").lower()
            sh = float(evt.get("shares", 0))
            pr = float(evt.get("price", 0))
            # Include brokerage + charges in cost basis per share
            charges = (float(evt.get("brokerage", 0)) +
                       float(evt.get("stt", 0)) +
                       float(evt.get("other_charges", 0)))
            if ttype == "buy":
                total_cost = open_shares * avg_cost + sh * pr + charges
                open_shares += sh
                avg_cost = total_cost / open_shares if open_shares else 0.0
            elif ttype == "sell":
                # Charges reduce realised P&L on sells
                realized_pnl    += (pr - avg_cost) * sh - charges
                sold_cost_basis += avg_cost * sh
                open_shares      = max(0.0, open_shares - sh)
        else:
            ca_type = evt.get("type", "").lower()
            ratio   = float(evt.get("ratio", 1))
            if ratio <= 0 or open_shares <= 0:
                continue
            if ca_type in ("split", "subdivision"):
                open_shares = round(open_shares * ratio, 6)
                avg_cost    = round(avg_cost / ratio, 6) if avg_cost else 0.0
            elif ca_type == "bonus":
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


# ---------------------------------------------------------------------------
# XIRR  — pure-Python money-weighted return solver (no scipy needed)
# ---------------------------------------------------------------------------
def _xirr(cash_flows: list[tuple]) -> float | None:
    """
    Compute XIRR (annualised money-weighted return) from a list of
    (date_str_or_date, amount) tuples.  Negative = cash out (buy),
    positive = cash in (sell / terminal value).

    Returns annualised rate as a plain float (e.g. 0.12 = 12 %).
    Returns None if unsolvable (< 30 days span, all same sign, etc.).
    """
    if not cash_flows:
        return None

    pairs: list[tuple] = []
    for d, a in cash_flows:
        if isinstance(d, str):
            try:
                d = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                return None
        pairs.append((d, float(a)))

    if not pairs:
        return None

    pairs.sort(key=lambda x: x[0])
    t0    = pairs[0][0]
    years = [(d - t0).days / 365.25 for d, _ in pairs]
    flows = [a for _, a in pairs]

    span  = (pairs[-1][0] - pairs[0][0]).days
    if span < 30:
        return None
    if all(f <= 0 for f in flows) or all(f >= 0 for f in flows):
        return None

    def npv(r: float) -> float:
        if r <= -1:
            return float("inf")
        return sum(f / (1 + r) ** t for f, t in zip(flows, years))

    def dnpv(r: float) -> float:
        return sum(-t * f / (1 + r) ** (t + 1) for f, t in zip(flows, years))

    # Newton-Raphson with fallback bisection
    r = 0.1
    for _ in range(100):
        fn  = npv(r)
        dfn = dnpv(r)
        if abs(dfn) < 1e-12:
            break
        r_new = r - fn / dfn
        if abs(r_new - r) < 1e-8:
            r = r_new
            break
        r = r_new
        if r <= -1:
            r = -0.999
    else:
        # Bisection fallback
        lo, hi = -0.999, 100.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if hi - lo < 1e-8:
                r = mid; break
            if npv(lo) * npv(mid) < 0:
                hi = mid
            else:
                lo = mid
        r = mid

    if not (-0.9999 < r < 100):
        return None
    return r


def _stock_xirr(txs: list, open_shares: float, ltp: float | None,
                ca_list: list, sym: str, all_entries: list) -> float | None:
    """
    Build cash flows for one stock and compute XIRR.
    Buys  → negative cash flow (money out).
    Sells → positive cash flow (money back in).
    Open lot terminal value today → positive cash flow.
    Also walks predecessor symbols via CA chain.
    """
    flows: list[tuple] = []

    # Walk predecessor chain (mergers / name changes)
    entries_by_sym = {str(e.get("symbol", "")).strip().upper(): e for e in all_entries}
    chain_syms = [sym]
    visited    = set()
    cur        = sym
    while cur and cur not in visited:
        visited.add(cur)
        pred_ca = next(
            (ca for ca in ca_list
             if ca.get("type", "").lower() in
                ("merger", "amalgamation", "name_change", "demerger", "spinoff", "spin-off")
             and ca.get("to_symbol", "").strip().upper() == cur),
            None,
        )
        if not pred_ca:
            break
        cur = pred_ca.get("from_symbol", "").strip().upper()
        if cur and cur not in chain_syms:
            chain_syms.append(cur)

    for cs in chain_syms:
        e = entries_by_sym.get(cs)
        if not e:
            continue
        for tx in e.get("transactions", []):
            ttype = tx.get("type", "buy").lower()
            sh    = float(tx.get("shares", 0))
            pr    = float(tx.get("price",  0))
            dt    = tx.get("date", "")
            charges = (float(tx.get("brokerage", 0)) +
                       float(tx.get("stt", 0)) +
                       float(tx.get("other_charges", 0)))
            if sh <= 0 or not dt:
                continue
            if ttype == "buy":
                flows.append((dt, -(sh * pr + charges)))   # money out
            elif ttype == "sell":
                flows.append((dt,  sh * pr - charges))     # money in

    # Terminal value for open lots
    if open_shares > 0 and ltp is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        flows.append((today, open_shares * ltp))

    r = _xirr(flows)
    return None if r is None else round(r * 100, 2)


def _resolve_cagr_inception(symbol: str, ca_list: list, all_entries: list):
    entries_by_sym = {str(e.get("symbol", "")).strip().upper(): e for e in all_entries}
    visited = set()
    cur = symbol.strip().upper()
    earliest = None
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


# ---------------------------------------------------------------------------
# CASH helpers
# ---------------------------------------------------------------------------
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
        # dividend, deposit → positive; withdrawal → negative
        if t.get("type") in ("deposit", "dividend"):
            b += amt
        else:
            b -= amt
    return round(b, 2)


# ---------------------------------------------------------------------------
# PORTFOLIO helpers
# ---------------------------------------------------------------------------
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
    if "transactions" not in entry and entry.get("shares") and entry.get("avg_cost"):
        entry = dict(entry)
        entry["transactions"] = [{
            "date":   entry.get("date", "2000-01-01"),
            "type":   "buy",
            "shares": float(entry["shares"]),
            "price":  float(entry["avg_cost"]),
        }]
        entry.pop("shares", None); entry.pop("avg_cost", None)
    return entry


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def portfolio_page():
    resp = make_response(render_template("portfolio.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/portfolio")
def api_portfolio():
    raw     = _load_portfolio()
    entries = [_migrate_entry(e) for e in raw]
    if not entries:
        return jsonify({"positions": [], "totals": {}})

    ca_list        = _load_ca()
    with _cache_lock:
        cache_snap = dict(_price_cache)

    total_mktval = 0.0; total_invested = 0.0; total_realized = 0.0
    enriched     = []

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
        day_gain_abs = None
        if ltp and change_pct is not None and open_shares > 0:
            day_gain_abs = round(open_shares * ltp * change_pct / (100.0 + change_pct), 2)
        realized_pct = round(realized_pnl / sold_cost_basis * 100, 2) if sold_cost_basis else None

        if mkt_value is not None: total_mktval   += mkt_value
        total_invested += invested
        total_realized += realized_pnl

        tx_rows = []
        _sh = 0.0; _ac = 0.0
        for tx in sorted(txs, key=lambda t: t.get("date", "")):
            ttype = tx.get("type", "buy").lower()
            sh    = float(tx.get("shares", 0)); pr = float(tx.get("price", 0))
            tx_pnl = None
            if ttype == "buy":
                total = _sh * _ac + sh * pr; _sh += sh
                _ac   = total / _sh if _sh else 0.0
            elif ttype == "sell":
                tx_pnl = round((pr - _ac) * sh, 2)
                _sh    = max(0.0, _sh - sh)
            tx_rows.append({"date": tx.get("date", ""), "type": ttype,
                            "shares": sh, "price": pr,
                            "value": round(sh * pr, 2), "pnl": tx_pnl})

        today_d   = datetime.now(timezone.utc).date()
        buy_dates = [t.get("date","") for t in txs if t.get("type","buy").lower()=="buy"  and t.get("date","")]
        sel_dates = [t.get("date","") for t in txs if t.get("type","buy").lower()=="sell" and t.get("date","")]
        first_buy = min(buy_dates) if buy_dates else None
        last_sell = max(sel_dates) if sel_dates else None
        ca_inception   = _resolve_cagr_inception(sym, ca_list, entries)
        inception_date = ca_inception

        # XIRR-based CAGR (money-weighted, per-lot accurate)
        cagr = _stock_xirr(txs, open_shares, ltp, ca_list, sym, entries)

        enriched.append({
            "symbol": sym, "name": name,
            "open_shares": open_shares, "avg_cost": avg_cost,
            "ltp": ltp, "change_pct": change_pct,
            "invested":    round(invested,   2),
            "mkt_value":   round(mkt_value,  2) if mkt_value  is not None else None,
            "unreal_pnl":  round(unreal_pnl, 2) if unreal_pnl is not None else None,
            "unreal_pct":  round(unreal_pct, 2) if unreal_pct is not None else None,
            "realized_pnl": realized_pnl, "realized_pct": realized_pct,
            "day_gain_abs": day_gain_abs,
            "status":       "Open" if open_shares > 0 else "Closed",
            "needs_data":   bool(entry.get("needs_data")),
            "total_pnl":    round(total_pnl, 2) if total_pnl is not None else None,
            "cagr": cagr, "inception_date": inception_date,
            "exchange":     entry.get("exchange", "nse"),
            "transactions": tx_rows,
        })

    total_unreal     = total_mktval - total_invested if total_mktval else None
    total_unreal_pct = (total_unreal / total_invested * 100) if (total_unreal is not None and total_invested) else None
    cash_txs     = _load_cash()
    cash_balance = _calc_cash_balance(cash_txs)
    port_total   = round(total_mktval + cash_balance, 2)
    cash_pct     = round(cash_balance / port_total * 100, 2) if port_total else None
    invested_pct = round(total_mktval  / port_total * 100, 2) if port_total else None

    for row in enriched:
        mv = row["mkt_value"]
        row["alloc_pct"] = round(mv / port_total * 100, 1) if (mv is not None and port_total) else None

    _open_cagr    = [(r["cagr"], r["mkt_value"]) for r in enriched
                     if r["cagr"] is not None and r["mkt_value"] is not None and r["open_shares"] > 0]
    _tot_open_mv  = sum(mv for _, mv in _open_cagr)
    weighted_cagr = round(sum(c * mv for c, mv in _open_cagr) / _tot_open_mv, 2) if _tot_open_mv else None

    # Portfolio XIRR — single true money-weighted return across all positions
    all_port_flows: list[tuple] = []
    for entry in entries:
        sym_  = str(entry.get("symbol", "")).strip().upper()
        for tx in entry.get("transactions", []):
            ttype = tx.get("type", "buy").lower()
            sh    = float(tx.get("shares", 0))
            pr    = float(tx.get("price",  0))
            dt    = tx.get("date", "")
            charges = (float(tx.get("brokerage", 0)) +
                       float(tx.get("stt", 0)) +
                       float(tx.get("other_charges", 0)))
            if sh <= 0 or not dt:
                continue
            if ttype == "buy":
                all_port_flows.append((dt, -(sh * pr + charges)))
            elif ttype == "sell":
                all_port_flows.append((dt,  sh * pr - charges))
    # Terminal value = total current market value of all open positions
    if total_mktval > 0:
        today_str = datetime.now(timezone.utc).date().isoformat()
        all_port_flows.append((today_str, total_mktval))
    port_xirr_raw = _xirr(all_port_flows)
    portfolio_xirr = round(port_xirr_raw * 100, 2) if port_xirr_raw is not None else None

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
            "portfolio_xirr":  portfolio_xirr,
        }
    })


@app.route("/api/portfolio/add", methods=["POST"])
def api_portfolio_add():
    body     = request.get_json(force=True, silent=True) or {}
    symbol   = (body.get("symbol")   or "").strip().upper()
    exchange = (body.get("exchange") or "nse").strip().lower()
    tx_type  = (body.get("type")     or "buy").strip().lower()
    tx_date  = (body.get("date")     or "").strip()
    try:
        shares = float(body.get("shares", 0)); price = float(body.get("price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares or price"}), 400
    if not symbol:          return jsonify({"error": "No symbol"}), 400
    if shares <= 0:         return jsonify({"error": "Shares must be > 0"}), 400
    if price  <= 0:         return jsonify({"error": "Price must be > 0"}), 400
    if tx_type not in ("buy", "sell"): return jsonify({"error": "type must be buy or sell"}), 400
    if symbol.endswith(".BO"):  symbol = symbol[:-3]
    elif symbol.endswith(".NS"): symbol = symbol[:-3]
    if not tx_date:
        from datetime import date
        tx_date = date.today().isoformat()
    entries = [_migrate_entry(e) for e in _load_portfolio()]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None:
        entry = {"symbol": symbol, "name": symbol, "exchange": exchange, "transactions": []}
        entries.append(entry)
    entry["exchange"]     = exchange
    # Optional enrichment fields
    jurisdiction = (body.get("jurisdiction") or "").strip().upper() or None
    currency     = (body.get("currency")     or "").strip().upper() or None
    acq_type     = (body.get("acquisition_type") or "secondary").strip().lower()
    if jurisdiction: entry["jurisdiction"] = jurisdiction
    if currency:     entry["currency"]     = currency
    tx = {"date": tx_date, "type": tx_type, "shares": shares, "price": price,
          "acquisition_type": acq_type}
    for field in ("brokerage", "stt", "other_charges"):
        try:
            v = float(body.get(field, 0) or 0)
            if v > 0: tx[field] = round(v, 2)
        except (TypeError, ValueError):
            pass
    entry["transactions"].append(tx)
    _save_portfolio(entries)
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/api/portfolio/remove_tx", methods=["POST"])
def api_portfolio_remove_tx():
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:   idx = int(body.get("tx_index", -1))
    except (TypeError, ValueError): return jsonify({"error": "Invalid tx_index"}), 400
    if not symbol: return jsonify({"error": "No symbol"}), 400
    entries = [_migrate_entry(e) for e in _load_portfolio()]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None: return jsonify({"error": "Symbol not found"}), 404
    txs = entry.get("transactions", [])
    if idx < 0 or idx >= len(txs): return jsonify({"error": "tx_index out of range"}), 400
    txs.pop(idx)
    _save_portfolio(entries)
    return jsonify({"status": "ok"})


@app.route("/api/portfolio/edit_tx", methods=["POST"])
def api_portfolio_edit_tx():
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    try:   idx = int(body.get("tx_index", -1))
    except (TypeError, ValueError): return jsonify({"error": "Invalid tx_index"}), 400
    tx_type = (body.get("type") or "buy").strip().lower()
    tx_date = (body.get("date") or "").strip()
    try:
        shares = float(body.get("shares", 0)); price = float(body.get("price", 0))
    except (TypeError, ValueError): return jsonify({"error": "Invalid shares or price"}), 400
    if not symbol or shares <= 0 or price <= 0: return jsonify({"error": "Invalid input"}), 400
    if tx_type not in ("buy", "sell"): return jsonify({"error": "type must be buy or sell"}), 400
    entries = [_migrate_entry(e) for e in _load_portfolio()]
    entry   = next((e for e in entries if e["symbol"] == symbol), None)
    if entry is None: return jsonify({"error": "Symbol not found"}), 404
    txs = entry.get("transactions", [])
    if idx < 0 or idx >= len(txs): return jsonify({"error": "tx_index out of range"}), 400
    acq_type = (body.get("acquisition_type") or "secondary").strip().lower()
    tx = {"date": tx_date, "type": tx_type, "shares": shares, "price": price,
          "acquisition_type": acq_type}
    for field in ("brokerage", "stt", "other_charges"):
        try:
            v = float(body.get(field, 0) or 0)
            if v > 0: tx[field] = round(v, 2)
        except (TypeError, ValueError):
            pass
    txs[idx] = tx
    _save_portfolio(entries)
    return jsonify({"status": "ok"})


@app.route("/api/portfolio/remove", methods=["POST"])
def api_portfolio_remove():
    body   = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol: return jsonify({"error": "No symbol"}), 400
    entries = _load_portfolio()
    new_entries = [e for e in entries if str(e.get("symbol","")).upper() != symbol]
    if len(new_entries) == len(entries): return jsonify({"error": "Symbol not found"}), 404
    _save_portfolio(new_entries)
    return jsonify({"status": "ok"})


@app.route("/api/portfolio/import_yahoo_csv", methods=["POST"])
def api_portfolio_import_yahoo_csv():
    import csv, io
    f = request.files.get("csv_file")
    if f is None: return jsonify({"error": "No file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
    except Exception:
        return jsonify({"error": "Could not decode file"}), 400

    reader    = csv.DictReader(io.StringIO(text))
    entries   = [_migrate_entry(e) for e in _load_portfolio()]
    entry_map = {e["symbol"]: e for e in entries}
    imported = 0; skipped = 0; new_syms = []; stub_syms = []; needs_data = []; cash_imports = []

    for row in reader:
        raw_sym = (row.get("Symbol") or "").strip()
        if raw_sym == "$$CASH_TX":
            td_raw  = (row.get("Trade Date") or "").strip()
            qty_raw = (row.get("Quantity") or "").strip()
            ctype   = (row.get("Transaction Type") or "").strip().upper()
            if td_raw and qty_raw and ctype in ("DEPOSIT", "WITHDRAWAL"):
                try:
                    amt = float(qty_raw)
                    if amt > 0:
                        td    = str(td_raw).replace("/","").replace("-","")
                        cdate = f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td)==8 else td_raw
                        cash_imports.append({"date": cdate, "type": ctype.lower(), "amount": amt, "note": ""})
                except ValueError:
                    pass
            skipped += 1; continue
        if not raw_sym: skipped += 1; continue
        if raw_sym.endswith(".NS"):   symbol = raw_sym[:-3].upper(); exchange = "nse"
        elif raw_sym.endswith(".BO"): symbol = raw_sym[:-3].upper(); exchange = "bse"
        else:                         symbol = raw_sym.upper();      exchange = "nse"
        trade_date_raw = (row.get("Trade Date") or "").strip()
        qty_raw        = (row.get("Quantity") or "").strip()
        price_raw      = (row.get("Purchase Price") or "").strip()
        tx_type_raw    = (row.get("Transaction Type") or "").strip().lower()
        if not trade_date_raw and not qty_raw and not tx_type_raw:
            if symbol not in entry_map:
                entry_map[symbol] = {"symbol": symbol, "name": symbol,
                                     "exchange": exchange, "transactions": [], "needs_data": True}
                stub_syms.append(symbol); needs_data.append(raw_sym)
            continue
        if not trade_date_raw or not qty_raw or not price_raw: skipped += 1; continue
        try:
            shares = float(qty_raw); price = float(price_raw)
        except ValueError: skipped += 1; continue
        if shares <= 0: skipped += 1; continue
        td      = str(trade_date_raw).replace("/","").replace("-","")
        tx_date = f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td)==8 else trade_date_raw
        tx_type = "sell" if tx_type_raw == "sell" else "buy"
        new_tx  = {"date": tx_date, "type": tx_type, "shares": shares, "price": price}
        if symbol not in entry_map:
            entry_map[symbol] = {"symbol": symbol, "name": symbol, "exchange": exchange, "transactions": []}
            new_syms.append(symbol)
        ex_txs = entry_map[symbol].get("transactions", [])
        dup = any(t.get("date")==tx_date and t.get("type")==tx_type
                  and float(t.get("shares",0))==shares and float(t.get("price",0))==price for t in ex_txs)
        if dup: skipped += 1; continue
        ex_txs.append(new_tx); imported += 1

    seen = set(); rebuilt = []
    for e in entries:
        s = e["symbol"]; rebuilt.append(entry_map.get(s, e)); seen.add(s)
    for s in new_syms + stub_syms:
        if s not in seen: rebuilt.append(entry_map[s]); seen.add(s)
    _save_portfolio(rebuilt)

    if cash_imports:
        existing_cash = _load_cash()
        existing_set  = {(t["date"],t["type"],float(t["amount"])) for t in existing_cash}
        new_cash = [c for c in cash_imports if (c["date"],c["type"],float(c["amount"])) not in existing_set]
        if new_cash:
            existing_cash.extend(new_cash)
            existing_cash.sort(key=lambda t: t.get("date",""))
            _save_cash(existing_cash)

    return jsonify({"status":"ok","imported":imported,"skipped":skipped,
                    "new_symbols":len(new_syms),"stubs":len(stub_syms),
                    "needs_data":needs_data,"cash_imported":len(cash_imports)})


# ---------------------------------------------------------------------------
# CASH API
# ---------------------------------------------------------------------------
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
    try:    amount = float(body.get("amount", 0))
    except: return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0: return jsonify({"error": "Amount must be > 0"}), 400
    if tx_type not in ("deposit", "withdrawal", "dividend"):
        return jsonify({"error": "Invalid type"}), 400
    tx = {"date": date, "type": tx_type, "amount": amount, "note": note}
    if tx_type == "dividend":
        sym = (body.get("symbol") or "").strip().upper()
        if sym: tx["symbol"] = sym
        for field in ("gross_per_share", "shares_at_record", "gross_amount",
                      "tds_rate_pct", "tds_amount"):
            try:
                v = float(body.get(field, 0) or 0)
                if v > 0: tx[field] = round(v, 4)
            except (TypeError, ValueError):
                pass
        jurisdiction = (body.get("jurisdiction") or "").strip().upper()
        if jurisdiction: tx["jurisdiction"] = jurisdiction
    txs = _load_cash()
    txs.append(tx)
    _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


@app.route("/api/cash/remove", methods=["POST"])
def api_cash_remove():
    body = request.get_json(force=True, silent=True) or {}
    try:   idx = int(body.get("index", -1))
    except: return jsonify({"error": "Invalid index"}), 400
    txs = _load_cash()
    if idx < 0 or idx >= len(txs): return jsonify({"error": "Index out of range"}), 400
    txs.pop(idx); _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


@app.route("/api/cash/edit", methods=["POST"])
def api_cash_edit():
    body    = request.get_json(force=True, silent=True) or {}
    try:    idx = int(body.get("index", -1))
    except: return jsonify({"error": "Invalid index"}), 400
    tx_type = (body.get("type") or "deposit").strip().lower()
    date    = (body.get("date") or "").strip()
    note    = str(body.get("note") or "").strip()[:200]
    try:    amount = float(body.get("amount", 0))
    except: return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0: return jsonify({"error": "Amount must be > 0"}), 400
    if tx_type not in ("deposit", "withdrawal", "dividend"):
        return jsonify({"error": "Invalid type"}), 400
    txs = _load_cash()
    if idx < 0 or idx >= len(txs): return jsonify({"error": "Index out of range"}), 400
    tx = {"date": date, "type": tx_type, "amount": amount, "note": note}
    if tx_type == "dividend":
        sym = (body.get("symbol") or "").strip().upper()
        if sym: tx["symbol"] = sym
        for field in ("gross_per_share", "shares_at_record", "gross_amount",
                      "tds_rate_pct", "tds_amount"):
            try:
                v = float(body.get(field, 0) or 0)
                if v > 0: tx[field] = round(v, 4)
            except (TypeError, ValueError):
                pass
        jurisdiction = (body.get("jurisdiction") or "").strip().upper()
        if jurisdiction: tx["jurisdiction"] = jurisdiction
    txs[idx] = tx
    _save_cash(txs)
    return jsonify({"status": "ok", "balance": _calc_cash_balance(txs)})


# ---------------------------------------------------------------------------
# CORPORATE ACTIONS API
# ---------------------------------------------------------------------------
@app.route("/api/corporate_actions")
def api_ca_list():
    return jsonify(_load_ca())


@app.route("/api/corporate_actions/add", methods=["POST"])
def api_ca_add():
    body    = request.get_json(force=True, silent=True) or {}
    ca_type = (body.get("type") or "").strip().lower()
    valid   = {"merger","amalgamation","name_change","demerger","spinoff",
               "spin-off","split","subdivision","bonus"}
    if ca_type not in valid: return jsonify({"error": f"Unknown type '{ca_type}'"}), 400
    date = (body.get("date") or "").strip()
    note = str(body.get("note") or "").strip()[:300]
    rec  = {"type": ca_type, "date": date, "note": note}
    if ca_type in ("split", "subdivision", "bonus"):
        sym = (body.get("symbol") or "").strip().upper()
        if not sym: return jsonify({"error": "symbol required"}), 400
        try:    ratio = float(body.get("ratio", 0))
        except: return jsonify({"error": "ratio must be a number"}), 400
        if ratio <= 0: return jsonify({"error": "ratio must be > 0"}), 400
        rec["symbol"] = sym; rec["ratio"] = ratio
    else:
        from_sym = (body.get("from_symbol") or "").strip().upper()
        to_sym   = (body.get("to_symbol")   or "").strip().upper()
        if not from_sym or not to_sym: return jsonify({"error": "from_symbol and to_symbol required"}), 400
        rec["from_symbol"] = from_sym; rec["to_symbol"] = to_sym
        if "ratio" in body:
            try:   rec["ratio"] = float(body["ratio"])
            except: pass
        if "cost_allocation_pct" in body:
            try:   rec["cost_allocation_pct"] = round(max(0.0, min(100.0, float(body["cost_allocation_pct"]))), 4)
            except: pass
    actions = _load_ca()
    rec["id"] = f"ca-{len(actions)+1:03d}"
    actions.append(rec); _save_ca(actions)
    return jsonify({"status": "ok", "action": rec})


@app.route("/api/corporate_actions/remove", methods=["POST"])
def api_ca_remove():
    body = request.get_json(force=True, silent=True) or {}
    try:   idx = int(body.get("index", -1))
    except: return jsonify({"error": "Invalid index"}), 400
    actions = _load_ca()
    if idx < 0 or idx >= len(actions): return jsonify({"error": "Index out of range"}), 400
    actions.pop(idx); _save_ca(actions)
    return jsonify({"status": "ok"})


@app.route("/api/corporate_actions/edit", methods=["POST"])
def api_ca_edit():
    body = request.get_json(force=True, silent=True) or {}
    try:   idx = int(body.get("index", -1))
    except: return jsonify({"error": "Invalid index"}), 400
    actions = _load_ca()
    if idx < 0 or idx >= len(actions): return jsonify({"error": "Index out of range"}), 400
    ca_type = (body.get("type") or "").strip().lower()
    valid   = {"merger","amalgamation","name_change","demerger","spinoff",
               "spin-off","split","subdivision","bonus"}
    if ca_type not in valid: return jsonify({"error": f"Unknown type '{ca_type}'"}), 400
    existing_id = actions[idx].get("id", f"ca-{idx+1:03d}")
    date = (body.get("date") or "").strip()
    note = str(body.get("note") or "").strip()[:300]
    rec  = {"id": existing_id, "type": ca_type, "date": date, "note": note}
    ca_cross = {"merger","amalgamation","name_change","demerger","spinoff","spin-off"}
    if ca_type in ca_cross:
        from_sym = (body.get("from_symbol") or "").strip().upper()
        to_sym   = (body.get("to_symbol")   or "").strip().upper()
        if not from_sym or not to_sym: return jsonify({"error": "from_symbol and to_symbol required"}), 400
        rec["from_symbol"] = from_sym; rec["to_symbol"] = to_sym
        if "ratio" in body:
            try:   rec["ratio"] = float(body["ratio"])
            except: pass
    else:
        sym = (body.get("symbol") or "").strip().upper()
        if not sym: return jsonify({"error": "symbol required"}), 400
        try:    ratio = float(body.get("ratio", 0))
        except: return jsonify({"error": "ratio must be a number"}), 400
        if ratio <= 0: return jsonify({"error": "ratio must be > 0"}), 400
        rec["symbol"] = sym; rec["ratio"] = ratio
    actions[idx] = rec
    _save_ca(actions)
    return jsonify({"status": "ok", "action": rec})


# ---------------------------------------------------------------------------
# CAPITAL GAINS API
# ---------------------------------------------------------------------------
@app.route("/api/capital_gains")
def api_capital_gains():
    entries = [_migrate_entry(e) for e in _load_portfolio()]
    ca_list = _load_ca()
    with _cache_lock:
        prices = dict(_price_cache)   # snapshot — keys are symbol strings
    result  = compute_all_cg(entries, ca_list, prices)
    return jsonify(result)


@app.route("/api/capital_gains/tax_rates")
def api_cg_tax_rates():
    return jsonify(get_tax_rate_table())


@app.route("/api/capital_gains/csv")
def api_capital_gains_csv():
    import csv, io
    entries = [_migrate_entry(e) for e in _load_portfolio()]
    with _cache_lock:
        prices = dict(_price_cache)
    result  = compute_all_cg(entries, _load_ca(), prices)
    events  = result.get("events", [])

    output = io.StringIO()
    fields = [
        "symbol", "jurisdiction", "currency", "acquisition_type",
        "buy_date", "sell_date", "shares",
        "buy_cost_per_share", "effective_cost_per_share",
        "sell_price_per_share", "sell_charges_per_share",
        "holding_days", "holding_period",
        "gross_gain", "tax_rate_pct", "estimated_tax", "after_tax_gain",
        "fy", "is_grandfathered", "is_unrealized",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(events)

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"]        = "text/csv"
    resp.headers["Content-Disposition"] = 'attachment; filename="capital_gains.csv"'
    return resp


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    os.chdir(BASE_DIR)
    _load_price_cache_from_disk()
    t = threading.Thread(target=_background_price_updater, daemon=True)
    t.start()
    print("=" * 50)
    print("  RULIAD CAPITAL — PORTFOLIO")
    print("  http://localhost:5001")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
