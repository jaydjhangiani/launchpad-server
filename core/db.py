"""SQLite helpers: connection, schema creation, panel/stock/dead-symbol queries."""

import os
import json
import sqlite3

DB_FILE = "launchpad.db"


def _db_connect():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def _init_db() -> None:
    """Create DB tables. On first run populates panels + panel_stocks from sheet_data.json."""
    con = _db_connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS panels (
            panel_id      TEXT PRIMARY KEY,
            sector_name   TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS panel_stocks (
            panel_id TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            name     TEXT,
            PRIMARY KEY (panel_id, symbol)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dead_symbols (
            symbol TEXT PRIMARY KEY,
            misses INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    if con.execute("SELECT COUNT(*) FROM panel_stocks").fetchone()[0] == 0:
        if os.path.exists("sheet_data.json"):
            with open("sheet_data.json") as _fh:
                _jdata = json.load(_fh)
            _rows = []
            for _pid, _stocks in _jdata.items():
                for _s in _stocks:
                    _rows.append((_pid, _s["symbol"], _s.get("name", _s["symbol"])))
            con.executemany(
                "INSERT OR IGNORE INTO panel_stocks (panel_id, symbol, name) VALUES (?,?,?)",
                _rows
            )
            con.commit()
            print(f"[DB] Migrated sheet_data.json → {DB_FILE} ({len(_rows)} rows)")
        else:
            print("[DB] WARNING: launchpad.db is empty and no sheet_data.json found.")
    con.close()


def _load_panels_from_db() -> list:
    """Return [{panel_id, sector_name, stocks:[{symbol,name}]}, ...] ordered by display_order."""
    con = _db_connect()
    panel_rows = con.execute(
        "SELECT panel_id, sector_name FROM panels ORDER BY display_order"
    ).fetchall()
    stock_rows = con.execute(
        "SELECT panel_id, symbol, name FROM panel_stocks ORDER BY rowid"
    ).fetchall()
    con.close()
    stocks_by_panel: dict = {}
    for pid, sym, name in stock_rows:
        clean = sym
        if clean.upper().endswith(".BO"):   clean = clean[:-3]
        elif clean.upper().endswith(".NS"): clean = clean[:-3]
        stocks_by_panel.setdefault(pid, []).append({"symbol": clean, "name": name or clean})
    result = []
    for pid, sector_name in panel_rows:
        result.append({
            "panel_id":    pid,
            "sector_name": sector_name,
            "stocks":      stocks_by_panel.get(pid, []),
        })
    return result


def _load_dead_symbols_from_db() -> dict:
    """Return {symbol: miss_count} for all persisted dead symbols."""
    con = _db_connect()
    rows = con.execute("SELECT symbol, misses FROM dead_symbols").fetchall()
    con.close()
    return {sym: misses for sym, misses in rows}


def _db_mark_dead(symbol: str, misses: int) -> None:
    """Upsert a dead-symbol record (called when miss count reaches threshold)."""
    con = _db_connect()
    con.execute(
        "INSERT INTO dead_symbols (symbol, misses) VALUES (?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET misses=excluded.misses",
        (symbol, misses)
    )
    con.commit()
    con.close()


def _db_clear_dead(symbol: str) -> None:
    """Remove a symbol from dead_symbols (it resolved again)."""
    con = _db_connect()
    con.execute("DELETE FROM dead_symbols WHERE symbol=?", (symbol,))
    con.commit()
    con.close()
