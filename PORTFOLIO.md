# Ruliad Capital Management Systems — Portfolio

A standalone transaction-based portfolio tracker for Indian equities (NSE + BSE).  
Runs independently on **port 5001**, completely separate from the Launchpad.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [How It Works](#how-it-works)
4. [Portfolio Table Columns](#portfolio-table-columns)
5. [Transactions](#transactions)
6. [CAGR Calculation](#cagr-calculation)
7. [Corporate Actions](#corporate-actions)
8. [Cash Ledger](#cash-ledger)
9. [Import from Yahoo Finance CSV](#import-from-yahoo-finance-csv)
10. [API Reference](#api-reference)
11. [Data Files](#data-files)
12. [Troubleshooting](#troubleshooting)

---

## Overview

| Feature | Detail |
|---|---|
| Position model | Transaction-based average-cost method |
| Live prices | yfinance (NSE + BSE), refreshed every 60s |
| P&L | Unrealised + Realised, per stock and total |
| CAGR | Per stock (annualised return from first buy) |
| Corporate actions | Splits, bonuses, mergers, demergers — CAGR continuity through chain |
| Cash ledger | Deposits + withdrawals; cash % of total portfolio |
| Yahoo CSV import | Import transactions directly from Yahoo Finance export |
| Completely standalone | Own server (`portfolio_server.py`), own port (5001), own launcher |

---

## Quick Start

### Start via launcher (recommended)

Double-click **`start_portfolio.bat`** on your Desktop or in `C:\Dev\launchpad_app\`.

It kills any prior instance on port 5001, starts `portfolio_server.py` minimised, waits 4 seconds, then opens `http://localhost:5001` in your default browser.

### Start manually

```powershell
cd C:\Dev\launchpad_app
python portfolio_server.py
```

Then open: **http://localhost:5001**

### Prerequisites

```powershell
pip install flask yfinance pandas
```

> The Portfolio and Launchpad can run **simultaneously** — they use different ports (5001 vs 5000) and different server processes. They share the same JSON data files.

---

## How It Works

### Position model — average cost method

Each symbol stores a list of transactions. On every API call, the server derives:

- **`open_shares`** — net shares currently held
- **`avg_cost`** — volume-weighted average purchase price
- **`realized_pnl`** — profit/loss from completed sells
- **`sold_cost_basis`** — cost of shares that were sold (for realized %)

Example:

```
Buy  100 shares @ ₹500   →  open=100, avg_cost=500
Buy   50 shares @ ₹600   →  open=150, avg_cost=533.33
Sell  80 shares @ ₹700   →  open= 70, avg_cost=533.33
                              realized_pnl = (700−533.33)×80 = ₹13,333
```

### Price fetch

On startup, `portfolio_server.py`:
1. Seeds prices from `price_cache.json` (shared with Launchpad) for instant display.
2. Fetches live prices for all portfolio symbols via `yfinance.download()` (NSE) and `yfinance fast_info` (BSE).
3. Repeats every **60 seconds** in a background thread.

---

## Portfolio Table Columns

| Column | Description |
|---|---|
| **SYM** | Ticker symbol (click to toggle full name) |
| **STATUS** | Open / Closed (click to cycle filter) |
| **SHARES** | Current open share count |
| **AVG COST** | Volume-weighted average purchase price |
| **LTP** | Last traded price (live) |
| **DAY** | Absolute day gain on the position (₹) |
| **DAY%** | Intraday % change |
| **INVESTED** | `open_shares × avg_cost` |
| **MKT VALUE** | `open_shares × ltp` |
| **UNREAL P&L** | `mkt_value − invested` |
| **UNREAL%** | `unreal_pnl / invested × 100` |
| **REAL P&L** | Profit/loss from completed sells |
| **REAL%** | `realized_pnl / sold_cost_basis × 100` |
| **ALLOC%** | This position as % of total portfolio (equity + cash) |
| **CAGR%** | Annualised return from inception date (see below) |
| **Actions** | BUY / SELL buttons, expand transactions |

All columns are **sortable** — click the header to sort, click again to reverse.

---

## Transactions

Every position is built from individual buy/sell transactions stored in `portfolio.json`.

### Add a transaction via UI

1. Click **BUY** or **SELL** in the action bar at the bottom.
2. Fill in: Symbol, Exchange (NSE/BSE), Date, Shares, Price.
3. Click **CONFIRM**.

### View / edit / delete transactions

Click the **▶** arrow on any row to expand the transaction history. Each row shows:
- Date, Type (buy/sell), Shares, Price, Value, and per-transaction realised P&L for sells.
- **✎** to edit a transaction, **✕** to delete it.

### Add a transaction via API

```powershell
Invoke-WebRequest -Uri "http://localhost:5001/api/portfolio/add" `
  -Method POST -ContentType "application/json" `
  -Body '{"symbol":"RELIANCE","exchange":"nse","type":"buy","date":"2024-01-15","shares":50,"price":2850}'
```

---

## CAGR Calculation

CAGR (Compound Annual Growth Rate) measures annualised return from the **first buy date**.

### Formula

$$\text{CAGR} = \left(\frac{\text{LTP}}{\text{avg\_cost}}\right)^{\frac{365.25}{\text{days}}} - 1$$

For **open positions**: from first buy date to today using LTP vs avg_cost.  
For **closed positions**: from first buy date to last sell date using realised returns.

### Rules

- Minimum holding period: **30 days** (shorter periods show `—`)
- Anomaly cap: **±9999%** (data entry errors with near-zero prices are suppressed)
- A `*` next to the CAGR value means the inception date was found through a **corporate action chain** (e.g. the stock was acquired via a merger)

---

## Corporate Actions

Corporate actions ensure CAGR is calculated correctly through mergers, splits, and demergers.

### Supported action types

| Type | Effect |
|---|---|
| `split` / `subdivision` | 1 share → N shares; avg_cost ÷ N |
| `bonus` | X bonus shares per 1 held; avg_cost diluted proportionally |
| `merger` / `amalgamation` | from_symbol absorbed into to_symbol at ratio; inception date chains back |
| `name_change` | Symbol renamed; CAGR chain walks back to original buy date |
| `demerger` / `spinoff` | Child entity split off; cost basis allocated by `cost_allocation_pct` |

### Example — Tata Steel Long Products merger

```json
{
  "id": "ca-001",
  "date": "2023-11-30",
  "type": "merger",
  "from_symbol": "TATASTLLP",
  "to_symbol": "TATASTEEL",
  "ratio": 6.7,
  "note": "1 TATASTLLP → 6.7 TATASTEEL"
}
```

With this entry, TATASTEEL's CAGR is measured from the original TATASTLLP buy date in 2011, not from the 2023 merger date.

### Adding a corporate action via UI

1. Click **CORPORATE ACTIONS** at the bottom of the portfolio page.
2. Select the action type.
3. Fill in the required fields (date, symbols, ratio).
4. Click **ADD ACTION**.

---

## Cash Ledger

Track all deposits and withdrawals alongside your equity positions.

### Summary row (top of page)

| Field | Description |
|---|---|
| **CASH** | Current cash balance |
| **CASH%** | Cash as % of total portfolio |
| **INVESTED%** | Equity as % of total portfolio |
| **PORTFOLIO TOTAL** | Equity market value + cash balance |

### Adding cash via UI

Click **CASH IN** (deposit) or **CASH OUT** (withdrawal), fill in the amount, date, and optional note.

### Adding cash via API

```powershell
Invoke-WebRequest -Uri "http://localhost:5001/api/cash/add" `
  -Method POST -ContentType "application/json" `
  -Body '{"type":"deposit","date":"2024-01-01","amount":500000,"note":"Initial capital"}'
```

### Editing / deleting cash entries

Click **CASH** at the bottom to expand the cash ledger. Each entry has **✎** (edit) and **✕** (delete) buttons.

---

## Import from Yahoo Finance CSV

You can bulk-import your portfolio from a Yahoo Finance CSV export.

### Steps

1. In Yahoo Finance, go to your Portfolio → Export.
2. Click **CSV** in the Portfolio action bar.
3. Select your CSV file.
4. Click **IMPORT**.

### What gets imported

| Row type | Result |
|---|---|
| Stock with trade data | Transaction added (deduplicated) |
| Stock with no trade data | Symbol stub added (`needs_data` flag) |
| `$$CASH_TX` deposit rows | Added to cash ledger |
| Duplicate transactions | Skipped |

---

## API Reference

Base URL: `http://localhost:5001`

### Portfolio

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/portfolio` | All positions with live prices, P&L, CAGR |
| POST | `/api/portfolio/add` | Add a buy or sell transaction |
| POST | `/api/portfolio/remove` | Remove entire symbol |
| POST | `/api/portfolio/remove_tx` | Remove a specific transaction |
| POST | `/api/portfolio/edit_tx` | Edit a specific transaction |
| POST | `/api/portfolio/import_yahoo_csv` | Import from Yahoo CSV file |

### Cash

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/cash` | Cash balance + all transactions |
| POST | `/api/cash/add` | Add deposit or withdrawal |
| POST | `/api/cash/remove` | Delete a cash entry by index |
| POST | `/api/cash/edit` | Edit a cash entry by index |

### Corporate Actions

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/corporate_actions` | List all corporate actions |
| POST | `/api/corporate_actions/add` | Add a new action |
| POST | `/api/corporate_actions/remove` | Delete by index |

### `GET /api/portfolio` — response shape

```json
{
  "positions": [
    {
      "symbol":        "RELIANCE",
      "name":          "Reliance Industries",
      "open_shares":   50,
      "avg_cost":      2850.00,
      "ltp":           3100.50,
      "change_pct":    0.82,
      "invested":      142500.00,
      "mkt_value":     155025.00,
      "unreal_pnl":    12525.00,
      "unreal_pct":    8.79,
      "realized_pnl":  0.0,
      "realized_pct":  null,
      "day_gain_abs":  1262.50,
      "status":        "Open",
      "alloc_pct":     12.3,
      "cagr":          18.45,
      "inception_date": null,
      "transactions":  [...]
    }
  ],
  "totals": {
    "invested":        1200000.0,
    "mkt_value":       1450000.0,
    "unreal_pnl":      250000.0,
    "unreal_pct":      20.83,
    "realized_pnl":    85000.0,
    "total_pnl":       335000.0,
    "cash_balance":    133292400.0,
    "portfolio_total": 134742400.0,
    "cash_pct":        98.93,
    "invested_pct":    1.07,
    "weighted_cagr":   22.14
  }
}
```

`inception_date` is non-null only when the CAGR start date was found via a corporate action chain (shown as `*` in the UI).  
`weighted_cagr` is market-value weighted across all open positions with valid prices.

---

## Data Files

All files are in `C:\Dev\launchpad_app\`. Shared with the Launchpad — edits in one app are immediately visible in the other.

| File | Contents | Safe to edit manually? |
|---|---|---|
| `portfolio.json` | All symbols + transaction lists | ✅ Yes (restart not needed) |
| `cash.json` | Deposit/withdrawal entries | ✅ Yes |
| `corporate_actions.json` | CA records (splits, mergers, …) | ✅ Yes |
| `price_cache.json` | Shared price cache (seeded from Launchpad) | ⚠️ Do not edit manually |

### `portfolio.json` format

```json
[
  {
    "symbol":   "RELIANCE",
    "name":     "Reliance Industries",
    "exchange": "nse",
    "transactions": [
      { "date": "2022-03-10", "type": "buy",  "shares": 50, "price": 2850 },
      { "date": "2023-11-20", "type": "sell", "shares": 10, "price": 3050 }
    ]
  }
]
```

### `cash.json` format

```json
[
  { "date": "2022-01-01", "type": "deposit",    "amount": 1000000, "note": "Initial capital" },
  { "date": "2022-03-10", "type": "withdrawal", "amount": 142500,  "note": "Bought RELIANCE" }
]
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Prices show `—` | Price not yet fetched; wait 60s for background thread, or restart |
| Port 5001 already in use | The `.bat` auto-kills it; or `netstat -aon \| findstr :5001` then `taskkill /f /pid <PID>` |
| CAGR shows `—` for a position | Position < 30 days old, or LTP not yet available |
| CAGR shows `*` | Normal — inception date traced through a corporate action chain |
| Merger/split not affecting CAGR | Check `corporate_actions.json` — `from_symbol` / `to_symbol` must match exactly |
| Import shows 0 transactions | Check CSV is the Yahoo Finance Portfolio export format |
| Portfolio and Launchpad show different prices | They share `price_cache.json` at startup but fetch independently after that; within 60s they converge |
