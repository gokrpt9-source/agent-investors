"""Swing pivots, market structure, support/resistance levels and stop candidates
(DESIGN §4.2, §8).

Ported from stock_dashboard/indicators/levels.py (copied, never imported) and extended:

* ``swing_pivots`` keeps the fractal definition (a bar is a swing high when its High is the
  unique maximum of the ``left + 1 + right`` window centred on it; ties are rejected) and
  returns dated pivots.
* ``structure_label`` returns the schema spellings ``HH_HL | LH_LL | transition | unknown``.
* ``cluster_levels`` merges pivots (highs and lows together — a broken resistance becomes
  support) lying within ``cluster_atr × ATR`` of a cluster's mean into one level with a touch
  count and the date of the most recent touch.
* ``split_levels`` gives supports (below the close, nearest first) and resistances (above the
  close, nearest first), capped per side.
* ``stop_candidates`` builds the three deterministic stop bases and keeps those below the
  close, ordered by descending price.

Pure functions; thresholds are passed in by the caller (``committee.yaml: technicals``).
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

__all__ = ["swing_pivots", "structure_label", "cluster_levels", "split_levels",
           "stop_candidates", "STOP_BASES"]

#: Stop-candidate basis labels (technicals schema ``stop_candidates[].basis``).
STOP_BASES = ("below_support_0.5atr", "close_minus_2atr", "below_sma200")


def swing_pivots(df: pd.DataFrame, left: int = 3, right: int = 3, lookback: int = 250) -> dict:
    """Fractal swing highs / lows over the trailing ``lookback`` bars.

    Bar ``i`` is a swing high when ``High[i]`` is the maximum of ``High[i-left .. i+right]``
    and no other bar in that window has the same High (tied extremes are rejected);
    symmetric for swing lows on ``Low``. The scan covers the last ``lookback + left + right``
    bars so pivots up to ``lookback`` bars back can be confirmed; the last ``right`` bars can
    never be pivots (not yet confirmed).

    Returns ``{"highs": [(Timestamp, price)], "lows": [...], "last_two_highs": [p, p],
    "last_two_lows": [p, p]}`` in chronological order."""
    left, right, lookback = int(left), int(right), int(lookback)
    window = df.tail(lookback + left + right)
    highs: list[tuple[Any, float]] = []
    lows: list[tuple[Any, float]] = []
    h = window["High"].to_numpy(dtype="float64")
    lo = window["Low"].to_numpy(dtype="float64")
    n = len(window)
    for i in range(left, n - right):
        seg_h = h[i - left: i + right + 1]
        seg_l = lo[i - left: i + right + 1]
        hi_v, lo_v = h[i], lo[i]
        if not np.isnan(hi_v) and not np.isnan(seg_h).any():
            if hi_v == seg_h.max() and int((seg_h == hi_v).sum()) == 1:
                highs.append((window.index[i], float(hi_v)))
        if not np.isnan(lo_v) and not np.isnan(seg_l).any():
            if lo_v == seg_l.min() and int((seg_l == lo_v).sum()) == 1:
                lows.append((window.index[i], float(lo_v)))
    return {
        "highs": highs,
        "lows": lows,
        "last_two_highs": [p for _, p in highs[-2:]],
        "last_two_lows": [p for _, p in lows[-2:]],
    }


def structure_label(pivots: dict) -> str:
    """``HH_HL`` / ``LH_LL`` / ``transition`` from the last two swing highs and lows;
    ``unknown`` when fewer than two of either exist."""
    hh = list(pivots.get("last_two_highs") or [])
    ll = list(pivots.get("last_two_lows") or [])
    if len(hh) < 2 or len(ll) < 2:
        return "unknown"
    if hh[-1] > hh[-2] and ll[-1] > ll[-2]:
        return "HH_HL"
    if hh[-1] < hh[-2] and ll[-1] < ll[-2]:
        return "LH_LL"
    return "transition"


def cluster_levels(pivots: dict | Iterable[tuple[Any, float]], tolerance: float) -> list[dict]:
    """Merge pivot prices into levels.

    ``pivots`` is a ``swing_pivots`` result (highs + lows are pooled) or an iterable of
    ``(date, price)``. Prices are scanned in ascending order; a pivot joins the current
    cluster when it lies within ``tolerance`` (absolute price units, e.g. 0.5 × ATR) of the
    cluster's running mean, otherwise it starts a new cluster.

    Returns levels sorted by price: ``{"price": mean, "touches": n, "last_date": latest
    pivot date, "low": min, "high": max, "kinds": {"high": a, "low": b}}``."""
    if isinstance(pivots, dict):
        points = [(d, float(p), "high") for d, p in pivots.get("highs", [])]
        points += [(d, float(p), "low") for d, p in pivots.get("lows", [])]
    else:
        points = [(d, float(p), "pivot") for d, p in pivots]
    points = [pt for pt in points if math.isfinite(pt[1])]
    if not points:
        return []
    tol = float(tolerance) if tolerance is not None and math.isfinite(float(tolerance)) else 0.0
    tol = max(tol, 0.0)
    points.sort(key=lambda pt: (pt[1], pt[0]))
    clusters: list[list[tuple[Any, float, str]]] = []
    for pt in points:
        if clusters:
            cur = clusters[-1]
            mean = sum(p for _, p, _ in cur) / len(cur)
            if abs(pt[1] - mean) <= tol:
                cur.append(pt)
                continue
        clusters.append([pt])
    levels: list[dict] = []
    for cl in clusters:
        prices = [p for _, p, _ in cl]
        dates = [d for d, _, _ in cl]
        kinds = {"high": sum(1 for *_, k in cl if k == "high"),
                 "low": sum(1 for *_, k in cl if k == "low")}
        levels.append({
            "price": sum(prices) / len(prices),
            "touches": len(cl),
            "last_date": max(dates),
            "low": min(prices),
            "high": max(prices),
            "kinds": kinds,
        })
    return levels


def split_levels(levels: list[dict], close: float, max_per_side: int) -> tuple[list[dict], list[dict]]:
    """Supports = levels strictly below ``close`` (nearest first), resistances = strictly
    above (nearest first); each side capped at ``max_per_side``. Levels are returned as
    shallow copies with ``id`` S1.. / R1.. added."""
    cap = max(0, int(max_per_side))
    below = sorted((lv for lv in levels if lv["price"] < close), key=lambda lv: -lv["price"])[:cap]
    above = sorted((lv for lv in levels if lv["price"] > close), key=lambda lv: lv["price"])[:cap]
    supports = [dict(lv, id=f"S{i}") for i, lv in enumerate(below, start=1)]
    resistances = [dict(lv, id=f"R{i}") for i, lv in enumerate(above, start=1)]
    return supports, resistances


def stop_candidates(close: float, atr_value: float | None, supports: list[dict],
                    sma200: float | None, *, support_buffer_atr: float, atr_multiple: float,
                    sma200_buffer_atr: float) -> list[dict]:
    """Deterministic stop candidates below ``close``.

    * ``below_support_0.5atr`` = nearest support (S1) − ``support_buffer_atr`` × ATR
    * ``close_minus_2atr``     = close − ``atr_multiple`` × ATR
    * ``below_sma200``         = SMA200 − ``sma200_buffer_atr`` × ATR, only when SMA200 < close

    Candidates that are not strictly between 0 and ``close`` are dropped. Result sorted by
    descending price with ids STOP1..; each item ``{"id","price","basis","ref","derivation"}``.
    Without a usable ATR there are no candidates (every basis needs it)."""
    out: list[dict] = []
    if close is None or not math.isfinite(close) or close <= 0:
        return out
    if atr_value is None or not math.isfinite(atr_value) or atr_value <= 0:
        return out
    if supports:
        s1 = supports[0]
        out.append({"price": s1["price"] - support_buffer_atr * atr_value, "basis": STOP_BASES[0],
                    "ref": s1.get("id", "S1"),
                    "derivation": f"{s1.get('id', 'S1')} - {support_buffer_atr:g} x ATR14"})
    out.append({"price": close - atr_multiple * atr_value, "basis": STOP_BASES[1], "ref": "close",
                "derivation": f"close - {atr_multiple:g} x ATR14"})
    if sma200 is not None and math.isfinite(sma200) and sma200 < close:
        out.append({"price": sma200 - sma200_buffer_atr * atr_value, "basis": STOP_BASES[2],
                    "ref": "SMA200", "derivation": f"SMA200 - {sma200_buffer_atr:g} x ATR14"})
    out = [c for c in out if 0 < c["price"] < close]
    out.sort(key=lambda c: -c["price"])
    for i, c in enumerate(out, start=1):
        c["id"] = f"STOP{i}"
    return out
