"""Pure pandas/numpy indicator math for compute_technicals.py (DESIGN §4.2, §8).

Ported from stock_dashboard/indicators/technical.py (copied, never imported) with the fixes
DESIGN §8 asks for:

* Wilder smoothing (``rma``) is seeded with the simple mean of the first ``n`` valid values
  (classic Wilder) instead of an un-seeded ``ewm(alpha=1/n)``; RSI therefore starts from the
  SMA of the first ``n`` deltas, and a flat series reads 50 (not 100).
* ``sma(.., 200)`` needs 200 bars (``min_periods == n``), never a partial window.
* ATR percentile is a trailing-window percentile (no look-ahead): bar ``t`` is ranked only
  against bars ``t-window+1 .. t``.

Every function takes/returns pandas objects indexed like the input and never raises on short
history: values that cannot be computed are NaN. No IO, no config access - callers pass the
periods (from ``committee.yaml: technicals``).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "sma", "rma", "ema", "rsi", "macd", "true_range", "atr", "adx", "trailing_percentile",
    "pct_change_over", "drawdown_from_peak_pct", "max_drawdown_pct", "sma_crossings",
    "range_width_pct", "relative_strength_pct", "align_closes", "last_value", "slope_pct",
]


def _series(s: Any) -> pd.Series:
    """Float64 Series view of ``s`` (lists/arrays get a RangeIndex)."""
    if isinstance(s, pd.Series):
        return s.astype("float64")
    return pd.Series(np.asarray(s, dtype="float64"))


# =====================================================================================
# Moving averages
# =====================================================================================


def sma(s: Any, n: int, min_periods: int | None = None) -> pd.Series:
    """Simple moving average; a value needs ``min_periods`` (default ``n``) observations."""
    x = _series(s)
    n = int(n)
    return x.rolling(n, min_periods=int(min_periods or n)).mean()


def ema(s: Any, span: int) -> pd.Series:
    """Exponential moving average ``ewm(span, adjust=False)`` (standard MACD smoothing)."""
    return _series(s).ewm(span=int(span), adjust=False).mean()


def rma(s: Any, n: int) -> pd.Series:
    """Wilder smoothing (RMA), seeded with the simple mean of the first ``n`` valid values.

    ``out[f+n-1] = mean(x[f .. f+n-1])`` where ``f`` is the first non-NaN position, then
    ``out[i] = (out[i-1]·(n-1) + x[i]) / n``. Earlier positions are NaN. A NaN input after
    the seed carries the previous average forward. Fewer than ``n`` valid values -> all NaN.
    """
    x = _series(s)
    vals = x.to_numpy(dtype="float64")
    n = int(n)
    out = np.full(len(vals), np.nan)
    if n <= 0:
        raise ValueError("rma period must be positive")
    valid = np.flatnonzero(~np.isnan(vals))
    if len(valid) < n:
        return pd.Series(out, index=x.index)
    first = int(valid[0])
    seed_end = first + n - 1
    if seed_end >= len(vals):
        return pd.Series(out, index=x.index)
    window = vals[first:seed_end + 1]
    if np.isnan(window).any():
        # gaps inside the seed window: seed on the first n valid values instead
        seed_end = int(valid[n - 1])
        window = vals[valid[:n]]
    prev = float(np.mean(window))
    out[seed_end] = prev
    for i in range(seed_end + 1, len(vals)):
        v = vals[i]
        if not np.isnan(v):
            prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return pd.Series(out, index=x.index)


# =====================================================================================
# Momentum
# =====================================================================================


def rsi(close: Any, n: int = 14) -> pd.Series:
    """Wilder RSI. Average gain/loss are seeded with the SMA of the first ``n`` deltas.

    Both averages zero (flat series) -> 50; only losses zero -> 100; only gains zero -> 0.
    The first ``n`` positions are NaN.
    """
    c = _series(close)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, n)
    avg_loss = rma(loss, n)
    g = avg_gain.to_numpy(dtype="float64")
    lo = avg_loss.to_numpy(dtype="float64")
    out = np.full(len(g), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        both = ~np.isnan(g) & ~np.isnan(lo)
        normal = both & (lo > 0)
        out[normal] = 100.0 - 100.0 / (1.0 + g[normal] / lo[normal])
        out[both & (lo == 0) & (g > 0)] = 100.0
        out[both & (lo == 0) & (g == 0)] = 50.0
    return pd.Series(out, index=c.index)


def macd(close: Any, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line (EMA fast − EMA slow), signal (EMA of the line) and histogram.

    EMAs use ``adjust=False``; the line is NaN for the first ``slow-1`` bars and the
    signal/hist for the first ``slow+signal-2`` bars (not enough history to be meaningful).
    """
    c = _series(close)
    line = ema(c, fast) - ema(c, slow)
    line = line.where(np.arange(len(c)) >= int(slow) - 1)
    sig = line.ewm(span=int(signal), adjust=False).mean()
    sig = sig.where(np.arange(len(c)) >= int(slow) + int(signal) - 2)
    hist = line - sig
    return pd.DataFrame({"line": line, "signal": sig, "hist": hist}, index=c.index)


