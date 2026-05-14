"""Price and market-cap fetchers plus background update threads."""

import gc
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yfinance as yf
import pandas as pd

import state
from core.cache import (
    _load_price_cache, _save_price_cache,
    _load_bse_override, _save_bse_override,
    _load_mktcap_cache, _save_mktcap_cache,
)
from core.db import _db_mark_dead, _db_clear_dead


# ─── NSE batch fetch ───────────────────────────────────────────────────────

def fetch_symbols_batch(symbols: list) -> dict:
    """Download latest price + volume for NSE symbols.

    Uses 2-day hourly data so the last bar is the true last-traded price,
    avoiding the multi-hour EOD propagation delay in yfinance daily closes.
    """
    result: dict = {}
    if not symbols:
        return result

    tickers_list = [s + ".NS" for s in symbols]
    try:
        df = yf.download(
            tickers_list,
            period="2d",
            interval="1h",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if df.empty:
            return result

        if isinstance(df.columns, pd.MultiIndex):
            close_df = df["Close"]
            vol_df   = df["Volume"]
        else:
            close_df = df[["Close"]].rename(columns={"Close": tickers_list[0]})
            vol_df   = df[["Volume"]].rename(columns={"Volume": tickers_list[0]})

        for ns_sym, sym in zip(tickers_list, symbols):
            try:
                if ns_sym not in close_df.columns:
                    continue
                closes = close_df[ns_sym].dropna()
                if closes.empty:
                    continue
                price       = float(closes.iloc[-1])
                last_date   = closes.index[-1].date()
                prev_closes = closes[closes.index.date < last_date]
                prev = float(prev_closes.iloc[-1]) if not prev_closes.empty else price
                chg  = price - prev
                pct  = (chg / prev * 100) if prev > 0 else 0.0
                if ns_sym in vol_df.columns:
                    today_vols = vol_df[ns_sym][vol_df[ns_sym].index.date == last_date]
                    vol = int(today_vols.sum()) if not today_vols.empty else 0
                else:
                    vol = 0
                result[sym] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                    "volume":     vol,
                }
            except Exception:
                pass

        del df, close_df, vol_df

    except Exception as e:
        print(f"[WARN] batch fetch error ({len(symbols)} syms): {e}")

    return result


# ─── BSE batch fetch ───────────────────────────────────────────────────────

def fetch_bse_batch(symbols: list) -> dict:
    """Fetch current price for BSE symbols using fast_info.
    Falls back to history() if fast_info has no last_price.
    """
    result: dict = {}
    if not symbols:
        return result

    for sym in symbols:
        ticker_str = sym + ".BO"
        try:
            fi    = yf.Ticker(ticker_str).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            if price is None or price != price:
                raise ValueError("no last_price in fast_info")
            prev = prev if (prev and prev == prev) else price
            chg  = price - prev
            pct  = (chg / prev * 100) if prev > 0 else 0.0
            vol  = getattr(fi, "three_month_average_volume", None) or 0
            result[sym] = {
                "price":      round(price, 2),
                "change":     round(chg,   2),
                "change_pct": round(pct,   2),
                "volume":     int(vol),
            }
        except Exception:
            try:
                hist = yf.Ticker(ticker_str).history(period="5d", interval="1d", auto_adjust=True)
                if hist.empty:
                    del hist
                    continue
                closes = hist["Close"].dropna()
                vols   = hist["Volume"].dropna()
                if closes.empty:
                    del hist
                    continue
                price = float(closes.iloc[-1])
                prev  = float(closes.iloc[-2]) if len(closes) >= 2 else price
                chg   = price - prev
                pct   = (chg / prev * 100) if prev > 0 else 0.0
                vol   = int(vols.iloc[-1]) if not vols.empty else 0
                result[sym] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                    "volume":     vol,
                }
                del hist
            except Exception:
                pass
        time.sleep(0.12)

    return result


# ─── Global / non-NSE batch fetch ─────────────────────────────────────────

