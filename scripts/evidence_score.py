"""Rule-based evidence quality per agent report (DESIGN §4.8, SPEC §5) -> ``evidence.json``.

Start at ``evidence.start`` (10) and deduct:

* ``null_required``  -``null_required_penalty`` (2) per required field of the role / sector class
  (``evidence.required_fields[role][sector_class]``, falling back to ``default``; dotted paths)
  that is missing, null, an empty string or an empty list;
* ``stale``          -``stale_penalty`` (1) per stale key figure (``value: null`` figures skipped):
  ``price``: more than ``price_max_trading_days`` (3) NYSE trading days before the run date
  (weekends + ``market_holidays`` excluded); ``fundamental``: run_date - (``filed`` or
  ``as_of`` + ``fundamental_filing_lag_days`` (45)) > ``fundamental_max_days_after_filing`` (120)
  calendar days; ``macro``: older than ``macro_max_days`` (45) calendar days; ``other``: not
  checked. A numeric figure without a usable ``as_of`` counts as stale;
* ``few_sources``    -``few_sources_penalty`` (2) when the report cites fewer than
  ``few_sources_threshold`` (3) distinct source URLs;
* ``missing_data``   -``missing_data_penalty`` (1) per ``missing_data`` item, at most
  ``missing_data_max`` (4) points in total;

then clamp to ``[floor, start]``. A failed stub (``status: "failed"``) or an unreadable report
scores ``failed_score`` (1) with status ``failed``.

API::

    score_report(report, *, role, sector_class, run_date, cfg=None) -> {"score","deductions",...}
    score_reports(items, out=None, *, run_date, cfg=None) -> evidence/v1 document
        items = [{"path","role","ticker","sector_class"}]; keys of ``reports`` are
        ``Path(path).as_posix()``.

CLI::

    python scripts/evidence_score.py --report R.json --role technical --sector-class default --run-date 2026-10-02
    python scripts/evidence_score.py --items items.json --run-date 2026-10-02 --out evidence.json
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Iterable, Mapping  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

from scripts.risk_reward import to_decimal  # noqa: E402

#: Fallbacks used only for keys absent from committee.yaml ``evidence`` (they mirror it).
DEFAULTS: dict[str, Any] = {
    "start": 10,
    "floor": 1,
    "null_required_penalty": 2,
    "stale_penalty": 1,
    "few_sources_threshold": 3,
    "few_sources_penalty": 2,
    "missing_data_penalty": 1,
    "missing_data_max": 4,
    "price_max_trading_days": 3,
    "fundamental_max_days_after_filing": 120,
    "fundamental_filing_lag_days": 45,
    "macro_max_days": 45,
    "failed_score": 1,
    "required_fields": {},
}

RULE_NULL_REQUIRED = "null_required"
RULE_STALE = "stale"
RULE_FEW_SOURCES = "few_sources"
RULE_MISSING_DATA = "missing_data"
RULE_FAILED = "failed_report"
RULE_UNREADABLE = "unreadable_report"


# =====================================================================================
# config
# =====================================================================================


def resolve_cfg(cfg: Mapping[str, Any] | None = None) -> tuple[dict, list | None]:
    """Return ``(evidence_section_with_defaults, market_holidays_or_None)``.

    ``cfg`` may be the whole committee config (has an ``evidence`` key), the ``evidence``
    section itself, or None (loads committee.yaml from the active config dir)."""
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    if isinstance(cfg, Mapping) and isinstance(cfg.get("evidence"), Mapping):
        section = cfg["evidence"]
    else:
        section = cfg or {}
    holidays = cfg.get("market_holidays") if isinstance(cfg, Mapping) else None
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in dict(section).items() if v is not None})
    return merged, (list(holidays) if holidays is not None else None)


def required_fields_for(role: str, sector_class: str | None, ev_cfg: Mapping[str, Any]) -> list[str]:
    """Dotted required-field paths for ``role`` / ``sector_class`` (falls back to ``default``)."""
    per_role = (ev_cfg.get("required_fields") or {}).get(role) or {}
    if not isinstance(per_role, Mapping):
        return list(per_role or [])
    fields = per_role.get(sector_class or "default")
    if fields is None:
        fields = per_role.get("default")
    return [str(f) for f in (fields or [])]


# =====================================================================================
# helpers
# =====================================================================================


def _num_out(x: Decimal | float | int) -> int | float:
    """Integers stay ints in JSON; anything else becomes a float rounded to 4 dp."""
    d = to_decimal(x)
    if d is None:
        return 0
    if d == d.to_integral_value():
        return int(d)
    return float(round(d, 4))


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    # Schema-valid placeholder for an assessment the agent could not source (operator/industry-ops
    # prompts tell agents to write "Not assessed: <reason>"): counts as a null required field.
    if isinstance(value, str) and value.strip().lower().startswith("not assessed"):
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


def _norm_url(url: Any) -> str | None:
    """Identity key of a source URL: scheme and host lowercased (they are case-insensitive), the
    fragment and trailing slashes of the path dropped. The path and query are case-SENSITIVE
    and kept verbatim, so ``https://x.com/AbC`` and ``https://x.com/abc`` stay distinct sources."""
    if not isinstance(url, str):
        return None
    u = url.strip().split("#", 1)[0]
    if not u:
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return u.rstrip("/")
    if not (parts.scheme and parts.netloc):
        return u.rstrip("/")
    userinfo, at, hostport = parts.netloc.rpartition("@")
    netloc = userinfo + at + hostport.lower()
    out = f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"
    if parts.query:
        out += "?" + parts.query
    return out


