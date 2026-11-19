"""Phase 3 candidate screen for one sector → ``phase3/screen_<sector>.json`` (``screen/v1``, DESIGN §4.7).

Universe = the top ``screen.etf_top_n`` (10 — all Yahoo exposes) holdings of the sector's benchmark
SPDR (``common.yf_funds(<sector ETF>).top_holdings``) + ``universe.yaml additions.<sector>``.
When Yahoo yields no holdings for the ETF, the fallback is ``etf_overrides.yaml: funds.<ETF>.top_holdings``
(``[{symbol, name?, weight_pct}]``; the entry must carry ``source_url`` + ``as_of`` like the
``sector_weights`` fallback, otherwise it is ignored with flag ``override_unsourced``); it is cited as
source ``D:CFG:etf_overrides.funds.<ETF>`` and flagged ``universe_from_override``.

Filters (first failing reason is recorded in ``excluded[]``):
``exclusion`` (universe.yaml exclusions) → ``held`` (already in the portfolio) → ``recent`` (rotation:
shortlisted for this sector in one of the last ``phase3.rotation.lookback_runs`` runs, see
``recent_shortlisted``; the run step passes them in so every run proposes new names) → gathered data:
``no_data`` (gatherer failed / market cap or ADV unknown) → ``mcap`` (< ``min_market_cap_usd``) →
``adv`` (63-day average dollar volume < ``min_avg_dollar_volume_usd``) → ``non_sec`` (no SEC filings).

Imputation guard: a candidate with more than ``screen.max_imputed_fraction`` (default 0.5) of its
quality + valuation inputs missing is not ranked at all (excluded ``no_data``: with most inputs imputed
to a neutral 0.5 it would rank by momentum alone); above ``screen.warn_imputed_fraction`` (default 0.25)
it stays but the document carries flag ``screen_imputation_heavy`` (medium). A ``common.ConfigError``
(e.g. a placeholder ``sec_contact_email``) is never swallowed into a degraded shortlist: it propagates.

Ranking (deterministic): every input is turned into a **percentile rank within the eligible set**
``p = (average rank − 1) / (n − 1)`` (worst 0, best 1, ties share their average rank; a single
valid value → 0.5). A missing input is imputed ``screen.impute_percentile`` (0.5) and listed in
``imputed``. Component score = mean of its input percentiles; composite = Σ ``screen.weights`` ×
component score (rounded to 4 dp). Sorted by composite desc, ties broken by ticker.

| component | default sectors | financials |
|---|---|---|
| quality   | operating margin, FCF margin, ROIC | operating margin, FCF margin, ROE |
| valuation | FCF yield, 1/(EV/EBIT)             | FCF yield, 1/(P/E), 1/(P/B)       |
| momentum  | RS vs SPY over ``screen.momentum_windows`` (6m, 12m; excess return in pp) | same |

"Lower is better" multiples are ranked through their reciprocal (earnings/book yield), so a negative
multiple ranks below every positive one; a zero/unknown multiple is missing (imputed).

Shortlist = the top ``min(phase3.shortlist_size, universe.max_candidates_per_sector)`` — never padded
when fewer tickers survive the filters (zero is a valid outcome).

Data gathering is pluggable: ``gather(ticker) -> dict`` returning raw metrics (numbers or Facts)::

    {"mcap", "adv_usd", "coverage": "sec|partial|non_sec"|None, "operating_margin_pct",
     "fcf_margin_pct", "roic_pct", "roe_pct", "fcf_yield_pct", "ev_ebit", "pe", "p_b",
     "rs_6m", "rs_12m", ..., "sources": [...], "missing_data": [...], "flags": [...]}

The default gatherer (``make_default_gatherer``) uses the cached ``yf_info`` (market cap), cached
price series via ``fetch_prices`` cut at ``as_of`` (ADV, last close + date-aligned RS vs the
benchmark use no bar after the run date) and
``fetch_fundamentals.run(ticker, out=None)`` (skipped when the size/liquidity filters already fail;
failures → imputation + ``missing_data``). A gatherer may expose ``gather.prefetch(tickers)``
which ``run`` calls once with every candidate before gathering.

CLI::

    python scripts/screen.py --sector health_care --out runs/<id>/phase3/screen_health_care.json [--held KO,PG]
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts import etf_lookthrough  # noqa: E402

import argparse  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
from typing import Any, Callable, Iterable, Mapping  # noqa: E402

import pandas as pd  # noqa: E402

SCHEMA = "screen"

#: Fallbacks used only when committee.yaml / universe.yaml lack the key.
DEFAULT_WEIGHTS = {"quality": 0.4, "valuation": 0.3, "momentum": 0.3}
DEFAULT_ETF_TOP_N = 10
DEFAULT_IMPUTE = 0.5
#: Share of a candidate's quality+valuation inputs that may be imputed before it is excluded / flagged
#: (``screen.max_imputed_fraction`` / ``screen.warn_imputed_fraction``).
DEFAULT_MAX_IMPUTED_FRACTION = 0.5
DEFAULT_WARN_IMPUTED_FRACTION = 0.25
DEFAULT_MOMENTUM_WINDOWS = ("6m", "12m")
DEFAULT_RS_WINDOWS = {"3m": 63, "6m": 126, "12m": 252}
DEFAULT_SHORTLIST = 6
DEFAULT_ADV_WINDOW = 63
DEFAULT_BENCHMARK = "SPY"
COMPOSITE_DP = 4

#: Component inputs per sector class: (raw key, +1 higher is better | −1 lower is better → reciprocal).
QUALITY_INPUTS = {
    "default": (("operating_margin_pct", 1), ("fcf_margin_pct", 1), ("roic_pct", 1)),
    "financials": (("operating_margin_pct", 1), ("fcf_margin_pct", 1), ("roe_pct", 1)),
}
VALUATION_INPUTS = {
    "default": (("fcf_yield_pct", 1), ("ev_ebit", -1)),
    "financials": (("fcf_yield_pct", 1), ("pe", -1), ("p_b", -1)),
}
#: Units / Fact kinds of the raw metrics (used when a gatherer returns plain numbers).
METRIC_UNITS = {
    "mcap": ("USD", "price"), "adv_usd": ("USD", "price"),
    "operating_margin_pct": ("pct", "fundamental"), "fcf_margin_pct": ("pct", "fundamental"),
    "roic_pct": ("pct", "fundamental"), "roe_pct": ("pct", "fundamental"),
    "fcf_yield_pct": ("pct", "fundamental"), "ev_ebit": ("x", "fundamental"), "pe": ("x", "fundamental"),
    "p_b": ("x", "fundamental"),
}
#: fundamentals/v1 JSON paths of the screen metrics.
FUNDAMENTAL_PATHS = {
    "operating_margin_pct": "margins_pct.operating", "fcf_margin_pct": "margins_pct.fcf",
    "roic_pct": "valuation.roic_pct", "roe_pct": "valuation.roe_pct",
    "fcf_yield_pct": "valuation.fcf_yield_pct", "ev_ebit": "valuation.ev_ebit",
    "pe": "valuation.pe", "p_b": "valuation.p_b",
}
META_KEYS = ("sources", "missing_data", "flags")


# =====================================================================================
# Config
# =====================================================================================


def _cfg(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if cfg is not None:
        return cfg
    try:
        return common.committee_cfg()
    except common.ConfigError:
        return {}


def _universe_cfg(ucfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if ucfg is not None:
        return ucfg
    try:
        return common.load_config("universe")
    except common.ConfigError:
        return {}


def screen_weights(cfg: Mapping[str, Any] | None = None) -> dict:
    """``screen.weights`` normalized to sum to 1 (only quality/valuation/momentum)."""
    raw = (_cfg(cfg).get("screen") or {}).get("weights") or DEFAULT_WEIGHTS
    w = {k: float(common.safe_float(raw.get(k)) or 0.0) for k in DEFAULT_WEIGHTS}
    s = sum(w.values())
    return {k: v / s for k, v in w.items()} if s > 0 else dict(DEFAULT_WEIGHTS)


def momentum_keys(cfg: Mapping[str, Any] | None = None) -> list[str]:
    windows = (_cfg(cfg).get("screen") or {}).get("momentum_windows") or list(DEFAULT_MOMENTUM_WINDOWS)
    return [f"rs_{w}" for w in windows]


def component_spec(sector_class: str, cfg: Mapping[str, Any] | None = None) -> dict:
    """``{"quality": [(key, dir)], "valuation": [...], "momentum": [...]}`` for a sector class."""
    cls = "financials" if sector_class == "financials" else "default"
    return {"quality": list(QUALITY_INPUTS[cls]), "valuation": list(VALUATION_INPUTS[cls]),
            "momentum": [(k, 1) for k in momentum_keys(cfg)]}


# =====================================================================================
# Pure math
# =====================================================================================


def percentile_ranks(values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Percentile rank within the non-null values: ``(average rank − 1)/(n − 1)``; n == 1 → 0.5;
    null stays None."""
    valid = {t: float(v) for t, v in values.items() if common.safe_float(v) is not None}
    out: dict[str, float | None] = {t: None for t in values}
    n = len(valid)
    if n == 0:
        return out
    if n == 1:
        out.update({t: 0.5 for t in valid})
        return out
    ordered = sorted(valid.items(), key=lambda kv: kv[1])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = (avg_rank - 1.0) / (n - 1.0)
        i = j + 1
    return out


