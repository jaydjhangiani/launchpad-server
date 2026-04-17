"""PostgreSQL helpers (NeonDB): connection, schema creation, panel/stock/dead-symbol queries."""

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _db_connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return psycopg2.connect(url)


def _init_db() -> None:
    """Create DB tables if they don't exist."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panels (
            panel_id      TEXT PRIMARY KEY,
            sector_name   TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_stocks (
            panel_id    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            name        TEXT,
            stock_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (panel_id, symbol)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dead_symbols (
            symbol TEXT PRIMARY KEY,
            misses INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    cur.close()
    con.close()


def _load_panels_from_db() -> list:
    """Return [{panel_id, sector_name, stocks:[{symbol,name}]}, ...] ordered by display_order."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("SELECT panel_id, sector_name FROM panels ORDER BY display_order")
    panel_rows = cur.fetchall()
    cur.execute("SELECT panel_id, symbol, name FROM panel_stocks ORDER BY panel_id, stock_order")
    stock_rows = cur.fetchall()
    cur.close()
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
    cur = con.cursor()
    cur.execute("SELECT symbol, misses FROM dead_symbols")
    rows = cur.fetchall()
    cur.close()
    con.close()
    return {sym: misses for sym, misses in rows}


def _db_mark_dead(symbol: str, misses: int) -> None:
    """Upsert a dead-symbol record (called when miss count reaches threshold)."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO dead_symbols (symbol, misses) VALUES (%s, %s) "
        "ON CONFLICT (symbol) DO UPDATE SET misses = EXCLUDED.misses",
        (symbol, misses)
    )
    con.commit()
    cur.close()
    con.close()


def _db_clear_dead(symbol: str) -> None:
    """Remove a symbol from dead_symbols (it resolved again)."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM dead_symbols WHERE symbol = %s", (symbol,))
    con.commit()
    cur.close()
    con.close()
