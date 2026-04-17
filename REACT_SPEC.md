# Ruliad Capital Management Systems — React Frontend Spec

## Overview

Replace the existing Jinja/vanilla-JS single-page app with a React + TypeScript frontend that connects to a local Flask backend at `http://localhost:5000`. The app is a Bloomberg Launchpad-style live market dashboard for Indian equities (NSE/BSE) and global instruments.

---

## Tech Stack

- **React 18 + TypeScript**
- **Vite** dev server (port 5173)
- **Tailwind CSS** for styling
- No UI component library — custom styled components only (dark terminal aesthetic)
- **TanStack Query** (`@tanstack/react-query`) for data fetching and polling
- No routing library needed — it's a single page with tab-based navigation

---

## Visual Design

**Theme:** Pure dark terminal / Bloomberg aesthetic.

- Background: `#0a0a0a` (near-black)
- Panel background: `#0d0d0d` with `1px solid #1a2a1a` border
- Primary accent: `#00ff41` (matrix green) — used for logo, highlights, active elements
- Up color: `#00ff41` (green)
- Down color: `#ff4444` (red)
- Flat/neutral: `#666`
- Text: `#c8c8c8` (light grey)
- Symbol text: `#e8e8e8`
- Price text: `#ffffff`
- Font: `'Courier New', Courier, monospace` throughout — uppercase labels everywhere
- Header bar height: ~40px, pinned to top
- Page tabs bar: ~32px, below header
- Footer ticker tape: ~28px, pinned to bottom
- No rounded corners (or 2px max), no shadows, no gradients
- Scrollbars: hidden or minimal styled

---

## Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ HEADER: Logo | Nifty 50 | Sensex | Bank Nifty | Refresh | Clock │
├─────────────────────────────────────────────────────────────────┤
│ PAGE TABS: [PAGE 1] [PAGE 2] [PAGE 3] [SECTORS 1-9] [+ADD PAGE] │  [+ NEW SECTOR]
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  4-COLUMN PANEL GRID (12 panels per page, 4 cols × 3 rows)      │
│  Each panel = "sector widget"                                   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│ FOOTER TICKER TAPE (scrolling marquee of all stock prices)      │
└─────────────────────────────────────────────────────────────────┘
```

- `+ NEW SECTOR` button floats to the far right of the page tabs bar
- Grid is 4 columns, auto-rows. Panels can span 1–4 rows (height property)
- Page tabs are drag-reorderable

---

## Header Bar

Left side:
- Orange square logo icon `■` + text `RULIAD CAPITAL MANAGEMENT SYSTEMS` (matrix green, letter-spaced)
- Index pills (read from API `indices` field): `NIFTY 50  ±X.XX%` | `SENSEX  ±X.XX%` | `BANK NIFTY  ±X.XX%`
  - Green if positive, red if negative
  - Show `--` until data arrives

Right side (left to right):
- `⟳ REFRESH` button — POST `/api/refresh`, shows `REFRESHING…` briefly
- `DATA: Xs AGO` label — green if <120s, amber `STALE` if 2-10min, red `VERY STALE` if >10min
- Market status badge: `● OPEN` (green) or `● CLOSED` (red) — based on IST time Mon–Fri 09:15–15:30
- Live clock `HH:MM:SS`

---

## Page Tabs Bar

- Tabs for each page (named `PAGE 1`, `PAGE 2`, etc., or custom name)
- Active tab highlighted with green bottom border / accent
- `+ ADD PAGE` button
- `+ NEW SECTOR` button (far right) — opens modal/inline dialog
- Single-click tab = navigate; double-click tab = rename (inline input)
- Tabs are drag-reorderable (call `POST /api/page/reorder`)
- Page label shows `N SECTORS – M TOTAL` count

---

## Sector Panel / Widget

Each panel card:

```
┌──────────────────────────────────────────┐
│ ⠿  [BSE] SECTOR NAME             ▲3 ▼5 –2  ☰ │
├──────────────────────────────────────────┤
│ SYMBOL INPUT [ADD]  [✕]                  │  ← add-ticker form (collapsible)
├──────────────────────────────────────────┤
│ SYM   LAST    CHG%   VOL    MCAP         │  ← shared column header
│ RELIANCE  2,450.00  +1.23%  12L  19kCr  │
│ HDFCBANK  1,820.50  -0.45%   8L  10kCr  │
│ ...                                      │
└──────────────────────────────────────────┘
```

- `⠿` drag handle (drag to reorder panels within the page grid)
- Badge: `GLOBAL` (blue-grey) or `BSE` (orange) or nothing (NSE default)
- Sector name: click to rename (inline input)
- `▲N ▼N –N` = count of stocks up/down/flat in the panel
- `☰` menu button → widget options menu (see below)
- Stock rows:
  - Symbol, Price (formatted Indian locale with commas), Change %, Volume (abbreviated: 12L, 8K, 2Cr), Market Cap
  - Row background flashes briefly green/red on price change
  - Right-click row → context menu: Open Yahoo Finance, Open TradingView, Copy Symbol, Remove from panel
  - Left-click row → opens TradingView chart in new tab
  - Rows are drag-reorderable within a panel (drag by symbol to move to another panel)
- The 4 column headers (SYM, LAST, CHG%, VOL, MCAP) are shared across the full grid row — clicking sorts all panels in that visual column simultaneously

### Widget Options Menu (☰)

- `+ Add Ticker` (toggle add form)
- `↑ Taller` / `↓ Shorter` (height +1 / -1, range 1–4)
- `⊞ Move to Page…` (submenu or inline picker for target page)
- `🗑 Delete Sector` (confirm dialog)

### Add Ticker Form

- Text input (auto-uppercase), placeholder: `NSE SYMBOL e.g. RELIANCE`
  - BSE panels: `BSE CODE e.g. 500325`
  - Global panels: `^DJI  GC=F  BZ=F  SI=F  CL=F`
- `ADD` button + `✕` cancel
- For global panels: row of quick-add chips: `DJIA`, `S&P`, `NQ`, `FTSE`, `N225`, `Gold`, `Silver`, `Copper`, `Plat`, `WTI`, `Brent`, `Gas`
- Error message shown inline if add fails

---

## New Sector Dialog

Modal or inline dropdown triggered by `+ NEW SECTOR`:
- Name input
- Mode selector: `NSE` | `BSE` | `GLOBAL` (toggle/radio)
- `CREATE` + `CANCEL` buttons
- On success: panel appears on current page

---

## Footer Ticker Tape

- Horizontal scrolling marquee of all stocks that have price data
- Format: `SYMBOL  ₹PRICE  +X.XX%` — price in green if up, red if down
- Infinite loop scroll, smooth, ~60px/s
- `◉ NSE LIVE (15-MIN DELAY)` label on far left
- Date on far right

---

## Data Fetching

- **Poll `GET /api/panels` every 5 seconds** using TanStack Query
- On each response, update prices in-place without re-mounting widgets (merge by panel `id`)
- API base URL: `http://localhost:5000` (configurable via `VITE_API_URL` env var)
- Show skeleton rows on first load (before any data arrives)
- On API error: show `POLL ERROR` in the data-age label, keep showing last known data

---

## API Reference

All mutations use `POST` with `Content-Type: application/json`.
The panel `index` (pi) used in mutation URLs is the 0-based position in the `panels` array returned by `GET /api/panels`. **Always re-read the index from the latest poll response after any mutation.**

### Data

| Endpoint | Method | Body | Description |
|---|---|---|---|
| `/api/panels` | GET | — | All panels, prices, indices |
| `/api/health` | GET | — | Liveness: `{status, panels, symbols, fetch_age_s}` |
| `/api/refresh` | POST | — | Force price refresh |

### Panel Mutations

| Endpoint | Method | Body | Description |
|---|---|---|---|
| `/api/panel/new` | POST | `{name, mode}` | Create panel. mode = `nse`\|`bse`\|`global` |
| `/api/panel/<pi>/delete` | POST | — | Delete panel |
| `/api/panel/<pi>/rename` | POST | `{name}` | Rename panel |
| `/api/panel/<pi>/add` | POST | `{symbol}` | Add ticker |
| `/api/panel/<pi>/remove` | POST | `{symbol}` | Remove ticker |
| `/api/panel/<pi>/edit` | POST | `{old_symbol, new_symbol}` | Rename ticker |
| `/api/panel/<pi>/setpage` | POST | `{page}` | Move panel to page |
| `/api/panel/<pi>/setheight` | POST | `{height}` | Set row-span (1–4) |
| `/api/panel/move` | POST | `{from, to}` | Reorder panel (move index) |
| `/api/panel/swap` | POST | `{a, b}` | Swap two panels |
| `/api/symbol/move` | POST | `{from_pi, to_pi, symbol}` | Move stock between panels |