def _directional(value: float | None, direction: int) -> float | None:
    if value is None:
        return None
    if direction < 0:
        return None if value == 0 else 1.0 / value
    return value


def rank_candidates(raws: Mapping[str, Mapping[str, Any]], *, sector_class: str = "default",
                    weights: Mapping[str, float] | None = None, impute: float = DEFAULT_IMPUTE,
                    cfg: Mapping[str, Any] | None = None) -> list[dict]:
    """Composite ranking of the eligible tickers' raw metrics (see module doc)."""
    weights = dict(weights) if weights else screen_weights(cfg)
    spec = component_spec(sector_class, cfg)
    tickers = sorted(raws)
    pct: dict[tuple[str, str], dict[str, float | None]] = {}
    for comp, inputs in spec.items():
        for key, direction in inputs:
            vals = {t: _directional(common.fact_value(raws[t].get(key)), direction) for t in tickers}
            pct[(comp, key)] = percentile_ranks(vals)
    rows: list[dict] = []
    for t in tickers:
        components: dict[str, dict] = {}
        imputed: list[str] = []
        composite = 0.0
        for comp, inputs in spec.items():
            ins: dict[str, dict] = {}
            used: list[float] = []
            for key, direction in inputs:
                p = pct[(comp, key)][t]
                if p is None:
                    imputed.append(f"{comp}.{key}")
                ins[key] = {"value": common.fact_value(raws[t].get(key)),
                            "basis": "reciprocal" if direction < 0 else "direct",
                            "pctile": None if p is None else round(p, 4), "imputed": p is None}
                used.append(impute if p is None else p)
            score = sum(used) / len(used) if used else impute
            composite += float(weights.get(comp, 0.0)) * score
            components[comp] = {"score": round(score, 4), "weight": round(float(weights.get(comp, 0.0)), 6),
                                "inputs": ins}
        rows.append({"ticker": t, "composite": round(composite, COMPOSITE_DP), "components": components,
                     "imputed": imputed})
    rows.sort(key=lambda r: (-r["composite"], r["ticker"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def average_dollar_volume(df: pd.DataFrame | None, window: int = DEFAULT_ADV_WINDOW) -> float | None:
    """Mean of Close × Volume over the last ``window`` bars (None if fewer bars)."""
    if df is None or len(df) < int(window) or "Close" not in df or "Volume" not in df:
        return None
    tail = df.iloc[-int(window):]
    dv = (tail["Close"].astype(float) * tail["Volume"].astype(float)).dropna()
    if len(dv) < int(window):
        return None
    return common.safe_float(dv.mean())


def relative_strength(df: pd.DataFrame | None, bench: pd.DataFrame | None, n: int) -> tuple[float | None, str | None]:
    """Excess return in pp over ``n`` bars on date-aligned closes: ``((1+r_t)/(1+r_b) − 1)·100``.
    Returns ``(value, last_common_date)``; ``(None, None)`` with fewer than n+1 common bars."""
    if df is None or bench is None or "Close" not in df or "Close" not in bench:
        return None, None
    both = pd.concat([df["Close"].rename("t"), bench["Close"].rename("b")], axis=1, join="inner").dropna()
    if len(both) < int(n) + 1:
        return None, None
    last, first = both.iloc[-1], both.iloc[-1 - int(n)]
    if first["t"] <= 0 or first["b"] <= 0:
        return None, None
    rt = last["t"] / first["t"] - 1.0
    rb = last["b"] / first["b"] - 1.0
    return round(((1.0 + rt) / (1.0 + rb) - 1.0) * 100.0, 4), common.date_str(both.index[-1])


def etf_override_holdings(etf: str, overrides: Mapping[str, Any] | None, top_n: int, *, as_of: str
                          ) -> tuple[list[dict], dict | None, list[dict]]:
    """Fallback universe rows from ``etf_overrides.funds.<ETF>.top_holdings`` →
    ``(rows [{"symbol","name","weight_pct"}], Source | None, flags)``. An entry without a usable
    ``source_url`` (http/https) + ``as_of`` is ignored (flag ``override_unsourced``)."""
    _secs, funds = etf_lookthrough._override_maps(overrides)
    entry = funds.get(common.norm_ticker(etf))
    if not entry or not entry.get("top_holdings"):
        return [], None, []
    url = entry.get("source_url")
    try:
        ov_as_of = common.date_str(entry.get("as_of")) if entry.get("as_of") else None
    except ValueError:
        ov_as_of = None
    if not url or not ov_as_of or not str(url).startswith(("http://", "https://")):
        return [], None, [common.flag("override_unsourced", "medium",
                                      f"etf_overrides.funds.{etf} lacks source_url/as_of — top_holdings ignored")]
    rows = etf_lookthrough.top_holdings(entry["top_holdings"], top_n)
    if not rows:
        return [], None, []
    sid = f"D:CFG:etf_overrides.funds.{etf}"
    src = common.dataset_source(sid, url=str(url), title=f"Fund factsheet (etf_overrides.funds.{etf})", accessed=as_of)
    flag = common.flag("universe_from_override", "medium",
                       f"{etf}: Yahoo holdings unavailable; universe taken from etf_overrides.funds.{etf}.top_holdings "
                       f"(as_of {ov_as_of}, {len(rows)} rows)")
    return rows, src, [flag]


def metrics_from_fundamentals(fund: Mapping[str, Any]) -> dict:
    """Screen metrics (Facts) + coverage + market cap from a ``fundamentals/v1`` payload."""
    out: dict[str, Any] = {"coverage": fund.get("coverage")}
    for key, path in FUNDAMENTAL_PATHS.items():
        out[key] = common.get_dotted(fund, path)
    out["market_cap"] = fund.get("market_cap")
    return out


# =====================================================================================
# Lazy data helpers (monkeypatch points in tests)
# =====================================================================================


def _load_series(ticker: str) -> pd.DataFrame | None:
    """Cached OHLCV via ``fetch_prices.load_series`` (stale allowed, never network)."""
    try:
        mod = importlib.import_module("scripts.fetch_prices")
    except ImportError:
        return None
    fn = getattr(mod, "load_series", None)
    if not callable(fn):
        return None
    try:
        return fn(ticker)
    except Exception:  # noqa: BLE001 - best-effort cache read
        return None


def _fetch_prices(tickers: list[str], as_of: str) -> str | None:
    """Fetch/caches price series through ``fetch_prices.run`` (no file written). Returns an error
    string or None."""
    try:
        mod = importlib.import_module("scripts.fetch_prices")
    except ImportError as exc:
        return f"fetch_prices unavailable: {exc}"
    try:
        mod.run(list(tickers), None, as_of=as_of)
    except common.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider boundary; the screen degrades gracefully
        return f"{type(exc).__name__}: {exc}"
    return None


def _run_fundamentals(ticker: str, *, price: float | None, as_of: str, sector_class: str) -> dict:
    """``fetch_fundamentals.run(ticker, out=None, ...)`` (raises on failure)."""
    mod = importlib.import_module("scripts.fetch_fundamentals")
    return mod.run(ticker, None, price=price, as_of=as_of,
                   sector_class=sector_class if sector_class in ("default", "financials") else None)


# =====================================================================================
# Default gatherer
# =====================================================================================


def make_default_gatherer(sector: str, *, as_of: str, cfg: Mapping[str, Any] | None = None,
                          universe_cfg: Mapping[str, Any] | None = None,
                          cache: common.DiskCache | None = None) -> Callable[[str], dict]:
    """Gatherer backed by Yahoo info (cached), price series and SEC fundamentals."""
    cfg = _cfg(cfg)
    ucfg = _universe_cfg(universe_cfg)
    sector_class = common.sector_class(sector)
    benchmark = common.norm_ticker(cfg.get("benchmark") or DEFAULT_BENCHMARK)
    rs_windows = {**DEFAULT_RS_WINDOWS, **((cfg.get("technicals") or {}).get("rs_windows") or {})}
    windows = list((cfg.get("screen") or {}).get("momentum_windows") or DEFAULT_MOMENTUM_WINDOWS)
    adv_window = int(ucfg.get("adv_window_days") or (cfg.get("prices") or {}).get("adv_window_days")
                     or DEFAULT_ADV_WINDOW)
    min_mcap = common.safe_float(ucfg.get("min_market_cap_usd"))
    min_adv = common.safe_float(ucfg.get("min_avg_dollar_volume_usd"))
    spec = component_spec(sector_class, cfg)
    fundamental_keys = [k for comp in ("quality", "valuation") for k, _ in spec[comp]]
    state: dict[str, Any] = {"prefetched": set(), "bench": None, "bench_loaded": False}

    def prefetch(tickers: Iterable[str]) -> None:
        todo = [common.norm_ticker(t) for t in tickers]
        todo = list(dict.fromkeys(todo + [benchmark]))
        err = _fetch_prices(todo, as_of)
        state["prefetch_error"] = err
        state["prefetched"].update(todo)

    def series(t: str) -> pd.DataFrame | None:
        """Cached series cut at ``as_of`` (point-in-time: no bar after the run date is used)."""
        df = _load_series(t)
        if df is None and t not in state["prefetched"]:
            _fetch_prices([t], as_of)
            state["prefetched"].add(t)
            df = _load_series(t)
        return etf_lookthrough.series_as_of(df, as_of)

    def bench() -> pd.DataFrame | None:
        if not state["bench_loaded"]:
            state["bench"] = series(benchmark)
            state["bench_loaded"] = True
        return state["bench"]

    def gather(ticker: str) -> dict:
        t = common.norm_ticker(ticker)
        raw: dict[str, Any] = {"sources": [], "missing_data": [], "flags": []}
        # market cap (Yahoo info, cached)
        info = etf_lookthrough.cached_yf_info(t, cfg=cfg, cache=cache)
        mcap = common.safe_float((info.payload or {}).get("marketCap"))
        if info.payload:
            raw["sources"].append(common.dataset_source(f"D:YF:{t}:INFO", accessed=as_of))
        if info.provider == "cache_stale":
            raw["flags"].append(common.flag("stale_cache", "medium", f"{t}: Yahoo info from stale cache"))
        raw["mcap"] = common.fact(mcap, "USD", f"D:YF:{t}:INFO" if mcap is not None else None, as_of, "price",
                                  derivation="info.marketCap" if mcap is not None else None)
        # prices: ADV + RS vs benchmark
        df = series(t)
        b = bench()
        px_sid = f"D:PX:{t}"
        if df is not None and len(df):
            raw["sources"].append(common.dataset_source(px_sid, accessed=as_of))
            last_day = common.date_str(df.index[-1])
            raw["last_close"] = common.fact(common.safe_float(df["Close"].iloc[-1]), "USD/share", px_sid, last_day, "price")
            adv = average_dollar_volume(df, adv_window)
            raw["adv_usd"] = common.fact(adv, "USD", px_sid, last_day, "price",
                                         None if adv is not None else "insufficient_history",
                                         derivation=f"mean(close x volume) over {adv_window} bars")
        else:
            raw["missing_data"].append(f"price history unavailable (none on/before {as_of})")
            raw["adv_usd"] = common.na_fact("USD", None, as_of, "price")
            raw["last_close"] = common.na_fact("USD/share", None, as_of, "price")
        if b is not None and len(b):
            raw["sources"].append(common.dataset_source(f"D:PX:{benchmark}", accessed=as_of))
        for w in windows:
            n = int(rs_windows.get(w) or 0)
            value, day = relative_strength(df, b, n) if n else (None, None)
            raw[f"rs_{w}"] = common.fact(value, "pp", px_sid if value is not None else None, day or as_of, "price",
                                         None if value is not None else "insufficient_history",
                                         derivation=f"excess return vs {benchmark} over {n} trading days")
        # fundamentals only when the size/liquidity filters can still pass (unknown ADV is excluded
        # as no_data anyway; unknown market cap may still come from the fundamentals payload)
        adv_v = common.fact_value(raw["adv_usd"])
        size_ok = mcap is None or min_mcap is None or mcap >= min_mcap
        liq_ok = adv_v is not None and (min_adv is None or adv_v >= min_adv)
        raw["coverage"] = None
        if not (size_ok and liq_ok):
            raw["fundamentals_skipped"] = "below market-cap/liquidity filters or no price history"
            return raw
        price = common.fact_value(raw.get("last_close"))
        try:
            fund = _run_fundamentals(t, price=price, as_of=as_of, sector_class=sector_class)
        except common.ConfigError:
            raise                      # misconfiguration (e.g. sec_contact_email) must stop the screen
        except Exception as exc:  # noqa: BLE001 - lazy optional dependency / provider boundary
            raw["missing_data"].append(f"fundamentals unavailable ({type(exc).__name__}: {exc}) — "
                                       "quality/valuation inputs imputed")
            return raw
        fund = fund or {}
        metrics = metrics_from_fundamentals(fund)
        raw.update({k: v for k, v in metrics.items() if k != "market_cap"})
        if mcap is None and common.fact_value(metrics.get("market_cap")) is not None:
            raw["mcap"] = metrics["market_cap"]
        raw["sources"].extend(fund.get("sources") or [])
        if fund.get("coverage") != "non_sec" and all(common.fact_value(raw.get(k)) is None for k in fundamental_keys):
            why = next((str(m) for m in fund.get("missing_data") or []), "")
            raw["missing_data"].append(f"fundamentals returned no usable screen metrics (coverage "
                                       f"{fund.get('coverage')}{': ' + why if why else ''}) — quality/valuation "
                                       "inputs imputed")
        if fund.get("coverage") == "partial":
            raw["flags"].append(common.flag("partial_fundamentals", "low", f"{t}: SEC coverage partial"))
        return raw

    gather.prefetch = prefetch  # type: ignore[attr-defined]
    return gather


# =====================================================================================
# run
# =====================================================================================


def _as_fact(x: Any, key: str, as_of: str) -> dict:
    """A raw metric as a Fact (gatherers may return plain numbers; source unknown → null)."""
    if common.is_fact(x):
        return dict(x)
    if key.startswith("rs_"):
        unit, kind = "pp", "price"
    else:
        unit, kind = METRIC_UNITS.get(key, ("value", "other"))
    return common.fact(common.safe_float(x), unit, None, as_of, kind)


def recent_shortlisted(sector: str, runs_root: Path | str | None = None, *, exclude_run: str | None = None,
                       lookback: int = 3) -> list[str]:
    """Tickers shortlisted for ``sector`` in the newest ``lookback`` earlier runs that screened it.

    Reads ``<runs_root>/<run_id>/phase3/screen_<sector>.json`` (newest run id first; ``exclude_run`` and
    dry runs — ids ending ``_dryrun`` — are skipped; runs without a screen file for the sector, such as
    ``--quick`` runs, do not count towards ``lookback``). Unreadable files are ignored. Newest first,
    no duplicates. ``lookback <= 0`` → ``[]``."""
    root = Path(runs_root) if runs_root is not None else common.runs_dir()
    if lookback <= 0 or not root.is_dir():
        return []
    found: list[str] = []
    counted = 0
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        if d.name == exclude_run or d.name.endswith("_dryrun"):
            continue
        f = d / "phase3" / f"screen_{sector}.json"
        if not f.is_file():
            continue
        try:
            doc = common.read_json(f)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, Mapping):
            continue
        counted += 1
        for raw_t in doc.get("shortlist") or []:
            try:
                t = common.norm_ticker(raw_t)
            except ValueError:
                continue
            if t not in found:
                found.append(t)
        if counted >= lookback:
            break
    return found


def run(sector: str, out: Path | None = None, *, held: Iterable[str], as_of: str | None = None,
        gather: Callable[[str], Mapping[str, Any] | None] | None = None, cfg: Mapping[str, Any] | None = None,
        universe_cfg: Mapping[str, Any] | None = None, cache: common.DiskCache | None = None,
        overrides: Mapping[str, Any] | None = None, recent: Iterable[str] | None = None) -> dict:
    """Screen one sector and return the ``screen/v1`` payload (written to ``out`` when given).

    ``overrides`` = etf_overrides.yaml dict for the ``funds.<ETF>.top_holdings`` universe fallback
    (default: loaded from the config dir). ``recent`` = tickers shortlisted in recent runs
    (``recent_shortlisted``); they are skipped (reason ``recent``) so each run proposes new names."""
    if sector not in common.SECTOR_KEYS:
        raise ValueError(f"unknown sector {sector!r}; expected one of {', '.join(common.SECTOR_KEYS)}")
    as_of_s = common.date_str(as_of) or common.today().isoformat()
    cfg = _cfg(cfg)
    ucfg = _universe_cfg(universe_cfg)
    scfg = cfg.get("screen") or {}
    etf = common.norm_ticker((cfg.get("sector_etfs") or {}).get(sector) or common.sector_etf(sector))
    sector_class = common.sector_class(sector)
    weights = screen_weights(cfg)
    impute = common.safe_float(scfg.get("impute_percentile"))
    impute = DEFAULT_IMPUTE if impute is None else impute
    top_n = int(scfg.get("etf_top_n") or DEFAULT_ETF_TOP_N)
    shortlist_n = int((cfg.get("phase3") or {}).get("shortlist_size") or DEFAULT_SHORTLIST)
    if common.safe_float(ucfg.get("max_candidates_per_sector")) is not None:
        shortlist_n = min(shortlist_n, int(ucfg["max_candidates_per_sector"]))
    min_mcap = common.safe_float(ucfg.get("min_market_cap_usd"))
    min_adv = common.safe_float(ucfg.get("min_avg_dollar_volume_usd"))
    cache = cache or common.DiskCache()

    sources: list[dict] = []
    missing: list[str] = []
    flags: list[dict] = []

    # ---- universe -------------------------------------------------------------------
    universe: list[dict] = []
    got = etf_lookthrough.cached_yf_funds(etf, cfg=cfg, cache=cache)
    top = etf_lookthrough.top_holdings((got.payload or {}).get("top_holdings"), top_n) if got.payload else []
    if top:
        sources.append(common.dataset_source(f"D:YF:{etf}:FUND", accessed=as_of_s))
        if got.provider == "cache_stale":
            flags.append(common.flag("stale_cache", "medium", f"{etf}: holdings from stale cache ({got.error})"))
        for h in top:
            universe.append({"ticker": h["symbol"], "origin": "etf_top10", "etf_weight_pct": h["weight_pct"],
                             "name": h.get("name")})
    else:
        why = got.error or "no top holdings"
        ov_rows, ov_src, ov_flags = etf_override_holdings(
            etf, overrides if overrides is not None else etf_lookthrough.load_overrides(), top_n, as_of=as_of_s)
        flags.extend(ov_flags)
        if ov_rows and ov_src:
            sources.append(ov_src)
            for h in ov_rows:
                universe.append({"ticker": h["symbol"], "origin": "etf_top10", "etf_weight_pct": h["weight_pct"],
                                 "name": h.get("name")})
        else:
            missing.append(f"{etf} top holdings unavailable ({why}) — universe limited to universe.yaml additions")
            flags.append(common.flag("universe_etf_unavailable", "high", f"{etf}: {why}"))
    seen = {u["ticker"] for u in universe}
    for raw_t in ((ucfg.get("additions") or {}).get(sector) or []):
        try:
            t = common.norm_ticker(raw_t)
        except ValueError:
            continue
        if t in seen:
            next(u for u in universe if u["ticker"] == t)["also_in_additions"] = True
            continue
        seen.add(t)
        universe.append({"ticker": t, "origin": "universe_add", "etf_weight_pct": None})

    # ---- static filters ---------------------------------------------------------------
    exclusions = {common.norm_ticker(x) for x in (ucfg.get("exclusions") or []) if str(x).strip()}
    held_set = {common.norm_ticker(x) for x in (held or []) if str(x).strip()}
    recent_set = {common.norm_ticker(x) for x in (recent or []) if str(x).strip()}
    excluded: list[dict] = []
    candidates: list[str] = []
    for u in universe:
        t = u["ticker"]
        if t in exclusions:
            excluded.append({"ticker": t, "reason": "exclusion", "detail": "listed in universe.yaml exclusions"})
        elif t in held_set:
            excluded.append({"ticker": t, "reason": "held", "detail": "already held (Phase 2 reviews it)"})
        elif t in recent_set:
            excluded.append({"ticker": t, "reason": "recent",
                             "detail": "shortlisted in a recent run (rotation: propose new names)"})
        else:
            candidates.append(t)

    # ---- gather + data filters --------------------------------------------------------
    gather = gather or make_default_gatherer(sector, as_of=as_of_s, cfg=cfg, universe_cfg=ucfg, cache=cache)
    prefetch = getattr(gather, "prefetch", None)
    if callable(prefetch) and candidates:
        prefetch(candidates)
    eligible: dict[str, dict] = {}
    for t in candidates:
        try:
            raw = gather(t)
        except common.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - pluggable gatherer boundary
            excluded.append({"ticker": t, "reason": "no_data", "detail": f"{type(exc).__name__}: {exc}"})
            missing.append(f"{t}: screen data unavailable ({type(exc).__name__}: {exc})")
            continue
        if not raw:
            excluded.append({"ticker": t, "reason": "no_data", "detail": "gatherer returned no data"})
            missing.append(f"{t}: screen data unavailable")
            continue
        raw = dict(raw)
        sources.extend(raw.get("sources") or [])
        missing.extend(f"{t}: {m}" for m in raw.get("missing_data") or [])
        flags.extend(raw.get("flags") or [])
        mcap = common.fact_value(raw.get("mcap"))
        adv = common.fact_value(raw.get("adv_usd"))
        reason, detail = None, None
        if mcap is None:
            reason, detail = "no_data", "market cap unavailable"
        elif min_mcap is not None and mcap < min_mcap:
            reason, detail = "mcap", f"market cap {mcap:,.0f} < {min_mcap:,.0f} USD"
        elif adv is None:
            reason, detail = "no_data", "average dollar volume unavailable"
        elif min_adv is not None and adv < min_adv:
            reason, detail = "adv", f"avg dollar volume {adv:,.0f} < {min_adv:,.0f} USD"
        elif raw.get("coverage") == "non_sec":
            reason, detail = "non_sec", "not an SEC filer (no EDGAR fundamentals)"
        if reason:
            excluded.append({"ticker": t, "reason": reason, "detail": detail, "mcap": mcap, "adv_usd": adv})
            continue
        eligible[t] = raw

    # ---- imputation guard ---------------------------------------------------------------
    max_imputed = common.safe_float(scfg.get("max_imputed_fraction"))
    max_imputed = DEFAULT_MAX_IMPUTED_FRACTION if max_imputed is None else max_imputed
    warn_imputed = common.safe_float(scfg.get("warn_imputed_fraction"))
    warn_imputed = DEFAULT_WARN_IMPUTED_FRACTION if warn_imputed is None else warn_imputed
    spec = component_spec(sector_class, cfg)
    qv_inputs = [(k, d) for comp in ("quality", "valuation") for k, d in spec[comp]]
    for t in list(eligible):
        absent = [k for k, d in qv_inputs if _directional(common.fact_value(eligible[t].get(k)), d) is None]
        frac = len(absent) / len(qv_inputs) if qv_inputs else 0.0
        why = f"{len(absent)} of {len(qv_inputs)} quality/valuation inputs missing ({', '.join(absent)})"
        if frac > max_imputed:
            excluded.append({"ticker": t, "reason": "no_data", "detail": f"{why}; too few to rank (not scored on imputed values)",
                             "mcap": common.fact_value(eligible[t].get("mcap")),
                             "adv_usd": common.fact_value(eligible[t].get("adv_usd"))})
            missing.append(f"{t}: {why} - excluded from the ranking")
            del eligible[t]
        elif frac > warn_imputed:
            flags.append(common.flag("screen_imputation_heavy", "medium",
                                     f"{t}: {why}; neutral percentile {impute:g} imputed"))

    # ---- ranking ----------------------------------------------------------------------
    origin = {u["ticker"]: u for u in universe}
    ranked = rank_candidates(eligible, sector_class=sector_class, weights=weights, impute=impute, cfg=cfg)
    for r in ranked:
        raw = eligible[r["ticker"]]
        raw_out = {"mcap": _as_fact(raw.get("mcap"), "mcap", as_of_s), "adv_usd": _as_fact(raw.get("adv_usd"), "adv_usd", as_of_s)}
        for comp in r["components"].values():
            for key in comp["inputs"]:
                if raw.get(key) is not None:
                    raw_out[key] = _as_fact(raw.get(key), key, as_of_s)
        r["raw"] = raw_out
        r["coverage"] = raw.get("coverage")
        r["origin"] = origin[r["ticker"]]["origin"]
        r["etf_weight_pct"] = origin[r["ticker"]].get("etf_weight_pct")
    ordered = [{k: r[k] for k in ("ticker", "rank", "composite", "components", "raw", "imputed", "coverage",
                                  "origin", "etf_weight_pct")} for r in ranked]
    shortlist = [r["ticker"] for r in ordered[:max(0, shortlist_n)]]
    if len(ordered) < shortlist_n:
        flags.append(common.flag("short_universe", "low",
                                 f"{len(ordered)} eligible candidate(s) < shortlist size {shortlist_n}; not padded"))
        skipped_recent = sorted(e["ticker"] for e in excluded if e["reason"] == "recent")
        if skipped_recent:
            flags.append(common.flag(
                "rotation_exhausted", "low",
                f"{len(skipped_recent)} recently shortlisted ticker(s) skipped ({', '.join(skipped_recent)}); only "
                f"{len(ordered)} fresh candidate(s) for {shortlist_n} slots - add names to universe.yaml additions "
                f"or lower phase3.rotation.lookback_runs for more variety"))

    payload = common.envelope(
        SCHEMA, as_of_s, sources=sources, missing_data=missing, flags=flags,
        sector=sector, benchmark_etf=etf, sector_class=sector_class,
        universe=universe, excluded=excluded,
        weights={k: round(v, 6) for k, v in weights.items()},
        filters={"min_market_cap_usd": min_mcap, "min_avg_dollar_volume_usd": min_adv,
                 "exclusions": sorted(exclusions), "held": sorted(held_set), "recent": sorted(recent_set),
                 "etf_top_n": top_n,
                 "shortlist_size": shortlist_n},
        method={"percentile": "(average rank - 1) / (n - 1) within the eligible set; single value -> 0.5",
                "impute_percentile": impute,
                "components": {c: [k for k, _ in ins] for c, ins in component_spec(sector_class, cfg).items()},
                "reciprocal_inputs": [k for c, ins in component_spec(sector_class, cfg).items() for k, d in ins if d < 0],
                "tie_break": "ticker ascending"},
        ranked=ordered, shortlist=shortlist)
    if out is not None:
        common.write_json(Path(out), payload)
    return payload


# =====================================================================================
# CLI
# =====================================================================================


def _portfolio_tickers() -> list[str]:
    try:
        port = common.load_config("portfolio")
    except common.ConfigError:
        return []
    out: list[str] = []
    for h in port.get("holdings") or []:
        for key in ("ticker", "proxy_ticker"):
            if h.get(key):
                out.append(common.norm_ticker(h[key]))
    return out


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Screen a sector's candidate universe (Phase 3).")
    parser.add_argument("--sector", required=True, choices=list(common.SECTOR_KEYS))
    parser.add_argument("--held", default=None, help="comma-separated held tickers (default: portfolio.yaml)")
    parser.add_argument("--out", default=None, help="output path (e.g. runs/<id>/phase3/screen_<sector>.json)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    common.apply_common_args(args)
    held = [t for t in args.held.split(",") if t.strip()] if args.held is not None else _portfolio_tickers()
    try:
        payload = run(args.sector, Path(args.out) if args.out else None, held=held, as_of=args.as_of)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": args.out, "sector": args.sector, "universe": len(payload["universe"]),
                      "excluded": len(payload["excluded"]), "shortlist": payload["shortlist"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