def distinct_source_urls(report: Mapping[str, Any]) -> list[str]:
    """Distinct (normalized) ``sources[].url`` values, first-seen order."""
    seen: list[str] = []
    for s in report.get("sources") or []:
        if not isinstance(s, Mapping):
            continue
        u = _norm_url(s.get("url"))
        if u and u not in seen:
            seen.append(u)
    return seen


def _safe_date(x: Any) -> date | None:
    try:
        return common.parse_date(x)
    except (ValueError, TypeError):
        return None


def _deduction(rule: str, points: Any, detail: str) -> dict:
    return {"rule": rule, "detail": detail, "points": _num_out(-abs(to_decimal(points) or Decimal(0)))}


def stale_reason(figure: Mapping[str, Any], run_date: date, ev_cfg: Mapping[str, Any],
                 holidays: Iterable[Any] | None = None) -> str | None:
    """Why a key figure is stale (None when fresh, skipped or not checked)."""
    if figure.get("value") is None:
        return None
    kind = str(figure.get("kind") or "other")
    if kind not in ("price", "fundamental", "macro"):
        return None
    name = figure.get("name") or "?"
    as_of = _safe_date(figure.get("as_of"))
    if kind == "price":
        if as_of is None:
            return f"price figure {name!r} has no usable as_of"
        age = common.trading_days_between(as_of, run_date, holidays)
        limit = int(ev_cfg["price_max_trading_days"])
        if age > limit:
            return (f"price figure {name!r} as of {as_of.isoformat()} is {age} trading days old "
                    f"(> {limit})")
        return None
    if kind == "fundamental":
        filed = _safe_date(figure.get("filed"))
        if filed is not None:
            ref, basis = filed, f"filed {filed.isoformat()}"
        elif as_of is not None:
            lag = int(ev_cfg["fundamental_filing_lag_days"])
            ref = as_of + timedelta(days=lag)
            basis = f"as_of {as_of.isoformat()} + {lag} d assumed filing lag"
        else:
            return f"fundamental figure {name!r} has no usable as_of/filed date"
        age = (run_date - ref).days
        limit = int(ev_cfg["fundamental_max_days_after_filing"])
        if age > limit:
            return f"fundamental figure {name!r} is {age} days past its filing ({basis}; > {limit})"
        return None
    # macro
    if as_of is None:
        return f"macro figure {name!r} has no usable as_of"
    age = (run_date - as_of).days
    limit = int(ev_cfg["macro_max_days"])
    if age > limit:
        return f"macro figure {name!r} as of {as_of.isoformat()} is {age} days old (> {limit})"
    return None


# =====================================================================================
# scoring
# =====================================================================================


def is_failed(report: Any) -> bool:
    return not isinstance(report, Mapping) or str(report.get("status") or "").lower() == "failed"


