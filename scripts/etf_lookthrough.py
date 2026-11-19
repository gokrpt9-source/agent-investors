"""Portfolio look-through: true sector exposure with funds decomposed (DESIGN §4.6).

Output ``phase1/lookthrough.json`` (schema ``lookthrough/v1``)::

    {"total_value_usd": Fact,
     "holdings": [{"ticker","asset_type","weight_pct": Fact,"sector","sector_split": {bucket: pct},
                   "classification_source","flags": [...], "sector_class","input","shares",
                   "price_ticker","proxy_ticker","price": Fact|null,"value_usd": Fact,
                   "composition_source" (funds),"contribution_pct": {bucket: pct of portfolio}}],
     "cash": {"value_usd": Fact, "weight_pct": Fact, "input": "cash_usd|cash_pct|none"},
     "sector_weights_pct": {<11 GICS keys>, "cash", "unclassified": pct},   # sums to 100 ± 0.01
     "funds": {"QQQ": {"expense_ratio_pct": Fact, "top10": [{"symbol","name","weight_pct"}],
                       "top10_concentration_pct": Fact, "source_id", "composition_source",
                       "price_ticker", "sector_split"}},
     "overlap": {"pairs": [{"a","b","overlap_pct","basis","common"}],
                 "direct_overlap": [{"fund","holding","fund_weight_pct","implied_portfolio_pct"}]}}

Classification (``classify_security``), first match wins:
portfolio ``sector_override`` → ``etf_overrides.securities.<T>`` (BRK-B → financials, flag
``conglomerate``) → Yahoo ``info['sector']`` via ``common.YF_SECTOR_MAP`` → SIC range map (a ``sic``
argument, else the lazy ``sic_fn``; ``run`` passes ``sec_sic`` = SEC submissions via the cached,
keyless EDGAR ticker map) → ``unclassified``. Asset type: caller/portfolio ``asset_type`` →
overrides → Yahoo ``quoteType`` (ETF/MUTUALFUND → fund) → stock. Funds get sector ``funds``.

Fund decomposition: ``common.yf_funds`` sector weightings (fractions → percent, Yahoo keys mapped
with ``YF_FUND_SECTOR_MAP``; unknown keys → unclassified). If they sum to 100 ± ``lookthrough.
renormalize_tolerance_pp`` (default 1 pp) they are renormalized to exactly 100; above that band
they are scaled down to 100 (flag); below it the residual goes to ``unclassified`` (flag). Fallback:
``etf_overrides.funds.<T>.sector_weights`` (must carry ``source_url`` + ``as_of``) → the holding's
``sector_override`` (100%) → 100% ``unclassified`` + ``missing_data``.

Weights: ``shares × last_close`` (prices.json payload; the ``proxy_ticker``'s price when set — a
ticker missing from prices is read from the cached series via ``fetch_prices.load_series``, cut at
``as_of``) or ``weight_pct`` of ``total_value_usd``; cash from ``cash_usd`` / ``cash_pct``.
A holding whose value is unknown (shares but no price, or neither shares nor weight_pct) is never
silently dropped from the denominator: with ``total_value_usd`` declared, the declared total is used
and the uncovered remainder goes to ``unclassified``; without it the portfolio total is unknown, so
every holding ``weight_pct`` is null (flag ``weights_unreliable``, high) and the sector look-through
is ``unclassified`` (+ ``cash_pct`` when given). ``weight_pct`` inputs that (with cash) exceed 100
without a total are rescaled per holding, so holding weights and sector buckets agree. Sector
weights are rounded to 2 dp and the rounding residual is put into the largest bucket, so they sum to 100
within ``lookthrough.sum_tolerance_pct``.

Overlap (basis ``lookthrough.overlap_basis`` = top10): for each pair of funds,
``overlap_pct = Σ_{common symbols} min(w_a, w_b)`` over their top-``lookthrough.top_n`` holdings;
``direct_overlap`` lists directly held stocks that sit in a fund's top 10.

Provenance: price Facts cite ``D:PX:<T>`` and every cited price source is listed in ``sources[]``
(also for ``weight_pct`` holdings, whose ``price`` is informational); investor-supplied numbers
(``total_value_usd``, ``cash.*``, ``weight_pct`` holding weights/values) cite the dataset source
``D:CFG:portfolio`` (always listed); a Fact with no source at all has ``source_id`` null.

Yahoo calls are cached here (``info`` kind, key ``info_<T>``, TTL ``cache_ttl_hours.info``; ``etf``
kind, key ``funds_<T>``, TTL ``cache_ttl_hours.etf``) with a flagged stale-cache fallback;
``screen.py`` reuses ``cached_yf_info`` / ``cached_yf_funds``.

CLI::

    python scripts/etf_lookthrough.py --prices runs/<id>/phase1/prices.json --out runs/<id>/phase1/lookthrough.json
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
from collections import OrderedDict  # noqa: E402
from typing import Any, Callable, Mapping, NamedTuple  # noqa: E402

SCHEMA = "lookthrough"
#: Investor-supplied inputs (portfolio.yaml: declared total, cash, weight_pct holdings) are cited as
#: ``D:CFG:portfolio``. The user's file has no upstream URL; the placeholder below follows the
#: convention already used by the repo fixtures / tests (tests/test_portfolio.py ``make_lookthrough``).
PORTFOLIO_SOURCE_ID = "D:CFG:portfolio"
PORTFOLIO_SOURCE_URL = "https://github.com/agent-investors/config/portfolio.yaml"
PORTFOLIO_SOURCE_TITLE = "portfolio.yaml (investor-supplied holdings, cash and totals)"
BUCKETS: tuple[str, ...] = common.SECTOR_KEYS + ("cash", "unclassified")
FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})

#: Fallbacks used only when committee.yaml lacks the key.
DEFAULT_SUM_TOLERANCE_PCT = 0.01
DEFAULT_TOP_N = 10
DEFAULT_OVERLAP_BASIS = "top10"
#: Fund sector weights within ±this many pp of 100 are treated as rounding noise and renormalized.
#: Read from ``lookthrough.renormalize_tolerance_pp`` when present (not in committee.yaml yet).
DEFAULT_RENORMALIZE_TOLERANCE_PP = 1.0
DEFAULT_TTL_HOURS = {"info": 24.0, "etf": 168.0, "fundamentals": 168.0}
#: ``fundamentals.submissions_keep_recent`` fallback (same trim as fetch_fundamentals' cache entries).
DEFAULT_SUBMISSIONS_KEEP = 40
#: Informational only: note when a declared total_value_usd differs from the priced total by more.
DECLARED_TOTAL_NOTE_PCT = 1.0

SECTOR_DP = 2      # sector_weights_pct rounding
WEIGHT_DP = 4      # holding weights / splits / contributions rounding


# =====================================================================================
# Config helpers
# =====================================================================================


def _cfg(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if cfg is not None:
        return cfg
    try:
        return common.committee_cfg()
    except common.ConfigError:
        return {}


def _lt_cfg(cfg: Mapping[str, Any] | None) -> dict:
    section = _cfg(cfg).get("lookthrough")
    return dict(section) if isinstance(section, Mapping) else {}


def _ttl(kind: str, cfg: Mapping[str, Any] | None = None) -> float:
    ttl = (_cfg(cfg).get("cache_ttl_hours") or {}).get(kind)
    f = common.safe_float(ttl)
    return DEFAULT_TTL_HOURS.get(kind, 24.0) if f is None else f


def load_overrides() -> dict:
    """``etf_overrides.yaml`` or an empty structure when it is missing/unreadable."""
    try:
        data = common.load_config("etf_overrides")
    except common.ConfigError:
        data = {}
    return data if isinstance(data, dict) else {}


def _override_maps(overrides: Mapping[str, Any] | None) -> tuple[dict, dict]:
    """``(securities, funds)`` keyed by normalized ticker. ``overrides`` may be the whole
    etf_overrides config or just its ``securities`` mapping; None → load the config."""
    if overrides is None:
        overrides = load_overrides()
    if not isinstance(overrides, Mapping):
        return {}, {}
    if "securities" in overrides or "funds" in overrides:
        secs, funds = overrides.get("securities") or {}, overrides.get("funds") or {}
    else:
        secs, funds = overrides, {}

    def norm(m: Any) -> dict:
        out: dict[str, dict] = {}
        for k, v in (m or {}).items():
            try:
                out[common.norm_ticker(k)] = dict(v or {})
            except (ValueError, TypeError):
                continue
        return out

    return norm(secs), norm(funds)


# =====================================================================================
# Cached Yahoo helpers (also used by screen.py)
# =====================================================================================


class Fetched(NamedTuple):
    payload: Any            # dict or None
    provider: str           # yfinance | cache | cache_stale | failed
    error: str | None
    fetched_at: str | None


def cached_call(kind: str, key: str, fn: Callable[[], Any], *, ttl_hours: float,
                source_url: str | None, cache: common.DiskCache | None = None) -> Fetched:
    """Cache (TTL) → ``fn()`` → stale cache. Errors are never cached."""
    cache = cache or common.DiskCache()
    hit = cache.get(kind, key, ttl_hours)
    if isinstance(hit, Mapping) and hit:
        return Fetched(dict(hit), "cache", None, getattr(hit, "fetched_at", None))
    try:
        payload = fn()
    except common.DataUnavailable as exc:
        stale = cache.get_stale(kind, key)
        err = f"{type(exc).__name__}: {exc}"
        if isinstance(stale, Mapping) and stale:
            return Fetched(dict(stale), "cache_stale", err, getattr(stale, "fetched_at", None))
        return Fetched(None, "failed", err, None)
    if isinstance(payload, Mapping) and payload:
        cache.put(kind, key, dict(payload), source_url)
        return Fetched(dict(payload), "yfinance", None, None)
    return Fetched(None, "failed", "empty response", None)


def cached_yf_info(ticker: str, *, cfg: Mapping[str, Any] | None = None,
                   cache: common.DiskCache | None = None) -> Fetched:
    t = common.norm_ticker(ticker)
    return cached_call("info", f"info_{t}", lambda: common.yf_info(t), ttl_hours=_ttl("info", cfg),
                       source_url=common.yf_quote_url(t), cache=cache)


def cached_yf_funds(ticker: str, *, cfg: Mapping[str, Any] | None = None,
                    cache: common.DiskCache | None = None) -> Fetched:
    t = common.norm_ticker(ticker)
    return cached_call("etf", f"funds_{t}", lambda: common.yf_funds(t), ttl_hours=_ttl("etf", cfg),
                       source_url=common.yf_holdings_url(t), cache=cache)


def sec_sic(ticker: str, *, cfg: Mapping[str, Any] | None = None,
            cache: common.DiskCache | None = None) -> dict:
    """SEC SIC code of ``ticker`` → ``{"sic": int|None, "cik": int|None, "error": str|None,
    "stale": bool}``. Keyless EDGAR: ticker → CIK via ``fetch_fundamentals.load_ticker_map`` (cache
    kind ``sec_map``), then the submissions JSON (cache kind ``sec_submissions``, key
    ``CIK##########``, TTL ``cache_ttl_hours.fundamentals`` — the entry fetch_fundamentals writes)
    with a stale-cache fallback. Never raises for data/config problems."""
    res: dict[str, Any] = {"sic": None, "cik": None, "error": None, "stale": False}
    try:
        t = common.norm_ticker(ticker)
        ff = importlib.import_module("scripts.fetch_fundamentals")
        edgar = importlib.import_module("scripts._edgar")
    except (ImportError, ValueError, TypeError) as exc:
        res["error"] = f"SEC lookup unavailable: {exc}"
        return res
    cfg = _cfg(cfg)
    try:
        tmap, meta = ff.load_ticker_map(cache, cfg)
    except (common.AginvError, OSError, ValueError, TypeError, KeyError) as exc:
        res["error"] = f"SEC ticker map unavailable: {type(exc).__name__}: {exc}"
        return res
    if tmap is None:
        res["error"] = f"SEC ticker map unavailable: {(meta or {}).get('error')}"
        return res
    res["stale"] = bool((meta or {}).get("stale"))
    cik, _title = ff.lookup_cik(t, tmap)
    if cik is None:
        res["error"] = "not in the SEC ticker map (no EDGAR filings)"
        return res
    res["cik"] = int(cik)
    url = common.sec_submissions_url(cik)
    keep = int(common.safe_float((cfg.get("fundamentals") or {}).get("submissions_keep_recent"))
               or DEFAULT_SUBMISSIONS_KEEP)

    def fetch() -> Any:
        try:
            return edgar.trim_submissions(common.http_get_json(url, sec=True), keep)
        except common.ConfigError as exc:  # e.g. sec_contact_email not set: treat as unavailable
            raise common.DataUnavailable(str(exc)) from exc

    got = cached_call("sec_submissions", f"CIK{int(cik):010d}", fetch, ttl_hours=_ttl("fundamentals", cfg),
                      source_url=url, cache=cache)
    if not got.payload:
        res["error"] = f"SEC submissions unavailable: {got.error}"
        return res
    res["stale"] = res["stale"] or got.provider == "cache_stale"
    sic = common.safe_float(got.payload.get("sic"))
    if sic is None or sic <= 0:
        res["error"] = "SEC submissions carry no SIC code"
        return res
    res["sic"] = int(sic)
    return res


# =====================================================================================
# Classification
# =====================================================================================


def classify_security(ticker: str, *, info: Mapping[str, Any] | None = None,
                      overrides: Mapping[str, Any] | None = None, sic: int | None = None,
                      asset_type: str | None = None, sector_override: str | None = None,
                      fetch_info: bool = True, info_ticker: str | None = None,
                      cache: common.DiskCache | None = None,
                      sic_fn: Callable[[str], int | None] | None = None) -> dict:
    """Classify one security → ``{"sector","asset_type","sector_class","classification_source","flags"}``.

    ``classification_source`` ∈ ``portfolio_override | etf_overrides | yfinance_info | sic |
    unclassified`` for stocks; for funds (sector ``funds``) it names where the fund asset type
    came from (``portfolio | etf_overrides | yfinance_info``). ``info`` is fetched lazily (cached
    ``yf_info`` of ``info_ticker`` or the ticker) only when an earlier rule did not decide and
    ``fetch_info`` is true; a failed fetch just moves on to the next rule. ``sic_fn(ticker)`` is
    called lazily for the SIC rule only when no ``sic`` was given and no earlier rule decided."""
    t = common.norm_ticker(ticker)
    securities, _funds = _override_maps(overrides)
    entry = securities.get(t) or {}
    flags: list[str] = list(dict.fromkeys(str(f) for f in (entry.get("flags") or [])))
    state: dict[str, Any] = {"info": dict(info) if isinstance(info, Mapping) else None,
                             "tried": isinstance(info, Mapping)}

    def get_info() -> Mapping[str, Any]:
        if not state["tried"] and fetch_info:
            state["tried"] = True
            state["info"] = cached_yf_info(info_ticker or t, cache=cache).payload
        return state["info"] or {}

    at, at_src = None, None
    if asset_type in ("stock", "fund"):
        at, at_src = asset_type, "portfolio"
    elif entry.get("asset_type") in ("stock", "fund"):
        at, at_src = entry["asset_type"], "etf_overrides"
    else:
        qt = str(get_info().get("quoteType") or "").strip().upper()
        if qt in FUND_QUOTE_TYPES:
            at, at_src = "fund", "yfinance_info"
        elif qt:
            at, at_src = "stock", "yfinance_info"
    if at == "fund":
        return {"sector": "funds", "asset_type": "fund", "sector_class": "fund",
                "classification_source": at_src, "flags": flags}

    sector, src = None, None
    if sector_override:
        if sector_override in common.SECTOR_KEYS:
            sector, src = sector_override, "portfolio_override"
        else:
            flags.append("invalid_sector_override")
    if sector is None and entry.get("sector") in common.SECTOR_KEYS:
        sector, src = entry["sector"], "etf_overrides"
    if sector is None:
        ysec = get_info().get("sector")
        mapped = common.YF_SECTOR_MAP.get(str(ysec).strip()) if ysec else None
        if mapped:
            sector, src = mapped, "yfinance_info"
        elif ysec:
            flags.append("unmapped_yahoo_sector")
    if sector is None and sic is None and sic_fn is not None:
        sic = sic_fn(t)
    if sector is None and sic is not None:
        mapped = common.sic_to_sector(sic)
        if mapped:
            sector, src = mapped, "sic"
    if sector is None:
        sector, src = "unclassified", "unclassified"
        flags.append("unclassified")
    return {"sector": sector, "asset_type": "stock",
            "sector_class": common.sector_class(sector, "stock"),
            "classification_source": src, "flags": list(dict.fromkeys(flags))}


# =====================================================================================
# Fund composition
# =====================================================================================


def normalize_split(raw: Mapping[str, float], *, tolerance_pp: float = DEFAULT_RENORMALIZE_TOLERANCE_PP,
                    label: str = "") -> tuple[dict | None, list[dict]]:
    """Bucket percentages → a split summing to 100 (see module doc). Returns ``(split, flags)``;
    ``split`` is None when nothing positive is left. Negative weights are dropped (flagged)."""
    flags: list[dict] = []
    clean: dict[str, float] = {}
    for k, v in raw.items():
        f = common.safe_float(v)
        if f is None or f == 0:
            continue
        if f < 0:
            flags.append(common.flag("negative_fund_weight", "low", f"{label}: {k} {f} dropped"))
            continue
        bucket = k if k in BUCKETS else "unclassified"
        clean[bucket] = clean.get(bucket, 0.0) + f
    total = sum(clean.values())
    if total <= 0:
        return None, flags
    if total > 100.0 + tolerance_pp:
        flags.append(common.flag("fund_weights_over_100", "low",
                                 f"{label}: sector weights sum to {total:.4f}%; scaled to 100"))
        clean = {k: v * 100.0 / total for k, v in clean.items()}
    elif total >= 100.0 - tolerance_pp:
        clean = {k: v * 100.0 / total for k, v in clean.items()}
    else:
        residual = 100.0 - total
        clean["unclassified"] = clean.get("unclassified", 0.0) + residual
        flags.append(common.flag("fund_weights_residual", "low",
                                 f"{label}: sector weights sum to {total:.4f}%; {residual:.4f} pp → unclassified"))
    split = OrderedDict((b, round(clean[b], WEIGHT_DP)) for b in BUCKETS if clean.get(b))
    return dict(split), flags


def top_holdings(entries: Any, n: int = DEFAULT_TOP_N) -> list[dict]:
    """Top-``n`` holdings ``[{"symbol","name","weight_pct"}]`` (normalized symbols, sorted by weight
    desc, unknown weights last, input order kept for ties)."""
    rows: list[dict] = []
    for h in entries or []:
        if not isinstance(h, Mapping):
            continue
        try:
            sym = common.norm_ticker(h.get("symbol"))
        except (ValueError, TypeError):
            continue
        w = common.safe_float(h.get("holding_pct") if "holding_pct" in h else h.get("weight_pct"))
        rows.append({"symbol": sym, "name": (str(h.get("name")).strip() or None) if h.get("name") else None,
                     "weight_pct": None if w is None else round(w, WEIGHT_DP)})
    rows.sort(key=lambda r: (r["weight_pct"] is None, -(r["weight_pct"] or 0.0)))
    return rows[: max(0, int(n))]


def fund_composition(ticker: str, *, price_ticker: str | None = None, overrides: Mapping[str, Any] | None = None,
                     as_of: str, cfg: Mapping[str, Any] | None = None, sector_override: str | None = None,
                     cache: common.DiskCache | None = None) -> dict:
    """Decompose one fund. Returns ``{"split","composition_source","source_id","sources","flags",
    "missing_data","top10","top10_concentration_pct","expense_ratio_pct"}``."""
    t = common.norm_ticker(ticker)
    pt = common.norm_ticker(price_ticker) if price_ticker else t
    lt = _lt_cfg(cfg)
    tol = common.safe_float(lt.get("renormalize_tolerance_pp"))
    tol = DEFAULT_RENORMALIZE_TOLERANCE_PP if tol is None else tol
    top_n = int(lt.get("top_n") or DEFAULT_TOP_N)
    _secs, fund_over = _override_maps(overrides)
    res: dict[str, Any] = {"split": None, "composition_source": "none", "source_id": None, "sources": [],
                           "flags": [], "missing_data": [], "top10": [], "expense_ratio_pct": None,
                           "top10_concentration_pct": None}
    top_raw: list = []
    top_sid: str | None = None
    top_as_of = as_of
    exp_val: float | None = None
    exp_sid: str | None = None
    exp_field: str | None = None
    exp_as_of = as_of

    got = cached_yf_funds(pt, cfg=cfg, cache=cache)
    if got.payload:
        sid = f"D:YF:{pt}:FUND"
        data = got.payload
        fact_as_of = as_of
        if got.provider == "cache_stale":
            stale_day = common.date_str(got.fetched_at) if got.fetched_at else None
            fact_as_of = min(stale_day, as_of) if stale_day else as_of
            res["flags"].append(common.flag("stale_cache", "medium",
                                            f"{pt}: fund data fetch failed ({got.error}); using cache from {got.fetched_at}"))
        res["sources"].append(common.dataset_source(sid, accessed=as_of))
        raw: dict[str, float] = {}
        for k, frac in (data.get("sector_weightings") or {}).items():
            f = common.safe_float(frac)
            if f is None:
                continue
            key = common.YF_FUND_SECTOR_MAP.get(str(k).strip().lower())
            if key is None:
                key = "unclassified"
                res["flags"].append(common.flag("unmapped_fund_sector_key", "low", f"{pt}: Yahoo key {k!r} → unclassified"))
            raw[key] = raw.get(key, 0.0) + f * 100.0
        split, fl = normalize_split(raw, tolerance_pp=tol, label=pt)
        res["flags"].extend(fl)
        if split:
            res.update(split=split, composition_source="yfinance", source_id=sid)
        if data.get("top_holdings"):
            top_raw, top_sid, top_as_of = list(data["top_holdings"]), sid, fact_as_of
        if common.safe_float(data.get("expense_ratio_pct")) is not None:
            exp_val, exp_sid = common.safe_float(data.get("expense_ratio_pct")), sid
            exp_field, exp_as_of = data.get("expense_ratio_field"), fact_as_of

    if res["split"] is None or not top_raw or exp_val is None:
        for key in dict.fromkeys([t, pt]):
            entry = fund_over.get(key)
            if not entry:
                continue
            url, ov_as_of = entry.get("source_url"), common.date_str(entry.get("as_of")) if entry.get("as_of") else None
            if not url or not ov_as_of or not str(url).startswith(("http://", "https://")):
                res["flags"].append(common.flag("override_unsourced", "medium",
                                                f"etf_overrides.funds.{key} lacks source_url/as_of — ignored"))
                continue
            sid = f"D:CFG:etf_overrides.funds.{key}"
            src = common.dataset_source(sid, url=str(url), title=f"Fund factsheet (etf_overrides.funds.{key})",
                                        accessed=as_of)
            used = False
            if res["split"] is None and entry.get("sector_weights"):
                split, fl = normalize_split(entry["sector_weights"], tolerance_pp=tol, label=f"overrides {key}")
                res["flags"].extend(fl)
                if split:
                    res.update(split=split, composition_source="etf_overrides", source_id=sid)
                    used = True
            if not top_raw and entry.get("top_holdings"):
                top_raw, top_sid, top_as_of, used = list(entry["top_holdings"]), sid, ov_as_of, True
            if exp_val is None and common.safe_float(entry.get("expense_ratio_pct")) is not None:
                exp_val, exp_sid, exp_as_of = common.safe_float(entry["expense_ratio_pct"]), sid, ov_as_of
                exp_field, used = f"etf_overrides.funds.{key}.expense_ratio_pct", True
            if used:
                res["sources"].append(src)
            break

    if res["split"] is None and sector_override in common.SECTOR_KEYS:
        res.update(split={sector_override: 100.0}, composition_source="sector_override")
        res["flags"].append(common.flag("fund_sector_override", "medium",
                                        f"{t}: no fund composition data; 100% {sector_override} per portfolio sector_override"))
        res["missing_data"].append(f"{t}: fund sector composition unavailable — used sector_override "
                                   f"{sector_override} (100%)")
    if res["split"] is None:
        res.update(split={"unclassified": 100.0}, composition_source="none")
        why = f" ({got.error})" if got.error else ""
        res["flags"].append(common.flag("fund_composition_missing", "medium", f"{t}: counted 100% unclassified"))
        res["missing_data"].append(f"{t}: fund sector composition unavailable from Yahoo{why} and "
                                   "etf_overrides.yaml — counted 100% unclassified")

    res["top10"] = top_holdings(top_raw, top_n)
    weights = [h["weight_pct"] for h in res["top10"] if h["weight_pct"] is not None]
    if weights:
        res["top10_concentration_pct"] = common.fact(round(sum(weights), WEIGHT_DP), "pct", top_sid, top_as_of,
                                                     "other", derivation=f"sum of top {len(res['top10'])} holding weights")
    else:
        res["top10_concentration_pct"] = common.na_fact("pct", top_sid, top_as_of, "other")
        res["missing_data"].append(f"{t}: top holdings unavailable (concentration/overlap not computed)")
    if exp_val is not None:
        res["expense_ratio_pct"] = common.fact(exp_val, "pct", exp_sid, exp_as_of, "other", derivation=exp_field)
    else:
        res["expense_ratio_pct"] = common.na_fact("pct", None, as_of, "other")
        res["missing_data"].append(f"{t}: expense ratio unavailable")
    res["sources"] = common.dedupe_sources(res["sources"])
    return res


# =====================================================================================
# Overlap
# =====================================================================================


def pair_overlap(top_a: list[dict], top_b: list[dict]) -> tuple[float | None, list[str]]:
    """``Σ min(w_a, w_b)`` over symbols in both lists (None when either list is empty)."""
    wa = {h["symbol"]: h.get("weight_pct") for h in top_a or []}
    wb = {h["symbol"]: h.get("weight_pct") for h in top_b or []}
    if not wa or not wb:
        return None, []
    common_syms = [s for s in wa if s in wb]
    total = 0.0
    for s in common_syms:
        a, b = common.safe_float(wa[s]), common.safe_float(wb[s])
        if a is not None and b is not None:
            total += min(a, b)
    return round(total, WEIGHT_DP), common_syms


def compute_overlap(funds: Mapping[str, Mapping[str, Any]], held_stocks: Mapping[str, float | None], *,
                    basis: str = DEFAULT_OVERLAP_BASIS, fund_weights: Mapping[str, float | None] | None = None) -> dict:
    """Pairwise fund overlap + direct overlap (a fund's top10 containing a directly held stock)."""
    names = list(funds)
    pairs: list[dict] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pct, syms = pair_overlap(funds[a].get("top10") or [], funds[b].get("top10") or [])
            pair = {"a": a, "b": b, "overlap_pct": pct, "basis": basis, "common": syms}
            if pct is None:
                pair["note"] = "top holdings unavailable for at least one fund"
            pairs.append(pair)
    direct: list[dict] = []
    for f in names:
        fw = common.safe_float((fund_weights or {}).get(f))
        for h in funds[f].get("top10") or []:
            if h["symbol"] in held_stocks:
                w = common.safe_float(h.get("weight_pct"))
                implied = None if (w is None or fw is None) else round(fw * w / 100.0, WEIGHT_DP)
                direct.append({"fund": f, "holding": h["symbol"], "fund_weight_pct": w,
                               "implied_portfolio_pct": implied, "basis": basis})
    return {"pairs": pairs, "direct_overlap": direct}


# =====================================================================================
# Sector weights rounding
# =====================================================================================


def round_buckets(buckets: Mapping[str, float], *, dp: int = SECTOR_DP,
                  tolerance: float = DEFAULT_SUM_TOLERANCE_PCT) -> dict:
    """Scale to 100, round to ``dp``, put the rounding residual into the largest bucket (first
    in ``BUCKETS`` order on ties) and assert the sum is 100 ± ``tolerance``."""
    total = sum(max(0.0, float(v or 0.0)) for v in buckets.values())
    if total <= 0:
        out = {b: 0.0 for b in BUCKETS}
        out["unclassified"] = 100.0
        return out
    order = [b for b in BUCKETS if b in buckets] + [b for b in buckets if b not in BUCKETS]
    rounded = {b: round(max(0.0, float(buckets[b] or 0.0)) * 100.0 / total, dp) for b in order}
    residual = round(100.0 - sum(rounded.values()), dp)
    if residual:
        largest = max(order, key=lambda b: (rounded[b], -order.index(b)))
        rounded[largest] = round(rounded[largest] + residual, dp)
    s = sum(rounded.values())
    assert abs(s - 100.0) <= tolerance + 1e-9, f"sector weights sum to {s}, not 100 ± {tolerance}"
    return {b: (0.0 if rounded[b] == 0 else rounded[b]) for b in order}


# =====================================================================================
# Prices
# =====================================================================================


def _load_series(ticker: str) -> Any:
    """Cached OHLCV via ``fetch_prices.load_series`` (never network); None when unavailable."""
    try:
        mod = importlib.import_module("scripts.fetch_prices")
    except ImportError:
        return None
    fn = getattr(mod, "load_series", None)
    if not callable(fn):
        return None
    try:
        return fn(ticker)
    except Exception:  # noqa: BLE001 - best-effort read of a cached series
        return None


def series_as_of(df: Any, as_of: Any) -> Any:
    """``df`` restricted to rows dated on/before ``as_of`` (point-in-time; a tz-aware index is
    compared on its calendar date). ``df`` is returned unchanged when it or ``as_of`` is None or the
    index cannot be read as dates."""
    day = common.date_str(as_of) if as_of is not None else None
    if df is None or not day:
        return df
    try:
        import pandas as pd  # noqa: PLC0415 - only needed when a cached series is used
        idx = pd.DatetimeIndex(pd.to_datetime(df.index))
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        return df.loc[idx.normalize() <= pd.Timestamp(day)]
    except (TypeError, ValueError, AttributeError, KeyError):
        return df


def _prices_doc(prices: Any) -> dict:
    if prices is None:
        return {}
    if isinstance(prices, (str, Path)):
        try:
            prices = common.read_json(prices)
        except (OSError, ValueError):
            return {}
    return dict(prices) if isinstance(prices, Mapping) else {}


def lookup_price(ticker: str, prices: Mapping[str, Any], *, as_of: str) -> tuple[dict | None, dict | None, str | None]:
    """Last close of ``ticker`` as ``(Fact, Source, origin)``: the prices.json payload first
    (``tickers.<T>.last_close``; a bare ``{T: Fact|number}`` map is accepted too), then the
    cached series cut at ``as_of`` (point-in-time). ``(None, None, None)`` when neither has a
    usable price."""
    t = common.norm_ticker(ticker)
    sid = f"D:PX:{t}"
    tickers = prices.get("tickers") if isinstance(prices.get("tickers"), Mapping) else prices
    entry = tickers.get(t) if isinstance(tickers, Mapping) else None
    f: dict | None = None
    if isinstance(entry, Mapping) and isinstance(entry.get("last_close"), Mapping):
        f = dict(entry["last_close"])
    elif common.is_fact(entry):
        f = dict(entry)
    elif common.safe_float(entry) is not None:
        f = common.fact(common.safe_float(entry), "USD/share", sid, as_of, "price")
    if f is not None and common.safe_float(f.get("value")) is not None and f["value"] > 0:
        src = next((dict(s) for s in prices.get("sources") or [] if isinstance(s, Mapping) and s.get("id") == f.get("source_id")),
                   None)
        if src is None and str(f.get("source_id") or "").startswith("D:PX:"):
            src = common.dataset_source(str(f["source_id"]), accessed=as_of)
        return f, src, "prices"
    df = series_as_of(_load_series(t), as_of)
    try:
        close = df["Close"].dropna() if df is not None else None
    except (KeyError, TypeError, AttributeError):
        close = None
    if close is not None and len(close):
        value = common.safe_float(close.iloc[-1])
        if value is not None and value > 0:
            day = common.date_str(close.index[-1])
            return (common.fact(value, "USD/share", sid, day, "price", derivation="cached series (fetch_prices.load_series)"),
                    common.dataset_source(sid, accessed=as_of), "cached_series")
    return None, None, None


# =====================================================================================
# run
# =====================================================================================


def _flag_codes(flags: list[dict]) -> set[str]:
    return {f.get("code") for f in flags}


def run(out: Path | None = None, *, portfolio: Mapping[str, Any] | None = None, prices: Any = None,
        as_of: str | None = None, overrides: Mapping[str, Any] | None = None,
        cfg: Mapping[str, Any] | None = None, cache: common.DiskCache | None = None) -> dict:
    """Build the ``lookthrough/v1`` payload (and write it to ``out``).

    ``portfolio`` = portfolio.yaml dict (default: loaded from the config dir); ``prices`` = the
    prices.json payload (dict or path; may be None → cached series only); ``overrides`` =
    etf_overrides.yaml dict (default: loaded)."""
    as_of_s = common.date_str(as_of) or common.today().isoformat()
    cfg = _cfg(cfg)
    lt = _lt_cfg(cfg)
    tol = common.safe_float(lt.get("sum_tolerance_pct"))
    tol = DEFAULT_SUM_TOLERANCE_PCT if tol is None else tol
    basis = str(lt.get("overlap_basis") or DEFAULT_OVERLAP_BASIS)
    if portfolio is None:
        portfolio = common.load_config("portfolio")
    if overrides is None:
        overrides = load_overrides()
    prices_doc = _prices_doc(prices)
    cache = cache or common.DiskCache()
    port_as_of = common.date_str(portfolio.get("as_of")) if portfolio.get("as_of") else None

    sources: list[dict] = [common.source(PORTFOLIO_SOURCE_ID, PORTFOLIO_SOURCE_URL, PORTFOLIO_SOURCE_TITLE,
                                         as_of_s)]
    missing: list[str] = []
    flags: list[dict] = []
    rows: list[dict] = []

    # ---- per holding: classification, price, value / weight input --------------------
    for h in portfolio.get("holdings") or []:
        t = common.norm_ticker(h["ticker"])
        proxy = common.norm_ticker(h["proxy_ticker"]) if h.get("proxy_ticker") else None
        pt = proxy or t
        shares = common.safe_float(h.get("shares"))
        wpct = common.safe_float(h.get("weight_pct"))
        mode = "shares" if (shares is not None and shares > 0) else ("weight_pct" if wpct is not None else "none")
        sec_res: dict[str, Any] = {}

        def sic_fn(tk: str, _res: dict = sec_res) -> int | None:
            _res.update(sec_sic(tk, cfg=cfg, cache=cache))
            return _res.get("sic")

        cls = classify_security(t, overrides=overrides, asset_type=h.get("asset_type"),
                                sector_override=h.get("sector_override"), info_ticker=pt, cache=cache,
                                sic_fn=sic_fn)
        if cls["classification_source"] == "yfinance_info":
            sources.append(common.dataset_source(f"D:YF:{pt}:INFO", accessed=as_of_s))
        elif cls["classification_source"] == "sic":
            sources.append(common.dataset_source("D:SEC:MAP", accessed=as_of_s))
            sources.append(common.dataset_source(f"D:SEC:{t}:SUB", cik=sec_res.get("cik"), accessed=as_of_s))
            flags.append(common.flag("classified_by_sic", "low",
                                     f"{t}: no Yahoo sector; classified {cls['sector']} from SEC SIC {sec_res.get('sic')}"))
            if sec_res.get("stale"):
                flags.append(common.flag("stale_cache", "medium", f"{t}: SEC SIC lookup served from stale cache"))
        hflags = list(cls["flags"])
        if proxy:
            hflags.append("proxy_priced")
        price_f, price_src, origin = lookup_price(pt, prices_doc, as_of=as_of_s)
        row: dict[str, Any] = {"ticker": t, "proxy": proxy, "pt": pt, "shares": shares, "wpct": wpct, "mode": mode,
                               "cls": cls, "flags": hflags, "price": price_f, "price_src": price_src,
                               "sector_override": h.get("sector_override"), "sic_error": sec_res.get("error"),
                               "value": None, "weight": None, "weight_kind": None}
        if price_f is not None and price_src:
            sources.append(price_src)           # the price Fact is emitted for every mode: define its source
        if mode == "shares":
            if price_f is None:
                missing.append(f"{t}: no price for {pt} (prices.json / cached series) — its value is unknown")
                flags.append(common.flag("holding_unpriced", "high", f"{t}: priced via {pt}, no price available"))
            else:
                row["value"] = shares * float(price_f["value"])
                if origin == "cached_series":
                    flags.append(common.flag("price_from_cached_series", "low",
                                             f"{pt} not in prices.json; last cached close used"))
        elif mode == "none":
            missing.append(f"{t}: neither shares nor weight_pct in portfolio.yaml (run /committee-setup) — "
                           "its value is unknown")
            flags.append(common.flag("holding_unweighted", "high", f"{t}: no shares or weight_pct"))
        if proxy:
            flags.append(common.flag("proxy_ticker", "low",
                                     f"{t}: prices and fund data from proxy {proxy}"
                                     + (" (shares are valued at the proxy's price)" if mode == "shares" else "")))
        rows.append(row)

    # ---- totals and weights --------------------------------------------------------------
    declared = common.safe_float(portfolio.get("total_value_usd"))
    cash_usd = common.safe_float(portfolio.get("cash_usd"))
    cash_pct = common.safe_float(portfolio.get("cash_pct"))
    cash_input = "cash_usd" if cash_usd is not None else ("cash_pct" if cash_pct is not None else "none")
    weight_rows = [r for r in rows if r["mode"] == "weight_pct"]
    share_rows = [r for r in rows if r["mode"] == "shares" and r["value"] is not None]
    share_sum = sum(r["value"] for r in share_rows)
    total: float | None = None
    cash_val: float | None = None
    cash_w: float = 0.0
    residual_w: float = 0.0
    derivation = ""
    weights_unknown = False
    # holdings whose value is unknown: shares without a price, or neither shares nor weight_pct
    unvalued = [r["ticker"] for r in rows if r["mode"] == "none" or (r["mode"] == "shares" and r["value"] is None)]
    unvalued_s = ", ".join(unvalued)

    if not weight_rows and unvalued:
        decl_cash = (cash_usd if cash_usd is not None
                     else (cash_pct / 100.0 * declared if (cash_pct is not None and declared) else 0.0))
        decl_gap_pct = ((declared - share_sum - decl_cash) / declared * 100.0) if declared and declared > 0 else None
        if decl_gap_pct is not None and decl_gap_pct >= -tol:
            # the declared total anchors the weights; the unvalued part is counted as unclassified
            total, cash_val = declared, decl_cash
            derivation = "portfolio.yaml total_value_usd (holdings without a value counted as unclassified)"
            if decl_gap_pct > tol:
                residual_w = decl_gap_pct
                flags.append(common.flag("unvalued_holdings_unclassified", "medium",
                                         f"{decl_gap_pct:.4f}% of total_value_usd not covered by priced holdings/cash "
                                         f"(no value for: {unvalued_s}) → unclassified"))
                missing.append(f"{decl_gap_pct:.2f}% of the declared portfolio value is not attributed to a priced "
                               f"holding or cash (no value for: {unvalued_s}) — counted as unclassified")
        else:
            # no usable total: renormalizing over the priced subset would overstate every weight
            weights_unknown = True
            cash_val = cash_usd if cash_usd is not None else None
            cash_w = cash_pct if cash_pct is not None else 0.0
            residual_w = max(0.0, 100.0 - cash_w)
            derivation = "unknown: holdings without a value and no usable total_value_usd"
            why = ("total_value_usd is not set" if not declared else
                   f"total_value_usd {declared:,.2f} is below the priced holdings + cash")
            flags.append(common.flag("weights_unreliable", "high",
                                     f"no value for {unvalued_s} and {why}: portfolio total unknown — holding weights "
                                     "left null and the sector look-through counted as unclassified"))
            missing.append(f"portfolio total unknown (no value for {unvalued_s}; {why}) — holding weights are null and "
                           "sector weights are unclassified; set total_value_usd, a price/proxy_ticker or weight_pct")
    elif not weight_rows:
        if cash_usd is not None:
            cash_val = cash_usd
            total = share_sum + cash_usd
        elif cash_pct is not None and cash_pct < 100:
            total = share_sum / (1.0 - cash_pct / 100.0)
            cash_val = total - share_sum
        else:
            cash_val = 0.0
            total = share_sum
        derivation = "sum(shares x last_close) + cash"
        if total is not None and total > 0 and declared:
            diff_pct = abs(declared - total) / total * 100.0
            if diff_pct > DECLARED_TOTAL_NOTE_PCT:
                flags.append(common.flag("declared_total_ignored", "low",
                                         f"total_value_usd {declared:,.2f} differs {diff_pct:.1f}% from priced total "
                                         f"{total:,.2f}; priced total used"))
        if total is None or total <= 0:
            total = None
    elif declared:
        total = declared
        for r in weight_rows:
            r["value"] = r["wpct"] / 100.0 * declared
        cash_val = cash_usd if cash_usd is not None else (cash_pct / 100.0 * declared if cash_pct is not None else 0.0)
        known = share_sum + sum(r["value"] for r in weight_rows) + cash_val
        gap_pct = (declared - known) / declared * 100.0
        derivation = "portfolio.yaml total_value_usd"
        if gap_pct > tol:
            residual_w = gap_pct
            flags.append(common.flag("unallocated_residual", "low",
                                     f"{gap_pct:.4f}% of total_value_usd not covered by holdings/cash → unclassified"))
            missing.append(f"{gap_pct:.2f}% of the declared portfolio value is not attributed to any holding or cash "
                           "(counted as unclassified)")
        elif gap_pct < -tol:
            total = known
            derivation = "sum of holdings + cash (exceeds declared total_value_usd)"
            flags.append(common.flag("total_value_mismatch", "medium",
                                     f"holdings + cash ({known:,.2f}) exceed total_value_usd ({declared:,.2f}); "
                                     "their sum is used as the total"))
        else:
            total = known
    else:
        derivation = "weight_pct as given (no total_value_usd)"
        for r in weight_rows:
            r["weight"] = r["wpct"]
            r["weight_kind"] = "given"
        if share_rows:
            missing.append("total_value_usd is required to combine shares- and weight-based holdings — "
                           "shares-based holdings excluded from look-through weights")
            flags.append(common.flag("total_value_missing", "high",
                                     "weight_pct holdings without total_value_usd; shares-based holdings unweighted"))
        cash_w = cash_pct if cash_pct is not None else 0.0
        if cash_usd is not None and cash_pct is None:
            missing.append("cash_usd cannot be weighted without total_value_usd (cash counted as 0%)")
        given_sum = sum(r["weight"] for r in weight_rows) + cash_w
        if given_sum > 100.0 + tol:
            # rescale every holding (and cash) so holding weights and sector buckets agree
            scale = 100.0 / given_sum
            for r in weight_rows:
                r["weight"] = r["wpct"] * scale
            cash_w *= scale
            derivation = "weight_pct rescaled so holdings + cash sum to 100 (no total_value_usd)"
            flags.append(common.flag("weights_over_100", "medium",
                                     f"weight_pct + cash sum to {given_sum:.4f}%; each weight scaled by "
                                     f"100/{given_sum:.4f}"))
        gap = 100.0 - sum(r["weight"] for r in weight_rows) - cash_w
        if gap > tol:
            residual_w = gap
            flags.append(common.flag("unallocated_residual", "low", f"{gap:.4f}% not covered by holdings/cash → unclassified"))

    if total is not None:
        for r in rows:
            if r["value"] is not None:
                r["weight"] = r["value"] / total * 100.0
                r["weight_kind"] = "priced" if r["mode"] == "shares" else "given"
        cash_w = (cash_val or 0.0) / total * 100.0
    elif not weight_rows and rows and not weights_unknown:
        missing.append("portfolio value could not be determined (no priced holdings and no cash)")

    # ---- per-holding output, fund decomposition, sector buckets -----------------------
    buckets: dict[str, float] = {b: 0.0 for b in BUCKETS}
    holdings_out: list[dict] = []
    funds_out: dict[str, dict] = {}
    fund_weights: dict[str, float | None] = {}
    price_days = [r["price"]["as_of"] for r in rows if r["price"] and r["value"] is not None and r["mode"] == "shares"
                  and r["price"].get("as_of")]
    for r in rows:
        t, cls = r["ticker"], r["cls"]
        if cls["asset_type"] == "fund":
            comp = fund_composition(t, price_ticker=r["pt"], overrides=overrides, as_of=as_of_s, cfg=cfg,
                                    sector_override=r["sector_override"], cache=cache)
            split = comp["split"]
            sources.extend(comp["sources"])
            flags.extend(comp["flags"])
            missing.extend(comp["missing_data"])
            funds_out[t] = {"expense_ratio_pct": comp["expense_ratio_pct"], "top10": comp["top10"],
                            "top10_concentration_pct": comp["top10_concentration_pct"],
                            "source_id": comp["source_id"], "composition_source": comp["composition_source"],
                            "price_ticker": r["pt"], "sector_split": split}
            fund_weights[t] = None if r["weight"] is None else round(r["weight"], WEIGHT_DP)
        else:
            split = {cls["sector"] if cls["sector"] in BUCKETS else "unclassified": 100.0}
            if cls["sector"] == "unclassified":
                sic_why = f"SEC SIC: {r['sic_error']}" if r.get("sic_error") else "SEC SIC unmapped"
                missing.append(f"{t}: sector could not be classified (no sector_override, etf_overrides entry or "
                               f"Yahoo sector; {sic_why}) — counted unclassified; set sector_override")
        w = r["weight"]
        contribution: dict[str, float] = {}
        if w is not None:
            for b, pct in split.items():
                c = w * pct / 100.0
                buckets[b] = buckets.get(b, 0.0) + c
                contribution[b] = round(c, WEIGHT_DP)
        if r["mode"] == "shares" and r["price"]:
            w_fact = common.fact(None if w is None else round(w, WEIGHT_DP), "pct", r["price"].get("source_id"),
                                 r["price"].get("as_of"), "price",
                                 derivation=("shares x last_close / total_value_usd" if w is not None else
                                             "not computed: portfolio total unknown (see weights_unreliable)"))
            v_fact = common.fact(r["value"], "USD", r["price"].get("source_id"), r["price"].get("as_of"), "price",
                                 derivation=f"{r['shares']:g} shares x {r['price']['value']} ({r['pt']} last close)")
        elif r["mode"] == "weight_pct":
            w_fact = common.fact(None if w is None else round(w, WEIGHT_DP), "pct", PORTFOLIO_SOURCE_ID,
                                 port_as_of or as_of_s, "other",
                                 derivation="portfolio.yaml weight_pct" + ("" if w is None or abs(w - r["wpct"]) < 1e-9
                                                                           else " (rescaled to the portfolio total)"))
            v_fact = common.fact(r["value"], "USD", PORTFOLIO_SOURCE_ID, port_as_of or as_of_s, "other",
                                 derivation="weight_pct x total_value_usd")
        else:
            w_fact = common.na_fact("pct", None, as_of_s, "price")
            v_fact = common.na_fact("USD", None, as_of_s, "price")
        entry: dict[str, Any] = {
            "ticker": t, "asset_type": cls["asset_type"], "weight_pct": w_fact,
            "sector": cls["sector"], "sector_split": split,
            "classification_source": cls["classification_source"], "flags": r["flags"],
            "sector_class": cls["sector_class"], "input": r["mode"], "shares": r["shares"],
            "price_ticker": r["pt"], "proxy_ticker": r["proxy"], "price": r["price"], "value_usd": v_fact,
            "contribution_pct": contribution,
        }
        if cls["asset_type"] == "fund":
            entry["composition_source"] = funds_out[t]["composition_source"]
        holdings_out.append(entry)

    buckets["cash"] += cash_w
    buckets["unclassified"] += residual_w
    raw_sum = sum(buckets.values())
    if raw_sum > 0 and abs(raw_sum - 100.0) > tol:
        flags.append(common.flag("weights_rescaled", "medium",
                                 f"look-through buckets summed to {raw_sum:.4f}% before normalization to 100"))
    if raw_sum <= 0:
        missing.append("no holding or cash could be weighted — sector weights are 100% unclassified")
    sector_weights = round_buckets(buckets, dp=SECTOR_DP, tolerance=tol)

    held_stocks = {h["ticker"]: (h["weight_pct"] or {}).get("value") for h in holdings_out if h["asset_type"] == "stock"}
    overlap = compute_overlap(funds_out, held_stocks, basis=basis, fund_weights=fund_weights)

    total_as_of = max(price_days) if price_days else (port_as_of or as_of_s)
    total_fact = common.fact(None if total is None else round(total, 2), "USD", PORTFOLIO_SOURCE_ID, total_as_of,
                             "price", derivation=derivation or None)
    cash_fact_w = common.fact(round(cash_w, WEIGHT_DP) if (total is not None or cash_pct is not None) else None,
                              "pct", PORTFOLIO_SOURCE_ID, total_as_of, "other",
                              derivation=f"portfolio.yaml {cash_input}")
    cash_fact_v = common.fact(None if cash_val is None else round(cash_val, 2), "USD", PORTFOLIO_SOURCE_ID,
                              port_as_of or as_of_s, "other", derivation=f"portfolio.yaml {cash_input}")

    payload = common.envelope(SCHEMA, as_of_s, sources=sources, missing_data=missing,
                              flags=_dedupe_flags(flags),
                              total_value_usd=total_fact, holdings=holdings_out,
                              cash={"value_usd": cash_fact_v, "weight_pct": cash_fact_w, "input": cash_input},
                              sector_weights_pct=sector_weights, funds=funds_out, overlap=overlap)
    if out is not None:
        common.write_json(Path(out), payload)
    return payload


def _dedupe_flags(flags: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for f in flags:
        key = (f.get("code"), f.get("severity"), f.get("detail"))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Sector look-through of the portfolio (funds decomposed).")
    parser.add_argument("--prices", default=None, help="prices.json from fetch_prices.py (optional)")
    parser.add_argument("--portfolio", default=None, help="portfolio YAML (default: <config>/portfolio.yaml)")
    parser.add_argument("--out", default=None, help="output path (e.g. runs/<id>/phase1/lookthrough.json)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    common.apply_common_args(args)
    portfolio = common.load_yaml(args.portfolio) if args.portfolio else None
    payload = run(Path(args.out) if args.out else None, portfolio=portfolio, prices=args.prices, as_of=args.as_of)
    print(json.dumps({"out": args.out, "total_value_usd": payload["total_value_usd"]["value"],
                      "sector_weights_pct": payload["sector_weights_pct"],
                      "missing_data": len(payload["missing_data"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
