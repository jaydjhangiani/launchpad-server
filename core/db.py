"""PostgreSQL helpers (NeonDB): connection pool, schema creation, panel/stock/dead-symbol queries."""

import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

# ─── Connection pool ──────────────────────────────────────────────────────
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = os.environ.get("DATABASE_URL")
                if not url:
                    raise RuntimeError("DATABASE_URL environment variable is not set")
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, url)
    return _pool


@contextmanager
def _get_conn():
    """Borrow a connection from the pool; commit on clean exit, rollback on error."""
    pool = _get_pool()
    con = pool.getconn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        pool.putconn(con)


def _init_db() -> None:
    """Create/update DB tables (idempotent)."""
    with _get_conn() as con:
        with con.cursor() as cur:
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
            # Add new columns to existing deployments — savepoints prevent
            # a column-already-exists error from aborting the transaction
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
                    exchange    TEXT NOT NULL DEFAULT 'nse',
                    PRIMARY KEY (panel_id, symbol)
                )
            """)
            # Add exchange column to existing deployments (idempotent)
            try:
                cur.execute("SAVEPOINT add_exchange")
                cur.execute("ALTER TABLE panel_stocks ADD COLUMN exchange TEXT NOT NULL DEFAULT 'nse'")
                cur.execute("RELEASE SAVEPOINT add_exchange")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT add_exchange")
            # Add FK constraint so console deletes of panels cascade to stocks
            try:
                cur.execute("SAVEPOINT add_fk")
                cur.execute("""
                    ALTER TABLE panel_stocks
                    ADD CONSTRAINT fk_panel_stocks_panel_id
                    FOREIGN KEY (panel_id) REFERENCES panels(panel_id) ON DELETE CASCADE
                """)
                cur.execute("RELEASE SAVEPOINT add_fk")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT add_fk")
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


def _load_panels_from_db() -> list:
    """Return panels ordered by display_order, each with mode/page/height and stock list."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT panel_id, sector_name, mode, page, height FROM panels ORDER BY display_order"
            )
            panel_rows = cur.fetchall()
            cur.execute(
                "SELECT panel_id, symbol, name, COALESCE(exchange,'nse') FROM panel_stocks ORDER BY panel_id, stock_order"
            )
            stock_rows = cur.fetchall()
    stocks_by_panel: dict = {}
    for pid, sym, name, exch in stock_rows:
        clean = sym
        if clean.upper().endswith(".BO"):   clean = clean[:-3]
        elif clean.upper().endswith(".NS"): clean = clean[:-3]
        stocks_by_panel.setdefault(pid, []).append({"symbol": clean, "name": name or clean, "exchange": exch or "nse"})
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
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT symbol, misses FROM dead_symbols")
            return {sym: misses for sym, misses in cur.fetchall()}


def _db_mark_dead(symbol: str, misses: int) -> None:
    """Upsert a dead-symbol record (called when miss count reaches threshold)."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO dead_symbols (symbol, misses) VALUES (%s, %s) "
                "ON CONFLICT (symbol) DO UPDATE SET misses = EXCLUDED.misses",
                (symbol, misses),
            )


def _db_clear_dead(symbol: str) -> None:
    """Remove a symbol from dead_symbols (it resolved again)."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM dead_symbols WHERE symbol = %s", (symbol,))


# ---------------------------------------------------------------------------
# Stock mutation helpers — direct writes, no in-memory mirror needed
# ---------------------------------------------------------------------------

def _db_add_stock(panel_id: str, symbol: str, name: str, exchange: str = "nse") -> None:
    """Append a stock to panel_stocks (no-op if already present, updates exchange if it exists)."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(stock_order), -1) + 1 FROM panel_stocks WHERE panel_id = %s",
                (panel_id,),
            )
            next_order = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order, exchange) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (panel_id, symbol) DO UPDATE SET exchange = EXCLUDED.exchange",
                (panel_id, symbol, name, next_order, exchange),
            )


def _db_remove_stock(panel_id: str, symbol: str) -> None:
    """Delete a stock from panel_stocks."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM panel_stocks WHERE panel_id = %s AND symbol = %s",
                (panel_id, symbol),
            )


def _db_rename_stock(panel_id: str, old_symbol: str, new_symbol: str) -> None:
    """Rename a stock symbol in-place within a panel."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE panel_stocks SET symbol = %s, name = %s "
                "WHERE panel_id = %s AND symbol = %s",
                (new_symbol, new_symbol, panel_id, old_symbol),
            )


def _db_rename_panel(panel_id: str, new_name: str) -> None:
    """Update sector_name for a panel."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE panels SET sector_name = %s WHERE panel_id = %s",
                (new_name, panel_id),
            )


