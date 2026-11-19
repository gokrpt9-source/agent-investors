"""Indicator math (scripts/_indicators.py) and pivots/levels/stops (scripts/_levels.py)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from conftest import make_ohlcv
from scripts import _indicators as ind
from scripts import _levels as lv


# =====================================================================================
# moving averages
# =====================================================================================


def test_sma_needs_full_window():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.tolist()[2:] == [2.0, 3.0, 4.0]
    assert ind.sma(s, 10).isna().all()


def test_sma200_requires_200_bars():
    df = make_ohlcv(n=199)
    assert ind.sma(df["Close"], 200).isna().all()
    df = make_ohlcv(n=200)
    assert not math.isnan(ind.sma(df["Close"], 200).iloc[-1])


def test_rma_seeded_with_sma_then_wilder_recursion():
    out = ind.rma(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)                 # mean(1,2,3)
    assert out.iloc[3] == pytest.approx((2.0 * 2 + 4) / 3)   # 8/3
    assert out.iloc[4] == pytest.approx((8 / 3 * 2 + 5) / 3)


def test_rma_skips_leading_nan_and_short_input():
    out = ind.rma(pd.Series([np.nan, 3.0, 3.0, 3.0, 6.0]), 3)
    assert out.iloc[:3].isna().all()
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)
    assert ind.rma(pd.Series([1.0, 2.0]), 3).isna().all()


# =====================================================================================
# RSI
# =====================================================================================


def test_rsi_rising_series_is_100():
    r = ind.rsi(pd.Series(np.linspace(100, 130, 40)), 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14:].eq(100.0).all()


def test_rsi_flat_series_is_50():
    r = ind.rsi(pd.Series(np.full(40, 50.0)), 14)
    assert r.iloc[14:].eq(50.0).all()


def test_rsi_falling_series_is_0():
    r = ind.rsi(pd.Series(np.linspace(130, 100, 40)), 14)
    assert r.iloc[14:].eq(0.0).all()


def test_rsi_hand_computed_wilder_case():
    # deltas +1, -0.5, +1, -0.5, +1 ; n = 3
    r = ind.rsi(pd.Series([10.0, 11.0, 10.5, 11.5, 11.0, 12.0]), 3)
    assert r.iloc[:3].isna().all()
    # seed: avg_gain = 2/3, avg_loss = 1/6 -> RS 4 -> 80
    assert r.iloc[3] == pytest.approx(80.0)
    # avg_gain = 4/9, avg_loss = 5/18 -> RS 1.6
    assert r.iloc[4] == pytest.approx(100 - 100 / 2.6)
    # avg_gain = 17/27, avg_loss = 5/27 -> RS 3.4
    assert r.iloc[5] == pytest.approx(100 - 100 / 4.4)


def test_rsi_bounds_on_random_walk():
    r = ind.rsi(make_ohlcv(n=300, seed=5)["Close"], 14).dropna()
    assert len(r) == 300 - 14
    assert ((r >= 0) & (r <= 100)).all()


# =====================================================================================
# MACD / ATR / ADX
# =====================================================================================


def test_macd_flat_series_is_zero_and_masks_warmup():
    m = ind.macd(pd.Series(np.full(80, 10.0)), 12, 26, 9)
    assert m["line"].iloc[:25].isna().all()
    assert m["signal"].iloc[:33].isna().all()
    assert m["line"].iloc[25:].abs().max() == pytest.approx(0.0)
    assert m["hist"].iloc[33:].abs().max() == pytest.approx(0.0)


def test_macd_linear_uptrend_positive_and_converges():
    m = ind.macd(pd.Series(np.linspace(100, 200, 400)), 12, 26, 9)
    line, sig, hist = m["line"].iloc[-1], m["signal"].iloc[-1], m["hist"].iloc[-1]
    assert line > 0 and sig > 0
    # a linear trend has a constant EMA spread: (26-12)/2 x step
    step = 100 / 399
    assert line == pytest.approx((26 - 12) / 2 * step, rel=1e-3)
    assert abs(hist) < 1e-6
    falling = ind.macd(pd.Series(np.linspace(200, 100, 400)), 12, 26, 9)
    assert falling["line"].iloc[-1] < 0


def test_atr_constant_range_and_gap():
    n = 40
    closes = np.full(n, 100.0)
    df = pd.DataFrame({"High": closes + 1, "Low": closes - 1, "Close": closes})
    a = ind.atr(df["High"], df["Low"], df["Close"], 14)
    assert a.iloc[:13].isna().all()
    assert a.iloc[13:].sub(2.0).abs().max() < 1e-12
    # true range uses the previous close on gaps
    tr = ind.true_range(pd.Series([10.0, 15.0]), pd.Series([9.0, 14.0]), pd.Series([9.5, 14.5]))
    assert tr.tolist() == [1.0, 5.5]


def test_atr_positive_on_random_walk():
    df = make_ohlcv(n=200, seed=6)
    a = ind.atr(df["High"], df["Low"], df["Close"], 14).dropna()
    assert (a > 0).all()


def test_adx_directional_on_trends():
    up = make_ohlcv(n=300, seed=1, drift=0.004, vol=0.008)
    d = ind.adx(up["High"], up["Low"], up["Close"], 14)
    assert d["adx"].iloc[:27].isna().all()           # first ADX at bar 2n-1 = 27
    assert not math.isnan(d["adx"].iloc[27])
    assert d["pdi"].iloc[-1] > d["ndi"].iloc[-1]
    assert d["adx"].iloc[-1] > 25
    down = make_ohlcv(n=300, seed=2, drift=-0.004, vol=0.008)
    dd = ind.adx(down["High"], down["Low"], down["Close"], 14)
    assert dd["ndi"].iloc[-1] > dd["pdi"].iloc[-1]
    assert 0 <= dd["adx"].dropna().min() and dd["adx"].dropna().max() <= 100


def test_adx_flat_series_is_zero_not_null():
    """Zero true range from the first bar: no directional movement -> DI/DX/ADX are 0 (same
    warm-up masking as a normal series), never NaN."""
    flat = pd.Series(10.0, index=range(300))
    d = ind.adx(flat, flat, flat, 14)
    assert d["pdi"].iloc[:14].isna().all() and d["adx"].iloc[:27].isna().all()
    assert d["pdi"].iloc[14:].eq(0.0).all() and d["ndi"].iloc[14:].eq(0.0).all()
    assert d["adx"].iloc[27:].eq(0.0).all()


def test_adx_flat_then_moving_recovers():
    vals = [10.0] * 60 + list(np.linspace(10.0, 20.0, 60))
    s = pd.Series(vals)
    d = ind.adx(s + 0.1, s - 0.1, s, 14)
    assert not d["adx"].iloc[27:].isna().any()
    assert d["pdi"].iloc[-1] > d["ndi"].iloc[-1] and d["adx"].iloc[-1] > 25


# =====================================================================================
# trailing percentile (no look-ahead)
# =====================================================================================


def test_trailing_percentile_increasing_series_is_100():
    p = ind.trailing_percentile(pd.Series(np.arange(1.0, 31.0)), 10)
    assert p.iloc[:9].isna().all()
    assert p.iloc[9:].eq(100.0).all()


def test_trailing_percentile_known_value_and_no_look_ahead():
    s = pd.Series([5.0, 1.0, 3.0, 2.0])
    p = ind.trailing_percentile(s, 4)
    assert p.iloc[-1] == pytest.approx(50.0)        # 2 of 4 values <= 2
    rng = np.random.default_rng(7)
    full = pd.Series(rng.normal(size=400))
    on_full = ind.trailing_percentile(full, 50)
    on_prefix = ind.trailing_percentile(full.iloc[:300], 50)
    # appending future bars never changes past percentiles
    pd.testing.assert_series_equal(on_full.iloc[:300], on_prefix)


# =====================================================================================
# returns / drawdowns / RS
# =====================================================================================


def test_drawdowns_known_values():
    s = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert ind.max_drawdown_pct(s) == pytest.approx(-25.0)
    assert ind.drawdown_from_peak_pct(s) == pytest.approx((110 / 120 - 1) * 100)
    assert ind.max_drawdown_pct(pd.Series([1.0, 2.0, 3.0])) == pytest.approx(0.0)


def test_pct_change_and_slope():
    s = pd.Series([100.0, 105.0, 110.0])
    assert ind.pct_change_over(s, 2) == pytest.approx(10.0)
    assert ind.pct_change_over(s, 3) is None
    assert ind.slope_pct(pd.Series([np.nan, 100.0, 102.0]), 1) == pytest.approx(2.0)
    assert ind.slope_pct(pd.Series([np.nan, 100.0, 102.0]), 2) is None


def test_relative_strength_uses_inner_joined_dates():
    a = pd.Series([100.0, 110.0, 121.0, 133.1], index=pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]))
    # benchmark lacks 01-07 and has an extra 01-09
    b = pd.Series([50.0, 50.0, 55.0, 60.0], index=pd.to_datetime(
        ["2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09"]))
    res = ind.relative_strength_pct(a, b, 2)
    assert res["aligned_rows"] == 3
    assert res["end"] == pd.Timestamp("2026-01-08")
    assert res["start"] == pd.Timestamp("2026-01-05")
    expected = ((133.1 / 100.0) / (55.0 / 50.0) - 1) * 100
    assert res["value"] == pytest.approx(expected)
    assert ind.relative_strength_pct(a, b, 3)["value"] is None


def test_sma_crossings_and_range_width():
    close = pd.Series([1.0, 3.0, 1.0, 3.0, 3.0])
    ma = pd.Series([2.0] * 5)
    assert ind.sma_crossings(close, ma) == 3
    assert ind.sma_crossings(close, pd.Series([2.0, np.nan, 2, 2, 2])) is None
    assert ind.range_width_pct(pd.Series([110.0, 105.0]), pd.Series([90.0, 95.0])) == pytest.approx(20.0)


# =====================================================================================
# pivots, structure, levels, stops
# =====================================================================================


def test_swing_pivots_detects_unique_extremes():
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 12, 13, 12, 11, 10]
    lows = [h - 2 for h in highs]
    df = pd.DataFrame({"High": highs, "Low": lows}, index=pd.bdate_range("2026-01-05", periods=len(highs)),
                      dtype="float64")
    piv = lv.swing_pivots(df, 3, 3, lookback=50)
    assert [p for _, p in piv["highs"]] == [15.0, 13.0]
    assert [p for _, p in piv["lows"]] == [8.0]
    assert piv["highs"][0][0] == df.index[3]


def test_swing_pivots_reject_tied_extremes():
    highs = [10, 11, 12, 15, 15, 12, 11, 10, 9, 8]
    df = pd.DataFrame({"High": highs, "Low": [h - 1 for h in highs]},
                      index=pd.bdate_range("2026-01-05", periods=len(highs)), dtype="float64")
    piv = lv.swing_pivots(df, 3, 3, lookback=50)
    assert piv["highs"] == []


def test_swing_pivots_within_bounds_on_random_data():
    df = make_ohlcv(n=300, seed=11)
    piv = lv.swing_pivots(df, 3, 3, lookback=250)
    lo, hi = df["Low"].min(), df["High"].max()
    assert piv["highs"] and piv["lows"]
    assert all(lo <= p <= hi for _, p in piv["highs"] + piv["lows"])
    # the last `right` bars can never be pivots (unconfirmed)
    assert all(d <= df.index[-4] for d, _ in piv["highs"] + piv["lows"])


@pytest.mark.parametrize("highs,lows,label", [
    ([10, 12], [5, 6], "HH_HL"),
    ([12, 10], [6, 5], "LH_LL"),
    ([10, 12], [6, 5], "transition"),
    ([10], [5, 6], "unknown"),
])
def test_structure_label(highs, lows, label):
    assert lv.structure_label({"last_two_highs": highs, "last_two_lows": lows}) == label


def test_cluster_levels_merges_within_tolerance():
    d = pd.to_datetime(["2026-01-05", "2026-02-05", "2026-03-05", "2026-04-06", "2026-05-05"])
    piv = {"highs": [(d[0], 100.0), (d[2], 100.4), (d[4], 110.0)], "lows": [(d[1], 99.8), (d[3], 90.0)]}
    levels = lv.cluster_levels(piv, tolerance=0.5)
    assert [lvl["touches"] for lvl in levels] == [1, 3, 1]
    mid = levels[1]
    assert mid["price"] == pytest.approx((100.0 + 100.4 + 99.8) / 3)
    assert mid["last_date"] == d[2]
    assert mid["kinds"] == {"high": 2, "low": 1}
    sup, res = lv.split_levels(levels, close=105.0, max_per_side=3)
    assert [s["id"] for s in sup] == ["S1", "S2"]
    assert sup[0]["price"] == pytest.approx(mid["price"]) and sup[1]["price"] == 90.0
    assert [r["id"] for r in res] == ["R1"] and res[0]["price"] == 110.0
    sup1, _ = lv.split_levels(levels, close=105.0, max_per_side=1)
    assert len(sup1) == 1


def test_stop_candidates_below_close_sorted_descending():
    supports = [{"id": "S1", "price": 95.0}, {"id": "S2", "price": 90.0}]
    stops = lv.stop_candidates(100.0, 2.0, supports, 92.0, support_buffer_atr=0.5, atr_multiple=2.0,
                               sma200_buffer_atr=0.5)
    # S1 - 0.5 x 2 = 94 ; close - 2 x 2 = 96 ; SMA200 - 0.5 x 2 = 91 -> descending by price
    assert [s["id"] for s in stops] == ["STOP1", "STOP2", "STOP3"]
    assert [s["price"] for s in stops] == pytest.approx([96.0, 94.0, 91.0])
    assert [s["basis"] for s in stops] == ["close_minus_2atr", "below_support_0.5atr", "below_sma200"]
    assert [s["ref"] for s in stops] == ["close", "S1", "SMA200"]
    assert all(s["price"] < 100.0 for s in stops)


def test_stop_candidates_drop_sma200_above_close_and_need_atr():
    stops = lv.stop_candidates(100.0, 2.0, [], 105.0, support_buffer_atr=0.5, atr_multiple=2.0,
                               sma200_buffer_atr=0.5)
    assert [(s["id"], s["basis"], s["price"]) for s in stops] == [("STOP1", "close_minus_2atr", 96.0)]
    assert lv.stop_candidates(100.0, None, [], 90.0, support_buffer_atr=0.5, atr_multiple=2.0,
                              sma200_buffer_atr=0.5) == []
    # a huge ATR pushes every candidate to <= 0 -> dropped
    assert lv.stop_candidates(1.0, 5.0, [{"id": "S1", "price": 0.9}], None, support_buffer_atr=0.5,
                              atr_multiple=2.0, sma200_buffer_atr=0.5) == []