def fetch_global_batch(symbols: list) -> dict:
    """Download latest daily close + volume for non-NSE symbols (indices, futures, etc.)."""
    result: dict = {}
    if not symbols:
        return result
    tickers = symbols if len(symbols) > 1 else symbols[0]
    try:
        df = yf.download(
            tickers,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if df.empty:
            return result
        if isinstance(df.columns, pd.MultiIndex):
            close_df = df["Close"]
            vol_df   = df["Volume"]
        else:
            sym0 = symbols[0]
            close_df = df[["Close"]].rename(columns={"Close": sym0})
            vol_df   = df[["Volume"]].rename(columns={"Volume": sym0})
        for sym in symbols:
            try:
                if sym not in close_df.columns:
                    continue
                closes = close_df[sym].dropna()
                vols   = vol_df[sym].dropna() if sym in vol_df.columns else pd.Series(dtype=float)
                if closes.empty:
                    continue
                price = float(closes.iloc[-1])
                prev  = float(closes.iloc[-2]) if len(closes) >= 2 else price
                chg   = price - prev
                pct   = (chg / prev * 100) if prev > 0 else 0.0
                vol   = int(vols.iloc[-1]) if not vols.empty else 0
                result[sym] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                    "volume":     vol,
                }
            except Exception:
                pass
        del df, close_df, vol_df
    except Exception as e:
        print(f"[WARN] global batch fetch error ({len(symbols)} syms): {e}")
    return result


# ─── Index fetch ──────────────────────────────────────────────────────────

def fetch_indices() -> dict:
    """Fetch NIFTY 50, SENSEX, BANK NIFTY using fast_info."""
    result: dict = {}
    index_map = {"^NSEI": "NIFTY 50", "^BSESN": "SENSEX", "^NSEBANK": "BANK NIFTY"}
    for ticker_str, label in index_map.items():
        try:
            fi    = yf.Ticker(ticker_str).fast_info
            price = fi.last_price
            prev  = fi.previous_close
            if price and prev and prev > 0:
                chg = price - prev
                pct = chg / prev * 100
                result[label] = {
                    "price":      round(price, 2),
                    "change":     round(chg,   2),
                    "change_pct": round(pct,   2),
                }
        except Exception:
            pass
    return result


# ─── Main price updater ───────────────────────────────────────────────────

def update_all_prices() -> None:
    t0 = time.time()
    ts = datetime.now().strftime("%H:%M:%S")
    bse_direct  = [s for s in state.all_symbols if s in state.bse_override]
    nse_symbols = [s for s in state.all_symbols if s not in state.bse_override]
    print(f"[{ts}] Refreshing {len(nse_symbols)} NSE + {len(bse_direct)} BSE + "
          f"{len(state.global_symbols)} global symbols ...")

    new_prices: dict = {}

    # NSE batch fetch with automatic BSE fallback for misses
    for i in range(0, len(nse_symbols), state.BATCH_SIZE):
        batch = [s for s in nse_symbols[i: i + state.BATCH_SIZE]
                 if state.dead_symbols.get(s, 0) < state.DEAD_THRESHOLD]
        if not batch:
            continue
        result = fetch_symbols_batch(batch)
        new_prices.update(result)
        missed = [s for s in batch if s not in result]
        if missed:
            bse_retry = fetch_bse_batch(missed)
            if bse_retry:
                new_prices.update(bse_retry)
                for s in bse_retry:
                    state.bse_override.add(s)
                    state.dead_symbols.pop(s, None)
                print(f"[INFO] BSE fallback resolved {list(bse_retry.keys())}")
            still_missed = [s for s in missed if s not in new_prices]
            for s in still_missed:
                state.dead_symbols[s] = state.dead_symbols.get(s, 0) + 1
                if state.dead_symbols[s] == state.DEAD_THRESHOLD:
                    print(f"[WARN] Marking {s} as dead after {state.DEAD_THRESHOLD} misses — persisting to DB")
                    _db_mark_dead(s, state.dead_symbols[s])
        for s in result:
            if s in state.dead_symbols:
                state.dead_symbols.pop(s)
                _db_clear_dead(s)
        time.sleep(0.3)

    # BSE-override symbols: fetch directly via .BO with NSE fallback for misses
    for i in range(0, len(bse_direct), state.BATCH_SIZE):
        batch = [s for s in bse_direct[i: i + state.BATCH_SIZE]
                 if state.dead_symbols.get(s, 0) < state.DEAD_THRESHOLD]
        if not batch:
            continue
        result = fetch_bse_batch(batch)
        new_prices.update(result)
        missed = [s for s in batch if s not in result]
        if missed:
            nse_retry = fetch_symbols_batch(missed)
            if nse_retry:
                new_prices.update(nse_retry)
                for s in nse_retry:
                    state.bse_override.discard(s)
                    state.dead_symbols.pop(s, None)
                print(f"[INFO] NSE fallback resolved {list(nse_retry.keys())}")
            still_missed = [s for s in missed if s not in new_prices]
            for s in still_missed:
                state.dead_symbols[s] = state.dead_symbols.get(s, 0) + 1
                if state.dead_symbols[s] == state.DEAD_THRESHOLD:
                    print(f"[WARN] Marking {s} as dead after {state.DEAD_THRESHOLD} misses — persisting to DB")
                    _db_mark_dead(s, state.dead_symbols[s])
        for s in result:
            if s in state.dead_symbols:
                state.dead_symbols.pop(s)
                _db_clear_dead(s)
        time.sleep(0.3)

    # Fetch non-NSE global symbols (commodities, foreign indices, futures)
    if state.global_symbols:
        for i in range(0, len(state.global_symbols), state.BATCH_SIZE):
            batch  = state.global_symbols[i: i + state.BATCH_SIZE]
            result = fetch_global_batch(batch)
            new_prices.update(result)
            time.sleep(0.2)

    new_indices = fetch_indices()

    with state.cache_lock:
        state.price_cache.update(new_prices)
        state.indices_cache.update(new_indices)
        state.last_fetch_time = time.time()
        if new_prices:
            state.last_good_fetch_time = state.last_fetch_time

    _save_price_cache()
    _save_bse_override()

    elapsed = round(time.time() - t0, 1)
    status  = f"Updated {len(new_prices)} prices" if new_prices else "NO DATA (network/rate-limit?)"
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {status} in {elapsed}s")
    gc.collect()


