# How to Edit Stocks & Add / Remove Tickers
### Bloomberg Launchpad – Quick Reference Card

---

## The 3 Ways to Manage Stocks

| Method | Best For | Persists? |
|---|---|---|
| **A — UI (browser)** | Daily use, one-off changes | ✅ Yes |
| **B — REST API / curl** | Scripting, bulk adds | ✅ Yes |
| **C — Edit JSON files** | Bulk reorganisation, rename display names | ✅ Yes (needs restart) |

---

## Method A — Using the Browser UI

### Adding a stock to a panel

1. Open **http://localhost:5000** in your browser.
2. Find the panel you want (e.g. **Private Banks**).
3. Click the orange **`+ ADD`** button in the panel's top-right corner.
4. A text input drops down. Type the **NSE symbol** — e.g. `HDFCBANK`.  
   *(Do NOT add `.NS` — the app handles that automatically.)*
5. Press **Enter** or click **ADD**.
6. The stock appears at the bottom of the panel immediately.  
   Its price populates within **5 seconds** (next poll cycle).
7. The change is **saved automatically** — it will survive server restarts.

```
╔══════════════════════╗
║ Private Banks    3▲1▼║  ← panel header
║──────────────────────║
║ [HDFCBANK    ] [ADD] ║  ← input appears after clicking + ADD
║──────────────────────║
║ SYM   LAST   CHG% VOL║
║ HDFCB 1782.5 +0.34%  ║
╚══════════════════════╝
```

> **How to find the right NSE symbol:**
> - Go to https://www.nseindia.com/ → search for the company name
> - The symbol shown (e.g. `BAJFINANCE`, `AXISBANK`, `WIPRO`) is what you type

---

### Removing a stock from a panel

1. Hover your mouse over the stock row you want to remove.
2. A red **`×`** button appears on the right edge of the row.
3. Click **`×`**.
4. The row fades out and is removed.
5. Removal is **saved automatically**.

> To undo a removal, use **`+ ADD`** to add the symbol back.

---

## Method B — REST API (curl / Python)

### Add a ticker

```powershell
# PowerShell
Invoke-WebRequest -Uri "http://localhost:5000/api/panel/5/add" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"symbol":"HDFCBANK"}'
```

```bash
# bash / Git Bash
curl -X POST http://localhost:5000/api/panel/5/add \
     -H "Content-Type: application/json" \
     -d '{"symbol": "HDFCBANK"}'
```

Replace `5` with the **panel index** (0–19) from the table below.

### Remove a ticker

```powershell
Invoke-WebRequest -Uri "http://localhost:5000/api/panel/5/remove" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"symbol":"HDFCBANK"}'
```

### Bulk-add tickers with Python

```python
import requests

# Add several stocks to PSU Banks (panel index = 1)
panel = 1
to_add = ["INDIANB", "BANKINDIA", "UNIONBANK", "MAHABANK"]

for sym in to_add:
    r = requests.post(
        f"http://localhost:5000/api/panel/{panel}/add",
        json={"symbol": sym},
    )
    result = r.json()
    status = result.get("status") or result.get("error")
    print(f"  {sym:15s}  {status}")
```

---

## Panel Index Reference

Use the index number when calling the API.

| Index | Sector |
|---|---|
| **0** | Brokerages |
| **1** | PSU Banks |
| **2** | Capital Markets |
| **3** | Insurance & Wealth |
| **4** | NBFC & Housing Finance |
| **5** | Private Banks |
| **6** | Information Technology |
| **7** | Consumer & FMCG |
| **8** | Pharmaceuticals |
| **9** | Automobiles |
| **10** | Industrials & Materials |
| **11** | Oil & Gas |
| **12** | Power & Energy |
| **13** | Auto Components |
| **14** | Cement |
| **15** | Media & Entertainment |
| **16** | Real Estate |
| **17** | Telecom |
| **18** | Metals & Mining |
| **19** | Logistics & Shipping |

---

## Method C — Editing JSON Files Directly

### `user_customizations.json` — for add/remove overrides

This file lives at `C:\Dev\launchpad_app\user_customizations.json`.  
It is the "override layer" — applied on top of the source data at every server startup.

**Structure:**

