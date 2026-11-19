"""compute_technicals.py (DESIGN §4.2): pure compute() on synthetic OHLCV, regime cascade,
levels/stops, RS on date-aligned series, <200-bar handling, schema validation, run() through
the fetch_prices cache/fixtures, and the CLI. Fully offline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from conftest import REPO_ROOT, make_ohlcv, make_ranging_ohlcv
from scripts import _indicators as ind
from scripts import common, compute_technicals as ct, fetch_prices

SCHEMAS = REPO_ROOT / "schemas"
STOP_BASES = {"below_support_0.5atr", "close_minus_2atr", "below_sma200"}


# --------------------------------------------------------------------------- helpers
def _schema_errors(doc: dict, name: str = "technicals") -> list[str]:
    resources = []
    for p in sorted(SCHEMAS.glob("*.json")) + sorted((SCHEMAS / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    schema = json.loads((SCHEMAS / "data" / f"{name}.schema.json").read_text(encoding="utf-8"))
    v = Draft202012Validator(schema, registry=Registry().with_resources(resources),
                             format_checker=FormatChecker())
    doc = json.loads(common.dumps_json(doc))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(doc)]


def _codes(payload: dict) -> dict[str, str]:
    return {f["code"]: f["severity"] for f in payload["flags"]}


def _seed_cache(ticker: str, df: pd.DataFrame, *, hours_old: float = 0.0) -> None:
    cache = common.DiskCache()
    key = fetch_prices.cache_key(ticker)
    cache.put_frame("prices", key, df, common.yf_history_url(ticker),
                    meta={"provider": "yfinance", "ticker": ticker, "period": "3y"})
    if hours_old:
        p = cache.path_for("prices", key)
        entry = common.read_json(p)
        entry["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat(timespec="seconds")
        common.write_json(p, entry)


@pytest.fixture(scope="module")
def full_payload() -> dict:
    df = make_ohlcv(n=800, seed=0)
    bench = make_ohlcv(n=800, seed=1)
    etf = make_ohlcv(n=800, seed=2)
    return ct.compute(df, bench, etf, ticker="ko", sector_etf="xlp", as_of="2026-10-02",
                      cfg=common.committee_cfg())


# --------------------------------------------------------------------------- full history
def test_full_history_payload_validates(full_payload):
    p = full_payload
    assert _schema_errors(p) == []
    assert p["schema"] == "technicals/v1"
    assert (p["ticker"], p["benchmark"], p["sector_etf"]) == ("KO", "SPY", "XLP")
    assert [s["id"] for s in p["sources"]] == ["D:PX:KO", "D:PX:SPY", "D:PX:XLP"]
    assert p["bars"] == 800 and p["last_date"] == "2026-10-02"
    assert not [f for f in p["flags"] if f["severity"] in ("medium", "high")]


def test_full_history_values_match_indicator_math(full_payload):
    df = make_ohlcv(n=800, seed=0)
    c = df["Close"]
    p = full_payload
    assert p["close"]["value"] == pytest.approx(c.iloc[-1], abs=1e-4)
    assert (p["close"]["source_id"], p["close"]["as_of"], p["close"]["kind"]) == ("D:PX:KO", "2026-10-02", "price")
    tr = p["trend"]
    assert tr["sma20"]["value"] == pytest.approx(c.tail(20).mean(), abs=1e-4)
    assert tr["sma200"]["value"] == pytest.approx(c.tail(200).mean(), abs=1e-4)
    sma200 = c.rolling(200).mean()
    assert tr["sma200_slope_20d_pct"]["value"] == pytest.approx((sma200.iloc[-1] / sma200.iloc[-21] - 1) * 100, abs=1e-4)
    assert tr["close_vs_sma200_pct"]["value"] == pytest.approx((c.iloc[-1] / sma200.iloc[-1] - 1) * 100, abs=1e-4)
    assert tr["regime"] in ct.REGIMES
    assert tr["structure"] in ("HH_HL", "LH_LL", "transition", "unknown")
    assert p["momentum"]["rsi14"]["value"] == pytest.approx(ind.rsi(c, 14).iloc[-1], abs=1e-4)
    atr = ind.atr(df["High"], df["Low"], c, 14).iloc[-1]
    assert p["volatility"]["atr14"]["value"] == pytest.approx(atr, abs=1e-4)
    assert p["volatility"]["atr14_pct"]["value"] == pytest.approx(atr / c.iloc[-1] * 100, abs=1e-4)
    assert 0 <= p["volatility"]["atr_pctile_252"]["value"] <= 100
    rng = p["range"]
    assert rng["high_52w"]["value"] == pytest.approx(df["High"].tail(252).max(), abs=1e-4)
    assert rng["low_52w"]["value"] == pytest.approx(df["Low"].tail(252).min(), abs=1e-4)
    assert rng["pct_from_high_52w"]["value"] <= 0
    assert rng["max_drawdown_1y_pct"]["value"] == pytest.approx(ind.max_drawdown_pct(c.tail(252)), abs=1e-4)
    peak = c[c.index >= c.index[-1] - pd.DateOffset(years=3)].max()
    assert rng["drawdown_from_3y_peak_pct"]["value"] == pytest.approx((c.iloc[-1] / peak - 1) * 100, abs=1e-4)
    vol = p["volume"]
    assert vol["avg20"]["value"] == pytest.approx(df["Volume"].tail(20).mean(), abs=1)
    tail = df.tail(63)
    assert vol["adv_usd_63d"]["value"] == pytest.approx((tail["Close"] * tail["Volume"]).mean(), rel=1e-6)
    m = ind.macd(c, 12, 26, 9)
    assert p["momentum"]["macd"]["hist"]["value"] == pytest.approx(m["hist"].iloc[-1], abs=1e-4)


def test_regime_tests_consistent_with_regime(full_payload):
    tr = full_payload["trend"]
    groups: dict[str, list[dict]] = {}
    for t in tr["regime_tests"]:
        assert set(t) >= {"regime", "test", "value", "pass"}
        groups.setdefault(t["regime"], []).append(t)
    assert list(groups) == ["choppy", "trend_up", "trend_down", "range"]
    matched = [r for r in groups if ct.regime_matches(groups[r])]
    expected = matched[0] if matched else "transition"
    assert tr["regime"] == expected


def test_levels_and_stops_below_and_above_close(full_payload):
    p = full_payload
    close = p["close"]["value"]
    sup, res = p["levels"]["supports"], p["levels"]["resistances"]
    max_side = common.committee_cfg()["technicals"]["max_levels_per_side"]
    assert sup and res
    assert len(sup) <= max_side and len(res) <= max_side
    assert [s["id"] for s in sup] == [f"S{i}" for i in range(1, len(sup) + 1)]
    assert [r["id"] for r in res] == [f"R{i}" for i in range(1, len(res) + 1)]
    sp = [s["price"]["value"] for s in sup]
    rp = [r["price"]["value"] for r in res]
    assert all(x < close for x in sp) and sp == sorted(sp, reverse=True)      # nearest first
    assert all(x > close for x in rp) and rp == sorted(rp)
    assert all(s["touches"] >= 1 and s["last_date"] <= "2026-10-02" for s in sup + res)
    stops = p["stop_candidates"]
    assert stops
    assert [s["id"] for s in stops] == [f"STOP{i}" for i in range(1, len(stops) + 1)]
    prices = [s["price"]["value"] for s in stops]
    assert all(0 < x < close for x in prices) and prices == sorted(prices, reverse=True)
    assert {s["basis"] for s in stops} <= STOP_BASES
    atr = p["volatility"]["atr14"]["value"]
    by_basis = {s["basis"]: s["price"]["value"] for s in stops}
    assert by_basis["close_minus_2atr"] == pytest.approx(close - 2.0 * atr, abs=1e-3)
    if "below_support_0.5atr" in by_basis:
        assert by_basis["below_support_0.5atr"] == pytest.approx(sp[0] - 0.5 * atr, abs=1e-3)


def test_weekly_closes_last_52_weeks(full_payload):
    wk = full_payload["weekly_closes_52w"]
    assert len(wk) == 52
    dates = [d for d, _ in wk]
    assert dates == sorted(dates) and len(set(dates)) == 52
    assert wk[-1] == ["2026-10-02", pytest.approx(full_payload["close"]["value"], abs=1e-4)]


def test_weekly_closes_use_actual_last_trading_day():
    idx = pd.bdate_range("2026-03-23", "2026-04-10")
    idx = idx[idx != pd.Timestamp("2026-04-03")]          # Good Friday: week ends Thursday
    close = pd.Series(np.arange(len(idx), dtype=float) + 100, index=idx)
    out = ct.weekly_closes(close, 52)
    assert [d for d, _ in out] == ["2026-03-27", "2026-04-02", "2026-04-10"]
    assert ct.weekly_closes(close, 2)[0][0] == "2026-04-02"


# --------------------------------------------------------------------------- regimes
@pytest.mark.parametrize("frame,expected", [
    (lambda: make_ohlcv(n=400, seed=1, drift=0.0025, vol=0.012), "trend_up"),
    (lambda: make_ohlcv(n=400, seed=2, drift=-0.0025, vol=0.015), "trend_down"),
    (lambda: make_ranging_ohlcv(n=300, seed=0), "range"),
    (lambda: make_ranging_ohlcv(n=300, seed=3), "choppy"),
])
def test_regime_on_synthetic_series(frame, expected):
    p = ct.compute(frame(), None, None, ticker="X")
    assert p["trend"]["regime"] == expected
    assert _schema_errors(p) == []


def _vals(**kw):
    base = {"adx": 30.0, "pdi": 30.0, "ndi": 10.0, "close": 110.0, "sma50": 105.0, "sma200": 100.0,
            "sma200_slope_pct": 1.0, "atr_pctile": 50.0, "sma50_crosses": 0, "range_width_pct": 20.0}
    base.update(kw)
    return base


@pytest.mark.parametrize("values,expected", [
    (_vals(), "trend_up"),
    (_vals(pdi=10.0, ndi=30.0, close=90.0, sma50=95.0, sma200=100.0, sma200_slope_pct=-1.0), "trend_down"),
    (_vals(adx=15.0, range_width_pct=8.0), "range"),
    (_vals(adx=15.0, range_width_pct=8.0, sma50_crosses=5, atr_pctile=90.0), "choppy"),   # choppy first
    (_vals(sma200_slope_pct=-0.5), "transition"),                                         # slope blocks trend_up
    (_vals(pdi=10.0, ndi=30.0, close=90.0, sma50=95.0, sma200=100.0, sma200_slope_pct=0.5), "transition"),
    (_vals(adx=22.0), "transition"),
    (_vals(sma200=None, sma200_slope_pct=None), "trend_up"),                              # <200 bars: skipped
    (_vals(sma200=None, sma200_slope_pct=None, adx=22.0), "transition"),                  # core ADX still gates
    (_vals(sma200=None, sma200_slope_pct=None, close=100.0), "transition"),               # close <= sma50 blocks
    (_vals(pdi=10.0, ndi=30.0, close=90.0, sma50=95.0, sma200=None, sma200_slope_pct=None), "trend_down"),
    (_vals(adx=None, pdi=None, ndi=None), "transition"),                                  # core null blocks
    (_vals(adx=15.0, sma50_crosses=5, atr_pctile=None), "choppy"),                        # atr_pctile skipped
    (_vals(adx=15.0, sma50_crosses=None, atr_pctile=None, range_width_pct=8.0), "range"), # crosses core
    (_vals(sma200=None, sma200_slope_pct=None, adx=12.0, range_width_pct=5.0), "range"),
])
def test_classify_regime_cascade(values, expected):
    regime, tests = ct.classify_regime(values, common.committee_cfg()["technicals"]["regime"])
    assert regime == expected
    if values["sma200"] is None:
        skipped = [t for t in tests if "sma200" in t["test"] and t["regime"].startswith("trend")]
        assert skipped and all(t["pass"] is None and t["skipped"] and not t["core"] for t in skipped)
    for t in tests:
        assert t["skipped"] == (t["pass"] is None and not t["core"])


def test_classify_regime_reads_thresholds():
    vals = _vals(adx=22.0)
    assert ct.classify_regime(vals, {"adx_trend_min": 25})[0] == "transition"
    assert ct.classify_regime(vals, {"adx_trend_min": 20})[0] == "trend_up"


# --------------------------------------------------------------------------- short history
def test_fewer_than_200_bars():
    df = make_ohlcv(n=150, seed=4)
    p = ct.compute(df, make_ohlcv(n=150, seed=5), None, ticker="NEWCO")
    assert _schema_errors(p) == []
    tr = p["trend"]
    assert tr["sma200"]["value"] is None and tr["sma200"]["na_reason"] == "insufficient_history"
    assert tr["sma200_slope_20d_pct"]["value"] is None
    assert tr["close_vs_sma200_pct"]["value"] is None
    assert tr["sma50"]["value"] is not None
    assert _codes(p)["insufficient_history"] == "medium"
    assert _codes(p)["regime_tests_skipped"] == "low"
    skipped = {x["test"] for x in tr["regime_tests"] if x["skipped"]}
    assert {"sma50 > sma200", "sma50 < sma200"} <= skipped
    assert any("SMA200" in m for m in p["missing_data"])
    assert p["range"]["high_52w"]["value"] is None
    assert p["range"]["high_52w"]["na_reason"] == "insufficient_history"
    assert p["volatility"]["atr_pctile_252"]["value"] is None
    assert p["relative_strength"]["vs_spy"]["12m"]["value"] is None
    assert p["relative_strength"]["vs_spy"]["3m"]["value"] is not None
    assert all(s["basis"] != "below_sma200" for s in p["stop_candidates"])


def test_short_history_strong_uptrend_is_trend_up():
    """DESIGN §4.2 'regime from the available tests': a recent IPO in a clear uptrend is
    trend_up even though the SMA200 tests cannot be evaluated."""
    p = ct.compute(make_ohlcv(n=150, seed=5, drift=0.006, vol=0.006), make_ohlcv(800, seed=1), None,
                   ticker="NEW", as_of="2026-10-02")
    assert _schema_errors(p) == []
    tr = p["trend"]
    assert tr["regime"] == "trend_up"
    up = [x for x in tr["regime_tests"] if x["regime"] == "trend_up"]
    assert all(x["pass"] is True for x in up if x["core"])
    assert all(x["skipped"] for x in up if not x["core"])
    assert "SMA200 tests" in next(f["detail"] for f in p["flags"] if f["code"] == "insufficient_history")
    # and a strong downtrend with short history is trend_down
    d = ct.compute(make_ohlcv(n=150, seed=5, drift=-0.006, vol=0.006), None, None, ticker="DN")
    assert d["trend"]["regime"] == "trend_down"


def test_atr_pctile_null_between_200_and_265_bars_is_flagged():
    p = ct.compute(make_ohlcv(n=230, seed=6), None, None, ticker="X")
    assert p["volatility"]["atr_pctile_252"]["value"] is None
    assert "insufficient_history" not in _codes(p)
    assert _codes(p)["regime_tests_skipped"] == "low"
    sk = [x for x in p["trend"]["regime_tests"] if x["skipped"]]
    assert [x["regime"] for x in sk] == ["choppy"] and "atr_pctile" in sk[0]["test"]
    full = ct.compute(make_ohlcv(n=400, seed=6), None, None, ticker="X")
    assert "regime_tests_skipped" not in _codes(full)


def test_flat_series_adx_is_zero_not_insufficient_history():
    df = make_ohlcv(n=300, seed=2)
    for col in ("Open", "High", "Low", "Close"):
        df[col] = 10.0
    p = ct.compute(df, None, None, ticker="FLAT")
    assert _schema_errors(p) == []
    mom = p["momentum"]
    for k in ("adx14", "pdi14", "ndi14"):
        assert mom[k]["value"] == 0.0 and mom[k]["na_reason"] is None, k
    assert mom["rsi14"]["value"] == pytest.approx(50.0)


def test_52w_range_with_200_to_251_bars_uses_available_and_flags():
    df = make_ohlcv(n=220, seed=6)
    p = ct.compute(df, None, None, ticker="X")
    assert p["range"]["high_52w"]["value"] == pytest.approx(df["High"].max(), abs=1e-4)
    assert _codes(p)["short_52w_window"] == "low"
    assert p["trend"]["sma200"]["value"] is not None


def test_empty_input_degrades_to_nulls():
    p = ct.compute(None, None, None, ticker="ZZZZ", sector_etf="XLK")
    assert _schema_errors(p) == []
    assert p["bars"] == 0 and p["close"]["value"] is None and p["close"]["na_reason"] == "missing"
    assert p["trend"]["regime"] == "transition" and p["trend"]["structure"] == "unknown"
    assert p["levels"] == {"supports": [], "resistances": []}
    assert p["stop_candidates"] == [] and p["weekly_closes_52w"] == []
    assert _codes(p)["no_price_data"] == "high"


# --------------------------------------------------------------------------- relative strength
def test_relative_strength_on_date_aligned_series():
    df = make_ohlcv(n=400, seed=10)
    bench = make_ohlcv(n=400, seed=11)
    # benchmark misses 30 random dates of the ticker and carries 10 extra weekend-ish dates
    rng = np.random.default_rng(0)
    drop = rng.choice(bench.index[:-5], size=30, replace=False)
    bench = bench.drop(index=drop)
    weekend = pd.DatetimeIndex(["2025-12-27", "2025-12-28", "2026-01-03", "2026-01-04", "2026-03-07",
                                "2026-03-08", "2026-06-06", "2026-06-07", "2026-09-26", "2026-09-27"], name="Date")
    extra = pd.DataFrame({c: 1.0 for c in common.OHLCV_COLUMNS}, index=weekend)
    extra["Close"] = np.linspace(500.0, 900.0, len(weekend))   # wild values that must not be used
    bench = pd.concat([bench, extra]).sort_index()
    p = ct.compute(df, bench, None, ticker="KO")
    aligned = pd.concat([df["Close"].rename("a"), bench["Close"].rename("b")], axis=1, join="inner").dropna()
    for label, bars in {"3m": 63, "6m": 126, "12m": 252}.items():
        a, b = aligned["a"], aligned["b"]
        expected = ((a.iloc[-1] / a.iloc[-1 - bars]) / (b.iloc[-1] / b.iloc[-1 - bars]) - 1) * 100
        fact = p["relative_strength"]["vs_spy"][label]
        assert fact["value"] == pytest.approx(expected, abs=1e-3)
        assert fact["source_id"] == "D:PX:SPY" and fact["unit"] == "pp"
        assert fact["derivation"].startswith("(1+r_KO)/(1+r_SPY)-1")
    # no sector ETF configured -> not applicable
    vs = p["relative_strength"]["vs_sector"]["6m"]
    assert vs["value"] is None and vs["na_reason"] == "not_applicable"
    assert _schema_errors(p) == []


def test_relative_strength_lagging_benchmark_is_flagged():
    df = make_ohlcv(n=300, seed=13)
    bench = make_ohlcv(n=300, seed=14).iloc[:-2]          # benchmark ends two bars earlier
    p = ct.compute(df, bench, None, ticker="KO")
    fact = p["relative_strength"]["vs_spy"]["3m"]
    assert fact["as_of"] == common.date_str(bench.index[-1])
    assert _codes(p)["rs_alignment_lag"] == "low"


def test_missing_benchmark_is_flagged():
    p = ct.compute(make_ohlcv(n=300, seed=15), None, make_ohlcv(n=300, seed=16), ticker="KO", sector_etf="XLP")
    assert p["relative_strength"]["vs_spy"]["3m"]["value"] is None
    assert p["relative_strength"]["vs_spy"]["3m"]["na_reason"] == "missing"
    assert p["relative_strength"]["vs_sector"]["3m"]["value"] is not None
    assert _codes(p)["benchmark_unavailable"] == "medium"
    assert [s["id"] for s in p["sources"]] == ["D:PX:KO", "D:PX:XLP"]


# --------------------------------------------------------------------------- volume
@pytest.mark.parametrize("recent,expected", [(2.0, "rising"), (0.5, "falling"), (1.0, "flat")])
def test_volume_trend(recent, expected):
    df = make_ohlcv(n=120, seed=17)
    df["Volume"] = 1_000_000.0
    df.iloc[-20:, df.columns.get_loc("Volume")] = 1_000_000.0 * recent
    p = ct.compute(df, None, None, ticker="X")
    v = p["volume"]
    assert v["trend"] == expected
    assert v["ratio_20_50"]["value"] == pytest.approx(recent / ((20 * recent + 30) / 50), abs=1e-4)


def test_volume_thresholds_from_config(tmp_env):
    def tweak(cfg):
        cfg["technicals"]["volume_rising_ratio"] = 1.5
        cfg["technicals"]["max_levels_per_side"] = 1

    tmp_env.update_config("committee", tweak)
    df = make_ohlcv(n=300, seed=17)
    df["Volume"] = 1_000_000.0
    df.iloc[-20:, df.columns.get_loc("Volume")] = 1_300_000.0     # ratio ~1.19: rising at 1.1, flat at 1.5
    p = ct.compute(df, None, None, ticker="X")
    assert p["volume"]["trend"] == "flat"
    assert len(p["levels"]["supports"]) <= 1 and len(p["levels"]["resistances"]) <= 1


# --------------------------------------------------------------------------- as_of
def test_compute_truncates_to_as_of():
    df = make_ohlcv(n=300, seed=18, end="2026-10-09")
    p = ct.compute(df, None, None, ticker="X", as_of="2026-10-02")
    assert p["last_date"] == "2026-10-02" and p["bars"] == 295
    assert _codes(p)["truncated_to_as_of"] == "low"
    assert p["as_of"] == "2026-10-02"


# --------------------------------------------------------------------------- run() / CLI
def test_run_fixture_mode_writes_valid_file(use_fixtures, tmp_path):
    use_fixtures("prices")
    out = tmp_path / "phase2" / "data" / "technicals_KO.json"
    payload = ct.run("KO", out, sector_etf="XLP")
    assert out.is_file()
    doc = common.read_json(out)
    assert _schema_errors(doc) == []
    assert doc == json.loads(common.dumps_json(payload))
    assert [s["id"] for s in doc["sources"]] == ["D:PX:KO", "D:PX:SPY", "D:PX:XLP"]
    assert all(s["path"].endswith(".csv") for s in doc["sources"])
    assert doc["price_providers"] == {"KO": "yfinance", "SPY": "yfinance", "XLP": "yfinance"}
    assert doc["bars"] == 300
    assert doc["relative_strength"]["vs_sector"]["12m"]["value"] is not None
    # series landed in the price cache for later readers
    assert fetch_prices.load_series("XLP") is not None


def test_run_reads_cache_first_and_resolves_sector_etf(use_fixtures, monkeypatch):
    use_fixtures("prices")
    fetch_prices.run(["KO", "SPY", "XLP"], include_fx=False)

    def boom(*a, **k):
        raise AssertionError("network seam must not be called")

    monkeypatch.setattr(common, "yf_history", boom)
    monkeypatch.setattr(common, "http_get", boom)
    p = ct.run("KO", None, sector="consumer_staples")
    assert p["sector_etf"] == "XLP"
    assert p["bars"] == 300 and _schema_errors(p) == []


def test_run_offline_stale_cache_propagates_flag():
    for t, seed in (("KO", 20), ("SPY", 21), ("XLP", 22)):
        _seed_cache(t, make_ohlcv(n=300, seed=seed), hours_old=72)
    p = ct.run("KO", None, sector_etf="XLP")
    assert p["bars"] == 300
    assert _codes(p)["stale_cache"] == "high"
    assert p["price_providers"]["KO"] == "cache_stale"
    assert _schema_errors(p) == []


def test_run_without_any_data_writes_degraded_file(tmp_path):
    out = tmp_path / "technicals_ZZZZ.json"
    p = ct.run("ZZZZ", out, sector_etf="XLK")
    assert out.is_file()
    assert p["bars"] == 0 and p["sources"] == []
    assert _codes(p)["no_price_data"] == "high"
    assert p["price_providers"] == {"ZZZZ": "failed", "SPY": "failed", "XLK": "failed"}
    assert _schema_errors(common.read_json(out)) == []


def test_cli_main_fixture_mode(use_fixtures, tmp_path, capsys):
    use_fixtures("prices")
    out = tmp_path / "technicals_KO.json"
    rc = ct.main(["--ticker", "KO", "--sector-etf", "XLP", "--out", str(out)])
    assert rc == 0
    doc = common.read_json(out)
    assert _schema_errors(doc) == []
    summary = json.loads(capsys.readouterr().out)
    assert summary["ticker"] == "KO" and summary["bars"] == 300
    assert summary["regime"] == doc["trend"]["regime"]


def test_cli_no_data_returns_1(tmp_path, capsys):
    rc = ct.main(["--ticker", "ZZZZ", "--out", str(tmp_path / "t.json")])
    assert rc == 1
    assert Path(tmp_path / "t.json").is_file()
    capsys.readouterr()


# --------------------------------------------------------------------------- float32 noise in price Facts
def _noisy_float32(df: pd.DataFrame) -> pd.DataFrame:
    """The frame as yfinance delivers it: OHLC through float32 (769.64 -> 769.6400146484375)."""
    out = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col].astype("float32").astype("float64")
    return out


def _scaled_to_last(df: pd.DataFrame, last_close: float) -> pd.DataFrame:
    out = df.copy()
    k = last_close / out["Close"].iloc[-1]
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col] * k
    out.loc[out.index[-1], "Close"] = last_close
    out.loc[out.index[-1], "High"] = max(out["High"].iloc[-1], last_close)
    out.loc[out.index[-1], "Low"] = min(out["Low"].iloc[-1], last_close)
    return out


@pytest.mark.parametrize("last", [769.64, 1500.12, 4321.55, 98.76])
def test_raw_close_facts_have_no_float32_noise(last):
    df = _noisy_float32(_scaled_to_last(make_ohlcv(n=400, seed=5, start=700.0), last))
    assert float(df["Close"].iloc[-1]) != last  # the input is genuinely noisy (e.g. 769.6400146484375)
    p = ct.compute(df, ticker="KO", as_of="2026-10-02", cfg=common.committee_cfg())
    assert p["close"]["value"] == last
    assert p["weekly_closes_52w"][-1] == ["2026-10-02", last]
    for key in ("high_52w", "low_52w"):
        v = p["range"][key]["value"]
        assert v == common.round_price(v) and len(repr(v)) <= 10, (key, v)
    # the raw 52w extremes are quoted levels: High/Low of the bars, cleaned of the float32 tail
    assert p["range"]["high_52w"]["value"] == round(common.round_price(float(df["High"].tail(252).max())), 4)
    assert p["range"]["low_52w"]["value"] == round(common.round_price(float(df["Low"].tail(252).min())), 4)
    for t in p["trend"]["regime_tests"]:
        for k in ("value", "threshold"):
            if t[k] is not None:
                assert t[k] == common.round_price(t[k], dp=4), (t["test"], t[k])


def test_price_rounding_does_not_change_indicator_math():
    """Only the output of raw closes is cleaned: every computed figure is exactly what it was."""
    df = make_ohlcv(n=500, seed=21, start=300.0)
    noisy = _noisy_float32(df)
    p = ct.compute(noisy, ticker="KO", as_of="2026-10-02", cfg=common.committee_cfg())
    c = noisy["Close"]
    assert p["trend"]["sma20"]["value"] == round(float(c.tail(20).mean()), 4)
    assert p["trend"]["sma50"]["value"] == round(float(c.tail(50).mean()), 4)
    assert p["trend"]["sma200"]["value"] == round(float(c.tail(200).mean()), 4)
    assert p["momentum"]["rsi14"]["value"] == round(float(ind.rsi(c, 14).iloc[-1]), 4)
    assert p["volatility"]["atr14"]["value"] == round(float(ind.atr(noisy["High"], noisy["Low"], c, 14).iloc[-1]), 4)
    assert p["momentum"]["macd"]["hist"]["value"] == round(float(ind.macd(c, 12, 26, 9)["hist"].iloc[-1]), 4)
    assert p["range"]["max_drawdown_1y_pct"]["value"] == round(float(ind.max_drawdown_pct(c.tail(252))), 4)
    # closes are cleaned but not otherwise altered
    assert p["close"]["value"] == pytest.approx(float(c.iloc[-1]), abs=1e-4)
    assert [v for _, v in p["weekly_closes_52w"]] == [
        round(common.round_price(float(x)), 4) for x in c.groupby(c.index.to_period("W-FRI")).tail(1).tail(52)]


def test_px_helper():
    assert ct._px(769.6400146484375) == 769.64
    assert ct._px(None) is None and ct._px(float("nan")) is None
    assert ct._px(1500.1199951171875) == 1500.12
    assert ct._px(0.123456, 4) == 0.1235


def test_cli_fixture_mode_with_real_cache_dir_is_refused(use_fixtures, tmp_path, monkeypatch, capsys):
    use_fixtures("prices")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", root)
    monkeypatch.delenv(common.ENV_CACHE_DIR)
    assert ct.main(["--ticker", "KO"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "real cache dir" in err and "Traceback" not in err
    assert not (root / "data").exists()
