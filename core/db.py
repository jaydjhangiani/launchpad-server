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
    """Create/update DB tables (idempotent)."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panels (
            panel_id      TEXT PRIMARY KEY,
            sector_name   TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0,
            mode          TEXT NOT NULL DEFAULT 'nse',
            page          INTEGER NOT NULL DEFAULT 0,
            height        INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Add new columns to existing deployments — use savepoints so a
    # column-already-exists error doesn't abort the whole transaction
    for col, defn in [
        ("mode",   "TEXT NOT NULL DEFAULT 'nse'"),
        ("page",   "INTEGER NOT NULL DEFAULT 0"),
        ("height", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        try:
            cur.execute(f"SAVEPOINT add_{col}")
            cur.execute(f"ALTER TABLE panels ADD COLUMN {col} {defn}")
            cur.execute(f"RELEASE SAVEPOINT add_{col}")
        except Exception:
            cur.execute(f"ROLLBACK TO SAVEPOINT add_{col}")
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS page_names (
            page_index INTEGER PRIMARY KEY,
            name       TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    con.commit()
    cur.close()
    con.close()


def _load_panels_from_db() -> list:
    """Return panels ordered by display_order, each with mode/page/height and stock list."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT panel_id, sector_name, mode, page, height FROM panels ORDER BY display_order"
    )
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
    for pid, sector_name, mode, page, height in panel_rows:
        result.append({
            "panel_id":    pid,
            "sector_name": sector_name,
            "mode":        mode or "nse",
            "page":        page if page is not None else 0,
            "height":      height if height is not None else 1,
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


# ---------------------------------------------------------------------------
# Stock mutation helpers — direct writes, no in-memory mirror needed
# ---------------------------------------------------------------------------

def _db_add_stock(panel_id: str, symbol: str, name: str) -> None:
    """Append a stock to panel_stocks (no-op if already present)."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(MAX(stock_order), -1) + 1 FROM panel_stocks WHERE panel_id = %s",
        (panel_id,),
    )
    next_order = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (panel_id, symbol) DO NOTHING",
        (panel_id, symbol, name, next_order),
    )
    con.commit()
    cur.close()
    con.close()


def _db_remove_stock(panel_id: str, symbol: str) -> None:
    """Delete a stock from panel_stocks."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM panel_stocks WHERE panel_id = %s AND symbol = %s",
        (panel_id, symbol),
    )
    con.commit()
    cur.close()
    con.close()


def _db_rename_stock(panel_id: str, old_symbol: str, new_symbol: str) -> None:
    """Rename a stock symbol in-place within a panel."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE panel_stocks SET symbol = %s, name = %s "
        "WHERE panel_id = %s AND symbol = %s",
        (new_symbol, new_symbol, panel_id, old_symbol),
    )
    con.commit()
    cur.close()
    con.close()


def _db_rename_panel(panel_id: str, new_name: str) -> None:
    """Update sector_name for a panel."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE panels SET sector_name = %s WHERE panel_id = %s",
        (new_name, panel_id),
    )
    con.commit()
    cur.close()
    con.close()


def _db_move_stock(src_panel_id: str, dst_panel_id: str, symbol: str, name: str) -> None:
    """Move a stock from one DB panel to another atomically."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM panel_stocks WHERE panel_id = %s AND symbol = %s",
        (src_panel_id, symbol),
    )
    cur.execute(
        "SELECT COALESCE(MAX(stock_order), -1) + 1 FROM panel_stocks WHERE panel_id = %s",
        (dst_panel_id,),
    )
    next_order = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (panel_id, symbol) DO NOTHING",
        (dst_panel_id, symbol, name, next_order),
    )
    con.commit()
    cur.close()
    con.close()


# ---------------------------------------------------------------------------
# Panel lifecycle helpers
# ---------------------------------------------------------------------------

def _db_create_panel(panel_id: str, sector_name: str, mode: str,
                     display_order: int, page: int) -> None:
    """Insert a new panel row."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO panels (panel_id, sector_name, mode, display_order, page) "
        "VALUES (%s, %s, %s, %s, %s)",
        (panel_id, sector_name, mode, display_order, page),
    )
    con.commit()
    cur.close()
    con.close()


