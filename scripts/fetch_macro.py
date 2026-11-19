"""FRED macro series -> ``phase1/macro.json`` (schema ``macro/v1``, DESIGN §4.5).

Source: FRED's keyless CSV endpoint ``https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>``
(header ``observation_date,<ID>`` — older files use ``DATE,<ID>``; missing values are ``.`` or
an empty cell). Every request goes through the ``common`` network seam, so fixture mode
(``AGINV_FIXTURES_DIR``) and offline mode work without code changes.

Series and their metadata come from ``committee.yaml: macro.series`` (``{id, name, frequency
d|w|m, units, change diff|pct, yoy}``). For each series the payload carries::

    {"name", "units", "frequency", "change", "provider": "fred|cache|cache_stale|failed",
     "stale": bool, "latest": Fact, "observation_date": "YYYY-MM-DD"|null,
     "chg_3m": Fact, "chg_12m": Fact, "yoy_pct": Fact|null, "history_tail": [[date, value], ...]}

Rules
-----
* Observations dated after ``as_of`` are ignored (point-in-time; a no-op on a live run).
* ``latest`` = last non-missing observation. Its Fact ``as_of`` is the observation date for
  daily/weekly series and the **period end** (month end) for monthly series; the raw
  ``observation_date`` is kept on the Fact and on the series entry.
* ``chg_3m`` / ``chg_12m`` are date lookups: the prior value is the last non-missing observation
  dated on or before ``latest − macro.chg_3m_days`` / ``latest − macro.chg_12m_days`` calendar
  days. Monthly series use the same windows expressed in months (``round(days·12/365)`` → 3 and
  12) so that, e.g., March vs December is always 3 observations apart regardless of month
  lengths. ``change: diff`` → latest − prior (unit ``pp`` for pct series, else the series unit);
  ``change: pct`` → percent change (null ``negative_denominator`` when prior ≤ 0).
* ``yoy_pct`` (only for ``yoy: true`` series, e.g. CPIAUCSL / CPILFESL): percent change vs the
  observation exactly 12 months earlier for monthly series (12 observations back on a regular
  series), vs the ``chg_12m_days`` date lookup otherwise. ``yoy: false`` → ``null``.
* ``history_tail`` = the last ``macro.history_tail`` (13) non-missing observations.
* Cache kind ``macro`` keyed by series id (TTL ``cache_ttl_hours.macro``); a failed fetch falls
  back to the stale cache (flag ``stale_cache``), else the entry carries null Facts and a
  ``missing_data`` line — one failed series never fails the run.

CLI::

    python scripts/fetch_macro.py --out runs/<id>/phase1/macro.json [--series DGS10,CPIAUCSL]

Exit codes: 0 ok (individual failed series are reported, not fatal), 1 every series failed,
2 a usage / config / IO problem (bad ``--as-of`` or ``AGINV_AS_OF``, unreadable config, no series
to fetch, unwritable ``--out``, fixture mode on the real cache dir) - one ``error: ...`` stderr line.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import bisect  # noqa: E402
import calendar  # noqa: E402
import csv  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from typing import Any, Iterable, Mapping, Sequence  # noqa: E402

SCHEMA = "macro"
CACHE_KIND = "macro"

#: Fallbacks used only when committee.yaml lacks the key.
DEFAULT_CHG_3M_DAYS = 91
DEFAULT_CHG_12M_DAYS = 365
DEFAULT_HISTORY_TAIL = 13
DEFAULT_TTL_HOURS = 24.0

#: Metadata for a series id requested ad hoc that is not listed in ``macro.series``.
DEFAULT_SERIES_META: dict[str, Any] = {"name": None, "frequency": "d", "units": "value",
                                       "change": "diff", "yoy": False}

#: Cell values FRED (and spreadsheets) use for "no observation".
MISSING_TOKENS = frozenset({"", ".", "na", "n/a", "#n/a", "nan", "null", "none"})

Observation = tuple[date, "float | None"]


# =====================================================================================
# Parsing
# =====================================================================================


def parse_fred_csv(text: str, series_id: str | None = None) -> list[Observation]:
    """Parse a FRED graph CSV into ``[(date, value|None), ...]`` sorted by date.

    Header ``observation_date,<ID>`` or ``DATE,<ID>`` (case-insensitive). The value column is
    the one named ``series_id`` when present, else the second column. ``.`` / empty / ``NA``
    cells → ``None``; rows whose date does not parse are skipped; duplicate dates keep the last
    row. HTML (an error page) or no usable rows → ``DataUnavailable``."""
    if text is None:
        raise common.DataUnavailable(f"empty FRED response for {series_id}")
    body = text[1:] if text.startswith("﻿") else text
    head = body.lstrip()[:200].lower()
    if head.startswith("<") or "<html" in head:
        raise common.DataUnavailable(f"FRED returned HTML instead of CSV for {series_id}")
    rows = [r for r in csv.reader(io.StringIO(body)) if r and any(c.strip() for c in r)]
    if not rows:
        raise common.DataUnavailable(f"FRED CSV for {series_id} is empty")
    header = [c.strip() for c in rows[0]]
    if not header or header[0].lower() not in ("observation_date", "date"):
        raise common.DataUnavailable(f"unexpected FRED CSV header for {series_id}: {header[:3]!r}")
    col = 1
    if series_id is not None:
        upper = [h.upper() for h in header]
        if series_id.upper() in upper:
            col = upper.index(series_id.upper())
    if len(header) <= col:
        raise common.DataUnavailable(f"FRED CSV for {series_id} has no value column")
    by_date: dict[date, float | None] = {}
    for r in rows[1:]:
        try:
            d = common.parse_date(r[0].strip())
        except (ValueError, IndexError):
            continue
        if d is None:
            continue
        cell = r[col].strip() if len(r) > col else ""
        by_date[d] = None if cell.lower() in MISSING_TOKENS else common.safe_float(cell)
    if not by_date:
        raise common.DataUnavailable(f"FRED CSV for {series_id} has no observations")
    return sorted(by_date.items())


# =====================================================================================
# Date helpers
# =====================================================================================


def shift_months(d: date, months: int) -> date:
    """``d`` moved back by ``months`` calendar months (day clamped to the month length)."""
    total = d.year * 12 + (d.month - 1) - int(months)
    year, m0 = divmod(total, 12)
    day = min(d.day, calendar.monthrange(year, m0 + 1)[1])
    return date(year, m0 + 1, day)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def days_to_months(days: int) -> int:
    """Calendar-day window expressed in whole months (91 → 3, 365 → 12)."""
    return max(1, int(round(int(days) * 12 / 365.0)))


def period_as_of(obs_date: date, frequency: str) -> date:
    """Fact ``as_of`` for an observation: month end for monthly series, else the date itself."""
    return month_end(obs_date) if frequency == "m" else obs_date


def last_on_or_before(points: Sequence[tuple[date, float]], target: date) -> tuple[date, float] | None:
    """Last ``(date, value)`` in the sorted non-missing ``points`` with ``date <= target``."""
    dates = [p[0] for p in points]
    i = bisect.bisect_right(dates, target)
    return points[i - 1] if i > 0 else None


# =====================================================================================
# Config
# =====================================================================================


def _macro_cfg(cfg: Mapping[str, Any] | None) -> dict:
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    m = cfg.get("macro") if isinstance(cfg, Mapping) else None
    return dict(m) if isinstance(m, Mapping) else {}


def _ttl_hours(cfg: Mapping[str, Any] | None) -> float:
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    ttl = (cfg.get("cache_ttl_hours") or {}).get(CACHE_KIND) if isinstance(cfg, Mapping) else None
    f = common.safe_float(ttl)
    return DEFAULT_TTL_HOURS if f is None else f


def series_metadata(cfg: Mapping[str, Any] | None = None) -> "dict[str, dict]":
    """``{series_id: {"name","frequency","units","change","yoy"}}`` in config order."""
    out: dict[str, dict] = {}
    for entry in _macro_cfg(cfg).get("series") or []:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        sid = str(entry["id"]).strip().upper()
        meta = dict(DEFAULT_SERIES_META)
        meta.update({k: entry[k] for k in ("name", "frequency", "units", "change", "yoy") if k in entry})
        out[sid] = meta
    return out


# =====================================================================================
# Fetch (cache -> FRED -> stale cache)
# =====================================================================================


def fetch_observations(series_id: str, *, cache: common.DiskCache | None = None,
                       ttl_hours: float | None = None) -> tuple[list[Observation], str, str | None, Any]:
    """Return ``(observations, provider, error, fetched_at)``.

    ``provider`` ∈ ``cache | fred | cache_stale | failed``. Errors are never cached; a failed
    fetch falls back to a stale cache entry when one exists."""
    cache = cache or common.DiskCache()
    ttl = DEFAULT_TTL_HOURS if ttl_hours is None else ttl_hours
    url = common.fred_csv_url(series_id)
    hit = cache.get(CACHE_KIND, series_id, ttl)
    obs = _obs_from_payload(hit)
    if obs:
        return obs, "cache", None, getattr(hit, "fetched_at", None)
    try:
        text = common.http_get_text(url)
        obs = parse_fred_csv(text, series_id)
    except common.DataUnavailable as exc:
        stale = cache.get_stale(CACHE_KIND, series_id)
        stale_obs = _obs_from_payload(stale)
        if stale_obs:
            return stale_obs, "cache_stale", f"{type(exc).__name__}: {exc}", getattr(stale, "fetched_at", None)
        return [], "failed", f"{type(exc).__name__}: {exc}", None
    cache.put(CACHE_KIND, series_id,
              {"series_id": series_id, "observations": [[d.isoformat(), v] for d, v in obs]}, url)
    return obs, "fred", None, None


def _obs_from_payload(payload: Any) -> list[Observation]:
    if not isinstance(payload, Mapping):
        return []
    out: list[Observation] = []
    for row in payload.get("observations") or []:
        try:
            d = common.parse_date(row[0])
        except (ValueError, TypeError, IndexError):
            continue
        if d is not None:
            out.append((d, common.safe_float(row[1]) if len(row) > 1 else None))
    return sorted(out)


# =====================================================================================
# Per-series computation (pure)
# =====================================================================================


def _change_unit(units: str, change: str) -> str:
    if change == "pct":
        return "pct"
    return "pp" if units == "pct" else units


def _change(latest: float, prior: float, change: str) -> tuple[float | None, str | None]:
    if change == "pct":
        if prior is None or prior <= 0:
            return None, "negative_denominator"
        return round((latest / prior - 1.0) * 100.0, 4), None
    return round(latest - prior, 4), None


def _prior(points: Sequence[tuple[date, float]], latest_date: date, frequency: str,
           days: int) -> tuple[date, float] | None:
    target = shift_months(latest_date, days_to_months(days)) if frequency == "m" \
        else latest_date - timedelta(days=int(days))
    return last_on_or_before(points, target)


def _exact_month_back(points: Sequence[tuple[date, float]], latest_date: date,
                      months: int) -> tuple[date, float] | None:
    target = shift_months(latest_date, months)
    for d, v in points:
        if d.year == target.year and d.month == target.month:
            return d, v
    return None


def failed_entry(series_id: str, meta: Mapping[str, Any], reason: str = "missing") -> dict:
    """Series entry with null Facts (used when no data could be obtained)."""
    units = str(meta.get("units") or "value")
    change = str(meta.get("change") or "diff")
    frequency = str(meta.get("frequency") or "d")
    na = lambda unit: common.na_fact(unit, None, None, "macro", reason)  # noqa: E731
    return {
        "name": meta.get("name"), "units": units, "frequency": frequency, "change": change,
        "provider": "failed", "stale": False,
        "latest": na(units), "observation_date": None,
        "chg_3m": na(_change_unit(units, change)), "chg_12m": na(_change_unit(units, change)),
        "yoy_pct": na("pct") if meta.get("yoy") else None,
        "history_tail": [],
    }


def compute_series(series_id: str, observations: Iterable[Observation], meta: Mapping[str, Any], *,
                   as_of: date, chg_3m_days: int = DEFAULT_CHG_3M_DAYS,
                   chg_12m_days: int = DEFAULT_CHG_12M_DAYS,
                   history_tail: int = DEFAULT_HISTORY_TAIL) -> dict | None:
    """Series entry from raw observations (None when no non-missing observation ≤ ``as_of``)."""
    units = str(meta.get("units") or "value")
    change = str(meta.get("change") or "diff")
    frequency = str(meta.get("frequency") or "d")
    sid = f"D:FRED:{series_id}"
    points = [(d, float(v)) for d, v in sorted(observations) if d <= as_of and v is not None]
    if not points:
        return None
    latest_date, latest_val = points[-1]
    latest_as_of = period_as_of(latest_date, frequency)
    extra: dict[str, Any] = {"observation_date": latest_date}
    if frequency == "m":
        extra["period_end"] = latest_as_of

    def change_fact(days: int) -> dict:
        unit = _change_unit(units, change)
        prior = _prior(points[:-1], latest_date, frequency, days)
        if prior is None:
            return common.na_fact(unit, sid, latest_as_of, "macro", "insufficient_history", **extra)
        value, reason = _change(latest_val, prior[1], change)
        op = "-" if change == "diff" else "/"
        return common.fact(value, unit, sid, latest_as_of, "macro", reason, **extra,
                           derivation=f"{change}: {latest_date.isoformat()} {op} {prior[0].isoformat()}",
                           note=f"prior {prior[0].isoformat()} = {prior[1]}")

    yoy: dict | None = None
    if meta.get("yoy"):
        prior = (_exact_month_back(points[:-1], latest_date, 12) if frequency == "m"
                 else _prior(points[:-1], latest_date, frequency, chg_12m_days))
        if prior is None:
            yoy = common.na_fact("pct", sid, latest_as_of, "macro", "insufficient_history", **extra)
        else:
            value, reason = _change(latest_val, prior[1], "pct")
            yoy = common.fact(value, "pct", sid, latest_as_of, "macro", reason, **extra,
                              derivation=f"yoy: {latest_date.isoformat()} / {prior[0].isoformat()}",
                              note=f"prior {prior[0].isoformat()} = {prior[1]}")

    tail_n = max(0, int(history_tail))
    tail = [[d.isoformat(), v] for d, v in points[-tail_n:]] if tail_n else []
    return {
        "name": meta.get("name"), "units": units, "frequency": frequency, "change": change,
        "latest": common.fact(latest_val, units, sid, latest_as_of, "macro", **extra),
        "observation_date": latest_date.isoformat(),
        "chg_3m": change_fact(chg_3m_days),
        "chg_12m": change_fact(chg_12m_days),
        "yoy_pct": yoy,
        "history_tail": tail,
    }


# =====================================================================================
# run
# =====================================================================================


def run(series: list[str] | None = None, out: Path | None = None, *, as_of: str | None = None,
        cfg: Mapping[str, Any] | None = None, cache: common.DiskCache | None = None) -> dict:
    """Fetch ``series`` (default: every ``macro.series`` id) and return the ``macro/v1`` payload;
    write it to ``out`` when given. Partial failures produce null entries + ``missing_data``."""
    as_of_s = common.date_str(as_of) or common.today().isoformat()
    as_of_d = common.parse_date(as_of_s)
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    mcfg = _macro_cfg(cfg)
    meta_all = series_metadata(cfg)
    ids = [str(s).strip().upper() for s in (series if series else list(meta_all)) if str(s).strip()]
    ids = list(dict.fromkeys(ids))
    chg_3m_days = int(mcfg.get("chg_3m_days") or DEFAULT_CHG_3M_DAYS)
    chg_12m_days = int(mcfg.get("chg_12m_days") or DEFAULT_CHG_12M_DAYS)
    tail_n = int(mcfg.get("history_tail") if mcfg.get("history_tail") is not None else DEFAULT_HISTORY_TAIL)
    ttl = _ttl_hours(cfg)
    cache = cache or common.DiskCache()

    sources: list[dict] = []
    missing: list[str] = []
    flags: list[dict] = []
    entries: dict[str, dict] = {}
    for sid in ids:
        meta = meta_all.get(sid)
        if meta is None:
            meta = dict(DEFAULT_SERIES_META)
            flags.append(common.flag("series_not_configured", "low",
                                     f"{sid} is not in committee.yaml macro.series; default metadata used"))
        label = f"FRED {sid}" + (f" ({meta.get('name')})" if meta.get("name") else "")
        obs, provider, error, fetched_at = fetch_observations(sid, cache=cache, ttl_hours=ttl)
        entry = None
        if obs:
            entry = compute_series(sid, obs, meta, as_of=as_of_d, chg_3m_days=chg_3m_days,
                                   chg_12m_days=chg_12m_days, history_tail=tail_n)
        if entry is None:
            reason = error or f"no observations on or before {as_of_s}"
            entry = failed_entry(sid, meta)
            missing.append(f"{label}: unavailable — {reason}")
            flags.append(common.flag("series_failed", "medium", f"{sid}: {reason}"))
            entries[sid] = entry
            continue
        entry["provider"] = provider
        entry["stale"] = provider == "cache_stale"
        if provider == "cache_stale":
            flags.append(common.flag("stale_cache", "medium",
                                     f"{sid}: fetch failed ({error}); using cached data fetched {fetched_at}"))
        sources.append(common.dataset_source(f"D:FRED:{sid}", title=f"FRED series {sid}"
                                             + (f" — {meta.get('name')}" if meta.get("name") else "") + " (CSV)",
                                             accessed=as_of_s))
        for key in ("chg_3m", "chg_12m", "yoy_pct"):
            f = entry.get(key)
            if isinstance(f, Mapping) and f.get("value") is None and f.get("na_reason") == "insufficient_history":
                missing.append(f"{label}: {key} — not enough history")
        entries[sid] = entry

    if ids and all(e.get("provider") == "failed" for e in entries.values()):
        flags.append(common.flag("all_series_failed", "high", "no FRED series could be fetched"))
    payload = common.envelope(SCHEMA, as_of_s, sources=sources, missing_data=missing, flags=flags,
                              series=entries)
    if out is not None:
        common.write_json(Path(out), payload)
    return payload


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Fetch FRED macro series into macro/v1 JSON.")
    parser.add_argument("--series", default=None,
                        help="comma-separated FRED ids (default: committee.yaml macro.series)")
    parser.add_argument("--out", default=None, help="output path (e.g. runs/<id>/phase1/macro.json)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
        series = [s for s in (args.series or "").split(",") if s.strip()] or None
        cfg = None
        if series is None:  # nothing to fetch without the configured series list
            cfg = common.committee_cfg()
            if not (cfg.get("macro") or {}).get("series"):
                raise common.ConfigError("committee.yaml macro.series is empty: pass --series or fix the config")
        payload = run(series, Path(args.out) if args.out else None, as_of=args.as_of, cfg=cfg)
    except (common.AginvError, ValueError, OSError) as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)  # one line, like the other CLIs
        return 2
    failed = sorted(k for k, v in payload["series"].items() if v.get("provider") == "failed")
    print(json.dumps({"out": args.out, "series": len(payload["series"]), "failed": failed,
                      "missing_data": len(payload["missing_data"])}, ensure_ascii=False))
    return 1 if payload["series"] and len(failed) == len(payload["series"]) else 0


if __name__ == "__main__":
    sys.exit(main())
