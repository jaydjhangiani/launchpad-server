# Ruliad Capital Management Systems — Launchpad

A real-time, Bloomberg-style equity monitoring terminal for **NSE-listed Indian stocks**.  
Built with Flask + yfinance + pure HTML/CSS/JS. No React, no database, no paid APIs.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Project Structure](#project-structure)
4. [Architecture](#architecture)
5. [Sector Panels](#sector-panels)
6. [How Prices Work](#how-prices-work)
7. [UI Controls](#ui-controls)
8. [API Reference](#api-reference)
9. [Configuration](#configuration)
10. [Persistence Files](#persistence-files)
11. [Troubleshooting](#troubleshooting)

---

## Overview

The Launchpad reads ~625 NSE stock symbols from `sheet_data.json` (extracted from an Excel workbook), distributes them across **26 sector panels** in a multi-page grid, and streams live prices from Yahoo Finance every **30 seconds**. The browser auto-refreshes every **5 seconds** without a full page reload.

| Feature | Detail |
|---|---|
| Live NSE prices | via `yfinance` (Yahoo Finance) |
| 26 sector panels across 3 pages | Brokerages → Textiles |
| Indices bar | NIFTY 50, SENSEX, BANK NIFTY live in the header |
| Sort | Click any column header (SYM / LAST / CHG% / VOL / MCAP) |
| Add ticker | `+ ADD` button in each panel |
| Remove ticker | Hover a row → `×` |
| Move / reorder | Drag panels and rows between pages |
| Price cache | Last-known prices reload instantly on restart |
| Auth | Optional HTTP Basic Auth via env vars |
| Multi-page | PAGE 1 / PAGE 2 / PAGE 3 + user-creatable pages |

---

## Quick Start

### Prerequisites

```powershell
python --version    # 3.10+
pip install flask yfinance pandas
```

### Start via launcher (recommended)

Double-click **`start_launchpad.bat`** — it kills any prior instance on port 5000, starts the server minimised, waits 4 seconds, then opens `http://localhost:5000` in your default browser.

### Start manually

```powershell
cd C:\Dev\launchpad_app
python app.py
```

Then open: **http://localhost:5000**

- Stale prices appear within **~1 second** (loaded from `price_cache.json`).
- Full live refresh of all 625 symbols takes **60–90 seconds** on first start.
- The `DATA: Xs AGO` indicator in the header shows how fresh the data is.

---

## Project Structure

```
launchpad_app/
├── app.py                    ← Flask backend — all server logic
├── portfolio_server.py       ← Separate Portfolio server (port 5001)
│
├── launchpad.db              ← SQLite database — all sector/stock data
├── user_customizations.json  ← Persisted add/remove/reorder edits per panel
├── price_cache.json          ← Last-known prices (speeds up cold restart)
├── mktcap_cache.json         ← Cached market caps
│
├── portfolio.json            ← Portfolio positions + transactions
├── cash.json                 ← Cash ledger
├── corporate_actions.json    ← Splits, bonuses, mergers for CAGR continuity
│
├── templates/
│   ├── index.html            ← Launchpad UI (HTML + CSS + vanilla JS)
│   └── portfolio.html        ← Portfolio UI (standalone, used by port 5001)
│
├── start_launchpad.bat       ← Launcher script for the Launchpad
├── start_portfolio.bat       ← Launcher script for the Portfolio
│
├── LAUNCHPAD.md              ← This file
└── PORTFOLIO.md              ← Portfolio documentation
```

---

## Architecture

```
Browser  http://localhost:5000
  │
  │  poll every 5s → GET /api/panels
  │  user actions → POST /api/panel/<n>/{add,remove,rename,...}
  ▼
Flask  app.py
  │  GET  /                  → renders index.html (no-cache)
  │  GET  /api/panels        → all panels + prices + indices
  │  POST /api/refresh       → trigger immediate re-fetch
  │  POST /api/panel/<n>/add → add ticker
  │  POST /api/panel/<n>/remove → remove ticker
  │  POST /api/page/...      → page management
  ▼
Background Thread  (background_updater)
  │  Startup:   _load_price_cache()  (disk → memory)
  │             update_all_prices()  (live yfinance fetch)
  │  Loop:      sleep(30s) → update_all_prices()
  │
  └─ yfinance.download() in batches of 50 symbols
     Results stored in price_cache dict (threading.Lock protected)
     Saved to price_cache.json after every cycle
```

### Threading model

- One daemon thread runs `background_updater()` for the app lifetime.
- `threading.Lock` (`cache_lock`) protects all reads/writes to `price_cache` and `indices_cache`.
- `use_reloader=False` is **mandatory** — Flask's reloader would spawn a second background thread.

---

## Sector Panels

26 panels across 3 pages, numbered 0–25 (used in REST API calls):

| # | Sector | # | Sector |
|---|---|---|---|
| 0 | Brokerages | 13 | Auto Components |
| 1 | PSU Banks | 14 | Cement |
| 2 | Capital Markets | 15 | Media & Entertainment |
| 3 | Insurance & Wealth | 16 | Real Estate |
| 4 | NBFC & Housing Finance | 17 | Telecom |
| 5 | Private Banks | 18 | Metals & Mining |
| 6 | Information Technology | 19 | Paper & Packaging |
| 7 | Consumer & FMCG | 20 | Logistics & Shipping |
| 8 | Diversified / Conglom. | 21 | Agrochem & Fertilisers |
| 9 | Pharmaceuticals | 22 | Speciality Chemicals |
| 10 | Automobiles | 23 | Hotels & Hospitality |
| 11 | Industrials & Materials | 24 | Textiles |
| 12 | Oil & Gas | 25 | Power & Energy |

---

## How Prices Work

### Symbol convention
All symbols are NSE. The `.NS` suffix is appended automatically (e.g. `RELIANCE` → `RELIANCE.NS`). Never type `.NS` in the UI or JSON files.

### Refresh lifecycle

```
Server start
 ├─ 1. _load_price_cache()     loads price_cache.json → memory  (~1s, stale)
 ├─ 2. update_all_prices()     first live fetch via yfinance    (60–90s)
 ├─ 3. price_cache.json saved  after full cycle
 └─ 4. Every 30s               repeat steps 2–3
```

### Indices
NIFTY 50, SENSEX, and BANK NIFTY are fetched separately in `fetch_indices()` and shown in the header bar.

### Market cap
Fetched once at startup then every 6 hours via `yfinance` `fast_info.market_cap`. Stored in `mktcap_cache.json`.

---

## UI Controls

| Control | Action |
|---|---|
| Click column header | Sort panel by that column (toggle asc/desc) |
| `+ ADD` in panel header | Expand add-ticker input |
| Hover row → `×` | Remove that ticker |
| Hover row → `✎` | Edit ticker name/exchange |
| Drag panel header | Move panel to a different page |
| `+ ADD PAGE` | Create a new empty page |
| `+ NEW SECTOR` | Create a new custom panel |
| Page tabs | Switch between PAGE 1 / PAGE 2 / PAGE 3 / custom pages |

---

## API Reference

Base URL: `http://localhost:5000`

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Serve launchpad UI |
| GET | `/api/panels` | All panels + live prices + indices |
| POST | `/api/refresh` | Trigger immediate price refresh |
| POST | `/api/panel/<n>/add` | Add ticker to panel n |
| POST | `/api/panel/<n>/remove` | Remove ticker from panel n |
| POST | `/api/panel/<n>/rename` | Rename panel n |
| POST | `/api/panel/<n>/edit` | Edit ticker in panel n |
| POST | `/api/panel/<n>/delete` | Delete entire panel n |
| POST | `/api/panel/<n>/setpage` | Move panel to a page |
| POST | `/api/panel/<n>/setheight` | Set panel row height |
| POST | `/api/panel/new` | Create new panel |
| POST | `/api/panel/swap` | Swap two panels |
| POST | `/api/symbol/move` | Move symbol between panels |
| POST | `/api/panel/move` | Move panel to position |
| POST | `/api/page/add` | Add a page |
| POST | `/api/page/<pg>/delete` | Delete a page |
| POST | `/api/page/<pg>/rename` | Rename a page |
| POST | `/api/page/reorder` | Reorder pages |

### Example — add a ticker

```powershell
Invoke-WebRequest -Uri "http://localhost:5000/api/panel/5/add" `
  -Method POST -ContentType "application/json" `
  -Body '{"symbol":"HDFCBANK"}'
```

### Example — trigger refresh

```powershell
Invoke-WebRequest -Uri "http://localhost:5000/api/refresh" -Method POST
```

---

## Configuration

Tuneable constants near the top of `app.py`:

| Constant | Default | Description |
|---|---|---|
| `FETCH_INTERVAL` | `30` | Seconds between background refreshes |
| `BATCH_SIZE` | `50` | Symbols per `yfinance.download()` call |

### Optional auth

Set environment variables before starting the server:

```powershell
$env:LAUNCHPAD_PASSWORD = "mysecret"
$env:LAUNCHPAD_USER     = "admin"     # default: "launchpad"
python app.py
```

Leave `LAUNCHPAD_PASSWORD` unset for open localhost access (default).

---

## Persistence Files

| File | Purpose | Edit safe? |
|---|---|---|
| `launchpad.db` | SQLite database — all sector/stock data | ✅ Via UI or API |
| `user_customizations.json` | All UI add/remove/rename changes | ✅ Yes (restart required) |
| `price_cache.json` | Last-known prices for instant reload | ⚠️ Do not edit manually |
| `mktcap_cache.json` | Cached market cap values | ⚠️ Delete to force re-fetch |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| All prices show `--` | yfinance rate-limited; wait 60s and reload |
| Port 5000 already in use | The `.bat` auto-kills it; or `netstat -aon \| findstr :5000` then `taskkill /f /pid <PID>` |
| Prices stale after restart | Delete `price_cache.json` to force a full fetch |
| Symbols show wrong price | Check the symbol exists on NSE; delisted stocks always show `--` |
| Server won't start | Ensure `launchpad.db` exists; it is auto-created on first run |