```json
{
  "5": {
    "added": [
      { "symbol": "HDFCBANK",  "name": "HDFC Bank Ltd" },
      { "symbol": "ICICIBANK", "name": "ICICI Bank Ltd" }
    ],
    "removed": ["RBLBANK"]
  },
  "18": {
    "added": [
      { "symbol": "HINDCOPPER", "name": "Hindustan Copper" }
    ],
    "removed": []
  }
}
```

**Rules:**
- Keys are **panel indices as strings** (`"0"` to `"19"`).
- `"added"` = stocks to append; each needs `symbol` and `name`.
- `"removed"` = NSE symbols to hide (even if present in the base data).
- **After editing, restart the server** for changes to take effect.

---

### `sheet_data.json` — the base stock list per sector

This file lives at `C:\Dev\launchpad_app\sheet_data.json`.  
It is the **permanent source of truth** extracted from the Excel workbook.  
Edit this to permanently restructure which stocks appear in each sector.

**Structure:**

```json
{
  "Sheet 1": [
    { "symbol": "EDELWEISS", "name": "Edelweiss Financial Services" },
    { "symbol": "CENTRUM",   "name": "Centrum Capital" }
  ],
  "Sheet 2": [
    { "symbol": "SBIN",       "name": "State Bank of India" },
    { "symbol": "BANKBARODA", "name": "Bank of Baroda" }
  ]
}
```

**To add a stock permanently to a sector:**

1. Open `sheet_data.json`.
2. Find the correct Sheet key (see sheet→sector mapping in README.md).
3. Add a new line: `{ "symbol": "NEWSYMBOL", "name": "Company Display Name" }`.
4. Save and restart the server.

**To remove a stock permanently:**

1. Delete its line from `sheet_data.json`.
2. Also remove it from `user_customizations.json` if it appears there.
3. Restart.

**To rename a company's display name:**

1. Change the `"name"` value in `sheet_data.json`.
2. Restart.

---

## Regenerating `sheet_data.json` from Excel

If you update `blp work.xlsx`, re-run the extractor:

```powershell
cd C:\Dev\launchpad_app
python extract_sheets.py
```

This **overwrites** `sheet_data.json` from scratch.  
Your `user_customizations.json` is untouched.

---

## Editing Worked Examples

### Example 1: Add IRFC to PSU Banks

**UI method:**
1. Find the "PSU Banks" panel.
2. Click `+ ADD`.
3. Type `IRFC` → press Enter.

**API method:**
```powershell
Invoke-WebRequest -Uri "http://localhost:5000/api/panel/1/add" `
  -Method POST -ContentType "application/json" `
  -Body '{"symbol":"IRFC"}'
```

---

### Example 2: Remove a delisted stock everywhere

1. Open `user_customizations.json`.
2. Find the panel that contains it, add the symbol to `"removed"`:

```json
{
  "0": {
    "added":   [],
    "removed": ["EMBLEMFIN"]
  }
}
```

3. Restart the server.

---

### Example 3: Move a stock from one panel to another

There is no "move" command. Instead:

1. Remove it from the old panel (UI `×` button or API remove).
2. Add it to the new panel (UI `+ ADD` or API add).

---

### Example 4: Create a watchlist panel with custom stocks

The panels are fixed at 20 (defined in `PANEL_CONFIG` in `app.py`).  
To repurpose a panel (e.g. turn "Logistics" into a personal watchlist):

1. Open `sheet_data.json`, find `"Sheet 22"`, and clear its contents:

```json
"Sheet 22": []
```

2. Open `app.py`, find the PANEL_CONFIG entry and rename the label:

```python
("My Watchlist", "Sheet 22"),
```

3. Restart the server.
4. Use `+ ADD` in the browser to populate with your chosen symbols.

---

## Quick Troubleshooting

| Problem | Fix |
|---|---|
| Added stock not showing | Verify NSE symbol is correct at nseindia.com. Give it 5s to get a price. |
| Can't add — "already in this panel" | Symbol is already there. Scroll down to find it. |
| Add button does nothing | Check browser console (F12) for errors. Make sure server is running. |
| Stock shows `--` after adding | Symbol may be delisted or wrong. Check nseindia.com. |
| Changes lost after restart | Only changes via UI / API / JSON files are persisted. Edits to `sheet_data.json` need a restart. |
| Want to reset everything | Delete `user_customizations.json`. Restart. All panels return to defaults from `sheet_data.json`. |