def score_report(report: Mapping[str, Any] | None, *, role: str, sector_class: str | None,
                 run_date: Any, cfg: Mapping[str, Any] | None = None) -> dict:
    """Score one report. Returns ``{"score","deductions","status","start","floor","raw_score"}``."""
    ev_cfg, holidays = resolve_cfg(cfg)
    start = to_decimal(ev_cfg["start"]) or Decimal(10)
    floor = to_decimal(ev_cfg["floor"]) or Decimal(1)
    rd = _safe_date(run_date) or common.today()

    if is_failed(report):
        failed_score = to_decimal(ev_cfg["failed_score"]) or floor
        if isinstance(report, Mapping):
            attempts = report.get("attempts")
            detail = "failed stub" + (f" after {attempts} attempt(s)" if attempts else "")
            rule = RULE_FAILED
        else:
            detail, rule = "report missing or unreadable", RULE_UNREADABLE
        return {"score": _num_out(failed_score), "status": "failed", "start": _num_out(start),
                "floor": _num_out(floor), "raw_score": _num_out(failed_score),
                "deductions": [_deduction(rule, start - failed_score, detail)]}

    deductions: list[dict] = []
    # 1. required fields
    pen_null = to_decimal(ev_cfg["null_required_penalty"]) or Decimal(0)
    for path in required_fields_for(role, sector_class, ev_cfg):
        if _is_null(common.get_dotted(report, path)):
            deductions.append(_deduction(RULE_NULL_REQUIRED, pen_null,
                                         f"required field {path!r} is null or missing "
                                         f"({role}/{sector_class or 'default'})"))
    # 2. stale key figures
    pen_stale = to_decimal(ev_cfg["stale_penalty"]) or Decimal(0)
    for fig in report.get("key_figures") or []:
        if not isinstance(fig, Mapping):
            continue
        reason = stale_reason(fig, rd, ev_cfg, holidays)
        if reason:
            deductions.append(_deduction(RULE_STALE, pen_stale, reason))
    # 3. distinct source URLs
    urls = distinct_source_urls(report)
    threshold = int(ev_cfg["few_sources_threshold"])
    if len(urls) < threshold:
        deductions.append(_deduction(RULE_FEW_SOURCES, ev_cfg["few_sources_penalty"],
                                     f"{len(urls)} distinct source URL(s) (< {threshold})"))
    # 4. missing_data items (capped)
    items = [m for m in (report.get("missing_data") or []) if str(m).strip()]
    if items:
        per = to_decimal(ev_cfg["missing_data_penalty"]) or Decimal(0)
        cap = to_decimal(ev_cfg["missing_data_max"])
        pts = per * len(items)
        capped = cap is not None and pts > cap
        if capped:
            pts = cap
        if pts:
            deductions.append(_deduction(
                RULE_MISSING_DATA, pts,
                f"{len(items)} missing_data item(s)" + (f" (capped at {_num_out(cap)} points)" if capped else "")))

    raw = start + sum((to_decimal(d["points"]) or Decimal(0)) for d in deductions)
    score = max(floor, min(start, raw))
    return {"score": _num_out(score), "status": "ok", "start": _num_out(start), "floor": _num_out(floor),
            "raw_score": _num_out(raw), "deductions": deductions}


def path_key(path: Any) -> str:
    """Key used for a report path in ``evidence.json`` (posix form of the path as given)."""
    return Path(str(path)).as_posix()


def _norm_role(role: Any) -> str | None:
    """Return ``role`` when it is a known role key (schemas ``role_key`` enum), else ``None``."""
    if not role:
        return None
    r = str(role).strip()
    return r if r in common.ROLE_KEYS else None


def role_from_filename(path: Any) -> str | None:
    """Role key from a report filename ``<role>_<T>[.r2|.failed].json`` (longest matching prefix)."""
    name = Path(str(path)).name
    for role in sorted(common.ROLE_KEYS, key=len, reverse=True):
        if name.startswith(role + "_"):
            return role
    return None


def _resolve_role(item: Mapping[str, Any], doc: Any, raw_path: Any) -> str | None:
    """Item ``role`` -> report ``agent_role`` -> filename prefix; first valid role key wins."""
    doc_role = doc.get("agent_role") if isinstance(doc, Mapping) else None
    for cand in (item.get("role"), doc_role):
        r = _norm_role(cand)
        if r:
            return r
    return role_from_filename(raw_path)


