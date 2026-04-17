"""
Shared mutable state for the Launchpad app.

All modules import this as ``import state`` and access globals via
``state.panels``, ``state.price_cache``, etc.  Never do
``from state import X`` for mutable/scalar values — attribute access
through the module object is required so reassignments are visible
everywhere.
"""

import threading

# ─── Panels & symbol lists (mutable at runtime) ───────────────────────────
panels:         list = []   # [{sector, stocks, id, page, height, mode}, ...]
all_symbols:    list = []   # all non-global symbols (NSE + BSE)
global_symbols: list = []   # raw yfinance tickers: ^DJI, GC=F, CL=F …
bse_symbols:    list = []   # DEPRECATED – kept for backward compat
bse_override:   set  = set()  # symbols to fetch via .BO (subset of all_symbols)
dead_symbols:   dict = {}   # symbol → miss count; persisted in DB
raw_sheet_data: dict = {}   # {panel_id: [{symbol, name}]}

# ─── Caches & locks ───────────────────────────────────────────────────────
price_cache:   dict = {}
indices_cache: dict = {}
mktcap_cache:  dict = {}
cache_lock  = threading.Lock()
mktcap_lock = threading.Lock()

# ─── Timing (scalars – always access as state.X = ... from other modules) ─
last_fetch_time:      float = 0
last_good_fetch_time: float = 0

# ─── Constants ────────────────────────────────────────────────────────────
PAGE_SIZE      = 12   # must match frontend PAGE_SIZE constant
BATCH_SIZE     = 50
DEAD_THRESHOLD = 3
FETCH_INTERVAL = 30   # seconds between background refreshes
