"""Per-ticker aggregation and action rules (DESIGN §4.10, SPEC §5) -> ``aggregate.json``.

For every ticker (holding or candidate):

* **Specialists** = every report except the Devil's Advocate. Roles in
  ``aggregation.mean_excluded_roles`` (osint, devils_advocate) and failed stubs are excluded from
  the conviction mean (failed ones still count evidence ``aggregation.failed_evidence`` = 1).
  Effective conviction: bullish ``c``; neutral ``aggregation.neutral_value`` (5); bearish
  ``upper - c`` (10 - c); clamped to ``aggregation.conviction_bounds``.
  ``conviction_mean = sum(eff * ev) / sum(ev)`` (ev = evidence score from evidence.json).
* **Devil's Advocate** adjustment ``aggregation.da_adjust`` (survives 0 / damaged -1 / broken -3);
  a failed or missing DA report adjusts 0 (noted).
* ``agg_conviction = clamp(mean + adj)`` and ``agg_evidence = mean of all report scores``
  (specialists incl. OSINT and failed = 1, plus the DA), both rounded half-up (Decimal) to
  ``aggregation.round_dp``.
* **rr** (stocks only): price = latest close; fv = fundamental ``extensions.valuation.
  fair_value_base``; downside stop = the technical report's ``invalidation_price`` when below
  price, else the highest ``stop_candidates[].price`` below price in ``technicals_<T>.json``,
  else the median of the other specialists' invalidations below price. A bearish specialist's
  invalidation above price is ignored; a bullish/neutral specialist whose invalidation is >=
  price raises the auto flag ``auto:invalidation_breached`` (high, must_address, unresolved).
  A stop closer than ``risk_reward.min_downside_pct`` (1.0 %) gives ``rr`` null /
  ``stop_too_tight`` (see ``risk_reward.py``).
* **Flags**: every red flag as ``<role>:<id>``; unresolved = high + must_address flags of the
  ``aggregation.unresolved_flag_roles`` roles not answered by a ``flag_responses`` entry with
  status mitigated|refuted, plus auto flags.
* **Rules** (``actions.*``) evaluated on rounded values, every test recorded in ``rule_trace``.

Two candidate (BUY) rules are deliberately STRICTER than SPEC section 5, which only asks for
"DA verdict != broken" and says nothing about flags (conservative deviations, both visible in
``rule_trace`` as ``C2.buy.da_ran`` / ``C2.buy.invalidation``):

* the Devil's Advocate must have run OK (a verdict is available). A missing, failed or
  verdict-less DA gives PASS with label ``da_unavailable`` (a BUY must never rest on a thesis
  nobody attacked). Holdings are unaffected (their DA adjustment stays 0 when the DA is absent).
* the auto flag ``auto:invalidation_breached`` (a bullish/neutral specialist's own stop is
  already hit) ALWAYS blocks BUY, whatever ``actions.candidate.block_on_unresolved_high_flag``
  says (that switch only governs ordinary OSINT flags and stays false by default). Label
  ``invalidation_breached``.

API::

    aggregate_ticker(item, *, evidence, cfg=None, as_of=None, profile=None) -> dict
    apply_rules(kind, asset_type, *, agg_conviction, agg_evidence, rr, da_verdict, unresolved_high,
                limit_breach, position_pct=None, cap_pct=None, invalidation_breached=False,
                cfg=None) -> (action, label, trace)
    run(items, out=None, *, evidence, cfg=None, as_of=None, profile=None) -> aggregate/v1 document

CLI::

    python scripts/aggregate.py --items items.json --evidence evidence.json --out aggregate.json
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Iterable, Mapping  # noqa: E402

from scripts import evidence_score  # noqa: E402
from scripts.risk_reward import (  # noqa: E402
    NULL_STOP_TOO_TIGHT, quantize_half_up, resolve_min_downside_pct, risk_reward, round_half_up, to_decimal)

#: Fallbacks used only for keys absent from committee.yaml (they mirror it).
DEFAULT_AGGREGATION: dict[str, Any] = {
    "neutral_value": 5,
    "da_adjust": {"survives": 0, "damaged": -1, "broken": -3},
    "unresolved_flag_roles": ["osint"],
    "mean_excluded_roles": ["osint", "devils_advocate"],
    "conviction_bounds": [1, 10],
    "round_dp": 1,
    "failed_evidence": 1,
}
DEFAULT_ACTIONS: dict[str, Any] = {
    "min_evidence": 5,
    "holding": {"add_conviction": 7, "add_evidence": 6, "add_rr": 2.0, "sell_max": 3.0, "trim_below": 4.5},
    "candidate": {"buy_conviction": 7, "buy_evidence": 6, "buy_rr": 2.5, "block_on_unresolved_high_flag": False},
    "sector_breach_tolerance_pp": 1.0,
}
DEFAULT_RR_DP = 2
#: Weights (position / sector / cap, percent) are compared after rounding to this many dp.
WEIGHT_ROUND_DP = 2

DA_ROLE = "devils_advocate"
STANCES = ("bullish", "neutral", "bearish")
DA_VERDICTS = ("survives", "damaged", "broken")
AUTO_INVALIDATION_FLAG = "auto:invalidation_breached"

LABEL_INSUFFICIENT = "insufficient_evidence"
LABEL_SELL_CONV = "sell_conviction"
LABEL_SELL_DA = "sell_da_broken"
LABEL_LIMIT = "limit_breach"
LABEL_ADD = "add"
LABEL_TRIM_CONV = "trim_conviction"
LABEL_HOLD = "hold"
LABEL_BUY = "buy"
LABEL_PASS = "pass"
LABEL_DA_UNAVAILABLE = "da_unavailable"
LABEL_INVALIDATION_BREACHED = "invalidation_breached"


# =====================================================================================
# config
# =====================================================================================


def _load_cfg(cfg: Mapping[str, Any] | None) -> dict:
    if cfg is None:
        try:
            return common.committee_cfg()
        except common.ConfigError:
            return {}
    return dict(cfg)


def _merged(base: Mapping[str, Any], override: Any) -> dict:
    out = dict(base)
    if isinstance(override, Mapping):
        for k, v in override.items():
            if v is None:
                continue
            if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
                out[k] = _merged(out[k], v)
            else:
                out[k] = v
    return out


def _agg_cfg(cfg: Mapping[str, Any]) -> dict:
    return _merged(DEFAULT_AGGREGATION, cfg.get("aggregation"))


def _act_cfg(cfg: Mapping[str, Any]) -> dict:
    return _merged(DEFAULT_ACTIONS, cfg.get("actions"))


def _rr_dp(cfg: Mapping[str, Any]) -> int:
    sec = cfg.get("risk_reward")
    if isinstance(sec, Mapping) and sec.get("round_dp") is not None:
        return int(sec["round_dp"])
    return DEFAULT_RR_DP


def _load_profile(profile: Mapping[str, Any] | None) -> dict:
    if profile is not None:
        return dict(profile)
    try:
        return common.load_config("investor_profile") or {}
    except common.ConfigError:
        return {}


# =====================================================================================
# small helpers
# =====================================================================================


def _num(x: Any) -> float | None:
    return common.fact_value(x)


def _out_num(d: Decimal | None) -> float | None:
    if d is None:
        return None
    f = float(d)
    return 0.0 if f == 0 else f


def _fmt(d: Any) -> str:
    if d is None:
        return "null"
    if isinstance(d, Decimal):
        return format(d, "f")  # keeps the quantized form: 7.0 stays "7.0", a config 7 stays "7"
    return str(d)


def _read_report(path: Any) -> tuple[dict | None, str | None]:
    """``(doc, problem)``; doc None when missing/unreadable."""
    if not path:
        return None, "no path"
    try:
        doc = common.read_json(path)
    except FileNotFoundError:
        return None, "file not found"
    except (OSError, ValueError) as exc:
        return None, f"unreadable ({exc.__class__.__name__})"
    if not isinstance(doc, dict):
        return None, "not a JSON object"
    return doc, None


def _is_failed_doc(doc: Mapping[str, Any] | None, path: Any) -> bool:
    if doc is None:
        return True
    if str(doc.get("status") or "").lower() == "failed":
        return True
    return str(path or "").endswith(".failed.json")


def _norm_key(p: Any) -> str:
    s = str(p).replace("\\", "/")
    try:
        s = os.path.normcase(os.path.abspath(s))
    except (TypeError, ValueError):
        pass
    return s.replace("\\", "/")


class _EvidenceIndex:
    """Look up evidence entries by report path regardless of slash style / relative form."""

    def __init__(self, evidence: Mapping[str, Any] | None):
        reports: Mapping[str, Any] = {}
        if isinstance(evidence, Mapping):
            reports = evidence.get("reports") if isinstance(evidence.get("reports"), Mapping) else evidence
        self.exact = {str(k): v for k, v in reports.items() if isinstance(v, Mapping)}
        self.norm = {_norm_key(k): v for k, v in self.exact.items()}

    def get(self, path: Any) -> Mapping[str, Any] | None:
        if path is None:
            return None
        for k in (str(path), Path(str(path)).as_posix()):
            if k in self.exact:
                return self.exact[k]
        return self.norm.get(_norm_key(path))


def _trace(trace: list[dict], rule: str, test: str, result: bool, **values: Any) -> bool:
    entry: dict[str, Any] = {"rule": rule, "test": test, "result": bool(result)}
    if values:
        entry["values"] = {k: (_out_num(v) if isinstance(v, Decimal) else v) for k, v in values.items()}
    trace.append(entry)
    return bool(result)


# =====================================================================================
# limit breach
# =====================================================================================


def compute_limit_breach(asset_type: str, *, position_pct: Any, cap_pct: Any, sector_pct: Any,
                         cio_target_pct: Any, max_sector_pct: Any, tolerance_pp: Any,
                         trace: list[dict] | None = None) -> tuple[bool, list[str], Decimal | None]:
    """``position > cap OR (stock AND sector_pct > min(max_sector_pct, cio_target) + tol)``,
    compared at ``WEIGHT_ROUND_DP``. Returns ``(breach, reasons, sector_limit)``."""
    tr = trace if trace is not None else []
    reasons: list[str] = []
    pos = quantize_half_up(position_pct, WEIGHT_ROUND_DP)
    cap = quantize_half_up(cap_pct, WEIGHT_ROUND_DP)
    if pos is not None and cap is not None:
        if _trace(tr, "limit_breach.position", f"position_pct {_fmt(pos)} > cap_pct {_fmt(cap)}",
                  pos > cap, position_pct=pos, cap_pct=cap):
            reasons.append(f"position {_fmt(pos)}% > cap {_fmt(cap)}%")
    else:
        _trace(tr, "limit_breach.position", "position_pct or cap_pct unknown -> not tested", False,
               position_pct=pos, cap_pct=cap)
    sector_limit: Decimal | None = None
    if asset_type == "stock":
        limits = [d for d in (quantize_half_up(max_sector_pct, WEIGHT_ROUND_DP),
                              quantize_half_up(cio_target_pct, WEIGHT_ROUND_DP)) if d is not None]
        sec = quantize_half_up(sector_pct, WEIGHT_ROUND_DP)
        tol = to_decimal(tolerance_pp) or Decimal(0)
        if limits and sec is not None:
            sector_limit = quantize_half_up(min(limits) + tol, WEIGHT_ROUND_DP)
            if _trace(tr, "limit_breach.sector",
                      f"sector_lookthrough_pct {_fmt(sec)} > min(max_sector_pct, cio_target_pct) "
                      f"+ tolerance = {_fmt(sector_limit)}", sec > sector_limit,
                      sector_lookthrough_pct=sec, max_sector_pct=_out_num(quantize_half_up(max_sector_pct, WEIGHT_ROUND_DP)),
                      cio_target_pct=_out_num(quantize_half_up(cio_target_pct, WEIGHT_ROUND_DP)),
                      tolerance_pp=tol):
                reasons.append(f"sector look-through {_fmt(sec)}% > limit {_fmt(sector_limit)}%")
        else:
            _trace(tr, "limit_breach.sector", "sector pct or limits unknown -> not tested", False,
                   sector_lookthrough_pct=sec)
    else:
        _trace(tr, "limit_breach.sector", f"asset_type {asset_type} -> sector test applies to stocks only", False)
    return bool(reasons), reasons, sector_limit


# =====================================================================================
# rules
# =====================================================================================


def apply_rules(kind: str, asset_type: str, *, agg_conviction: Any, agg_evidence: Any, rr: Any,
                da_verdict: str | None, unresolved_high: bool, limit_breach: bool,
                position_pct: Any = None, cap_pct: Any = None, invalidation_breached: bool = False,
                cfg: Mapping[str, Any] | None = None) -> tuple[str, str, list[dict]]:
    """Apply the holding / candidate action rules. Values are rounded half-up first
    (conviction/evidence: ``aggregation.round_dp``; rr: ``risk_reward.round_dp``; weights:
    ``WEIGHT_ROUND_DP``). Returns ``(action, label, rule_trace)``.

    ``da_verdict`` is None when the Devil's Advocate did not run OK: a candidate then cannot be
    BUY (``da_unavailable``). ``invalidation_breached`` (the ``auto:invalidation_breached`` flag)
    always blocks a candidate BUY."""
    c = _load_cfg(cfg)
    agg, acts = _agg_cfg(c), _act_cfg(c)
    dp = int(agg["round_dp"])
    conv = quantize_half_up(agg_conviction, dp)
    ev = quantize_half_up(agg_evidence, dp)
    rrv = quantize_half_up(rr, _rr_dp(c))
    is_fund = asset_type == "fund"
    da_broken = da_verdict == "broken"
    tr: list[dict] = []

    if kind == "candidate":
        cand = acts["candidate"]
        bc, be, brr = (to_decimal(cand["buy_conviction"]), to_decimal(cand["buy_evidence"]),
                       to_decimal(cand["buy_rr"]))
        block = bool(cand.get("block_on_unresolved_high_flag"))
        if not _trace(tr, "C1.conviction_available", "agg_conviction is not null", conv is not None,
                      agg_conviction=conv):
            return "PASS", LABEL_INSUFFICIENT, tr
        ok = _trace(tr, "C2.buy.conviction", f"agg_conviction {_fmt(conv)} >= {_fmt(bc)}", conv >= bc,
                    agg_conviction=conv)
        ok &= _trace(tr, "C2.buy.evidence", f"agg_evidence {_fmt(ev)} >= {_fmt(be)}",
                     ev is not None and ev >= be, agg_evidence=ev)
        if is_fund:
            _trace(tr, "C2.buy.rr", "fund -> no rr test", True)
        else:
            ok &= _trace(tr, "C2.buy.rr", f"rr {_fmt(rrv)} >= {_fmt(brr)}", rrv is not None and rrv >= brr, rr=rrv)
        # conservative deviation from SPEC section 5: the DA must have run OK (a verdict exists)
        da_ran = da_verdict in DA_VERDICTS
        ok &= _trace(tr, "C2.buy.da_ran", "Devil's Advocate ran OK (a verdict is available; "
                     "stricter than SPEC, which only requires verdict != broken)", da_ran, da_verdict=da_verdict)
        ok &= _trace(tr, "C2.buy.da", f"DA verdict {da_verdict or 'null'} != broken", not da_broken,
                     da_verdict=da_verdict)
        # the auto flag always blocks BUY, independent of block_on_unresolved_high_flag
        ok &= _trace(tr, "C2.buy.invalidation", "no bullish/neutral specialist invalidation at or above the "
                     "price (auto:invalidation_breached always blocks BUY)", not invalidation_breached,
                     invalidation_breached=bool(invalidation_breached))
        if block:
            ok &= _trace(tr, "C2.buy.flags", "no unresolved high flag", not unresolved_high,
                         unresolved_high=unresolved_high)
        else:
            _trace(tr, "C2.buy.flags", "unresolved high flags do not block BUY "
                   "(actions.candidate.block_on_unresolved_high_flag false)", True, unresolved_high=unresolved_high)
        if ok:
            return "BUY", LABEL_BUY, tr
        if not da_ran:
            return "PASS", LABEL_DA_UNAVAILABLE, tr
        if invalidation_breached:
            return "PASS", LABEL_INVALIDATION_BREACHED, tr
        return "PASS", LABEL_PASS, tr

    hold = acts["holding"]
    min_ev = to_decimal(acts["min_evidence"])
    sell_max, trim_below = to_decimal(hold["sell_max"]), to_decimal(hold["trim_below"])
    add_c, add_e, add_rr = to_decimal(hold["add_conviction"]), to_decimal(hold["add_evidence"]), to_decimal(hold["add_rr"])
    breach_action = "TRIM" if limit_breach else "HOLD"

    # (1) no conviction at all
    if _trace(tr, "R1.insufficient_evidence.conviction_null", "agg_conviction is null", conv is None,
              agg_conviction=conv, limit_breach=limit_breach):
        return breach_action, LABEL_INSUFFICIENT, tr
    # (2) evidence below the minimum
    if _trace(tr, "R2.insufficient_evidence.low_evidence", f"agg_evidence {_fmt(ev)} < min_evidence {_fmt(min_ev)}",
              ev is None or ev < min_ev, agg_evidence=ev, limit_breach=limit_breach):
        return breach_action, LABEL_INSUFFICIENT, tr
    # (3) SELL
    if _trace(tr, "R3.sell.conviction", f"agg_conviction {_fmt(conv)} <= sell_max {_fmt(sell_max)}",
              conv <= sell_max, agg_conviction=conv):
        return "SELL", LABEL_SELL_CONV, tr
    if _trace(tr, "R3.sell.da_broken", f"DA verdict {da_verdict or 'null'} == broken", da_broken,
              da_verdict=da_verdict):
        return "SELL", LABEL_SELL_DA, tr
    # (4) limit breach
    if _trace(tr, "R4.trim.limit_breach", "limit_breach", bool(limit_breach), limit_breach=limit_breach):
        return "TRIM", LABEL_LIMIT, tr
    # (5) ADD
    ok = _trace(tr, "R5.add.conviction", f"agg_conviction {_fmt(conv)} >= {_fmt(add_c)}", conv >= add_c,
                agg_conviction=conv)
    ok &= _trace(tr, "R5.add.evidence", f"agg_evidence {_fmt(ev)} >= {_fmt(add_e)}", ev >= add_e, agg_evidence=ev)
    if is_fund:
        _trace(tr, "R5.add.rr", "fund -> no rr test", True)
    else:
        ok &= _trace(tr, "R5.add.rr", f"rr {_fmt(rrv)} >= {_fmt(add_rr)}", rrv is not None and rrv >= add_rr, rr=rrv)
    ok &= _trace(tr, "R5.add.flags", "no unresolved high flag", not unresolved_high, unresolved_high=unresolved_high)
    pos = quantize_half_up(position_pct, WEIGHT_ROUND_DP)
    cap = quantize_half_up(cap_pct, WEIGHT_ROUND_DP)
    if cap is None:
        _trace(tr, "R5.add.position_below_cap", "no cap configured -> passes", True, position_pct=pos)
    else:
        ok &= _trace(tr, "R5.add.position_below_cap", f"position_pct {_fmt(pos)} < cap_pct {_fmt(cap)}",
                     pos is not None and pos < cap, position_pct=pos, cap_pct=cap)
    if ok:
        return "ADD", LABEL_ADD, tr
    # (6) conviction TRIM
    if _trace(tr, "R6.trim.conviction", f"{_fmt(sell_max)} < agg_conviction {_fmt(conv)} < trim_below {_fmt(trim_below)}",
              sell_max < conv < trim_below, agg_conviction=conv):
        return "TRIM", LABEL_TRIM_CONV, tr
    # (7) HOLD
    _trace(tr, "R7.hold", "no other rule fired", True)
    return "HOLD", LABEL_HOLD, tr


# =====================================================================================
# per-ticker aggregation
# =====================================================================================


def _effective(stance: str, conviction: Decimal, agg: Mapping[str, Any]) -> Decimal:
    lo, hi = (to_decimal(b) for b in agg["conviction_bounds"])
    if stance == "neutral":
        eff = to_decimal(agg["neutral_value"])
    elif stance == "bearish":
        eff = hi - conviction
    else:
        eff = conviction
    return max(lo, min(hi, eff))


def _evidence_score_for(path: Any, doc: dict | None, failed: bool, role: str, sector_class: str,
                        idx: _EvidenceIndex, agg: Mapping[str, Any], cfg: Mapping[str, Any],
                        as_of: str, notes: list[str]) -> Decimal:
    if failed:
        return to_decimal(agg["failed_evidence"]) or Decimal(1)
    entry = idx.get(path)
    score = to_decimal(entry.get("score")) if entry else None
    if score is None:
        res = evidence_score.score_report(doc, role=role, sector_class=sector_class, run_date=as_of, cfg=cfg)
        score = to_decimal(res["score"]) or Decimal(1)
        notes.append(f"{role}: evidence not found in evidence.json - scored inline ({_fmt(score)})")
    return score


def _invalidation(doc: Mapping[str, Any] | None) -> float | None:
    if not isinstance(doc, Mapping):
        return None
    v = common.safe_float(doc.get("invalidation_price"))
    return v if (v is not None and v > 0) else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _stop_candidate(technicals_path: Any, price: float, notes: list[str]) -> tuple[float | None, str | None]:
    if not technicals_path:
        return None, None
    tech, problem = _read_report(technicals_path)
    if tech is None:
        notes.append(f"technicals file unavailable ({problem})")
        return None, None
    best: tuple[float, str] | None = None
    for cand in tech.get("stop_candidates") or []:
        if not isinstance(cand, Mapping):
            continue
        p = _num(cand.get("price"))
        if p is None or not (0 < p < price):
            continue
        if best is None or p > best[0]:
            best = (p, str(cand.get("id") or ""))
    return (best[0], best[1]) if best else (None, None)


def aggregate_ticker(item: Mapping[str, Any], *, evidence: Mapping[str, Any] | None,
                     cfg: Mapping[str, Any] | None = None, as_of: Any = None,
                     profile: Mapping[str, Any] | None = None) -> dict:
    """Aggregate one ticker item (see module docstring / integration API)."""
    c = _load_cfg(cfg)
    agg, acts = _agg_cfg(c), _act_cfg(c)
    prof = _load_profile(profile)
    run_as_of = common.date_str(as_of) or common.today().isoformat()
    idx = evidence if isinstance(evidence, _EvidenceIndex) else _EvidenceIndex(evidence)
    dp = int(agg["round_dp"])

    ticker = common.norm_ticker(item["ticker"])
    kind = item.get("kind") or "holding"
    asset_type = item.get("asset_type") or "stock"
    sector = item.get("sector") or ("funds" if asset_type == "fund" else "unclassified")
    sector_class = item.get("sector_class") or common.sector_class(sector, asset_type)
    ctx = dict(item.get("context") or {})
    notes: list[str] = []
    missing: list[str] = []

    price_raw = item.get("price")
    price = _num(price_raw)
    if common.is_fact(price_raw):
        price_fact = dict(price_raw)
    elif price is not None:
        price_fact = common.fact(price, "USD/share", f"D:PX:{ticker}", run_as_of, "price")
    else:
        price_fact = None
        missing.append(f"{ticker}: latest price missing")
    if price is not None and price <= 0:
        price = None

    # ------------------------------------------------------------------ reports
    reports: dict[str, Any] = dict(item.get("reports") or {})
    da_path = item.get("da_path")
    if da_path is None and DA_ROLE in reports:
        da_path = reports.pop(DA_ROLE)
    else:
        reports.pop(DA_ROLE, None)
    excluded = set(agg["mean_excluded_roles"])
    specialists: list[dict] = []
    docs: dict[str, dict] = {}
    weighted_sum, weight_total = Decimal(0), Decimal(0)
    scores: list[Decimal] = []
    for role, path in reports.items():
        doc, problem = _read_report(path)
        failed = _is_failed_doc(doc, path)
        if doc is None:
            notes.append(f"{role}: report {problem} - treated as failed")
            missing.append(f"{ticker}: {role} report missing ({problem})")
        elif failed:
            notes.append(f"{role}: failed stub - excluded from conviction, evidence {agg['failed_evidence']}")
        ev = _evidence_score_for(path, doc, failed, role, sector_class, idx, agg, c, run_as_of, notes)
        scores.append(ev)
        entry: dict[str, Any] = {"role": role, "path": Path(str(path)).as_posix() if path else None,
                                 "status": "failed" if failed else "ok", "stance": None, "conviction": None,
                                 "effective_conviction": None, "evidence": _out_num(ev), "in_mean": False,
                                 "invalidation_price": None}
        if not failed and doc is not None:
            docs[role] = doc
            stance = doc.get("stance") if doc.get("stance") in STANCES else None
            conv = to_decimal(doc.get("conviction"))
            entry.update({"stance": stance, "conviction": _out_num(conv), "invalidation_price": _invalidation(doc)})
            if stance is not None and conv is not None:
                eff = _effective(stance, conv, agg)
                entry["effective_conviction"] = _out_num(eff)
                if role not in excluded:
                    entry["in_mean"] = True
                    weighted_sum += eff * ev
                    weight_total += ev
            elif role not in excluded:
                notes.append(f"{role}: stance/conviction missing - excluded from the mean")
        specialists.append(entry)

    conviction_mean = (weighted_sum / weight_total) if weight_total > 0 else None

    # ------------------------------------------------------------------ devil's advocate
    adj_map = agg["da_adjust"]
    da_block: dict[str, Any] = {"path": None, "status": "not_run", "verdict": None, "adj": 0, "evidence": None}
    da_doc: dict | None = None
    if da_path:
        doc, problem = _read_report(da_path)
        failed = _is_failed_doc(doc, da_path)
        ev = _evidence_score_for(da_path, doc, failed, DA_ROLE, sector_class, idx, agg, c, run_as_of, notes)
        scores.append(ev)
        da_block.update({"path": Path(str(da_path)).as_posix(), "evidence": _out_num(ev)})
        if failed:
            da_block.update({"status": "failed", "adj": 0,
                             "note": f"DA {'report ' + problem if doc is None else 'failed'} - adjustment 0"})
            notes.append(da_block["note"])
            if doc is None:
                missing.append(f"{ticker}: devils_advocate report missing ({problem})")
        else:
            da_doc = doc
            verdict = doc.get("verdict_on_thesis") if doc.get("verdict_on_thesis") in DA_VERDICTS else None
            adj = to_decimal(adj_map.get(verdict, 0)) if verdict else Decimal(0)
            da_block.update({"status": "ok", "verdict": verdict, "adj": _out_num(adj)})
            if verdict is None:
                da_block["note"] = "DA verdict missing - adjustment 0"
                notes.append(da_block["note"])
    else:
        da_block["note"] = "no Devil's Advocate report for this ticker"
    da_adj = to_decimal(da_block["adj"]) or Decimal(0)

    lo, hi = (to_decimal(b) for b in agg["conviction_bounds"])
    if conviction_mean is not None:
        raw = max(lo, min(hi, conviction_mean + da_adj))
        agg_conviction = round_half_up(raw, dp)
    else:
        agg_conviction = None
    agg_evidence = round_half_up(sum(scores, Decimal(0)) / len(scores), dp) if scores else None

    # ------------------------------------------------------------------ flags
    all_flags: list[dict] = []
    responses: dict[str, list[dict]] = {}
    flag_sources: list[tuple[str, Mapping[str, Any]]] = list(docs.items())
    if da_doc is not None:
        flag_sources.append((DA_ROLE, da_doc))
    for role, doc in flag_sources:
        for resp in doc.get("flag_responses") or []:
            if isinstance(resp, Mapping) and resp.get("flag_ref"):
                responses.setdefault(str(resp["flag_ref"]), []).append(
                    {"role": role, "status": resp.get("status"), "note": resp.get("note")})
    unresolved_roles = set(agg["unresolved_flag_roles"])
    unresolved: list[dict] = []
    for role, doc in flag_sources:
        for rf in doc.get("red_flags") or []:
            if not isinstance(rf, Mapping) or not rf.get("id"):
                continue
            fid = f"{role}:{rf['id']}"
            resp = responses.get(fid, [])
            resolved = any(r.get("status") in ("mitigated", "refuted") for r in resp)
            entry = {"flag_id": fid, "role": role, "id": rf.get("id"), "flag": rf.get("flag"),
                     "severity": rf.get("severity"), "must_address": bool(rf.get("must_address")),
                     "source_ids": list(rf.get("source_ids") or []), "resolved": resolved,
                     "responses": resp, "auto": False}
            all_flags.append(entry)
            if (role in unresolved_roles and entry["severity"] == "high" and entry["must_address"]
                    and not resolved):
                unresolved.append(entry)

    breached: list[str] = []
    if price is not None:
        for sp in specialists:
            inv = sp.get("invalidation_price")
            if sp["status"] == "ok" and sp["stance"] in ("bullish", "neutral") and inv is not None and inv >= price:
                breached.append(f"{sp['role']} ({sp['stance']}) invalidation {inv:g} >= price {price:g}")
    if breached:
        auto = {"flag_id": AUTO_INVALIDATION_FLAG, "role": "aggregate", "id": None,
                "flag": "Invalidation price at or above the current price: " + "; ".join(breached),
                "severity": "high", "must_address": True, "source_ids": [], "resolved": False,
                "responses": [], "auto": True}
        all_flags.append(auto)
        unresolved.append(auto)
    unresolved_high = any(f.get("severity") == "high" for f in unresolved)

    # ------------------------------------------------------------------ risk / reward
    rr_dp = _rr_dp(c)
    if asset_type == "fund":
        rr_block: dict[str, Any] = {"price": price, "fv_base": None, "invalidation": None,
                                    "invalidation_source": None, "upside_pct": None, "downside_pct": None,
                                    "rr": None, "null_reason": "not_applicable"}
    else:
        fund_doc = docs.get("fundamental")
        fv_base = common.safe_float(common.get_dotted(fund_doc, "extensions.valuation.fair_value_base")) \
            if fund_doc else None
        inv_value, inv_source, inv_ref = None, "none", None
        if price is not None:
            tech_doc = docs.get("technical")
            tinv = _invalidation(tech_doc)
            if tinv is not None and tinv < price:
                inv_value, inv_source = tinv, "technical_report"
                inv_ref = common.get_dotted(tech_doc, "extensions.invalidation_ref")
            else:
                if tinv is not None:
                    notes.append(f"technical invalidation {tinv:g} is not below price {price:g} - not used as stop")
                stop, stop_id = _stop_candidate(item.get("technicals_path"), price, notes)
                if stop is not None:
                    inv_value, inv_source, inv_ref = stop, "technicals_stop_candidate", stop_id
                else:
                    others = [sp["invalidation_price"] for sp in specialists
                              if sp["role"] != "technical" and sp["status"] == "ok"
                              and sp.get("invalidation_price") is not None and sp["invalidation_price"] < price]
                    med = _median(others)
                    if med is not None:
                        inv_value, inv_source = med, "specialist_median"
        rr_res = risk_reward(price, fv_base, inv_value, round_dp=rr_dp, cfg=c)
        rr_block = {"price": price, "fv_base": fv_base, "invalidation": inv_value,
                    "invalidation_source": inv_source, "invalidation_ref": inv_ref, **rr_res}
        if rr_res.get("null_reason") == NULL_STOP_TOO_TIGHT:
            notes.append(f"rr null: stop {inv_value:g} ({inv_source}) is only {rr_res['downside_pct']:g}% below "
                         f"price {price:g} (< risk_reward.min_downside_pct "
                         f"{_fmt(resolve_min_downside_pct(c))}%) - stop too tight for a meaningful ratio")
        if fv_base is None:
            missing.append(f"{ticker}: fundamental fair_value_base unavailable - rr null")

    # ------------------------------------------------------------------ limits + rules
    cap_pct = ctx.get("cap_pct")
    if cap_pct is None:
        cap_pct = prof.get("max_fund_position_pct" if asset_type == "fund" else "max_position_pct")
    max_sector_pct = ctx.get("max_sector_pct", prof.get("max_sector_pct"))
    tol = ctx.get("sector_breach_tolerance_pp", acts.get("sector_breach_tolerance_pp"))
    position_pct = ctx.get("position_pct")
    trace: list[dict] = []
    limit_breach, breach_reasons, sector_limit = compute_limit_breach(
        asset_type, position_pct=position_pct, cap_pct=cap_pct, sector_pct=ctx.get("sector_lookthrough_pct"),
        cio_target_pct=ctx.get("cio_target_pct"), max_sector_pct=max_sector_pct, tolerance_pp=tol, trace=trace)
    # (for candidates the limit-breach tests are recorded for transparency; BUY/PASS ignores them)
    action, label, rule_trace = apply_rules(
        kind, asset_type, agg_conviction=agg_conviction, agg_evidence=agg_evidence, rr=rr_block.get("rr"),
        da_verdict=da_block.get("verdict"), unresolved_high=unresolved_high, limit_breach=limit_breach,
        position_pct=position_pct, cap_pct=cap_pct, invalidation_breached=bool(breached), cfg=c)

    inputs = {"price": price_fact, "position_pct": common.safe_float(position_pct),
              "cap_pct": common.safe_float(cap_pct), "sector_lookthrough_pct": common.safe_float(ctx.get("sector_lookthrough_pct")),
              "cio_target_pct": common.safe_float(ctx.get("cio_target_pct")),
              "max_sector_pct": common.safe_float(max_sector_pct),
              "sector_breach_tolerance_pp": common.safe_float(tol),
              "sector_limit_pct": _out_num(sector_limit), "limit_breach": bool(limit_breach),
              "limit_breach_reasons": breach_reasons}
    return {
        "ticker": ticker, "kind": kind, "asset_type": asset_type, "sector": sector, "sector_class": sector_class,
        "inputs": inputs, "specialists": specialists,
        "conviction_mean": round_half_up(conviction_mean, 4) if conviction_mean is not None else None,
        "da": da_block, "agg_conviction": agg_conviction, "agg_evidence": agg_evidence, "rr": rr_block,
        "flags": {"all": all_flags, "unresolved": unresolved},
        "rule_action": action, "rule_label": label, "rule_trace": trace + rule_trace,
        "notes": notes, "_missing": missing,
    }


# =====================================================================================
# run
# =====================================================================================


def run(items: Iterable[Mapping[str, Any]], out: str | Path | None = None, *,
        evidence: Mapping[str, Any] | None, cfg: Mapping[str, Any] | None = None,
        as_of: Any = None, profile: Mapping[str, Any] | None = None) -> dict:
    """Aggregate every item and return (and optionally write) the ``aggregate/v1`` document."""
    c = _load_cfg(cfg)
    prof = _load_profile(profile)
    run_as_of = (common.date_str(as_of)
                 or (common.date_str(evidence.get("as_of")) if isinstance(evidence, Mapping) and evidence.get("as_of") else None)
                 or common.today().isoformat())
    idx = _EvidenceIndex(evidence)
    tickers: dict[str, dict] = {}
    flags: list[dict] = []
    missing: list[str] = []
    for item in items:
        res = aggregate_ticker(item, evidence=idx, cfg=c, as_of=run_as_of, profile=prof)
        t = res.pop("ticker")
        missing.extend(res.pop("_missing"))
        if t in tickers:
            flags.append(common.flag("duplicate_ticker", "medium", f"{t} appeared more than once; last item kept"))
        for sp in res["specialists"]:
            if sp["status"] == "failed":
                flags.append(common.flag("report_failed", "medium", f"{t}: {sp['role']} report failed"))
        if res["da"].get("status") == "failed":
            flags.append(common.flag("report_failed", "medium", f"{t}: devils_advocate report failed"))
        if any(f.get("flag_id") == AUTO_INVALIDATION_FLAG for f in res["flags"]["unresolved"]):
            flags.append(common.flag("invalidation_breached", "high", f"{t}: specialist invalidation >= price"))
        tickers[t] = res
    doc = common.envelope("aggregate", run_as_of, flags=flags, missing_data=missing, tickers=tickers)
    if out is not None:
        common.write_json(out, doc)
    return doc


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Aggregate specialist reports and apply the action rules (DESIGN 4.10).")
    parser.add_argument("--items", required=True, help="JSON list of ticker items (or {\"items\": [...]})")
    parser.add_argument("--evidence", required=True, help="evidence.json (evidence/v1)")
    parser.add_argument("--out", help="aggregate.json to write (default: print)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
    except common.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raw = common.read_json(args.items)
    items = raw.get("items", []) if isinstance(raw, Mapping) else raw
    evidence = common.read_json(args.evidence)
    doc = run(items, args.out, evidence=evidence, as_of=args.as_of)
    if args.out:
        summary = {t: {"rule_action": v["rule_action"], "agg_conviction": v["agg_conviction"],
                       "agg_evidence": v["agg_evidence"], "rr": (v.get("rr") or {}).get("rr")}
                   for t, v in doc["tickers"].items()}
        print(json.dumps({"out": Path(args.out).as_posix(), "tickers": summary}, ensure_ascii=False))
    else:
        print(common.dumps_json(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