def _db_delete_panel(panel_id: str) -> None:
    """Delete a panel and all its stocks."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM panel_stocks WHERE panel_id = %s", (panel_id,))
    cur.execute("DELETE FROM panels WHERE panel_id = %s", (panel_id,))
    con.commit()
    cur.close()
    con.close()


def _db_update_display_orders(ordered_ids: list) -> None:
    """Bulk-update display_order so it matches the position in ordered_ids."""
    con = _db_connect()
    cur = con.cursor()
    cur.executemany(
        "UPDATE panels SET display_order = %s WHERE panel_id = %s",
        [(i, pid) for i, pid in enumerate(ordered_ids)],
    )
    con.commit()
    cur.close()
    con.close()


def _db_set_panel_page(panel_id: str, page: int) -> None:
    con = _db_connect()
    cur = con.cursor()
    cur.execute("UPDATE panels SET page = %s WHERE panel_id = %s", (page, panel_id))
    con.commit()
    cur.close()
    con.close()


def _db_set_panel_height(panel_id: str, height: int) -> None:
    con = _db_connect()
    cur = con.cursor()
    cur.execute("UPDATE panels SET height = %s WHERE panel_id = %s", (height, panel_id))
    con.commit()
    cur.close()
    con.close()


def _db_shift_pages(threshold: int, delta: int) -> None:
    """Shift page numbers by delta for all panels where page >= threshold."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE panels SET page = page + %s WHERE page >= %s", (delta, threshold)
    )
    con.commit()
    cur.close()
    con.close()


def _db_remap_pages(remap: dict) -> None:
    """Bulk remap panel page numbers: remap = {old_page: new_page}.
    Uses a two-phase update to avoid conflicts when pages swap."""
    changed = [(old, new) for old, new in remap.items() if old != new]
    if not changed:
        return
    con = _db_connect()
    cur = con.cursor()
    # Phase 1: move to unique negative temp values
    cur.executemany(
        "UPDATE panels SET page = -(page + 1) WHERE page = %s",
        [(old,) for old, _ in changed],
    )
    # Phase 2: apply final mapping
    cur.executemany(
        "UPDATE panels SET page = %s WHERE page = %s",
        [(new, -(old + 1)) for old, new in changed],
    )
    con.commit()
    cur.close()
    con.close()


# ---------------------------------------------------------------------------
# Page-name and settings helpers
# ---------------------------------------------------------------------------

def _db_get_page_count() -> int:
    """Return stored page count, falling back to max(page)+1 from panels."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'page_count'")
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT COALESCE(MAX(page) + 1, 1) FROM panels")
        count = cur.fetchone()[0]
    else:
        count = int(row[0])
    cur.close()
    con.close()
    return count


def _db_set_page_count(count: int) -> None:
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES ('page_count', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (str(count),),
    )
    con.commit()
    cur.close()
    con.close()


def _db_get_page_names() -> dict:
    """Return {str(page_index): name} for all named pages."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("SELECT page_index, name FROM page_names")
    rows = cur.fetchall()
    cur.close()
    con.close()
    return {str(idx): name for idx, name in rows}


def _db_set_page_name(page_index: int, name: str) -> None:
    """Upsert a page name, or delete it when name is empty."""
    con = _db_connect()
    cur = con.cursor()
    if name:
        cur.execute(
            "INSERT INTO page_names (page_index, name) VALUES (%s, %s) "
            "ON CONFLICT (page_index) DO UPDATE SET name = EXCLUDED.name",
            (page_index, name),
        )
    else:
        cur.execute("DELETE FROM page_names WHERE page_index = %s", (page_index,))
    con.commit()
    cur.close()
    con.close()


def _db_remap_page_names(remap: dict) -> None:
    """Remap page_names indices: remap = {old_index: new_index}."""
    con = _db_connect()
    cur = con.cursor()
    cur.execute("SELECT page_index, name FROM page_names")
    rows = cur.fetchall()
    if not rows:
        cur.close()
        con.close()
        return
    cur.execute("DELETE FROM page_names")
    new_rows = [(remap.get(idx, idx), name) for idx, name in rows]
    cur.executemany(
        "INSERT INTO page_names (page_index, name) VALUES (%s, %s) "
        "ON CONFLICT (page_index) DO NOTHING",
        new_rows,
    )
    con.commit()
    cur.close()
    con.close()
