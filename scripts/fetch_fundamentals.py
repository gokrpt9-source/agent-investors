"""fetch_fundamentals.py — SEC EDGAR fundamentals + price-derived ratios (DESIGN.md §4.3).

Writes ``fundamentals_<T>.json`` (schema ``fundamentals/v1``)::

    python scripts/fetch_fundamentals.py --ticker KO --out runs/<id>/phase2/data/fundamentals_KO.json
    python scripts/fetch_fundamentals.py --ticker JPM --price 300.5 --sector-class financials

Data path: ticker -> CIK via ``company_tickers.json`` (cache kind ``sec_map``), then XBRL
companyfacts + submissions (cache kinds ``sec_companyfacts`` / ``sec_submissions``), all through
``common.http_get_json(..., sec=True)`` (SEC User-Agent + rate limiter). Parsing is in
``scripts/_edgar.py``; this module assembles the payload: Facts, ratios, the yfinance ``marketCap``
cross-check (``common.yf_info``), lag detection and flags. In fixture mode
(``AGINV_FIXTURES_DIR``) the disk cache is bypassed so fixture runs never read or pollute real data.

Price: the ``price`` argument (float or a price Fact), else the last cached close from
``fetch_prices.load_series`` (never network), else null + missing_data.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts import _edgar  # noqa: E402
from scripts._edgar import Value  # noqa: E402
from scripts import valuation as _valuation  # noqa: E402  (captive-finance config helpers)

SCHEMA = "fundamentals"
KIND_MAP = "sec_map"
KIND_CF = "sec_companyfacts"
KIND_SUB = "sec_submissions"
MAP_KEY = "company_tickers"
#: Fallback TTLs (hours) when committee.yaml lacks cache_ttl_hours.sec_map / .fundamentals.
DEFAULT_TTL_HOURS = {"sec_map": 168.0, "fundamentals": 168.0}

TTM_KEYS = ("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "fcf", "da",
            "ebitda", "pretax_income", "income_tax")
BALANCE_KEYS = ("cash", "st_investments", "total_debt", "net_debt", "equity", "goodwill", "intangibles")
VALUATION_KEYS = ("ev", "pe", "ev_ebit", "ev_sales", "fcf_yield_pct", "net_debt_ebitda", "p_b", "p_tbv",
                  "roe_pct", "roic_pct", "tax_rate_used")
#: For sector_class=financials these are reported as not_meaningful (DESIGN §4.3 step 11; ROIC added).
FINANCIALS_NOT_MEANINGFUL = ("ev", "ev_ebit", "ev_sales", "net_debt_ebitda", "roic_pct")
#: Concepts that banks/insurers do not report; a null for financials is not_meaningful, not missing.
FINANCIALS_NM_CONCEPTS = ("gross_profit", "operating_income", "ebitda")
#: What a missing TTM input silences downstream; appended to its missing_data entry so the null EV/EBIT,
#: ROIC, EBITDA, FCF ... cells are never unexplained (e.g. NKE/JNJ report no OperatingIncomeLoss).
MISSING_CONSEQUENCE = {
    "gross_profit": " - gross margin not computed",
    "operating_income": " - operating margin, EBITDA, EV/EBIT, ROIC and net debt/EBITDA not computed "
                        "(read the income statement in the 10-K/10-Q)",
    "da": " - EBITDA and net debt/EBITDA not computed",
    "capex": " - FCF, FCF margin and FCF yield not computed",
}


# =============================================================================================
# Config helpers
# =============================================================================================


def _committee() -> dict:
    try:
        return common.committee_cfg()
    except common.ConfigError:
        return {}


def fund_cfg(committee: Mapping[str, Any] | None = None) -> dict:
    """``committee.yaml: fundamentals`` merged over ``_edgar.DEFAULTS``."""
    cfg = committee if committee is not None else _committee()
    return _edgar.settings((cfg or {}).get("fundamentals") or {})


def _ttl(name: str, committee: Mapping[str, Any] | None = None) -> float:
    cfg = committee if committee is not None else _committee()
    v = common.safe_float(((cfg or {}).get("cache_ttl_hours") or {}).get(name))
    return v if v is not None else DEFAULT_TTL_HOURS[name]


def share_class_override(ticker: str) -> dict:
    """``etf_overrides.yaml: securities.<T>`` share-class settings:
    ``{"shares_tags": list|None, "class_factor": float|None, "source_url": str|None}``."""
    try:
        ov = common.load_config("etf_overrides")
    except common.ConfigError:
        return {"shares_tags": None, "class_factor": None, "source_url": None}
    securities = (ov or {}).get("securities") or {}
    entry: Mapping[str, Any] = {}
    for key, val in securities.items():
        try:
            if common.norm_ticker(key) == ticker and isinstance(val, Mapping):
                entry = val
                break
        except ValueError:
            continue
    sc = entry.get("share_class") or {}
    tags = [str(t).strip() for t in (sc.get("shares_tags") or []) if str(t).strip()] or None
    factor = common.safe_float(sc.get("class_factor"))
    if factor is not None and factor <= 0:
        factor = None
    url = entry.get("source_url")
    return {"shares_tags": tags, "class_factor": factor, "source_url": url if isinstance(url, str) else None}


# =============================================================================================
# Fetching (common seam + DiskCache)
# =============================================================================================


def normalize_ticker_map(raw: Any) -> dict[str, dict]:
    """SEC ``company_tickers.json`` (``{"0": {"cik_str","ticker","title"}}``) -> ``{TICKER: {cik, title}}``
    with ``.`` -> ``-`` (BRK.B -> BRK-B). An already-normalized map passes through."""
    out: dict[str, dict] = {}
    items = raw.items() if isinstance(raw, Mapping) else enumerate(raw or [])
    for key, row in items:
        if not isinstance(row, Mapping):
            continue
        if "cik" in row and "cik_str" not in row and isinstance(key, str) and not key.isdigit():
            t, cik = key, row.get("cik")                      # already normalized
        else:
            t, cik = row.get("ticker"), row.get("cik_str")
        t = str(t or "").strip().upper().replace(".", "-")
        cik_n = common.safe_float(cik)
        if not t or cik_n is None or t in out:
            continue
        out[t] = {"cik": int(cik_n), "title": row.get("title")}
    return out


def _get_json(cache: common.DiskCache | None, kind: str, key: str, url: str, ttl_hours: float,
              transform: Callable[[Any], Any] | None = None,
              fresh_ok: Callable[[Any], bool] | None = None) -> tuple[Any, dict]:
    """Cache (fresh) -> SEC via the seam -> stale cache. Returns ``(payload|None, meta)``;
    ``meta = {"url","stale","error","cached"}``. The cache is bypassed in fixture mode.
    ``fresh_ok(hit)`` False rejects a fresh cache hit written by an older format (refetched; the old
    entry still serves as the stale fallback if the network is down)."""
    meta: dict[str, Any] = {"url": url, "stale": False, "error": None, "cached": False}
    use_cache = cache is not None and common.active_fixtures_dir() is None
    if use_cache:
        hit = cache.get(kind, key, ttl_hours)  # type: ignore[union-attr]
        if hit is not None and (fresh_ok is None or fresh_ok(hit)):
            meta["cached"] = True
            return hit, meta
    try:
        raw = common.http_get_json(url, sec=True)
        payload = transform(raw) if transform else raw
        if use_cache:
            cache.put(kind, key, payload, url)  # type: ignore[union-attr]
        return payload, meta
    except common.DataUnavailable as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        if use_cache:
            stale = cache.get_stale(kind, key)  # type: ignore[union-attr]
            if stale is not None:
                meta["stale"] = True
                return stale, meta
        return None, meta


def load_ticker_map(cache: common.DiskCache | None = None, committee: Mapping[str, Any] | None = None
                    ) -> tuple[dict[str, dict] | None, dict]:
    """``{TICKER: {"cik","title"}}`` (cache kind ``sec_map``, TTL ``cache_ttl_hours.sec_map``)."""
    data, meta = _get_json(cache, KIND_MAP, MAP_KEY, common.SEC_TICKERS_URL, _ttl("sec_map", committee),
                           normalize_ticker_map)
    return (normalize_ticker_map(data) if data is not None else None), meta


def lookup_cik(ticker: str, tmap: Mapping[str, Mapping[str, Any]]) -> tuple[int | None, str | None]:
    t = common.norm_ticker(ticker)
    for cand in (t, t.replace("-", "."), t.replace("-", "")):
        row = tmap.get(cand)
        if row:
            return int(row["cik"]), row.get("title")
    return None, None


def _resolve_price(ticker: str, price: Any, as_of: date, price_as_of: Any = None
                   ) -> tuple[float | None, str | None, str]:
    """``(value, as_of 'YYYY-MM-DD', origin)``; origin ``argument|price_cache|none``."""
    if isinstance(price, Mapping):
        v = common.fact_value(price)
        d = common.date_str(price.get("as_of")) or common.date_str(price_as_of) or as_of.isoformat()
        return (v if v is not None and v > 0 else None), d, "argument"
    if price is not None:
        v = common.safe_float(price)
        d = common.date_str(price_as_of) or as_of.isoformat()
        return (v if v is not None and v > 0 else None), d, "argument"
    try:
        from scripts import fetch_prices  # noqa: PLC0415 - optional sibling, loaded lazily
    except ImportError:
        return None, None, "none"
    try:
        df = fetch_prices.load_series(ticker)
    except Exception:  # noqa: BLE001 - a missing/odd cache must not break fundamentals
        return None, None, "none"
    if df is None or getattr(df, "empty", True) or "Close" not in getattr(df, "columns", []):
        return None, None, "none"
    close = df["Close"].dropna()
    try:
        close = close[close.index <= common.parse_date(as_of).isoformat()]  # type: ignore[union-attr]
    except (TypeError, ValueError):
        pass
    if close.empty:
        return None, None, "none"
    v = common.safe_float(close.iloc[-1])
    return (v if v is not None and v > 0 else None), common.date_str(close.index[-1]), "price_cache"


def _yf_market_cap(ticker: str) -> tuple[float | None, str | None]:
    """Yahoo ``marketCap`` for the cross-check: ``(value, error)``."""
    try:
        info = common.yf_info(ticker)
    except common.DataUnavailable as exc:
        return None, f"{type(exc).__name__}: {exc}"
    v = common.safe_float((info or {}).get("marketCap"))
    if v is None or v <= 0:
        return None, "Yahoo info has no marketCap"
    return v, None


# =============================================================================================
# Fact helpers
# =============================================================================================


class _Facts:
    """Builds Facts for one ticker (source ids, kinds, na handling)."""

    def __init__(self, ticker: str):
        self.t = ticker
        self.cf = f"D:SEC:{ticker}:CF"
        self.sub = f"D:SEC:{ticker}:SUB"
        self.px = f"D:PX:{ticker}"
        self.yf = f"D:YF:{ticker}:INFO"

    def of(self, v: Value, unit: str, *, sid: str | None = None, kind: str = "fundamental",
           nm: bool = False) -> dict:
        sid = sid or self.cf
        extra = {k: x for k, x in (v.extra or {}).items() if x is not None}
        if v.ok:
            return common.fact(v.value, unit, sid, v.end, kind, filed=v.filed, tag=v.tag,
                               derivation=v.derivation, **extra)
        reason = v.na_reason or "missing"
        if nm and reason == "missing":
            reason = "not_meaningful"
        return common.fact(None, unit, sid, v.end, kind, reason, tag=v.tag, derivation=v.derivation, **extra)

    def na(self, unit: str, reason: str = "missing", *, sid: str | None = None, kind: str = "fundamental",
           as_of: Any = None, **extra: Any) -> dict:
        return common.fact(None, unit, sid or self.cf, as_of, kind, reason, **extra)


def _val(f: Mapping[str, Any]) -> float | None:
    return f.get("value") if isinstance(f, Mapping) else None


def _ratio(facts: _Facts, num: Mapping[str, Any], den: Mapping[str, Any], unit: str, derivation: str,
           ref: Mapping[str, Any], *, scale: float = 1.0, same_period: list[str] | None = None,
           label: str | None = None) -> dict:
    """num/den x scale as a Fact; null inputs propagate their na_reason; den <= 0 ->
    ``negative_denominator``. ``ref`` supplies as_of/filed (the fundamental period).

    ``same_period`` (a list collecting mismatch descriptions) marks a flow/flow ratio (margins):
    both TTM Facts must end on the same period, else the ratio is null ``missing`` instead of
    silently dividing e.g. an FY gross profit by a FY+YTD revenue."""
    as_of, filed = ref.get("as_of"), ref.get("filed")
    n, d = _val(num), _val(den)
    if n is None:
        return facts.na(unit, num.get("na_reason") or "missing", as_of=as_of, derivation=derivation)
    if d is None:
        return facts.na(unit, den.get("na_reason") or "missing", as_of=as_of, derivation=derivation)
    if same_period is not None and num.get("as_of") != den.get("as_of"):
        why = f"periods differ: {num.get('as_of')} vs {den.get('as_of')}"
        same_period.append(f"{label or derivation} ({why})")
        return facts.na(unit, "missing", as_of=as_of, derivation=f"{derivation} ({why})")
    if d <= 0:
        return facts.na(unit, "negative_denominator", as_of=as_of, derivation=derivation)
    return common.fact(n / d * scale, unit, facts.cf, as_of, "fundamental", filed=filed, derivation=derivation)


def _combine(a: Value, b: Value, fn: Callable[[float, float], float], derivation: str) -> Value:
    """Same-period combination of two Values (e.g. FCF = OCF - capex); the period derivation of
    ``a`` (e.g. ``FY2025+3M2026-3M2025``) is appended."""
    if not a.ok:
        return Value.na(a.na_reason or "missing", end=a.end, derivation=derivation)
    if not b.ok:
        return Value.na(b.na_reason or "missing", end=b.end, derivation=derivation)
    if a.end != b.end:
        return Value.na("missing", derivation=f"{derivation} (periods differ: {a.end} vs {b.end})")
    deriv = f"{derivation} ({a.derivation})" if a.derivation else derivation
    return Value(fn(a.value, b.value), a.end, max(a.filed, b.filed), None, deriv, accn=a.accn)  # type: ignore[arg-type,type-var]


def _obs_value(o: _edgar.Obs | None) -> Value:
    if o is None:
        return Value.na("missing")
    return Value(o.val, o.end, o.filed, o.tag, f"FY{_edgar.fiscal_year(o.end)}", accn=o.accn)


# =============================================================================================
# Payload assembly
# =============================================================================================


def _null_blocks(facts: _Facts, reason: str = "missing", *, sector_class: str = "default") -> dict:
    def nf(unit: str, key: str = "") -> dict:
        r = reason
        if sector_class == "financials" and key in FINANCIALS_NOT_MEANINGFUL + ("gross",):
            r = "not_meaningful"
        return facts.na(unit, r)
    return {
        "shares": nf("shares"), "market_cap": facts.na("USD", reason, kind="price"),
        "ttm": {k: nf("USD") for k in TTM_KEYS},
        "fy": [],
        "balance": {k: nf("USD") for k in BALANCE_KEYS},
        "margins_pct": {k: nf("pct", k) for k in ("gross", "operating", "net", "fcf")},
        "growth_pct": {k: nf("pct") for k in ("revenue_cagr_3y", "ni_cagr_3y", "fcf_cagr_3y", "revenue_yoy_ttm")},
        "valuation": {k: nf("fraction" if k == "tax_rate_used" else "USD" if k == "ev" else
                            "pct" if k.endswith("_pct") else "x", k) for k in VALUATION_KEYS},
        "tag_map": {},
    }


def _cagr_fact(facts: _Facts, fy: Mapping[int, Value], years: int, label: str, *, stale_before: date | None
               ) -> dict:
    if not fy:
        return facts.na("pct", "insufficient_history", derivation=f"{label}: no fiscal-year history")
    last = max(fy)
    end_v = fy[last]
    if stale_before is not None and end_v.end is not None and end_v.end < stale_before:
        return facts.na("pct", "missing", as_of=end_v.end, derivation=f"{label}: newest FY{last} is stale")
    base = fy.get(last - years)
    deriv = f"(FY{last}/FY{last - years})^(1/{years})-1"
    if base is None or not base.ok or not end_v.ok:
        return facts.na("pct", "insufficient_history", as_of=end_v.end, derivation=deriv)
    val, reason = _edgar.cagr_pct(base.value, end_v.value, years)
    if val is None:
        return facts.na("pct", reason or "missing", as_of=end_v.end, derivation=deriv)
    return common.fact(val, "pct", facts.cf, end_v.end, "fundamental", filed=end_v.filed, derivation=deriv)


def build_payload(ticker: str, *, cik: int, cf: Mapping[str, Any], sub: Mapping[str, Any] | None,
                  price: float | None, price_as_of: str | None, as_of: date, sector_class: str | None = None,
                  map_title: str | None = None, cfg: Mapping[str, Any] | None = None,
                  overrides: Mapping[str, Any] | None = None,
                  yf_cap: Callable[[str], tuple[float | None, str | None]] | None = None,
                  valuation_cfg: Mapping[str, Any] | None = None) -> dict:
    """Pure-ish assembly of the §4.3 payload from already-fetched companyfacts/submissions.
    Returns ``{"payload": {...}, "sources": [...], "flags": [...], "missing_data": [...]}``."""
    cfg = _edgar.settings(cfg)
    T = common.norm_ticker(ticker)
    facts = _Facts(T)
    flags: list[dict] = []
    missing: list[str] = []
    sources: list[dict] = [common.dataset_source("D:SEC:MAP"),
                           common.dataset_source(facts.cf, cik=cik)]
    if sub:
        sources.append(common.dataset_source(facts.sub, cik=cik))
    ov = dict(overrides or {"shares_tags": None, "class_factor": None, "source_url": None})

    # ---- entity / sector class -----------------------------------------------------------
    sic_raw = (sub or {}).get("sic")
    sic_num = common.safe_float(sic_raw)
    sic = int(sic_num) if sic_num is not None else None
    if sector_class in ("default", "financials"):
        sclass = sector_class
    else:
        sclass = _edgar.sector_class_from_sic(sic, cfg)
    fin = sclass == "financials"
    entity = {"name": (sub or {}).get("name") or cf.get("entityName") or map_title, "sic": sic,
              "sic_description": (sub or {}).get("sicDescription") or None,
              "fiscal_year_end": (sub or {}).get("fiscalYearEnd") or None, "sector_class": sclass}

    # ---- parse -----------------------------------------------------------------------------
    p = _edgar.parse(cf, cfg, as_of=as_of, shares_tags=ov.get("shares_tags"), class_factor=ov.get("class_factor"))
    for code, sev, detail in p.flags:
        flags.append(common.flag(code, sev, detail))
    missing.extend(p.missing)
    max_gap = int(cfg.get("concept_max_gap_days", cfg.get("debt_formula_max_gap_days", 400)))
    stale_before = (p.ref_end - timedelta(days=max_gap)) if p.ref_end else None

    # ---- TTM -------------------------------------------------------------------------------
    t = dict(p.ttm)
    t["fcf"] = _combine(t["ocf"], t["capex"], lambda a, b: a - b, "ocf-capex")
    t["ebitda"] = _combine(t["operating_income"], t["da"], lambda a, b: a + b, "operating_income+da")
    ttm = {k: facts.of(t[k], "USD", nm=fin and k in FINANCIALS_NM_CONCEPTS) for k in TTM_KEYS}

    # ---- balance ---------------------------------------------------------------------------
    b = dict(p.balance)
    debt = p.debt.value
    b["total_debt"] = debt
    st_inv = b["st_investments"]
    if debt.ok and b["cash"].ok:
        nd = debt.value - b["cash"].value - (st_inv.value if st_inv.ok else 0.0)  # type: ignore[operator]
        nd_deriv = "total_debt-cash-st_investments" + ("" if st_inv.ok else " (st_investments n/a -> 0)")
        b["net_debt"] = Value(nd, debt.end, max(debt.filed, b["cash"].filed), None, nd_deriv)  # type: ignore[type-var]
    else:
        b["net_debt"] = Value.na((debt if not debt.ok else b["cash"]).na_reason or "missing",
                                 derivation="total_debt-cash-st_investments")
    balance = {k: facts.of(b[k], "USD") for k in BALANCE_KEYS}
    if debt.ok and p.debt.formula:
        balance["total_debt"]["formula"] = p.debt.formula

    for k in ("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "da", "pretax_income",
              "income_tax"):
        if ttm[k]["value"] is None and ttm[k]["na_reason"] in ("missing", "insufficient_history"):
            missing.append(f"ttm.{k}: {ttm[k]['na_reason'].replace('_', ' ')} in SEC companyfacts"
                           + MISSING_CONSEQUENCE.get(k, ""))
    for k in ("cash", "equity"):
        if balance[k]["value"] is None:
            missing.append(f"balance.{k}: not reported in SEC companyfacts")

    # ---- price, shares, market cap (+ Yahoo cross-check) ----------------------------------
    price_fact = (common.fact(price, "USD/share", facts.px, price_as_of, "price") if price is not None
                  else facts.na("USD/share", sid=facts.px, kind="price"))
    if price is not None:
        sources.append(common.dataset_source(facts.px))
    else:
        missing.append(f"price for {T} unavailable (no price argument and no cached close) — "
                       "market cap and price ratios not computed")
    sh = p.shares
    shares_edgar = facts.of(sh.value, "shares")
    if sh.class_factor is not None:
        shares_edgar["class_factor"] = sh.class_factor
        flags.append(common.flag("share_class_factor_applied", "low",
                                 f"EDGAR share count multiplied by class_factor {sh.class_factor:g} "
                                 "(config/etf_overrides.yaml)"))
        if ov.get("source_url"):
            try:
                sources.append(common.dataset_source(f"D:CFG:etf_overrides.securities.{T}.share_class",
                                                     url=ov["source_url"]))
            except ValueError:
                pass
        else:
            flags.append(common.flag("class_factor_unsourced", "medium",
                                     "share_class.class_factor has no source_url in etf_overrides.yaml"))
    usable = sh.value.ok and not p.shares_stale and (sh.value.value or 0) > 0
    if p.shares_stale:
        flags.append(common.flag("shares_stale", "high",
                                 f"newest EDGAR share count {sh.tag} is from {sh.value.end}, more than {max_gap} d "
                                 f"before the newest balance sheet {p.newest_bs}"))
        missing.append(f"shares: no current EDGAR share count (newest {sh.tag} is from {sh.value.end})")
    elif not sh.value.ok:
        missing.append("shares: no share count in SEC companyfacts")
    mcap_edgar = sh.value.value * price if (usable and price is not None) else None  # type: ignore[operator]
    yf_mcap, yf_err = (None, None)
    if price is not None:
        yf_mcap, yf_err = (yf_cap or _yf_market_cap)(T)
        if yf_mcap is not None:
            sources.append(common.dataset_source(facts.yf))
        elif usable:
            flags.append(common.flag("mcap_crosscheck_unavailable", "low",
                                     f"Yahoo marketCap cross-check skipped: {yf_err}"))
    threshold = float(cfg.get("share_count_divergence_pct", 10))
    divergence = (abs(mcap_edgar - yf_mcap) / yf_mcap * 100.0) if (mcap_edgar and yf_mcap) else None
    ambiguous = (divergence is not None and divergence > threshold) or (not usable and yf_mcap is not None)
    if ambiguous and yf_mcap is not None and price is not None:
        if divergence is not None:
            why = (f"EDGAR shares {sh.value.value:,.0f} ({sh.value.derivation}) x price {price:g} = "
                   f"{mcap_edgar:,.0f} vs Yahoo marketCap {yf_mcap:,.0f}: divergence {divergence:.1f}% > {threshold:g}%")
        else:
            why = (f"no current EDGAR share count ({sh.value.derivation or 'none'}"
                   f"{', as of ' + str(sh.value.end) if sh.value.end else ''}); Yahoo marketCap {yf_mcap:,.0f} used")
        flags.append(common.flag("share_count_ambiguous", "high", why))
        market_cap = common.fact(yf_mcap, "USD", facts.yf, price_as_of, "price", derivation="yfinance_marketCap",
                                 note="EDGAR share count ambiguous (multi-class or stale); see flags")
        shares = common.fact(yf_mcap / price, "shares", facts.yf, price_as_of, "fundamental",
                             derivation="yfinance_marketCap/price",
                             note="implied share count consistent with the quoted share class price")
    elif mcap_edgar is not None:
        market_cap = common.fact(mcap_edgar, "USD", facts.cf, price_as_of, "price",
                                 derivation=f"shares({sh.value.end}) x price", tag=sh.tag)
        shares = shares_edgar
    else:
        market_cap = facts.na("USD", "missing", kind="price",
                              derivation="needs price and a current EDGAR share count")
        shares = shares_edgar if usable else facts.na("shares", "missing", as_of=sh.value.end, tag=sh.tag,
                                                       derivation=sh.value.derivation)

    # ---- margins ---------------------------------------------------------------------------
    rev = ttm["revenue"]
    mism: list[str] = []
    margins = {
        "gross": (facts.na("pct", "not_meaningful", as_of=rev.get("as_of")) if fin else
                  _ratio(facts, ttm["gross_profit"], rev, "pct", "gross_profit/revenue", rev, scale=100.0,
                         same_period=mism, label="margins_pct.gross")),
        "operating": _ratio(facts, ttm["operating_income"], rev, "pct", "operating_income/revenue", rev,
                            scale=100.0, same_period=mism, label="margins_pct.operating"),
        "net": _ratio(facts, ttm["net_income"], rev, "pct", "net_income/revenue", rev, scale=100.0,
                      same_period=mism, label="margins_pct.net"),
        "fcf": _ratio(facts, ttm["fcf"], rev, "pct", "fcf/revenue", rev, scale=100.0,
                      same_period=mism, label="margins_pct.fcf"),
    }

    # ---- valuation -------------------------------------------------------------------------
    tax_v = t["income_tax"].value if t["income_tax"].ok else None
    pre_v = t["pretax_income"].value if t["pretax_income"].ok else None
    if tax_v is not None and pre_v is not None and t["income_tax"].end != t["pretax_income"].end:
        mism.append(f"valuation.tax_rate_used (income_tax TTM ends {t['income_tax'].end}, "
                    f"pretax_income TTM ends {t['pretax_income'].end}; default rate used)")
        tax_v = pre_v = None
    rate, rate_flag, rate_deriv = _edgar.effective_tax_rate(tax_v, pre_v, cfg)
    if rate_flag:
        flags.append(common.flag(rate_flag, "low", rate_deriv))
    tax_fact = common.fact(rate, "fraction", facts.cf, ttm["pretax_income"].get("as_of") or p.ref_end,
                           "fundamental", filed=ttm["pretax_income"].get("filed"), derivation=rate_deriv)
    val: dict[str, dict] = {}
    cash_f, debt_f = balance["cash"], balance["total_debt"]
    if _val(market_cap) is not None and _val(debt_f) is not None and _val(cash_f) is not None:
        st = _val(balance["st_investments"]) or 0.0
        ev_v = market_cap["value"] + debt_f["value"] - cash_f["value"] - st
        ev_deriv = "market_cap+total_debt-cash-st_investments" + (
            "" if _val(balance["st_investments"]) is not None else " (st_investments n/a -> 0)")
        val["ev"] = common.fact(ev_v, "USD", facts.cf, debt_f.get("as_of"), "fundamental",
                                filed=debt_f.get("filed"), derivation=ev_deriv)
    else:
        first_null = next(f for f in (market_cap, debt_f, cash_f) if _val(f) is None)
        val["ev"] = facts.na("USD", first_null.get("na_reason") or "missing",
                             derivation="market_cap+total_debt-cash-st_investments")
    ni, oi, eq = ttm["net_income"], ttm["operating_income"], balance["equity"]
    val["pe"] = _ratio(facts, market_cap, ni, "x", "market_cap/net_income_ttm", ni)   # NI <= 0 -> negative_denominator
    val["ev_ebit"] = _ratio(facts, val["ev"], oi, "x", "ev/operating_income_ttm", oi)
    val["ev_sales"] = _ratio(facts, val["ev"], rev, "x", "ev/revenue_ttm", rev)
    val["fcf_yield_pct"] = _ratio(facts, ttm["fcf"], market_cap, "pct", "fcf_ttm/market_cap", ttm["fcf"], scale=100.0)
    val["net_debt_ebitda"] = _ratio(facts, balance["net_debt"], ttm["ebitda"], "x", "net_debt/ebitda_ttm",
                                    ttm["ebitda"])
    val["p_b"] = _ratio(facts, market_cap, eq, "x", "market_cap/equity", eq)
    # P/TBV = mcap/(equity-goodwill-intangibles); a missing goodwill/intangibles component is NOT
    # assumed to be 0 (that overstates tangible book) -> null "missing" (DESIGN §1.1).
    gw, ia = _val(balance["goodwill"]), _val(balance["intangibles"])
    tbv_deriv = "market_cap/(equity-goodwill-intangibles)"
    tbv_gaps = [n for n, v in (("goodwill", gw), ("intangibles", ia)) if v is None]
    if _val(eq) is not None and tbv_gaps:
        val["p_tbv"] = facts.na("x", "missing", as_of=eq.get("as_of"),
                                derivation=f"{tbv_deriv} ({', '.join(tbv_gaps)} not reported in EDGAR)")
        missing.append(f"valuation.p_tbv: {' and '.join(tbv_gaps)} not reported in SEC companyfacts — "
                       "tangible book value not computed (read the balance sheet / intangibles note)")
    else:
        if _val(eq) is not None:
            tbv = Value(eq["value"] - gw - ia, common.parse_date(eq.get("as_of")),  # type: ignore[operator]
                        common.parse_date(eq.get("filed")), None, "equity-goodwill-intangibles")
            tbv_fact = facts.of(tbv, "USD")
        else:
            tbv_fact = eq
        val["p_tbv"] = _ratio(facts, market_cap, tbv_fact, "x", tbv_deriv, eq)
    val["roe_pct"] = _ratio(facts, ni, eq, "pct", "net_income_ttm/equity", ni, scale=100.0)
    if _val(oi) is not None:
        nopat = {"value": oi["value"] * (1.0 - rate), "na_reason": None}
    else:
        nopat = {"value": None, "na_reason": oi.get("na_reason")}
    if _val(eq) is not None and _val(balance["net_debt"]) is not None:
        ic = {"value": eq["value"] + balance["net_debt"]["value"], "na_reason": None}
    else:
        ic = {"value": None, "na_reason": (eq if _val(eq) is None else balance["net_debt"]).get("na_reason")}
    roic_deriv = "operating_income_ttm*(1-tax_rate)/(equity+net_debt)"
    if _val(eq) is not None and eq["value"] <= 0:
        val["roic_pct"] = facts.na("pct", "not_meaningful", as_of=oi.get("as_of"),
                                   derivation=f"{roic_deriv}: not meaningful, equity <= 0 (negative book equity)")
    elif ic["value"] is not None and ic["value"] <= 0:
        val["roic_pct"] = facts.na("pct", "not_meaningful", as_of=oi.get("as_of"),
                                   derivation=f"{roic_deriv}: not meaningful, equity + net_debt <= 0")
    else:
        val["roic_pct"] = _ratio(facts, nopat, ic, "pct", roic_deriv, oi, scale=100.0)
    val["tax_rate_used"] = tax_fact
    if fin:
        for k in FINANCIALS_NOT_MEANINGFUL:
            unit = "USD" if k == "ev" else "pct" if k.endswith("_pct") else "x"
            val[k] = facts.na(unit, "not_meaningful", derivation="not meaningful for financials (sector_class)")
    valuation = {k: val[k] for k in VALUATION_KEYS}
    balance_notes = _captive_finance(T, fin=fin, sic=sic, balance=balance, valuation=valuation, market_cap=market_cap,
                                     ttm=ttm, vcfg=_valuation.resolve_cfg(valuation_cfg), flags=flags,
                                     facts=facts)
    if mism:
        flags.append(common.flag("period_mismatch", "medium",
                                 "TTM inputs cover different periods; ratio not computed: " + "; ".join(mism)))
        missing.extend(f"{m.split(' (', 1)[0]}: TTM inputs cover different periods" for m in mism
                       if not m.startswith("valuation.tax_rate_used"))

    # ---- FY history + growth ---------------------------------------------------------------
    hist: dict[str, dict[int, Value]] = {c: {y: _obs_value(o) for y, o in p.flows[c].fy.items()}
                                         for c in _edgar.FY_HISTORY_CONCEPTS}
    fcf_hist: dict[int, Value] = {}
    for y in set(hist["ocf"]) & set(hist["capex"]):
        fcf_hist[y] = _combine(hist["ocf"][y], hist["capex"][y], lambda a, c: a - c, "ocf-capex")
    years = sorted({y for c in hist.values() for y in c}, reverse=True)[: int(cfg.get("fy_history_years", 5))]
    fy_rows = []
    for y in years:
        row: dict[str, Any] = {"fy": int(y)}
        ends = [hist[c][y].end for c in hist if y in hist[c] and hist[c][y].end]
        row["period_end"] = max(ends).isoformat() if ends else None
        for c in _edgar.FY_HISTORY_CONCEPTS:
            row[c] = facts.of(hist[c].get(y) or Value.na("missing"), "USD",
                              nm=fin and c in FINANCIALS_NM_CONCEPTS)
        row["fcf"] = facts.of(fcf_hist.get(y) or Value.na("missing"), "USD")
        fy_rows.append(row)
    n_years = int(cfg.get("cagr_years", 3))
    growth = {
        "revenue_cagr_3y": _cagr_fact(facts, hist["revenue"], n_years, "revenue", stale_before=stale_before),
        "ni_cagr_3y": _cagr_fact(facts, hist["net_income"], n_years, "net_income", stale_before=stale_before),
        "fcf_cagr_3y": _cagr_fact(facts, fcf_hist, n_years, "fcf", stale_before=stale_before),
    }
    rflow, rnow = p.flows["revenue"], t["revenue"]
    prior = rflow.prior_ttm
    yoy_deriv = f"TTM({rnow.end})/TTM({prior.end if prior else '?'})-1"
    if not rnow.ok:
        growth["revenue_yoy_ttm"] = facts.na("pct", rnow.na_reason or "missing", derivation="ttm revenue yoy")
    elif prior is None or not prior.ok:
        growth["revenue_yoy_ttm"] = facts.na("pct", "insufficient_history", as_of=rnow.end, derivation=yoy_deriv)
    elif prior.value <= 0:  # type: ignore[operator]
        growth["revenue_yoy_ttm"] = facts.na("pct", "negative_denominator", as_of=rnow.end, derivation=yoy_deriv)
    else:
        growth["revenue_yoy_ttm"] = common.fact((rnow.value / prior.value - 1.0) * 100.0, "pct", facts.cf,  # type: ignore[operator]
                                                rnow.end, "fundamental", filed=rnow.filed,
                                                derivation=f"TTM {rnow.derivation} / TTM {prior.derivation} - 1")

    # ---- tag map ---------------------------------------------------------------------------
    tag_map: dict[str, dict] = {}
    for c, r in p.flows.items():
        entry: dict[str, Any] = {"tag": r.tag, "stitched": bool(r.stitched)}
        if r.stitched:
            entry["fy_tags"] = {str(y): r.fy_tags[y] for y in sorted(r.fy_tags)}
        tag_map[c] = entry
    if t["gross_profit"].ok and (t["gross_profit"].derivation or "").startswith("revenue-cogs"):
        tag_map["gross_profit"] = {"tag": None, "stitched": False, "derived": "revenue-cogs"}
        if p.flows["gross_profit"].tag:
            tag_map["gross_profit"]["ignored_tag"] = p.flows["gross_profit"].tag   # period-misaligned
    if t["da"].ok and (t["da"].derivation or "").startswith("depreciation + amortization"):
        tag_map["da"] = {"tag": None, "stitched": False, "derived": "+".join(_edgar.DA_PARTS)}
    for c, r in p.instants.items():
        tag_map[c] = {"tag": r.tag, "stitched": False}
    tag_map["total_debt"] = {"tag": debt.tag, "stitched": False, "formula": p.debt.formula}
    if p.debt.assumed_zero:
        tag_map["total_debt"]["assumed_zero"] = True
    if p.debt.current_portion_missing:
        tag_map["total_debt"]["current_portion_missing"] = True
    tag_map["shares"] = {"tag": sh.tag, "stitched": False, "classes_summed": sh.classes_summed,
                         "class_factor": sh.class_factor}

    # ---- lag (step 13) ---------------------------------------------------------------------
    lag = _edgar.detect_lag(cf, sub, cik, as_of=as_of)
    if lag.lag:
        flags.append(common.flag("companyfacts_lag", "high", lag.message or "companyfacts lags submissions"))
        missing.append(lag.message or "companyfacts lags the latest periodic filing")
    sub_latest = None
    if lag.latest:
        sub_latest = {k: lag.latest.get(k) for k in ("form", "filed", "report_date", "accn")}
        sub_latest["url"] = _edgar.filing_url(cik, lag.latest["accn"], lag.latest.get("primary_document"))

    coverage = "sec" if (ttm["revenue"]["value"] is not None and ttm["net_income"]["value"] is not None
                         and balance["equity"]["value"] is not None and usable) else "partial"
    payload = {
        "ticker": T, "cik": int(cik), "coverage": coverage, "entity": entity,
        "companyfacts_latest": p.latest, "submissions_latest": sub_latest, "lag_detected": bool(lag.lag),
        "price": price_fact, "shares": shares, "shares_edgar": shares_edgar, "market_cap": market_cap,
        "ttm": ttm, "fy": fy_rows, "balance": balance, "margins_pct": margins, "growth_pct": growth,
        "valuation": valuation, "tag_map": tag_map,
    }
    if balance_notes:
        payload["balance_notes"] = balance_notes
    sources = common.dedupe_sources(sources)
    _drop_dangling_null_sources(payload, {s["id"] for s in sources})
    return {"payload": payload, "sources": sources, "flags": flags, "missing_data": missing}


CAPTIVE_NOTE = ("consolidated figures include the captive finance subsidiary (its debt funds finance receivables); "
                "leverage ratios (net debt/EBITDA, EV-based multiples, ROIC) are overstated/distorted")
#: Facts of a captive-finance company that are built on consolidated debt (annotated, values kept).
CAPTIVE_ANNOTATED = (("balance", "total_debt"), ("balance", "net_debt"), ("valuation", "ev"), ("valuation", "ev_ebit"),
                     ("valuation", "ev_sales"), ("valuation", "net_debt_ebitda"), ("valuation", "roic_pct"))


def _append_note(fact: dict, text: str) -> None:
    fact["note"] = f"{fact['note']}; {text}" if fact.get("note") else text


def _captive_finance(T: str, *, fin: bool, sic: int | None, balance: dict, valuation: dict, market_cap: dict,
                     ttm: Mapping[str, dict], vcfg: Mapping[str, Any], flags: list[dict], facts: "_Facts") -> list[str]:
    """Captive-finance handling (DESIGN §10). Returns ``balance_notes`` (empty for ordinary companies).

    * ticker in ``valuation.captive_finance_tickers`` (or with a net-debt override): flag
      ``captive_finance`` (medium), annotate the consolidated-debt facts (values are KEPT so existing
      consumers keep working, but carry a ``note``) and add ``valuation.net_debt_ebitda_ex_captive``:
      null (``not_applicable``: the captive's debt cannot be separated from XBRL companyfacts) unless a
      configured override fraction gives the industrial net debt.
    * otherwise, generic safeguard: industrials / consumer_discretionary with consolidated net debt /
      market cap above ``valuation.possible_captive_net_debt_to_mcap`` -> low flag
      ``possible_captive_finance`` (the tag map resolves no financing-revenue concept, so this is
      deliberately cheap and false-positive tolerant)."""
    if fin:
        return []
    basis, frac, _invalid = _valuation.net_debt_basis(T, vcfg)
    nd, ebitda, mcap = _val(balance["net_debt"]), _val(ttm["ebitda"]), _val(market_cap)
    if basis != "consolidated":
        flags.append(common.flag("captive_finance", "medium",
                                 f"{T} consolidates a captive finance subsidiary: " + CAPTIVE_NOTE
                                 + ". valuation.py does not deduct consolidated net debt for this ticker"))
        for blk, key in CAPTIVE_ANNOTATED:
            node = (balance if blk == "balance" else valuation).get(key)
            if isinstance(node, dict):
                _append_note(node, "includes captive finance debt")
        if frac is not None and nd is not None and ebitda is not None and ebitda > 0:
            valuation["net_debt_ebitda_ex_captive"] = common.fact(
                nd * frac / ebitda, "x", facts.cf, balance["net_debt"].get("as_of"), "fundamental",
                filed=balance["net_debt"].get("filed"),
                derivation=f"net_debt x {frac:g} (valuation.captive_finance_net_debt_pct_override)/ebitda_ttm",
                note="configured assumption for the industrial share of consolidated net debt, not a reported figure")
        else:
            valuation["net_debt_ebitda_ex_captive"] = facts.na(
                "x", "not_applicable", as_of=balance["net_debt"].get("as_of"), derivation="net_debt_industrial/ebitda_ttm",
                note="captive finance debt cannot be separated from XBRL companyfacts")
        return [CAPTIVE_NOTE[0].upper() + CAPTIVE_NOTE[1:] + ".",
                "valuation.net_debt_ebitda is the consolidated ratio; net_debt_ebitda_ex_captive is "
                + ("a configured-assumption estimate." if frac is not None else
                   "null (captive debt cannot be separated from XBRL companyfacts) - read the 10-K segment data.")]
    thr = float(vcfg.get("possible_captive_net_debt_to_mcap", 0.25))
    sector = common.sic_to_sector(sic)
    if (sector in _valuation.POSSIBLE_CAPTIVE_SECTORS and nd is not None and mcap is not None and mcap > 0
            and nd / mcap > thr):
        flags.append(common.flag("possible_captive_finance", "low",
                                 f"consolidated net debt is {nd / mcap:.0%} of market cap (> {thr:.0%}) in {sector}: "
                                 "check whether a captive finance arm (financing revenue / finance receivables) is "
                                 "consolidated; if so net debt and net debt/EBITDA overstate industrial leverage"))
    return []


def _drop_dangling_null_sources(node: Any, known: set[str]) -> None:
    """A null Fact must not cite a source id that is absent from ``sources[]`` (e.g. ``D:SEC:LUMI.TA:CF``
    for a non-SEC ticker, or ``D:PX:T`` when no price exists): set its ``source_id`` to null in place."""
    if isinstance(node, dict):
        if (common.is_fact(node) and node.get("value") is None and isinstance(node.get("source_id"), str)
                and node["source_id"] not in known):
            node["source_id"] = None
        for v in node.values():
            _drop_dangling_null_sources(v, known)
    elif isinstance(node, list):
        for v in node:
            _drop_dangling_null_sources(v, known)


def _degraded(T: str, *, cik: int | None, coverage: str, reason_missing: list[str], flags: list[dict],
              sources: list[dict], price: float | None, price_as_of: str | None, as_of: date,
              sector_class: str | None, entity: Mapping[str, Any] | None = None) -> dict:
    facts = _Facts(T)
    sclass = sector_class if sector_class in ("default", "financials") else (
        (entity or {}).get("sector_class") or "default")
    blocks = _null_blocks(facts, sector_class=sclass)
    price_fact = (common.fact(price, "USD/share", facts.px, price_as_of, "price") if price is not None
                  else facts.na("USD/share", sid=facts.px, kind="price"))
    if price is not None:
        sources = sources + [common.dataset_source(facts.px)]
    ent = {"name": None, "sic": None, "sic_description": None, "fiscal_year_end": None, "sector_class": sclass}
    ent.update({k: v for k, v in (entity or {}).items() if k in ent and v is not None})
    payload = {"ticker": T, "cik": cik, "coverage": coverage, "entity": ent, "companyfacts_latest": None,
               "submissions_latest": None, "lag_detected": False, "price": price_fact, **blocks}
    _drop_dangling_null_sources(payload, {s["id"] for s in sources})
    return common.envelope(SCHEMA, as_of, sources=sources, missing_data=reason_missing, flags=flags, **payload)


# =============================================================================================
# Public API
# =============================================================================================


def run(ticker: str, out: Path | str | None = None, *, price: Any = None, as_of: Any = None,
        sector_class: str | None = None, cache: common.DiskCache | None = None, price_as_of: Any = None) -> dict:
    """Fetch + assemble ``fundamentals/v1`` for ``ticker``; writes ``out`` when given.

    ``price``: float or a price Fact (else the cached close from ``fetch_prices.load_series``).
    ``sector_class``: ``default|financials`` (else SIC 6000-6799 except 6798 -> financials).
    Never raises for data problems (non-SEC tickers, outages -> null Facts + missing_data/flags);
    ``ConfigError`` (e.g. missing ``sec_contact_email``) propagates."""
    T = common.norm_ticker(ticker)
    as_of_d = common.parse_date(as_of) or common.today()
    committee = _committee()
    cfg = fund_cfg(committee)
    cache = cache if cache is not None else common.DiskCache()
    pv, p_as_of, _origin = _resolve_price(T, price, as_of_d, price_as_of)

    tmap, map_meta = load_ticker_map(cache, committee)
    flags: list[dict] = []
    if map_meta.get("stale"):
        flags.append(common.flag("stale_cache", "high", f"SEC ticker map served from stale cache ({map_meta['error']})"))
    if tmap is None:
        doc = _degraded(T, cik=None, coverage="partial", flags=flags + [common.flag(
            "sec_unavailable", "high", f"SEC ticker map unavailable: {map_meta['error']}")],
            reason_missing=[f"SEC ticker map unavailable ({map_meta['error']}) — fundamentals not fetched"],
            sources=[], price=pv, price_as_of=p_as_of, as_of=as_of_d, sector_class=sector_class)
        return _finish(doc, out)
    cik, title = lookup_cik(T, tmap)
    if cik is None:
        doc = _degraded(T, cik=None, coverage="non_sec", flags=flags, reason_missing=["not an SEC filer"],
                        sources=[common.dataset_source("D:SEC:MAP")], price=pv, price_as_of=p_as_of,
                        as_of=as_of_d, sector_class=sector_class)
        return _finish(doc, out)

    ttl_f = _ttl("fundamentals", committee)
    key = f"CIK{int(cik):010d}"
    sub, sub_meta = _get_json(cache, KIND_SUB, key, common.sec_submissions_url(cik), ttl_f,
                              lambda raw: _edgar.trim_submissions(raw, int(cfg.get("submissions_keep_recent", 40))))
    cf, cf_meta = _get_json(cache, KIND_CF, key, common.sec_companyfacts_url(cik), ttl_f, _edgar.trim_companyfacts,
                            fresh_ok=lambda hit: int(hit.get("trim_rev") or 0) >= _edgar.TRIM_REV)  # older trims lack tags
    for what, meta in (("submissions", sub_meta), ("companyfacts", cf_meta)):
        if meta.get("stale"):
            flags.append(common.flag("stale_cache", "high", f"SEC {what} served from stale cache ({meta['error']})"))
    missing: list[str] = []
    if sub is None:
        missing.append(f"SEC submissions unavailable ({sub_meta['error']}) — lag check and SIC not available")
    if cf is None:
        entity = None
        if sub:
            sic = common.safe_float(sub.get("sic"))
            entity = {"name": sub.get("name") or title, "sic": int(sic) if sic is not None else None,
                      "sic_description": sub.get("sicDescription"), "fiscal_year_end": sub.get("fiscalYearEnd"),
                      "sector_class": _edgar.sector_class_from_sic(sic, cfg)}
        sources = [common.dataset_source("D:SEC:MAP")]
        if sub:
            sources.append(common.dataset_source(f"D:SEC:{T}:SUB", cik=cik))
        doc = _degraded(T, cik=int(cik), coverage="partial", flags=flags + [common.flag(
            "sec_unavailable", "high", f"SEC companyfacts unavailable: {cf_meta['error']}")],
            reason_missing=missing + [f"SEC companyfacts unavailable ({cf_meta['error']})"], sources=sources,
            price=pv, price_as_of=p_as_of, as_of=as_of_d, sector_class=sector_class, entity=entity)
        return _finish(doc, out)

    built = build_payload(T, cik=int(cik), cf=cf, sub=sub, price=pv, price_as_of=p_as_of, as_of=as_of_d,
                          sector_class=sector_class, map_title=title, cfg=cfg, overrides=share_class_override(T),
                          valuation_cfg=committee.get("valuation") if isinstance(committee, Mapping) else None)
    doc = common.envelope(SCHEMA, as_of_d, sources=built["sources"], missing_data=missing + built["missing_data"],
                          flags=flags + built["flags"], **built["payload"])
    return _finish(doc, out)


def _finish(doc: dict, out: Path | str | None) -> dict:
    doc = common.to_jsonable(doc)
    if out is not None:
        common.write_json(out, doc)
    return doc


# =============================================================================================
# CLI
# =============================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(description="SEC EDGAR fundamentals + ratios -> fundamentals_<T>.json (DESIGN §4.3)")
    ap.add_argument("--ticker", required=True, help="ticker, e.g. KO or BRK.B")
    ap.add_argument("--out", help="output JSON path (default: print a summary only)")
    ap.add_argument("--price", type=float, default=None, help="share price (default: cached close)")
    ap.add_argument("--sector-class", choices=["default", "financials"], default=None,
                    help="override the SIC-based sector class")
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    try:
        common.apply_common_args(args)
        doc = run(args.ticker, Path(args.out) if args.out else None, price=args.price, as_of=args.as_of,
                  sector_class=args.sector_class)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    summary = {"ticker": doc["ticker"], "coverage": doc["coverage"], "cik": doc.get("cik"),
               "lag_detected": doc.get("lag_detected"), "out": args.out,
               "flags": [f["code"] for f in doc.get("flags", [])], "missing_data": len(doc.get("missing_data", []))}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