# ─── Background price thread ──────────────────────────────────────────────

def background_updater() -> None:
    """Runs forever: initial fetch then refresh every FETCH_INTERVAL seconds.
    Exceptions inside update_all_prices() are caught and logged so the thread
    never dies silently.
    """
    _load_price_cache()
    _load_bse_override()
    with state.cache_lock:
        if state.price_cache and state.last_fetch_time == 0:
            state.last_fetch_time = time.time() - state.FETCH_INTERVAL
            state.last_good_fetch_time = state.last_fetch_time
    while True:
        try:
            update_all_prices()
        except Exception as e:
            print(f"[ERROR] background_updater crashed: {e} — continuing in {state.FETCH_INTERVAL}s")
        time.sleep(state.FETCH_INTERVAL)


# ─── Market-cap fetcher ───────────────────────────────────────────────────

def _fetch_one_mktcap(sym: str):
    """Fetch market cap for one symbol via yfinance fast_info."""
    try:
        mc = yf.Ticker(sym + ".NS").fast_info.market_cap
        return sym, (mc if mc and mc > 0 else None)
    except Exception:
        return sym, None


def fetch_mktcap_all() -> None:
    """Fetch market cap using a thread pool to parallelise fast_info calls.
    8 workers × ~0.6s per call → 600 symbols in ~45 seconds.
    """
    t0 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching market caps for "
          f"{len(state.all_symbols)} symbols...")
    new_mc: dict = {}
    hit = 0
    with ThreadPoolExecutor(max_workers=8) as exe:
        futures = {exe.submit(_fetch_one_mktcap, sym): sym for sym in state.all_symbols}
        for fut in as_completed(futures):
            try:
                sym, mc = fut.result(timeout=20)
                if mc:
                    new_mc[sym] = mc
                    hit += 1
            except Exception:
                pass
    with state.mktcap_lock:
        state.mktcap_cache.update(new_mc)
    _save_mktcap_cache()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Market caps fetched: "
          f"{hit}/{len(state.all_symbols)} in {round(time.time()-t0, 1)}s")


def mktcap_updater() -> None:
    """Runs forever: initial fetch then refresh every 6 hours.
    Exceptions are caught and logged so the thread never dies silently.
    """
    _load_mktcap_cache()
    while True:
        try:
            fetch_mktcap_all()
        except Exception as e:
            print(f"[ERROR] mktcap_updater crashed: {e} — continuing in 6h")
        time.sleep(6 * 3600)
