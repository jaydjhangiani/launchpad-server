#!/usr/bin/env python
"""
One-time migration: copy launchpad.db (SQLite) → NeonDB (PostgreSQL).

Usage:
  1. Ensure DATABASE_URL is set in your .env file.
  2. Run:  python migrate_to_neon.py
  3. Verify the output, then delete this script and launchpad.db if all is well.
"""

import os
import sqlite3
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SQLITE_FILE = "launchpad.db"


def main():
    if not os.path.exists(SQLITE_FILE):
        print(f"ERROR: {SQLITE_FILE} not found — nothing to migrate.")
        return

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL is not set. Add it to your .env file.")
        return

    print(f"Reading from {SQLITE_FILE} ...")
    src = sqlite3.connect(SQLITE_FILE)
    panels = src.execute(
        "SELECT panel_id, sector_name, display_order FROM panels ORDER BY display_order"
    ).fetchall()
    stocks = src.execute(
        "SELECT panel_id, symbol, name FROM panel_stocks ORDER BY rowid"
    ).fetchall()
    dead = src.execute(
        "SELECT symbol, misses FROM dead_symbols"
    ).fetchall()
    src.close()
    print(f"  {len(panels)} panels, {len(stocks)} stocks, {len(dead)} dead symbols")

    print("Connecting to NeonDB ...")
    dst = psycopg2.connect(url)
    cur = dst.cursor()

    # Ensure tables exist (idempotent)
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

    # Migrate panels
    for row in panels:
        cur.execute(
            "INSERT INTO panels (panel_id, sector_name, display_order) VALUES (%s, %s, %s) "
            "ON CONFLICT (panel_id) DO UPDATE SET "
            "  sector_name = EXCLUDED.sector_name, "
            "  display_order = EXCLUDED.display_order",
            row,
        )
    print(f"  Migrated {len(panels)} panels")

    # Migrate panel_stocks — track per-panel insertion order for stock_order
    order_counter: dict = {}
    for panel_id, symbol, name in stocks:
        i = order_counter.get(panel_id, 0)
        cur.execute(
            "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (panel_id, symbol) DO NOTHING",
            (panel_id, symbol, name, i),
        )
        order_counter[panel_id] = i + 1
    print(f"  Migrated {len(stocks)} stocks")

    # Migrate dead symbols
    for row in dead:
        cur.execute(
            "INSERT INTO dead_symbols (symbol, misses) VALUES (%s, %s) "
            "ON CONFLICT (symbol) DO UPDATE SET misses = EXCLUDED.misses",
            row,
        )
    print(f"  Migrated {len(dead)} dead symbols")

    dst.commit()
    cur.close()
    dst.close()
    print("\nMigration complete! You can now delete launchpad.db.")


if __name__ == "__main__":
    main()
