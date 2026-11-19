"""Technical snapshot of one ticker -> ``technicals_<T>.json`` (schema ``technicals/v1``,
DESIGN §4.2).

``compute()`` is pure (frames in, payload out); ``run()`` loads the ticker, the committee
benchmark (``committee.yaml: benchmark``, SPY) and the sector ETF through ``fetch_prices``
(cache first; fetched when absent), calls ``compute()`` and writes the file. Every threshold
comes from ``committee.yaml: technicals``; indicator math lives in ``scripts/_indicators.py``
and pivots/levels/stops in ``scripts/_levels.py``.

Regime (first match wins): choppy -> trend_up -> trend_down -> range -> transition; every test
is recorded in ``trend.regime_tests``. Fewer than 200 bars -> SMA200 (and its slope, 52-week
range) null with ``na_reason: insufficient_history`` plus a flag; the regime is then decided by
the tests that can still be evaluated.

API::

    compute(ohlcv, bench=None, sector_etf_df=None, *, ticker, sector_etf=None, sources=None,
            cfg=None, as_of=None, benchmark=None) -> dict
    run(ticker, out=None, *, sector_etf=None, as_of=None, sector=None) -> dict
    classify_regime(values, regime_cfg) -> (regime, tests)

CLI: ``python scripts/compute_technicals.py --ticker KO --sector-etf XLP --out <path>``.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts import _indicators as ind  # noqa: E402
from scripts import _levels as lv  # noqa: E402
from scripts import fetch_prices  # noqa: E402

import argparse  # noqa: E402
import math  # noqa: E402
import operator  # noqa: E402
from typing import Any, Callable  # noqa: E402

import pandas as pd  # noqa: E402

SCHEMA_NAME = "technicals"
REGIMES = ("trend_up", "trend_down", "range", "choppy", "transition")

#: Module defaults, used only for keys missing from committee.yaml ``technicals`` (the shipped
#: config defines every one of these except ``stop_sma200_buffer_atr``, which falls back to
#: ``stop_support_buffer_atr``).
TECH_DEFAULTS: dict[str, Any] = {
    "sma_periods": [20, 50, 200],
    "rsi_period": 14,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "atr_period": 14,
    "adx_period": 14,
    "atr_pctile_window": 252,
    "high_low_window": 252,
    "high_low_min_bars": 200,
    "sma200_slope_days": 20,
    "max_drawdown_window": 252,
    "rs_windows": {"3m": 63, "6m": 126, "12m": 252},
    "volume_short": 20,
    "volume_long": 50,
    "volume_rising_ratio": 1.10,
    "volume_falling_ratio": 0.90,
    "adv_window": 63,
    "pivot_left": 3,
    "pivot_right": 3,
    "level_lookback": 250,
    "level_cluster_atr": 0.5,
    "max_levels_per_side": 3,
    "stop_support_buffer_atr": 0.5,
    "stop_atr_multiple": 2.0,
    "weekly_closes_weeks": 52,
}
REGIME_DEFAULTS: dict[str, Any] = {
    "adx_trend_min": 25,
    "adx_range_max": 20,
    "choppy_window": 20,
    "choppy_min_sma50_crosses": 4,
    "choppy_atr_pctile_min": 80,
    "choppy_adx_max": 20,
    "range_window": 20,
    "range_max_width_pct": 10,
    "sma200_slope_min_pct": 0.0,
}
#: Calendar look-back of ``drawdown_from_3y_peak_pct`` (the "3y" is part of the field name).
PEAK_LOOKBACK_YEARS = 3


# =====================================================================================
# Config
# =====================================================================================


def tech_cfg(cfg: dict | None = None) -> dict:
    """``committee.yaml: technicals`` merged over ``TECH_DEFAULTS`` (``regime`` merged over
    ``REGIME_DEFAULTS``); ``stop_sma200_buffer_atr`` defaults to ``stop_support_buffer_atr``."""
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    raw = dict(cfg.get("technicals") or {})
    out = dict(TECH_DEFAULTS)
    out.update({k: v for k, v in raw.items() if k != "regime" and v is not None})
    regime = dict(REGIME_DEFAULTS)
    regime.update({k: v for k, v in (raw.get("regime") or {}).items() if v is not None})
    out["regime"] = regime
    out.setdefault("stop_sma200_buffer_atr", out["stop_support_buffer_atr"])
    rs = dict(TECH_DEFAULTS["rs_windows"])
    rs.update(raw.get("rs_windows") or {})
    out["rs_windows"] = rs
    return out


# =====================================================================================
# Small helpers
# =====================================================================================


def _rd(x: Any, dp: int = 4) -> float | None:
    f = common.safe_float(x)
    return None if f is None else round(f, dp)


def _px(x: Any, dp: int = 4) -> float | None:
    """A raw price level (Close/High/Low) for output: float32 noise removed first (``769.6400146484375``
    -> ``769.64``; 4 dp alone would leave ``1500.1201`` for a float32 1500.12), then ``dp`` decimals."""
    return _rd(common.round_price(x), dp)


def _prep(df: pd.DataFrame | None, as_of: Any, ticker: str = "") -> pd.DataFrame:
    """Clean OHLCV truncated to bars on/before ``as_of``; empty frame when unusable."""
    empty = pd.DataFrame(columns=common.OHLCV_COLUMNS, dtype="float64",
                         index=pd.DatetimeIndex([], name="Date"))
    if df is None or len(df) == 0:
        return empty
    try:
        out = common.clean_ohlcv(df, ticker=ticker)
    except common.DataUnavailable:
        return empty
    d = common.parse_date(as_of)
    if d is not None:
        out = out[out.index <= pd.Timestamp(d)]
    return out if not out.empty else empty


class _FactMaker:
    """Builds price Facts for one series (source id + as-of date fixed)."""

    def __init__(self, source_id: str | None, as_of: str | None, default_na: str):
        self.sid = source_id
        self.as_of = as_of
        self.default_na = default_na

    def __call__(self, value: Any, unit: str, *, dp: int = 4, na: str | None = None,
                 source_id: str | None = "__default__", as_of: Any = "__default__", raw: bool = False,
                 **extra: Any) -> dict:
        v = _px(value, dp) if raw else _rd(value, dp)  # raw = a quoted price level, not a computed figure
        sid = self.sid if source_id == "__default__" else source_id
        aod = self.as_of if as_of == "__default__" else as_of
        reason = None if v is not None else (na or self.default_na)
        return common.fact(v, unit, sid, aod, "price", na_reason=reason, **extra)


def _test(regime: str, label: str, value: Any, op: Callable[[float, float], bool], threshold: Any,
          *, core: bool = True) -> dict:
    """One regime test record ``{"regime","test","value","threshold","pass","core"}``; ``pass`` is
    None when an input is missing (the test cannot be evaluated). ``core`` tests must be
    evaluable for their regime to match; a non-core test with a missing input is ``skipped``."""
    v = common.safe_float(value)
    t = common.safe_float(threshold)
    passed = None if (v is None or t is None) else bool(op(v, t))
    return {"regime": regime, "test": label, "value": _px(v, 4), "threshold": _px(t, 4), "pass": passed,
            "core": bool(core), "skipped": bool(passed is None and not core)}


def regime_matches(group: list[dict]) -> bool:
    """A regime matches when every core test passes and every non-core test passes or was
    skipped for lack of history (DESIGN §4.2 "regime from the available tests")."""
    return all((t["pass"] is True) or (t.get("skipped") is True) for t in group) and         any(t["pass"] is True for t in group)


def _label(x: Any) -> str:
    f = common.safe_float(x)
    if f is None:
        return "null"
    if f == 0:
        f = 0.0  # never print "-0"
    return f"{f:g}"


def classify_regime(values: dict, regime_cfg: dict | None = None) -> tuple[str, list[dict]]:
    """Rule cascade (first match wins): choppy -> trend_up -> trend_down -> range -> transition.

    ``values`` keys: ``adx, pdi, ndi, close, sma50, sma200, sma200_slope_pct, atr_pctile,
    sma50_crosses, range_width_pct`` (None when unavailable). A regime is decided from the
    tests that can be evaluated: it matches when every evaluable test passes and all of its
    core tests are evaluable. Long-history inputs are non-core and skipped when null
    (trend_*: ``sma50 vs sma200`` and ``sma200_slope``, i.e. < 200 bars; choppy:
    ``atr_pctile``, i.e. < ~265 bars). Core tests (ADX, DI, close vs SMA50, SMA50 crosses,
    range width) block their regime when null. Returns ``(regime, tests)``."""
    rc = dict(REGIME_DEFAULTS)
    rc.update(regime_cfg or {})
    ge, gt, lt, le = operator.ge, operator.gt, operator.lt, operator.le
    v = {k: common.safe_float(values.get(k)) for k in
         ("adx", "pdi", "ndi", "close", "sma50", "sma200", "sma200_slope_pct", "atr_pctile",
          "sma50_crosses", "range_width_pct")}
    slope_min = float(rc["sma200_slope_min_pct"])
    win, rwin = int(rc["choppy_window"]), int(rc["range_window"])
    groups: list[tuple[str, list[dict]]] = [
        ("choppy", [
            _test("choppy", f"sma50_crosses_{win}d >= {_label(rc['choppy_min_sma50_crosses'])}",
                  v["sma50_crosses"], ge, rc["choppy_min_sma50_crosses"]),
            _test("choppy", f"atr_pctile > {_label(rc['choppy_atr_pctile_min'])}", v["atr_pctile"], gt,
                  rc["choppy_atr_pctile_min"], core=False),
            _test("choppy", f"adx < {_label(rc['choppy_adx_max'])}", v["adx"], lt, rc["choppy_adx_max"]),
        ]),
        ("trend_up", [
            _test("trend_up", f"adx >= {_label(rc['adx_trend_min'])}", v["adx"], ge, rc["adx_trend_min"]),
            _test("trend_up", "pdi > ndi", v["pdi"], gt, v["ndi"]),
            _test("trend_up", "close > sma50", v["close"], gt, v["sma50"]),
            _test("trend_up", "sma50 > sma200", v["sma50"], gt, v["sma200"], core=False),
            _test("trend_up", f"sma200_slope_pct > {_label(slope_min)}", v["sma200_slope_pct"], gt, slope_min,
                  core=False),
        ]),
        ("trend_down", [
            _test("trend_down", f"adx >= {_label(rc['adx_trend_min'])}", v["adx"], ge, rc["adx_trend_min"]),
            _test("trend_down", "ndi > pdi", v["ndi"], gt, v["pdi"]),
            _test("trend_down", "close < sma50", v["close"], lt, v["sma50"]),
            _test("trend_down", "sma50 < sma200", v["sma50"], lt, v["sma200"], core=False),
            _test("trend_down", f"sma200_slope_pct < {_label(-slope_min)}", v["sma200_slope_pct"], lt, -slope_min,
                  core=False),
        ]),
        ("range", [
            _test("range", f"adx < {_label(rc['adx_range_max'])}", v["adx"], lt, rc["adx_range_max"]),
            _test("range", f"range_width_{rwin}d_pct <= {_label(rc['range_max_width_pct'])}", v["range_width_pct"], le,
                  rc["range_max_width_pct"]),
        ]),
    ]
    tests: list[dict] = []
    regime = None
    for name, group in groups:
        tests.extend(group)
        if regime is None and regime_matches(group):
            regime = name
    return (regime or "transition"), tests


def weekly_closes(close: pd.Series, weeks: int) -> list[list[Any]]:
    """Last ``weeks`` week-ending (W-FRI) closes as ``[[date, close], ...]``; each date is the
    actual last trading day of that week (e.g. a Thursday before a Friday holiday)."""
    c = close.dropna()
    if c.empty or weeks <= 0:
        return []
    idx = pd.DatetimeIndex(c.index)
    last_per_week = c.groupby(idx.to_period("W-FRI")).tail(1)
    tail = last_per_week.tail(int(weeks))
    return [[common.date_str(d), _px(v, 4)] for d, v in tail.items()]


def _default_sources(names: list[str]) -> list[dict]:
    return [common.dataset_source(f"D:PX:{t}") for t in names]


# =====================================================================================
# compute (pure)
# =====================================================================================


def compute(ohlcv: pd.DataFrame | None, bench: pd.DataFrame | None = None,
            sector_etf_df: pd.DataFrame | None = None, *, ticker: str, sector_etf: str | None = None,
            sources: list[dict] | None = None, cfg: dict | None = None, as_of: Any = None,
            benchmark: str | None = None, extra_flags: list[dict] | None = None,
            extra_missing: list[str] | None = None, providers: dict | None = None) -> dict:
    """Build the ``technicals/v1`` payload from OHLCV frames (no IO).

    ``bench`` / ``sector_etf_df`` may be None (RS facts null). Frames are truncated to bars
    on/before ``as_of`` (default ``common.today()``). ``sources`` defaults to yfinance dataset
    sources for the series supplied."""
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    tc = tech_cfg(cfg)
    rcfg = tc["regime"]
    t = common.norm_ticker(ticker)
    b = common.norm_ticker(benchmark or cfg.get("benchmark") or "SPY")
    e = common.norm_ticker(sector_etf) if sector_etf else None
    as_of_d = common.parse_date(as_of) or common.today()
    flags: list[dict] = list(extra_flags or [])
    missing: list[str] = list(extra_missing or [])

    raw_rows = 0 if ohlcv is None else len(ohlcv)
    df = _prep(ohlcv, as_of_d, t)
    bdf = _prep(bench, as_of_d, b)
    edf = _prep(sector_etf_df, as_of_d, e or "") if e else _prep(None, as_of_d)
    n = int(len(df))
    if ohlcv is not None and raw_rows > n and n > 0:
        flags.append(common.flag("truncated_to_as_of", "low",
                                 f"{t}: {raw_rows - n} bar(s) after {as_of_d.isoformat()} ignored"))
    sid = f"D:PX:{t}"
    last_date = common.date_str(df.index[-1]) if n else None
    F = _FactMaker(sid if n else None, last_date, "insufficient_history" if n else "missing")
    if n == 0:
        flags.append(common.flag("no_price_data", "high", f"{t}: no price history available; every figure is null"))
        missing.append(f"price history for {t} (all providers failed)")

    c, h, lo, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    close = ind.last_value(c)

    # ---------------------------------------------------------------- trend
    p_short, p_mid, p_long = [int(p) for p in list(tc["sma_periods"])[:3]]
    sma_s, sma_m, sma_l = ind.sma(c, p_short), ind.sma(c, p_mid), ind.sma(c, p_long)
    sma20_v, sma50_v, sma200_v = ind.last_value(sma_s), ind.last_value(sma_m), ind.last_value(sma_l)
    slope_days = int(tc["sma200_slope_days"])
    slope_v = ind.slope_pct(sma_l, slope_days)
    cvs = None if (close is None or not sma200_v) else (close / sma200_v - 1.0) * 100.0
    if 0 < n < p_long:
        flags.append(common.flag("insufficient_history", "medium",
                                 f"{t}: only {n} bars (< {p_long}): SMA{p_long}, its slope and the "
                                 "52-week range are null; trend_up/trend_down are judged without the "
                                 "SMA200 tests (ADX, DI and close-vs-SMA50 still required)"))
        missing.append(f"SMA{p_long} for {t} (only {n} daily bars)")

    # ---------------------------------------------------------------- momentum / volatility
    rsi_v = ind.last_value(ind.rsi(c, int(tc["rsi_period"])))
    m = ind.macd(c, int(tc["macd_fast"]), int(tc["macd_slow"]), int(tc["macd_signal"]))
    adx_df = ind.adx(h, lo, c, int(tc["adx_period"]))
    adx_v, pdi_v, ndi_v = (ind.last_value(adx_df[k]) for k in ("adx", "pdi", "ndi"))
    atr_s = ind.atr(h, lo, c, int(tc["atr_period"]))
    atr_v = ind.last_value(atr_s)
    atr_pct_v = None if (atr_v is None or not close) else atr_v / close * 100.0
    atr_window = int(tc["atr_pctile_window"])
    atr_pctile_v = ind.last_value(ind.trailing_percentile(atr_s / c, atr_window)) if n else None

    # ---------------------------------------------------------------- regime
    cw, rw = int(rcfg["choppy_window"]), int(rcfg["range_window"])
    crosses = ind.sma_crossings(c.tail(cw), sma_m.tail(cw)) if n >= cw else None
    width = ind.range_width_pct(h.tail(rw), lo.tail(rw)) if n >= rw else None
    regime, tests = classify_regime({
        "adx": adx_v, "pdi": pdi_v, "ndi": ndi_v, "close": close, "sma50": sma50_v, "sma200": sma200_v,
        "sma200_slope_pct": slope_v, "atr_pctile": atr_pctile_v, "sma50_crosses": crosses,
        "range_width_pct": width}, rcfg)
    skipped = [f"{x['regime']}: {x['test']}" for x in tests if x.get("skipped")]
    if n and skipped:
        flags.append(common.flag("regime_tests_skipped", "low",
                                 f"{t}: {len(skipped)} regime test(s) skipped for lack of history "
                                 f"({'; '.join(skipped)}); those regimes judged on the remaining tests"))

    pivots = lv.swing_pivots(df, int(tc["pivot_left"]), int(tc["pivot_right"]), int(tc["level_lookback"])) \
        if n else {"highs": [], "lows": [], "last_two_highs": [], "last_two_lows": []}
    structure = lv.structure_label(pivots)

    trend = {
        "sma20": F(sma20_v, "USD/share", derivation=f"SMA{p_short} of Close"),
        "sma50": F(sma50_v, "USD/share", derivation=f"SMA{p_mid} of Close"),
        "sma200": F(sma200_v, "USD/share", derivation=f"SMA{p_long} of Close"),
        "sma200_slope_20d_pct": F(slope_v, "pct", derivation=f"SMA{p_long} change over {slope_days} bars"),
        "close_vs_sma200_pct": F(cvs, "pct", derivation=f"Close / SMA{p_long} - 1"),
        "regime": regime,
        "regime_tests": tests,
        "structure": structure,
    }

    # ---------------------------------------------------------------- range
    hl_window, hl_min = int(tc["high_low_window"]), int(tc["high_low_min_bars"])
    hi52 = lo52 = None
    hl_note = None
    if n >= hl_window:
        hi52, lo52 = float(h.tail(hl_window).max()), float(lo.tail(hl_window).min())
        hl_note = f"max High / min Low over last {hl_window} bars"
    elif n >= hl_min:
        hi52, lo52 = float(h.max()), float(lo.min())
        hl_note = f"max High / min Low over all {n} available bars (< {hl_window})"
        flags.append(common.flag("short_52w_window", "low",
                                 f"{t}: 52-week high/low computed over {n} bars (< {hl_window})"))
    pct_hi = None if (hi52 is None or close is None or hi52 <= 0) else (close / hi52 - 1.0) * 100.0
    peak_start = (pd.Timestamp(df.index[-1]) - pd.DateOffset(years=PEAK_LOOKBACK_YEARS)) if n else None
    dd_3y = ind.drawdown_from_peak_pct(c[c.index >= peak_start]) if n else None
    mdd_window = int(tc["max_drawdown_window"])
    mdd = ind.max_drawdown_pct(c.tail(mdd_window)) if n >= 2 else None
    mdd_bars = min(n, mdd_window)
    rng = {
        "high_52w": F(hi52, "USD/share", raw=True, derivation=hl_note),
        "low_52w": F(lo52, "USD/share", raw=True, derivation=hl_note),
        "pct_from_high_52w": F(pct_hi, "pct", derivation="Close / high_52w - 1"),
        "drawdown_from_3y_peak_pct": F(dd_3y, "pct",
                                       derivation=f"Close / max Close over the last {PEAK_LOOKBACK_YEARS}y - 1"),
        "max_drawdown_1y_pct": F(mdd, "pct", derivation=f"largest peak-to-trough Close decline over {mdd_bars} bars"),
    }

    # ---------------------------------------------------------------- relative strength
    def rs_block(other: pd.DataFrame, sym: str | None, label: str) -> dict:
        out: dict[str, dict] = {}
        windows = {str(k): int(v) for k, v in tc["rs_windows"].items()}
        if sym is None:
            for k in windows:
                out[k] = common.na_fact("pp", None, last_date, "price", "not_applicable",
                                        note=f"no {label} configured")
            return out
        osid = f"D:PX:{sym}"
        if n == 0 or other.empty:
            if n and other.empty:
                missing.append(f"price history for {sym} ({label}); relative strength vs {sym} is null")
                flags.append(common.flag(f"{label}_unavailable", "medium",
                                         f"{t}: no price history for {sym}; RS vs {sym} null"))
            for k in windows:
                out[k] = common.na_fact("pp", None, last_date, "price", "missing")
            return out
        lag_flagged = False
        for k, bars in windows.items():
            res = ind.relative_strength_pct(c, other["Close"], bars)
            if res["value"] is None:
                out[k] = common.na_fact("pp", osid, last_date, "price", "insufficient_history",
                                        note=f"{res['aligned_rows']} aligned bars <= {bars}")
                continue
            end = common.date_str(res["end"])
            out[k] = common.fact(_rd(res["value"], 4), "pp", osid, end, "price",
                                 derivation=(f"(1+r_{t})/(1+r_{sym})-1 over {bars} date-aligned bars "
                                             f"{common.date_str(res['start'])}..{end}"),
                                 note=f"{t} {_rd(res['ret_a_pct'], 2)}% vs {sym} {_rd(res['ret_b_pct'], 2)}%")
            if end != last_date and not lag_flagged:
                lag_flagged = True
                flags.append(common.flag("rs_alignment_lag", "low",
                                         f"{t} vs {sym}: last common date {end} is before {t}'s last bar {last_date}"))
        return out

    relative = {"vs_spy": rs_block(bdf, b, "benchmark"), "vs_sector": rs_block(edf, e, "sector_etf")}

    # ---------------------------------------------------------------- volume
    vs, vl = int(tc["volume_short"]), int(tc["volume_long"])
    avg_s = float(vol.tail(vs).mean()) if n >= vs else None
    avg_l = float(vol.tail(vl).mean()) if n >= vl else None
    ratio = None
    ratio_na = "insufficient_history"
    if avg_s is not None and avg_l is not None:
        if avg_l > 0:
            ratio = avg_s / avg_l
        else:
            ratio_na = "missing"
            flags.append(common.flag("no_volume_data", "low", f"{t}: no volume reported; volume trend is flat"))
    rising, falling = float(tc["volume_rising_ratio"]), float(tc["volume_falling_ratio"])
    vtrend = "flat"
    if ratio is not None:
        if ratio >= rising:
            vtrend = "rising"
        elif ratio <= falling:
            vtrend = "falling"
    adv_w = int(tc["adv_window"])
    adv = None
    if n >= adv_w:
        tail = df.tail(adv_w)
        adv = float((tail["Close"] * tail["Volume"]).mean())
        if float(tail["Volume"].sum()) <= 0:
            adv = None
    volume = {
        "avg20": F(avg_s, "shares", dp=0, derivation=f"mean Volume over {vs} bars"),
        "avg50": F(avg_l, "shares", dp=0, derivation=f"mean Volume over {vl} bars"),
        "ratio_20_50": F(ratio, "x", na=ratio_na if ratio is None and n else None,
                         derivation=f"avg{vs} / avg{vl}"),
        "trend": vtrend,
        "adv_usd_63d": F(adv, "USD", dp=2, na=("missing" if n >= adv_w else None),
                         derivation=f"mean(Close x Volume) over {adv_w} bars"),
    }

    # ---------------------------------------------------------------- levels + stops
    tol = (float(tc["level_cluster_atr"]) * atr_v) if atr_v else 0.0
    levels_raw = lv.cluster_levels(pivots, tol)
    sup_raw, res_raw = lv.split_levels(levels_raw, close if close is not None else math.nan,
                                       int(tc["max_levels_per_side"])) if close is not None else ([], [])

    def level_out(item: dict) -> dict:
        return {"id": item["id"],
                "price": F(item["price"], "USD/share",
                           derivation=(f"mean of {item['touches']} swing pivot(s) within "
                                       f"{tc['level_cluster_atr']:g} x ATR14 ({_rd(item['low'], 4)}..{_rd(item['high'], 4)})")),
                "touches": int(item["touches"]),
                "last_date": common.date_str(item["last_date"])}

    levels = {"supports": [level_out(x) for x in sup_raw], "resistances": [level_out(x) for x in res_raw]}
    stops_raw = lv.stop_candidates(close if close is not None else math.nan, atr_v, sup_raw, sma200_v,
                                   support_buffer_atr=float(tc["stop_support_buffer_atr"]),
                                   atr_multiple=float(tc["stop_atr_multiple"]),
                                   sma200_buffer_atr=float(tc["stop_sma200_buffer_atr"]))
    stops = [{"id": s["id"], "price": F(s["price"], "USD/share", derivation=s["derivation"]),
              "basis": s["basis"], "ref": s["ref"]} for s in stops_raw]

    # ---------------------------------------------------------------- assemble
    names = [x for x in (t, b, e) if x]
    if sources is None:
        frames = {t: df, b: bdf}
        if e:
            frames[e] = edf
        sources = _default_sources([x for x in dict.fromkeys(names) if not frames.get(x, df).empty])

    payload = common.envelope(
        SCHEMA_NAME, as_of_d, sources=sources, missing_data=missing, flags=flags,
        ticker=t, benchmark=b, sector_etf=e,
        close=F(close, "USD/share", raw=True, na="missing" if not n else None),
        trend=trend,
        momentum={
            "rsi14": F(rsi_v, "index", derivation=f"Wilder RSI({tc['rsi_period']})"),
            "macd": {
                "line": F(ind.last_value(m["line"]), "USD/share",
                          derivation=f"EMA{tc['macd_fast']} - EMA{tc['macd_slow']} of Close"),
                "signal": F(ind.last_value(m["signal"]), "USD/share", derivation=f"EMA{tc['macd_signal']} of MACD line"),
                "hist": F(ind.last_value(m["hist"]), "USD/share", derivation="MACD line - signal"),
            },
            "adx14": F(adx_v, "index", derivation=f"Wilder ADX({tc['adx_period']})"),
            "pdi14": F(pdi_v, "index", derivation=f"+DI({tc['adx_period']})"),
            "ndi14": F(ndi_v, "index", derivation=f"-DI({tc['adx_period']})"),
        },
        volatility={
            "atr14": F(atr_v, "USD/share", derivation=f"Wilder ATR({tc['atr_period']})"),
            "atr14_pct": F(atr_pct_v, "pct", derivation="ATR14 / Close"),
            "atr_pctile_252": F(atr_pctile_v, "pct",
                                derivation=f"percentile of ATR14/Close within the trailing {atr_window} bars (no look-ahead)"),
        },
        range=rng,
        relative_strength=relative,
        volume=volume,
        levels=levels,
        stop_candidates=stops,
        weekly_closes_52w=weekly_closes(c, int(tc["weekly_closes_weeks"])),
        bars=n,
        first_date=common.date_str(df.index[0]) if n else None,
        last_date=last_date,
        price_providers=dict(providers or {}),
    )
    return payload


# =====================================================================================
# run / CLI
# =====================================================================================


def _resolve_sector_etf(sector_etf: str | None, sector: str | None, cfg: dict) -> str | None:
    if sector_etf:
        return common.norm_ticker(sector_etf)
    if sector:
        etf = (cfg.get("sector_etfs") or {}).get(sector) or common.sector_etf(sector)
        return common.norm_ticker(etf) if etf else None
    return None


def run(ticker: str, out: str | Path | None = None, *, sector_etf: str | None = None, as_of: Any = None,
        sector: str | None = None, cache: Any = None, benchmark: str | None = None) -> dict:
    """Load ticker + benchmark + sector ETF series via ``fetch_prices`` (cache first, fetched
    when absent), compute the technicals payload and write it to ``out`` when given.

    Never raises for missing price data: a ticker with no data yields a payload of null Facts
    with flag ``no_price_data`` (high) so the pipeline can continue."""
    try:
        cfg = common.committee_cfg()
    except common.ConfigError:
        cfg = {}
    t = common.norm_ticker(ticker)
    b = common.norm_ticker(benchmark or cfg.get("benchmark") or "SPY")
    e = _resolve_sector_etf(sector_etf, sector, cfg)
    as_of_d = common.parse_date(as_of) or common.today()
    store = cache if isinstance(cache, common.DiskCache) else (common.DiskCache(cache) if cache else common.DiskCache())

    frames: dict[str, pd.DataFrame | None] = {}
    providers: dict[str, str] = {}
    sources: list[dict] = []
    flags: list[dict] = []
    missing: list[str] = []
    for sym in dict.fromkeys(x for x in (t, b, e) if x):
        try:
            res = fetch_prices.fetch_series(sym, cache=store, cfg=cfg)
        except fetch_prices.PriceFetchError as exc:
            frames[sym] = None
            flags.extend(exc.flags)
            providers[sym] = "failed"
            continue
        frames[sym] = res["frame"]
        providers[sym] = res["provider"]
        flags.extend(res["flags"])
        sources.append(fetch_prices.price_source(sym, res))

    payload = compute(frames.get(t), frames.get(b), frames.get(e) if e else None, ticker=t, sector_etf=e,
                      sources=sources, cfg=cfg, as_of=as_of_d, benchmark=b, extra_flags=flags,
                      extra_missing=missing, providers=providers)
    if out is not None:
        common.write_json(out, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Compute the technicals/v1 snapshot for one ticker.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--out", help="output JSON path (prints the payload when omitted)")
    parser.add_argument("--sector-etf", dest="sector_etf", help="sector benchmark ETF (e.g. XLP)")
    parser.add_argument("--sector", help="sector key; resolves the sector ETF when --sector-etf is absent")
    parser.add_argument("--benchmark", help="market benchmark (default committee.yaml benchmark)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
        out = Path(args.out) if args.out else None
        payload = run(args.ticker, out, sector_etf=args.sector_etf, as_of=args.as_of, sector=args.sector,
                      benchmark=args.benchmark)
    except (common.ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if out is None:
        print(common.dumps_json(payload))
    else:
        print(common.dumps_json({
            "out": out.as_posix(), "ticker": payload["ticker"], "as_of": payload["as_of"],
            "bars": payload["bars"], "regime": payload["trend"]["regime"],
            "close": payload["close"]["value"],
            "flags": [f"{f['code']}:{f['severity']}" for f in payload["flags"]]}))
    return 0 if payload["bars"] else 1


if __name__ == "__main__":
    sys.exit(main())