def _norm_ticker(t: Any) -> str | None:
    if not t:
        return None
    try:
        return common.norm_ticker(t)
    except ValueError:
        return None


def score_reports(items: Iterable[Mapping[str, Any]], out: str | Path | None = None, *,
                  run_date: Any, cfg: Mapping[str, Any] | None = None) -> dict:
    """Score every report in ``items`` (``{"path","role","ticker","sector_class"}``) and return
    the ``evidence/v1`` document (written to ``out`` when given)."""
    rd = _safe_date(run_date) or common.today()
    reports: dict[str, dict] = {}
    flags: list[dict] = []
    missing: list[str] = []
    for item in items:
        raw_path = item.get("path")
        if not raw_path:
            continue
        key = path_key(raw_path)
        doc: Any = None
        try:
            doc = common.read_json(raw_path)
        except (OSError, ValueError) as exc:
            flags.append(common.flag("report_unreadable", "medium", f"{key}: {exc.__class__.__name__}"))
            missing.append(f"report not readable: {key}")
        role = _resolve_role(item, doc, raw_path)
        given_role = item.get("role")
        if given_role and role != str(given_role).strip():
            flags.append(common.flag("report_role_invalid", "low",
                                     f"{key}: item role {str(given_role)!r} is not a known role key"
                                     + (f"; using {role!r}" if role else "")))
        if role is None:
            # No role from the item, the report or the filename: an entry would violate the
            # evidence schema (agent_role enum), so skip it and record why.
            flags.append(common.flag("report_role_unknown", "medium",
                                     f"{key}: no role from item, report agent_role or filename; not scored"))
            missing.append(f"report role unknown, not scored: {key}")
            continue
        ticker = _norm_ticker(item.get("ticker") or (doc.get("ticker") if isinstance(doc, Mapping) else None))
        sc = item.get("sector_class") or "default"
        res = score_report(doc if isinstance(doc, Mapping) else None, role=role or "", sector_class=sc,
                           run_date=rd, cfg=cfg)
        reports[key] = {"agent_role": role, "ticker": ticker, "sector_class": sc, "path": key,
                        "status": res["status"], "score": res["score"], "start": res["start"],
                        "floor": res["floor"], "raw_score": res["raw_score"], "deductions": res["deductions"]}
    doc_out = common.envelope("evidence", rd, flags=flags, missing_data=missing, reports=reports)
    if out is not None:
        common.write_json(out, doc_out)
    return doc_out


#: DESIGN §4.8 name.
score_run_reports = score_reports


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Rule-based evidence score of agent reports (DESIGN 4.8).")
    parser.add_argument("--report", help="score a single report JSON")
    parser.add_argument("--role", help="agent role of --report (default: the report's agent_role)")
    parser.add_argument("--sector-class", default="default", choices=list(common.SECTOR_CLASSES))
    parser.add_argument("--items", help="JSON list of {path, role, ticker, sector_class} to score")
    parser.add_argument("--run-date", default=None, help="run date YYYY-MM-DD (default: today / --as-of)")
    parser.add_argument("--out", help="write evidence.json here (with --items)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
    except common.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_date = args.run_date or common.today().isoformat()
    if _safe_date(run_date) is None:
        print(f"error: --run-date is not a date: {run_date!r}", file=sys.stderr)
        return 2
    if args.items:
        raw = common.read_json(args.items)
        items = raw.get("items", []) if isinstance(raw, Mapping) else raw
        doc = score_reports(items, args.out, run_date=run_date)
        if not args.out:
            print(common.dumps_json(doc))
        else:
            print(json.dumps({"out": Path(args.out).as_posix(), "reports": len(doc["reports"])}))
        return 0
    if not args.report:
        parser.error("one of --report or --items is required")
    try:
        report = common.read_json(args.report)
    except (OSError, ValueError):
        report = None
    role = args.role or (report.get("agent_role") if isinstance(report, Mapping) else None) or ""
    res = score_report(report, role=role, sector_class=args.sector_class, run_date=run_date)
    print(common.dumps_json(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