def _db_move_stock(src_panel_id: str, dst_panel_id: str, symbol: str, name: str) -> None:
    """Move a stock from one DB panel to another atomically.
    Raises ValueError if symbol is not in source panel.
    """
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM panel_stocks WHERE panel_id = %s AND symbol = %s",
                (src_panel_id, symbol),
            )
            if cur.rowcount == 0:
                raise ValueError(f"{symbol} not found in source panel {src_panel_id}")
            cur.execute(
                "SELECT COALESCE(MAX(stock_order), -1) + 1 FROM panel_stocks WHERE panel_id = %s",
                (dst_panel_id,),
            )
            next_order = cur.fetchone()[0]
            # Use DO UPDATE so if the symbol already exists in dst it gets
            # overwritten rather than the DELETE being rolled back silently.
            cur.execute(
                "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (panel_id, symbol) DO UPDATE SET name = EXCLUDED.name",
                (dst_panel_id, symbol, name, next_order),
            )


# ---------------------------------------------------------------------------
# Panel lifecycle helpers
# ---------------------------------------------------------------------------

def _db_next_panel_id() -> str:
    """Return the next sequential panel ID (Sheet N+1)."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT panel_id FROM panels WHERE panel_id ~ '^Sheet [0-9]+$'")
            nums = []
            for (pid,) in cur.fetchall():
                try:
                    nums.append(int(pid.split()[1]))
                except (IndexError, ValueError):
                    pass
            return f"Sheet {max(nums, default=0) + 1}"


def _db_create_panel(panel_id: str, sector_name: str, mode: str,
                     display_order: int, page: int) -> None:
    """Insert a new panel row."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO panels (panel_id, sector_name, mode, display_order, page) "
                "VALUES (%s, %s, %s, %s, %s)",
                (panel_id, sector_name, mode, display_order, page),
            )


def _db_delete_panel(panel_id: str) -> None:
    """Delete a panel and all its stocks (FK CASCADE handles panel_stocks)."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM panels WHERE panel_id = %s", (panel_id,))


def _db_update_display_orders(ordered_ids: list) -> None:
    """Bulk-update display_order so it matches the position in ordered_ids."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.executemany(
                "UPDATE panels SET display_order = %s WHERE panel_id = %s",
                [(i, pid) for i, pid in enumerate(ordered_ids)],
            )


def _db_set_panel_page(panel_id: str, page: int) -> None:
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("UPDATE panels SET page = %s WHERE panel_id = %s", (page, panel_id))


def _db_set_panel_height(panel_id: str, height: int) -> None:
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("UPDATE panels SET height = %s WHERE panel_id = %s", (height, panel_id))


def _db_shift_pages(threshold: int, delta: int) -> None:
    """Shift page numbers by delta for all panels where page >= threshold."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE panels SET page = page + %s WHERE page >= %s", (delta, threshold)
            )


def _db_remap_pages(remap: dict) -> None:
    """Bulk remap panel page numbers: remap = {old_page: new_page}.
    Uses a two-phase update to avoid conflicts when pages swap."""
    changed = [(old, new) for old, new in remap.items() if old != new]
    if not changed:
        return
    with _get_conn() as con:
        with con.cursor() as cur:
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


# ---------------------------------------------------------------------------
# Page-name and settings helpers
# ---------------------------------------------------------------------------

def _db_get_page_count() -> int:
    """Return stored page count, falling back to max(page)+1 from panels."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'page_count'")
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT COALESCE(MAX(page) + 1, 1) FROM panels")
                return cur.fetchone()[0]
            return int(row[0])


def _db_set_page_count(count: int) -> None:
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES ('page_count', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(count),),
            )


def _db_get_page_names() -> dict:
    """Return {str(page_index): name} for all named pages."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT page_index, name FROM page_names")
            return {str(idx): name for idx, name in cur.fetchall()}


def _db_set_page_name(page_index: int, name: str) -> None:
    """Upsert a page name, or delete it when name is empty."""
    with _get_conn() as con:
        with con.cursor() as cur:
            if name:
                cur.execute(
                    "INSERT INTO page_names (page_index, name) VALUES (%s, %s) "
                    "ON CONFLICT (page_index) DO UPDATE SET name = EXCLUDED.name",
                    (page_index, name),
                )
            else:
                cur.execute("DELETE FROM page_names WHERE page_index = %s", (page_index,))


def _db_remap_page_names(remap: dict) -> None:
    """Remap page_names indices: remap = {old_index: new_index}."""
    with _get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT page_index, name FROM page_names")
            rows = cur.fetchall()
            if not rows:
                return
            cur.execute("DELETE FROM page_names")
            new_rows = [(remap.get(idx, idx), name) for idx, name in rows]
            cur.executemany(
                "INSERT INTO page_names (page_index, name) VALUES (%s, %s) "
                "ON CONFLICT (page_index) DO NOTHING",
                new_rows,
            )