# =====================================================================================
# Volatility / trend strength
# =====================================================================================


def true_range(high: Any, low: Any, close: Any) -> pd.Series:
    """max(H−L, |H−C₋₁|, |L−C₋₁|); the first bar (no previous close) is H−L."""
    h, lo, c = _series(high), _series(low), _series(close)
    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1, skipna=True)
    return tr.astype("float64")


def atr(high: Any, low: Any, close: Any, n: int = 14) -> pd.Series:
    """Average True Range: Wilder RMA of the true range (seeded with its first-n mean)."""
    return rma(true_range(high, low, close), n)


def adx(high: Any, low: Any, close: Any, n: int = 14) -> pd.DataFrame:
    """Wilder ADX with +DI / −DI. Columns ``adx``, ``pdi``, ``ndi``.

    +DM = up-move if up > down and up > 0, −DM symmetric; TR, +DM and −DM start at the
    second bar and are Wilder-smoothed with the same seed so the DI ratios are classic.
    DX = 100·|+DI − −DI| / (+DI + −DI) (0 when both are 0); ADX = RMA(DX, n). A zero smoothed
    true range (flat series) gives +DI = −DI = DX = ADX = 0 rather than null. The first
    DI value appears at bar ``n``, the first ADX at bar ``2n−1``.
    """
    h, lo, c = _series(high), _series(low), _series(close)
    up = h.diff()
    dn = -lo.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    tr = true_range(h, lo, c)
    # align all three on bar 1.. (bar 0 has no previous bar)
    plus_dm.iloc[:1] = np.nan
    minus_dm.iloc[:1] = np.nan
    tr = tr.copy()
    tr.iloc[:1] = np.nan
    s_tr = rma(tr, n)
    s_p = rma(plus_dm, n)
    s_m = rma(minus_dm, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = s_tr.where(s_tr != 0)
        pdi = 100.0 * s_p / denom
        ndi = 100.0 * s_m / denom
    # zero smoothed true range (a series flat since its first bar): no directional movement,
    # so +DI = −DI = 0 (|DM| <= TR, hence the smoothed DMs are 0 too) and DX/ADX are 0, not null
    flat = s_tr == 0
    pdi = pdi.where(~flat, 0.0)
    ndi = ndi.where(~flat, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        di_sum = (pdi + ndi)
        dx = (100.0 * (pdi - ndi).abs() / di_sum.where(di_sum != 0))
    dx = dx.where(~(di_sum == 0), 0.0)  # both DIs zero -> no directional movement -> DX 0
    dx = dx.where(~pdi.isna())
    adx_s = rma(dx, n)
    return pd.DataFrame({"adx": adx_s, "pdi": pdi, "ndi": ndi}, index=h.index)


def trailing_percentile(s: Any, window: int, min_periods: int | None = None) -> pd.Series:
    """Percentile rank (0–100) of each value within its trailing ``window`` (inclusive).

    Bar ``t`` is compared only with bars ``t-window+1 .. t`` (no look-ahead): the share of
    those values ``<= x_t``, × 100. Needs ``min_periods`` (default ``window``) valid values.
    """
    x = _series(s)
    w = int(window)
    mp = int(min_periods or w)

    def _rank(arr: np.ndarray) -> float:
        cur = arr[-1]
        if np.isnan(cur):
            return np.nan
        valid = arr[~np.isnan(arr)]
        return float((valid <= cur).sum()) / float(len(valid)) * 100.0

    return x.rolling(w, min_periods=mp).apply(_rank, raw=True)


# =====================================================================================
# Returns, drawdowns, ranges
# =====================================================================================


def last_value(s: Any) -> float | None:
    """Last value of a Series as a finite float, else None."""
    x = _series(s)
    if len(x) == 0:
        return None
    v = float(x.iloc[-1])
    return v if math.isfinite(v) else None


def pct_change_over(close: Any, bars: int) -> float | None:
    """Percent change of the last value versus ``bars`` bars earlier; None when the history
    is too short or the base is not positive."""
    c = _series(close).dropna()
    bars = int(bars)
    if bars <= 0 or len(c) <= bars:
        return None
    base = float(c.iloc[-1 - bars])
    last = float(c.iloc[-1])
    if not (math.isfinite(base) and math.isfinite(last)) or base <= 0:
        return None
    return (last / base - 1.0) * 100.0


def slope_pct(s: Any, bars: int) -> float | None:
    """Percent change of a (smoothed) series over ``bars`` bars, using its last valid value
    and the value ``bars`` positions earlier; None when either is NaN/missing."""
    x = _series(s)
    bars = int(bars)
    if len(x) <= bars:
        return None
    last = float(x.iloc[-1])
    base = float(x.iloc[-1 - bars])
    if not (math.isfinite(last) and math.isfinite(base)) or base == 0:
        return None
    return (last / base - 1.0) * 100.0


def drawdown_from_peak_pct(close: Any) -> float | None:
    """(last / max(close) − 1) × 100 over the whole input (≤ 0)."""
    c = _series(close).dropna()
    if c.empty:
        return None
    peak = float(c.max())
    if peak <= 0:
        return None
    return (float(c.iloc[-1]) / peak - 1.0) * 100.0


def max_drawdown_pct(close: Any) -> float | None:
    """Largest peak-to-trough decline inside the input, in percent (≤ 0). The peak is the
    running maximum within the input itself."""
    c = _series(close).dropna()
    if c.empty:
        return None
    running = c.cummax()
    dd = c / running - 1.0
    return float(dd.min()) * 100.0


def range_width_pct(high: Any, low: Any) -> float | None:
    """(max(High) − min(Low)) / midpoint × 100 over the input window."""
    h, lo = _series(high).dropna(), _series(low).dropna()
    if h.empty or lo.empty:
        return None
    hi, lw = float(h.max()), float(lo.min())
    mid = (hi + lw) / 2.0
    if mid <= 0:
        return None
    return (hi - lw) / mid * 100.0


def sma_crossings(close: Any, ma: Any) -> int | None:
    """Number of times ``close`` switched side relative to ``ma`` between consecutive bars
    of the input window (``len-1`` comparisons). None if any ``ma`` value is NaN."""
    c, m = _series(close), _series(ma)
    if len(c) < 2 or m.isna().any() or c.isna().any():
        return None
    above = (c.to_numpy() > m.to_numpy())
    return int((above[1:] != above[:-1]).sum())


def align_closes(a: pd.Series, b: pd.Series) -> pd.DataFrame:
    """Inner-join two close series on their dates -> DataFrame columns ``a``, ``b``
    (rows with a NaN in either dropped, sorted by date)."""
    df = pd.concat([_series(a).rename("a"), _series(b).rename("b")], axis=1, join="inner")
    df = df.dropna().sort_index()
    return df


def relative_strength_pct(a: pd.Series, b: pd.Series, bars: int) -> dict:
    """Excess return of ``a`` over ``b`` on date-aligned (inner-joined) closes:
    ``((1+r_a)/(1+r_b) − 1) × 100`` over the last ``bars`` aligned bars.

    Returns ``{"value", "start", "end", "bars", "aligned_rows", "ret_a_pct", "ret_b_pct"}``;
    ``value`` is None when fewer than ``bars+1`` aligned rows exist."""
    df = align_closes(a, b)
    bars = int(bars)
    res: dict[str, Any] = {"value": None, "start": None, "end": None, "bars": bars,
                           "aligned_rows": int(len(df)), "ret_a_pct": None, "ret_b_pct": None}
    if len(df) <= bars or bars <= 0:
        return res
    a0, a1 = float(df["a"].iloc[-1 - bars]), float(df["a"].iloc[-1])
    b0, b1 = float(df["b"].iloc[-1 - bars]), float(df["b"].iloc[-1])
    if min(a0, a1, b0, b1) <= 0:
        return res
    ra, rb = a1 / a0 - 1.0, b1 / b0 - 1.0
    res.update({
        "value": ((1.0 + ra) / (1.0 + rb) - 1.0) * 100.0,
        "start": df.index[-1 - bars], "end": df.index[-1],
        "ret_a_pct": ra * 100.0, "ret_b_pct": rb * 100.0,
    })
    return res
