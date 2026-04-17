#!/usr/bin/env python
"""
One-time migration: transfer user_customizations.json → NeonDB.

Migrates:
  - 26 user-created (_uc_*) panels and their stocks
  - Page assignments for all panels
  - Height settings for all panels
  - Display order (__order__)
  - Page names (__page_names__)
  - Page count (__page_count__)

Run ONCE after deploying the NeonDB-only app:
    python migrate_customs_to_neon.py
"""

import json
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
CUSTOMS_FILE = "user_customizations.json"


def main():
    if not os.path.exists(CUSTOMS_FILE):
        print(f"{CUSTOMS_FILE} not found — nothing to migrate.")
        return

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set in .env")
        return

    with open(CUSTOMS_FILE) as f:
        customs = json.load(f)

    con = psycopg2.connect(url)
    cur = con.cursor()

    # Fetch panels already in DB
    cur.execute("SELECT panel_id FROM panels")
    existing = {row[0] for row in cur.fetchall()}
    print(f"Panels already in DB: {len(existing)}")

    order      = customs.get("__order__", [])
    pages      = customs.get("__pages__", {})
    heights    = customs.get("__heights__", {})
    page_names = customs.get("__page_names__", {})
    page_count = customs.get("__page_count__", 0)

    # ── 1. Bulk-update display_order for DB panels from __order__ ───────────
    for i, pid in enumerate(order):
        if pid in existing:
            cur.execute(
                "UPDATE panels SET display_order = %s WHERE panel_id = %s", (i, pid)
            )
    print("Updated display_order for existing panels")

    # ── 2. Create user-created panels ───────────────────────────────────────
    created = 0
    for i, pid in enumerate(order):
        if pid.startswith("_uc_") and pid not in existing:
            v = customs.get(pid, {})
            sector = v.get("sector_name", "Custom Panel")
            mode   = v.get("mode", "nse")
            page   = pages.get(pid, 0)
            cur.execute(
                "INSERT INTO panels (panel_id, sector_name, mode, display_order, page) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (panel_id) DO NOTHING",
                (pid, sector, mode, i, page),
            )
            removed = set(v.get("removed", []))
            added   = [s for s in v.get("added", [])
                       if s.get("symbol") and s["symbol"] not in removed]
            for j, s in enumerate(added):
                cur.execute(
                    "INSERT INTO panel_stocks (panel_id, symbol, name, stock_order) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (panel_id, symbol) DO NOTHING",
                    (pid, s["symbol"], s.get("name", s["symbol"]), j),
                )
            print(f"  Created {sector} ({pid}) — {len(added)} stocks")
            created += 1
    print(f"Created {created} user-created panels")

    # ── 3. Update page + height for ALL panels ───────────────────────────────
    all_pids = existing | {k for k in customs if k.startswith("_uc_")}
    updated_pg = 0
    updated_ht = 0
    for pid in all_pids:
        pg = pages.get(pid)
        ht = heights.get(pid)
        if pg is not None:
            cur.execute("UPDATE panels SET page = %s WHERE panel_id = %s", (pg, pid))
            updated_pg += 1
        if ht is not None:
            cur.execute("UPDATE panels SET height = %s WHERE panel_id = %s", (ht, pid))
            updated_ht += 1
    print(f"Updated page for {updated_pg} panels, height for {updated_ht} panels")

    # ── 4. Propagate sector_name renames stored in JSON → DB ────────────────
    renamed = 0
    for pid in existing:
        v = customs.get(pid, {})
        if v.get("sector_name"):
            cur.execute(
                "UPDATE panels SET sector_name = %s WHERE panel_id = %s",
                (v["sector_name"], pid),
            )
            renamed += 1
    print(f"Propagated {renamed} sector name overrides to DB")

    # ── 5. Page names ────────────────────────────────────────────────────────
    for idx_str, name in page_names.items():
        if name:
            cur.execute(
                "INSERT INTO page_names (page_index, name) VALUES (%s, %s) "
                "ON CONFLICT (page_index) DO UPDATE SET name = EXCLUDED.name",
                (int(idx_str), name),
            )
    print(f"Inserted {len(page_names)} page names: {page_names}")

    # ── 6. Page count ────────────────────────────────────────────────────────
    if page_count:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('page_count', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(page_count),),
        )
        print(f"Set page_count = {page_count}")

    con.commit()
    cur.close()
    con.close()
    print("\nMigration complete! You can now delete user_customizations.json.")


if __name__ == "__main__":
    main()
