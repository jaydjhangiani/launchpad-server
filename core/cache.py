"""Disk-persistence helpers for price cache, market-cap cache, and BSE-override set."""

import os
import json

import state

PRICE_CACHE_FILE  = "price_cache.json"
MKTCAP_CACHE_FILE = "mktcap_cache.json"
BSE_OVERRIDE_FILE = "bse_override.json"


# ─── Price cache ──────────────────────────────────────────────────────────

def _load_price_cache() -> None:
    """Load previously saved prices from disk into the in-memory cache."""
    if os.path.exists(PRICE_CACHE_FILE):
        try:
            with open(PRICE_CACHE_FILE) as fh:
                loaded = json.load(fh)
            with state.cache_lock:
                state.price_cache.update(loaded.get("prices", {}))
                state.indices_cache.update(loaded.get("indices", {}))
            print(f"[INIT] Loaded {len(state.price_cache)} cached prices from disk")
        except Exception as e:
            print(f"[WARN] Could not load price cache: {e}")


def _save_price_cache() -> None:
    try:
        with state.cache_lock:
            snapshot = {"prices": dict(state.price_cache), "indices": dict(state.indices_cache)}
        with open(PRICE_CACHE_FILE, "w") as fh:
            json.dump(snapshot, fh)
    except Exception as e:
        print(f"[WARN] Could not save price cache: {e}")


# ─── Market-cap cache ─────────────────────────────────────────────────────

def _load_mktcap_cache() -> None:
    if os.path.exists(MKTCAP_CACHE_FILE):
        try:
            with open(MKTCAP_CACHE_FILE) as fh:
                loaded = json.load(fh)
            with state.mktcap_lock:
                state.mktcap_cache.update(loaded)
            print(f"[INIT] Loaded {len(state.mktcap_cache)} cached market caps from disk")
        except Exception as e:
            print(f"[WARN] Could not load mktcap cache: {e}")


def _save_mktcap_cache() -> None:
    try:
        with state.mktcap_lock:
            snapshot = dict(state.mktcap_cache)
        with open(MKTCAP_CACHE_FILE, "w") as fh:
            json.dump(snapshot, fh)
    except Exception as e:
        print(f"[WARN] Could not save mktcap cache: {e}")


# ─── BSE-override persistence ─────────────────────────────────────────────

def _load_bse_override() -> None:
    """Restore persisted bse_override symbols (filtered to current all_symbols)."""
    if not os.path.exists(BSE_OVERRIDE_FILE):
        return
    try:
        with open(BSE_OVERRIDE_FILE) as fh:
            saved = set(json.load(fh))
        restored = saved & set(state.all_symbols)
        state.bse_override.update(restored)
        if restored:
            print(f"[INIT] Restored {len(restored)} BSE-override symbols from disk")
    except Exception as e:
        print(f"[WARN] Could not load bse_override cache: {e}")


def _save_bse_override() -> None:
    try:
        with open(BSE_OVERRIDE_FILE, "w") as fh:
            json.dump(sorted(state.bse_override), fh)
    except Exception as e:
        print(f"[WARN] Could not save bse_override cache: {e}")