### Page Mutations

| Endpoint | Method | Body | Description |
|---|---|---|---|
| `/api/page/add` | POST | — | Add empty page |
| `/api/page/<pg>/delete` | POST | — | Delete page (must be empty) |
| `/api/page/<pg>/rename` | POST | `{name}` | Rename page tab |
| `/api/page/reorder` | POST | `{order: [2,0,1,...]}` | Reorder pages |

### `GET /api/panels` Response Shape

```json
{
  "panels": [
    {
      "id": "Sheet 1",
      "sector": "Brokerages",
      "mode": "nse",
      "page": 0,
      "height": 1,
      "stocks": [
        {
          "symbol": "EDELWEISS",
          "name": "EDELWEISS",
          "price": 102.35,
          "change": 1.2,
          "change_pct": 1.19,
          "volume": 450000,
          "market_cap": 9800000000
        }
      ]
    }
  ],
  "indices": {
    "NIFTY 50":   { "price": 22500.0, "change": 120.5, "change_pct": 0.54 },
    "SENSEX":     { "price": 74200.0, "change": -80.0, "change_pct": -0.11 },
    "BANK NIFTY": { "price": 48100.0, "change": 210.0, "change_pct": 0.44 }
  },
  "timestamp": "09:32:15 IST",
  "date": "18 Apr 2026",
  "fetch_age": 12,
  "good_age": 12,
  "last_good_ts": "09:32:03",
  "next_refresh": 18,
  "page_count": 3,
  "page_names": { "0": "PAGE 1", "1": "Sectors", "2": "PAGE 3" }
}
```

---

## State Management Notes

- `panelData` = array of panels from last poll, keyed by `id`
- On each poll: merge incoming data into existing state by panel `id` — update `stocks`, `sector`, `mode` in-place; do NOT replace the array (avoids full re-render)
- `panelSort` = per-panel sort state persisted in `localStorage` key `panelSort` (JSON object keyed by panel `id`)
- After any panel add/delete, prune orphaned `panelSort` keys
- `currentPage` = integer, persisted in `location.hash` (e.g. `#1` for page 2)
- `pageNames` = object from API response, custom labels for page tabs
- `totalPages` = `Math.max(page_count from API, max page index in panels + 1)`

---

## Drag & Drop

- **Panel reorder**: drag a panel widget by its `⠿` handle → drop onto another panel → calls `POST /api/panel/move`
- **Symbol move**: drag a stock row → drop onto another panel → calls `POST /api/symbol/move`
- **Page tab reorder**: drag a page tab → drop onto another tab → calls `POST /api/page/reorder`
- Use HTML5 native drag-and-drop (`draggable`, `onDragStart`, `onDrop`)
- Symbol drag uses `dataTransfer.setData('text/symbol', symbol)` to distinguish from panel drag
- Page drag uses `dataTransfer.setData('text/page', pageIndex)`
- Visual feedback: dragged item gets 0.4 opacity; drop target gets green border highlight

---

## Sorting

- 5 sort columns per panel: `symbol`, `price` (LAST), `change_pct` (CHG%), `volume` (VOL), `market_cap` (MCAP)
- Sort state: `{ col: 'change_pct', dir: 'desc' }` — persisted in `localStorage` per panel id
- Column headers are shared across all panels in the same visual grid column (4 columns) — clicking a header sorts all panels in that column simultaneously
- Sort indicator: `▲` / `▼` shown in the active header cell

---

## Number Formatting

```
Price:      Indian locale with commas  →  2,450.00
Change%:    +1.23% / -0.45%
Volume:     ≥1Cr → Xr (e.g. 1.2Cr) | ≥1L → XL | ≥1K → XK | else raw
Market Cap: ≥1LCr → X.XLCr | ≥1kCr → XkCr | else XCr
            (1 Cr = 10M INR, 1 L = 100K INR)
```

---

## Out of Scope

- Authentication / login
- Portfolio page (separate Flask app at port 5001, not part of this build)
- WebSocket / SSE — polling every 5s is sufficient
- Mobile / responsive layout — desktop only (min-width 1024px)
