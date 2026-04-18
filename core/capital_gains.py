"""
Capital Gains computation engine — Ruliad Capital Management Systems

FIFO lot matching with intra-symbol CA adjustments (split/subdivision/bonus).
Jurisdiction-aware holding-period classification, tax-rate tables, and FY-level
exemption application.

Supported jurisdictions: IN, US, UK, JP, FR, DE

Tax rules:
  IN  — STCG 20% / LTCG 12.5% (post 23-Jul-2024 budget); 15%/10% before.
         LTCG exemption ₹1.25L/FY (was ₹1L).  12-month threshold.
         Grandfathering: if `grandfathered_cost_per_share` set on a buy tx,
         cost basis for LTCG = max(actual, GF).
  US  — STCG 22% (ordinary income estimate) / LTCG 15%.  12-month threshold.
  UK  — 20% flat (higher-rate estimate; basic=10%).  No STCG/LTCG distinction.
         £3,000 annual CGT exemption.
  JP  — 20.315% flat. No holding-period distinction.
  FR  — 30% (PFU). No holding-period distinction.
  DE  — Abgeltungsteuer 26.375% STCG; LTCG exempt (Spekulationsfrist >1yr).
         €1,000 Sparer-Pauschbetrag applied at FY level.

All monetary amounts in the entry's native currency (INR for IN, USD for US, etc.).
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

# ---------------------------------------------------------------------------
# Holding-period thresholds  (days; 0 = no distinction — treat all as "long")
# ---------------------------------------------------------------------------
_THRESHOLD: dict[str, int] = {
    "IN": 365,
    "US": 365,
    "UK": 0,
    "JP": 0,
    "FR": 0,
    "DE": 365,
}

# India budget-2024 effective date
_IN_BUDGET_2024 = datetime.date(2024, 7, 23)


def _in_rates(sell_date: datetime.date) -> dict:
    if sell_date >= _IN_BUDGET_2024:
        return {"stcg": 20.0, "ltcg": 12.5, "ltcg_exempt": 125_000.0}
    return {"stcg": 15.0, "ltcg": 10.0, "ltcg_exempt": 100_000.0}


# Static event-level tax rates {short, long} for non-IN jurisdictions (%)
# long==0 → gain is tax-exempt (e.g. DE > 1yr)
_RATES: dict[str, dict[str, float]] = {
    "US": {"short": 22.0,   "long": 15.0},
    "UK": {"short": 20.0,   "long": 20.0},    # higher-rate; basic = 10%
    "JP": {"short": 20.315, "long": 20.315},
    "FR": {"short": 30.0,   "long": 30.0},    # PFU
    "DE": {"short": 26.375, "long": 0.0},     # > 1 yr private sale exempt
}

# Annual LTCG exemptions applied at FY + jurisdiction level (non-IN)
_LTCG_EXEMPT_STATIC: dict[str, float] = {
    "UK": 3_000.0,    # GBP — FY 2024/25
    "DE": 1_000.0,    # EUR — Sparer-Pauschbetrag (single filer)
}

# ---------------------------------------------------------------------------
# Date / fiscal-year helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(s).strip())
    except (ValueError, AttributeError):
        return None


def _holding_days(buy: str, sell: str) -> int:
    b, s = _parse_date(buy), _parse_date(sell)
    return max(0, (s - b).days) if b and s else 0


def _classify(days: int, jur: str) -> str:
    t = _THRESHOLD.get(jur, 365)
    return "long" if (t == 0 or days > t) else "short"


def _tax_rate(jur: str, period: str, sell_str: str) -> float:
    if jur == "IN":
        sd = _parse_date(sell_str) or datetime.date.today()
        r  = _in_rates(sd)
        return r["stcg"] if period == "short" else r["ltcg"]
    r = _RATES.get(jur, {"short": 0.0, "long": 0.0})
    return r["short"] if period == "short" else r["long"]


def _ltcg_exempt_for(jur: str, sell_str: str) -> float:
    if jur == "IN":
        sd = _parse_date(sell_str) or datetime.date.today()
        return _in_rates(sd)["ltcg_exempt"]
    return _LTCG_EXEMPT_STATIC.get(jur, 0.0)


def _fiscal_year(sell_str: str, jur: str) -> str:
    sd = _parse_date(sell_str)
    if sd is None:
        return "UNKNOWN"
    if jur == "IN":
        y = sd.year if sd.month >= 4 else sd.year - 1
        return f"{y}-{str(y + 1)[2:]}"
    if jur == "UK":
        y = sd.year if (sd.month > 4 or (sd.month == 4 and sd.day >= 6)) else sd.year - 1
        return f"{y}/{str(y + 1)[2:]}"
    return str(sd.year)


# ---------------------------------------------------------------------------
# Per-entry FIFO lot engine
# ---------------------------------------------------------------------------

def compute_cg_events(
    entry: dict,
    ca_list: list,
    current_price: float | None = None,
) -> list[dict]:
    """
    FIFO lot matching for one portfolio entry.

    - Buy transactions push a lot onto the queue.
    - Sell transactions consume lots from the front, emitting CG events.
    - Intra-symbol CAs (split/subdivision/bonus) adjust existing lots in-place.
    - `grandfathered_cost_per_share` on a buy transaction overrides the cost
      basis for IN LTCG events (grandfathering clause for pre-Jan-2018 holdings).
    - If `current_price` is supplied, remaining open lots are also valued using
      today's price and emitted as unrealized CG events (is_unrealized=True).

    Returns a list of CG event dicts, one per matched lot slice.
    """
    sym  = str(entry.get("symbol", "")).strip().upper()
    jur  = str(entry.get("jurisdiction", "IN")).strip().upper()
    curr = str(entry.get("currency", "INR")).strip().upper()

    txs = sorted(entry.get("transactions", []), key=lambda t: t.get("date", ""))
    cas = sorted(
        [ca for ca in ca_list
         if ca.get("type", "").lower() in ("split", "subdivision", "bonus")
         and str(ca.get("symbol", "")).strip().upper() == sym],
        key=lambda ca: ca.get("date", ""),
    )

    timeline: list[tuple[str, str, dict]] = (
        [(t.get("date", ""), "tx", t) for t in txs] +
        [(c.get("date", ""), "ca", c) for c in cas]
    )
    timeline.sort(key=lambda x: x[0])

    lots: list[dict[str, Any]] = []   # FIFO queue — oldest first
    events: list[dict] = []

    for ev_date, kind, evt in timeline:
        if kind == "tx":
            tx_type = str(evt.get("type", "buy")).lower()
            shares  = float(evt.get("shares", 0) or 0)
            price   = float(evt.get("price",  0) or 0)
            charges = (
                float(evt.get("brokerage",     0) or 0) +
                float(evt.get("stt",           0) or 0) +
                float(evt.get("other_charges", 0) or 0)
            )
            acq = str(evt.get("acquisition_type", "secondary")).lower()
            if shares <= 0:
                continue

            if tx_type == "buy":
                cps = (shares * price + charges) / shares   # cost per share incl. charges
                lot: dict[str, Any] = {
                    "date":             ev_date,
                    "shares":           shares,
                    "cost_per_share":   round(cps, 6),
                    "acquisition_type": acq,
                }
                gf = evt.get("grandfathered_cost_per_share")
                if gf is not None:
                    try:
                        lot["grandfathered_cost_per_share"] = float(gf)
                    except (TypeError, ValueError):
                        pass
                lots.append(lot)

            elif tx_type == "sell":
                sell_ch_ps = charges / shares   # sell-side charges per share
                remaining  = shares

                while remaining > 1e-8 and lots:
                    lot   = lots[0]
                    taken = min(lot["shares"], remaining)

                    days   = _holding_days(lot["date"], ev_date)
                    period = _classify(days, jur)
                    rate   = _tax_rate(jur, period, ev_date)

                    # Effective cost — apply India grandfathering for LTCG events
                    eff_cost = lot["cost_per_share"]
                    gf_cost  = lot.get("grandfathered_cost_per_share")
                    is_gf    = False
                    if gf_cost is not None and jur == "IN" and period == "long":
                        eff_cost = max(eff_cost, float(gf_cost))
                        is_gf    = True

                    gross = round(taken * price - taken * eff_cost - taken * sell_ch_ps, 2)
                    # Event-level tax: pre-exemption, on gains only (losses = 0 tax)
                    tax   = round(max(0.0, gross) * rate / 100, 2)
                    fy    = _fiscal_year(ev_date, jur)
                    exempt = _ltcg_exempt_for(jur, ev_date) if period == "long" else 0.0

                    events.append({
                        "symbol":                    sym,
                        "jurisdiction":              jur,
                        "currency":                  curr,
                        "acquisition_type":          lot["acquisition_type"],
                        "buy_date":                  lot["date"],
                        "sell_date":                 ev_date,
                        "shares":                    round(taken, 6),
                        "buy_cost_per_share":        round(lot["cost_per_share"], 4),
                        "effective_cost_per_share":  round(eff_cost, 4),
                        "sell_price_per_share":      round(price, 4),
                        "sell_charges_per_share":    round(sell_ch_ps, 4),
                        "holding_days":              days,
                        "holding_period":            period,   # "short" | "long"
                        "gross_gain":                gross,
                        "tax_rate_pct":              rate,
                        "ltcg_exempt_annual":        exempt,   # FY-level; applied in summary
                        "estimated_tax":             tax,      # pre-exemption, for display
                        "after_tax_gain":            round(gross - tax, 2),
                        "fy":                        fy,
                        "is_grandfathered":          is_gf,
                    })

                    lot["shares"] -= taken
                    if lot["shares"] <= 1e-8:
                        lots.pop(0)
                    remaining -= taken

        else:  # kind == "ca"
            ca_type = str(evt.get("type", "")).lower()
            ratio   = float(evt.get("ratio", 1) or 1)
            if ratio <= 0:
                continue

            if ca_type in ("split", "subdivision"):
                for lot in lots:
                    lot["shares"]         = round(lot["shares"] * ratio, 6)
                    lot["cost_per_share"] = round(lot["cost_per_share"] / ratio, 6)
                    gf = lot.get("grandfathered_cost_per_share")
                    if gf is not None:
                        lot["grandfathered_cost_per_share"] = round(gf / ratio, 6)

            elif ca_type == "bonus":
                # Bonus shares: zero cost basis; FIFO position after all existing lots
                new_lots = []
                for lot in lots:
                    new_sh = round(lot["shares"] * ratio, 6)
                    if new_sh > 1e-8:
                        new_lots.append({
                            "date":             ev_date,
                            "shares":           new_sh,
                            "cost_per_share":   0.0,
                            "acquisition_type": "bonus",
                        })
                lots.extend(new_lots)

    # ── Unrealized events for remaining open lots ────────────────────────────
    today_str = datetime.date.today().isoformat()
    if current_price and current_price > 0 and lots:
        for lot in lots:
            if lot["shares"] <= 1e-8:
                continue
            taken  = lot["shares"]
            days   = _holding_days(lot["date"], today_str)
            period = _classify(days, jur)
            rate   = _tax_rate(jur, period, today_str)

            eff_cost = lot["cost_per_share"]
            gf_cost  = lot.get("grandfathered_cost_per_share")
            is_gf    = False
            if gf_cost is not None and jur == "IN" and period == "long":
                eff_cost = max(eff_cost, float(gf_cost))
                is_gf    = True

            gross = round(taken * current_price - taken * eff_cost, 2)
            tax   = round(max(0.0, gross) * rate / 100, 2)
            fy    = _fiscal_year(today_str, jur)
            exempt = _ltcg_exempt_for(jur, today_str) if period == "long" else 0.0

            events.append({
                "symbol":                    sym,
                "jurisdiction":              jur,
                "currency":                  curr,
                "acquisition_type":          lot["acquisition_type"],
                "buy_date":                  lot["date"],
                "sell_date":                 today_str,
                "shares":                    round(taken, 6),
                "buy_cost_per_share":        round(lot["cost_per_share"], 4),
                "effective_cost_per_share":  round(eff_cost, 4),
                "sell_price_per_share":      round(current_price, 4),
                "sell_charges_per_share":    0.0,
                "holding_days":              days,
                "holding_period":            period,
                "gross_gain":                gross,
                "tax_rate_pct":              rate,
                "ltcg_exempt_annual":        exempt,
                "estimated_tax":             tax,
                "after_tax_gain":            round(gross - tax, 2),
                "fy":                        fy,
                "is_grandfathered":          is_gf,
                "is_unrealized":             True,
            })

    return events


# ---------------------------------------------------------------------------
# Portfolio-level aggregation
# ---------------------------------------------------------------------------

def compute_all_cg(entries: list[dict], ca_list: list, prices: dict | None = None) -> dict:
    """
    Compute CG events for all portfolio entries and return
    {events: [...], summary: {...}}.

    `prices` should be a dict keyed by symbol → float (the current LTP).
    When provided, open lots are also included as unrealized events.
    """
    all_events: list[dict] = []
    for entry in entries:
        try:
            sym = str(entry.get("symbol", "")).strip().upper()
            current_price: float | None = None
            if prices:
                raw = prices.get(sym)
                if isinstance(raw, dict):
                    current_price = raw.get("price")
                elif isinstance(raw, (int, float)):
                    current_price = float(raw)
            all_events.extend(compute_cg_events(entry, ca_list, current_price))
        except Exception:
            pass   # don't let one broken entry kill the whole computation

    all_events.sort(key=lambda e: (e.get("is_unrealized", False), e.get("sell_date", "")))
    return {"events": all_events, "summary": _summarize(all_events)}


def _summarize(events: list[dict]) -> dict:
    """
    Aggregate events into by_fy and by_jurisdiction with LTCG exemptions applied.
    Realized and unrealized events are tracked separately.

    LTCG exemption logic (per FY + jurisdiction, realized only):
      1. Sum all realized LTCG gains and losses → net_ltcg
      2. Subtract exemption from positive net_ltcg → taxable_ltcg
      3. STCG losses do NOT offset LTCG gains here (left to user's tax return).
    """
    realized   = [e for e in events if not e.get("is_unrealized")]
    unrealized = [e for e in events if e.get("is_unrealized")]

    groups: dict[tuple, list] = defaultdict(list)
    for ev in realized:
        groups[(ev["jurisdiction"], ev["fy"])].append(ev)

    # Also compute unrealized subtotals globally
    unreal_stcg = round(sum(e["gross_gain"] for e in unrealized if e["holding_period"]=="short"), 2)
    unreal_ltcg = round(sum(e["gross_gain"] for e in unrealized if e["holding_period"]=="long"),  2)
    unreal_tax  = round(sum(e["estimated_tax"]  for e in unrealized), 2)
    unreal_after = round(sum(e["after_tax_gain"] for e in unrealized), 2)

    by_fy:  dict[str, dict] = {}
    by_jur: dict[str, dict] = {}
    total_gross = total_tax = total_after = 0.0

    for (jur, fy), evs in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        st = [e for e in evs if e["holding_period"] == "short"]
        lt = [e for e in evs if e["holding_period"] == "long"]

        stcg_gross = round(sum(e["gross_gain"] for e in st), 2)
        ltcg_gross = round(sum(e["gross_gain"] for e in lt), 2)

        stcg_rate  = st[0]["tax_rate_pct"] if st else 0.0
        ltcg_rate  = lt[0]["tax_rate_pct"] if lt else 0.0

        ltcg_exempt = evs[0]["ltcg_exempt_annual"] if evs else 0.0

        stcg_taxable = max(0.0, stcg_gross)
        stcg_tax     = round(stcg_taxable * stcg_rate / 100, 2)

        ltcg_exempt_applied = min(ltcg_exempt, max(0.0, ltcg_gross))
        ltcg_taxable        = max(0.0, ltcg_gross - ltcg_exempt_applied)
        ltcg_tax            = round(ltcg_taxable * ltcg_rate / 100, 2)

        fy_tax   = round(stcg_tax + ltcg_tax, 2)
        fy_after = round(stcg_gross + ltcg_gross - fy_tax, 2)

        total_gross += stcg_gross + ltcg_gross
        total_tax   += fy_tax
        total_after += fy_after

        key = f"{fy} ({jur})"
        by_fy[key] = {
            "fy":           fy,
            "jurisdiction": jur,
            "stcg": {
                "gross_gain":   stcg_gross,
                "taxable_gain": stcg_taxable,
                "tax_rate_pct": stcg_rate,
                "tax":          stcg_tax,
                "after_tax":    round(stcg_gross - stcg_tax, 2),
                "event_count":  len(st),
            },
            "ltcg": {
                "gross_gain":       ltcg_gross,
                "annual_exemption": ltcg_exempt,
                "exempt_applied":   round(ltcg_exempt_applied, 2),
                "taxable_gain":     round(ltcg_taxable, 2),
                "tax_rate_pct":     ltcg_rate,
                "tax":              ltcg_tax,
                "after_tax":        round(ltcg_gross - ltcg_tax, 2),
                "event_count":      len(lt),
            },
            "total_events":   len(evs),
            "total_tax":      fy_tax,
            "total_after_tax": fy_after,
        }

        if jur not in by_jur:
            by_jur[jur] = {
                "stcg_gross": 0.0, "ltcg_gross": 0.0,
                "total_tax":  0.0, "after_tax":  0.0, "events": 0,
            }
        j = by_jur[jur]
        j["stcg_gross"] = round(j["stcg_gross"] + stcg_gross, 2)
        j["ltcg_gross"] = round(j["ltcg_gross"] + ltcg_gross, 2)
        j["total_tax"]  = round(j["total_tax"]  + fy_tax,     2)
        j["after_tax"]  = round(j["after_tax"]  + fy_after,   2)
        j["events"]    += len(evs)

    return {
        "total_gross_gain":          round(total_gross, 2),
        "total_estimated_tax":       round(total_tax,   2),
        "total_after_tax_gain":       round(total_after, 2),
        "unrealized_stcg":           unreal_stcg,
        "unrealized_ltcg":           unreal_ltcg,
        "unrealized_estimated_tax":  unreal_tax,
        "unrealized_after_tax_gain": unreal_after,
        "by_fy":                     by_fy,
        "by_jurisdiction":           by_jur,
    }


# ---------------------------------------------------------------------------
# Post-tax CAGR helper  (for future integration into portfolio enrichment)
# ---------------------------------------------------------------------------

def post_tax_cagr(
    total_invested: float,
    realized_after_tax_gain: float,
    unrealized_gain: float | None,
    inception_date_str: str,
) -> float | None:
    """
    Annualised post-tax return.

    - realized_after_tax_gain: sum of after_tax_gain from closed CG events
    - unrealized_gain: open position unrealized P&L (no tax event yet)
    - inception_date_str: earliest buy date for the position

    Returns CAGR % or None if insufficient data.
    """
    if total_invested <= 0:
        return None
    inception = _parse_date(inception_date_str)
    if inception is None:
        return None
    years = (datetime.date.today() - inception).days / 365.25
    if years < 1 / 365:
        return None
    total_return = realized_after_tax_gain + (unrealized_gain or 0.0)
    ratio = 1.0 + total_return / total_invested
    if ratio <= 0:
        return None
    return round((ratio ** (1.0 / years) - 1.0) * 100, 2)


# ---------------------------------------------------------------------------
# Tax-rate reference  (for UI display)
# ---------------------------------------------------------------------------

def get_tax_rate_table() -> dict:
    """Return the full tax-rate reference table for all supported jurisdictions."""
    today = datetime.date.today()
    in_r  = _in_rates(today)
    return {
        "IN": {
            "stcg_pct":          in_r["stcg"],
            "ltcg_pct":          in_r["ltcg"],
            "ltcg_exempt":       in_r["ltcg_exempt"],
            "holding_threshold": "12 months",
            "note":              "Listed equity/ETF/equity MF; STT must be paid",
            "budget_note":       "Rates updated 23-Jul-2024",
        },
        "US": {
            "stcg_pct":          _RATES["US"]["short"],
            "ltcg_pct":          _RATES["US"]["long"],
            "ltcg_exempt":       0.0,
            "holding_threshold": "12 months",
            "note":              "Common mid-bracket estimate; actual rate varies by income",
        },
        "UK": {
            "stcg_pct":          _RATES["UK"]["short"],
            "ltcg_pct":          _RATES["UK"]["long"],
            "ltcg_exempt":       _LTCG_EXEMPT_STATIC["UK"],
            "holding_threshold": "No distinction",
            "note":              "Higher-rate taxpayer estimate; basic-rate = 10%",
        },
        "JP": {
            "stcg_pct":          _RATES["JP"]["short"],
            "ltcg_pct":          _RATES["JP"]["long"],
            "ltcg_exempt":       0.0,
            "holding_threshold": "No distinction",
            "note":              "Tokutei account (withholding at source)",
        },
        "FR": {
            "stcg_pct":          _RATES["FR"]["short"],
            "ltcg_pct":          _RATES["FR"]["long"],
            "ltcg_exempt":       0.0,
            "holding_threshold": "No distinction",
            "note":              "Prélèvement Forfaitaire Unique (PFU)",
        },
        "DE": {
            "stcg_pct":          _RATES["DE"]["short"],
            "ltcg_pct":          _RATES["DE"]["long"],
            "ltcg_exempt":       _LTCG_EXEMPT_STATIC["DE"],
            "holding_threshold": "12 months",
            "note":              "Abgeltungsteuer; >1yr private sale exempt. Sparer-Pauschbetrag applied.",
        },
    }
