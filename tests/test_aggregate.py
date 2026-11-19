"""aggregate.py (DESIGN 4.10, SPEC 5 + 10): stance inversion, evidence weighting, DA adjustments,
clamp + half-up rounding, every action-rule boundary, insufficient-evidence cap, limit breaches,
fund rules, OSINT flag resolution, auto invalidation flag, rr stop fallbacks, schema-valid output."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import aggregate as agg
from scripts import common

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "aggregate"
RUN_DATE = "2026-10-02"
PROFILE = {"max_position_pct": 10, "max_fund_position_pct": 40, "max_sector_pct": 35}


# --------------------------------------------------------------------------- helpers
def schema_errors(doc: Any, schema_id: str) -> list[str]:
    resources = []
    for p in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    reg = Registry().with_resources(resources)
    validator = Draft202012Validator(reg.contents(schema_id), registry=reg, format_checker=FormatChecker())
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(doc)]


def template(name: str) -> dict:
    return json.loads((FIX / f"template_{name}.json").read_text(encoding="utf-8"))


class Desk:
    """Writes synthetic reports for one ticker into tmp_path and records their evidence scores."""

    def __init__(self, root: Path, ticker: str = "TST"):
        self.root = root / ticker
        self.root.mkdir(parents=True, exist_ok=True)
        self.ticker = ticker
        self.evidence: dict[str, Any] = {"schema": "evidence/v1", "as_of": RUN_DATE, "reports": {}}

    def _write(self, name: str, doc: dict, role: str, score: float | None, status: str) -> str:
        path = self.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        if score is not None:
            self.evidence["reports"][path.as_posix()] = {
                "agent_role": role, "ticker": self.ticker, "status": status, "score": score,
                "start": 10, "floor": 1, "deductions": []}
        return str(path)

    def report(self, role: str, *, stance: str = "bullish", conviction: int = 7, invalidation: float | None = None,
               fv_base: float | None = None, evidence: float | None = 10, red_flags: list | None = None,
               flag_responses: list | None = None) -> str:
        doc = template(role)
        doc["ticker"] = self.ticker
        doc["stance"] = stance
        doc["conviction"] = conviction
        doc["invalidation_price"] = invalidation
        if role == "fundamental":
            doc["extensions"]["valuation"]["fair_value_base"] = fv_base
        doc["red_flags"] = red_flags or []
        doc["flag_responses"] = flag_responses or []
        return self._write(f"{role}_{self.ticker}.json", doc, role, evidence, "ok")

    def failed(self, role: str) -> str:
        doc = template("failed")
        doc.update({"agent_role": role, "ticker": self.ticker, "order_id": f"p2-a-{role}-{self.ticker}"})
        return self._write(f"{role}_{self.ticker}.failed.json", doc, role, 1, "failed")

    def da(self, verdict: str = "survives", *, evidence: float = 10, red_flags: list | None = None,
           flag_responses: list | None = None) -> str:
        doc = template("devils_advocate")
        doc["ticker"] = self.ticker
        doc["verdict_on_thesis"] = verdict
        doc["red_flags"] = red_flags or []
        doc["flag_responses"] = flag_responses or []
        return self._write(f"devils_advocate_{self.ticker}.json", doc, "devils_advocate", evidence, "ok")

    def da_failed(self) -> str:
        doc = template("failed")
        doc.update({"agent_role": "devils_advocate", "ticker": self.ticker,
                    "order_id": f"p2-b-devils_advocate-{self.ticker}"})
        return self._write(f"devils_advocate_{self.ticker}.failed.json", doc, "devils_advocate", 1, "failed")


def item(desk: Desk, reports: dict[str, str], *, da_path: str | None = None, kind: str = "holding",
         asset_type: str = "stock", price: float | None = 100.0, technicals_path: str | None = None,
         sector: str = "consumer_staples", **context: Any) -> dict:
    ctx = {"position_pct": 5.0, "cap_pct": None, "sector_lookthrough_pct": 20.0, "cio_target_pct": 25.0}
    ctx.update(context)
    px = common.fact(price, "USD/share", f"D:PX:{desk.ticker}", RUN_DATE, "price") if price is not None else None
    return {"ticker": desk.ticker, "kind": kind, "asset_type": asset_type, "sector": sector,
            "sector_class": common.sector_class(sector, asset_type), "price": px, "reports": reports,
            "da_path": da_path, "technicals_path": technicals_path, "context": ctx}


def run_one(desk: Desk, it: dict, cfg: Any = None) -> dict:
    doc = agg.run([it], evidence=desk.evidence, as_of=RUN_DATE, profile=PROFILE, cfg=cfg)
    errors = schema_errors(doc, "urn:agent-investors:data/aggregate")
    assert errors == [], errors
    return doc["tickers"][desk.ticker]


def add_ready(desk: Desk, **over: Any) -> dict:
    """Holding that satisfies every ADD test: conv 8.0, ev 10, rr (130-100)/(100-90) = 3.0."""
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0),
               "technical": desk.report("technical", conviction=8, invalidation=90.0)}
    return item(desk, reports, da_path=desk.da("survives"), **over)


@pytest.fixture()
def desk(tmp_path: Path) -> Desk:
    return Desk(tmp_path)


def labels(res: dict) -> tuple[str, str]:
    return res["rule_action"], res["rule_label"]


# --------------------------------------------------------------------------- conviction math
def test_add_ready_holding(desk: Desk) -> None:
    res = run_one(desk, add_ready(desk))
    assert res["agg_conviction"] == 8.0 and res["agg_evidence"] == 10.0
    assert res["rr"]["rr"] == 3.0 and res["rr"]["invalidation_source"] == "technical_report"
    assert labels(res) == ("ADD", "add")
    assert any(t["rule"] == "R5.add.rr" and t["result"] for t in res["rule_trace"])


def test_bearish_eight_counts_as_two_and_neutral_as_five(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", stance="bearish", conviction=8, fv_base=90.0),
               "technical": desk.report("technical", stance="neutral", conviction=9, invalidation=95.0)}
    res = run_one(desk, item(desk, reports))
    sp = {s["role"]: s for s in res["specialists"]}
    assert sp["fundamental"]["effective_conviction"] == 2.0
    assert sp["technical"]["effective_conviction"] == 5.0
    assert res["conviction_mean"] == 3.5          # (2*10 + 5*10) / 20
    assert res["agg_conviction"] == 3.5
    assert labels(res) == ("TRIM", "trim_conviction")


def test_bearish_ten_clamps_to_lower_bound(desk: Desk) -> None:
    res = run_one(desk, item(desk, {"fundamental": desk.report("fundamental", stance="bearish", conviction=10)}))
    assert res["specialists"][0]["effective_conviction"] == 1.0
    assert res["agg_conviction"] == 1.0
    assert labels(res) == ("SELL", "sell_conviction")


def test_evidence_weighted_mean(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=120.0, evidence=10),
               "technical": desk.report("technical", stance="bearish", conviction=8, invalidation=None, evidence=5)}
    res = run_one(desk, item(desk, reports))
    assert res["conviction_mean"] == 6.0          # (8*10 + 2*5) / 15
    assert res["agg_evidence"] == 7.5             # (10 + 5) / 2
    # an unweighted mean would give 5.0
    assert res["agg_conviction"] == 6.0


def test_osint_and_failed_reports_count_evidence_only(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, evidence=9),
               "technical": desk.report("technical", conviction=6, invalidation=90.0, evidence=9),
               "osint": desk.report("osint", stance="bearish", conviction=9, evidence=6),
               "operator_ceo": desk.failed("operator_ceo")}
    res = run_one(desk, item(desk, reports, da_path=desk.da("survives", evidence=8)))
    sp = {s["role"]: s for s in res["specialists"]}
    assert sp["osint"]["in_mean"] is False and sp["osint"]["effective_conviction"] == 1.0
    assert sp["operator_ceo"]["status"] == "failed" and sp["operator_ceo"]["in_mean"] is False
    assert sp["operator_ceo"]["evidence"] == 1
    assert res["conviction_mean"] == 7.0          # (8*9 + 6*9) / 18
    assert res["agg_evidence"] == 6.6             # (9 + 9 + 6 + 1 + 8) / 5 = 6.6
    assert any("operator_ceo" in n and "failed" in n for n in res["notes"])


def test_all_specialists_failed_gives_null_conviction(desk: Desk) -> None:
    reports = {"fundamental": desk.failed("fundamental"), "technical": desk.failed("technical")}
    res = run_one(desk, item(desk, reports, da_path=desk.da("survives")))
    assert res["agg_conviction"] is None and res["conviction_mean"] is None
    assert res["agg_evidence"] == 4.0             # (1 + 1 + 10) / 3
    assert labels(res) == ("HOLD", "insufficient_evidence")
    res2 = run_one(desk, item(desk, reports, da_path=desk.da("survives"), position_pct=12.0))
    assert labels(res2) == ("TRIM", "insufficient_evidence")


# --------------------------------------------------------------------------- devil's advocate
@pytest.mark.parametrize("verdict,adj,conv,action", [
    ("survives", 0, 8.0, "ADD"),
    ("damaged", -1, 7.0, "ADD"),
    ("broken", -3, 5.0, "SELL"),
])
def test_da_adjustments(desk: Desk, verdict: str, adj: int, conv: float, action: str) -> None:
    it = add_ready(desk)
    it["da_path"] = desk.da(verdict)
    res = run_one(desk, it)
    assert res["da"]["verdict"] == verdict and res["da"]["adj"] == adj
    assert res["agg_conviction"] == conv
    assert res["rule_action"] == action
    if verdict == "broken":
        assert res["rule_label"] == "sell_da_broken"


def test_da_failed_or_missing_adjusts_zero_and_counts_evidence_one(desk: Desk) -> None:
    it = add_ready(desk)
    it["da_path"] = desk.da_failed()
    res = run_one(desk, it)
    assert res["da"]["status"] == "failed" and res["da"]["adj"] == 0 and res["da"]["verdict"] is None
    assert "adjustment 0" in res["da"]["note"]
    assert res["agg_conviction"] == 8.0
    assert res["agg_evidence"] == 7.0             # (10 + 10 + 1) / 3
    it["da_path"] = str(desk.root / "devils_advocate_TST.json.missing")
    res2 = run_one(desk, it)
    assert res2["da"]["status"] == "failed" and res2["da"]["adj"] == 0
    assert res2["agg_evidence"] == 7.0
    # no DA at all (e.g. funds): adj 0, nothing counted
    it["da_path"] = None
    res3 = run_one(desk, it)
    assert res3["da"]["status"] == "not_run" and res3["agg_evidence"] == 10.0


def test_clamp_after_da_adjustment(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=2, fv_base=130.0),
               "technical": desk.report("technical", conviction=2, invalidation=90.0)}
    res = run_one(desk, item(desk, reports, da_path=desk.da("broken")))
    assert res["conviction_mean"] == 2.0
    assert res["agg_conviction"] == 1.0           # 2 - 3 = -1 -> clamped to 1


def test_mean_rounds_half_up(desk: Desk) -> None:
    # (7*10 + 7*9 + 6*1) / 20 = 139/20 = 6.95 -> 7.0 (ROUND_HALF_UP on the exact decimal)
    reports = {"fundamental": desk.report("fundamental", conviction=7, fv_base=130.0, evidence=10),
               "technical": desk.report("technical", conviction=7, invalidation=90.0, evidence=9),
               "operator_ceo": desk.report("operator_ceo", conviction=6, evidence=1)}
    res = run_one(desk, item(desk, reports))
    assert res["conviction_mean"] == 6.95
    assert res["agg_conviction"] == 7.0
    assert res["agg_evidence"] == 6.7             # 20/3 = 6.666.. -> 6.7
    assert labels(res) == ("ADD", "add")
    # one conviction point less on the low-evidence report: 138/20 = 6.9 -> no ADD
    desk.report("operator_ceo", conviction=5, evidence=1)
    res2 = run_one(desk, item(desk, reports))
    assert res2["agg_conviction"] == 6.9
    assert labels(res2) == ("HOLD", "hold")


# --------------------------------------------------------------------------- rule boundaries
BASE = dict(agg_conviction=8.0, agg_evidence=8.0, rr=3.0, da_verdict="survives", unresolved_high=False,
            limit_breach=False, position_pct=5.0, cap_pct=10.0)


def decide(kind: str = "holding", asset_type: str = "stock", **over: Any) -> tuple[str, str, list]:
    args = dict(BASE)
    args.update(over)
    return agg.apply_rules(kind, asset_type, **args)


@pytest.mark.parametrize("over,expected", [
    # ADD conviction >= 7.0 (6.95 rounds half-up to 7.0; 6.94 -> 6.9)
    ({"agg_conviction": 7.0}, "ADD"), ({"agg_conviction": 6.95}, "ADD"),
    ({"agg_conviction": 6.94}, "HOLD"), ({"agg_conviction": 6.9}, "HOLD"),
    # ADD evidence >= 6.0
    ({"agg_evidence": 6.0}, "ADD"), ({"agg_evidence": 5.95}, "ADD"),
    ({"agg_evidence": 5.94}, "HOLD"), ({"agg_evidence": 5.9}, "HOLD"),
    # ADD rr >= 2.0 (2 dp)
    ({"rr": 2.0}, "ADD"), ({"rr": 1.995}, "ADD"), ({"rr": 1.99}, "HOLD"), ({"rr": None}, "HOLD"),
    # ADD needs position < cap
    ({"position_pct": 9.99}, "ADD"), ({"position_pct": 10.0}, "HOLD"), ({"position_pct": None}, "HOLD"),
    ({"cap_pct": None}, "ADD"),
    # ADD blocked by an unresolved high flag
    ({"unresolved_high": True}, "HOLD"),
    # SELL conviction <= 3.0 (3.05 -> 3.1 -> conviction TRIM; 3.04 -> 3.0 -> SELL)
    ({"agg_conviction": 3.0}, "SELL"), ({"agg_conviction": 3.04}, "SELL"),
    ({"agg_conviction": 3.05}, "TRIM"), ({"agg_conviction": 3.1}, "TRIM"),
    # TRIM 3.0 < conviction < 4.5 (4.45 -> 4.5 -> HOLD; 4.44 -> 4.4 -> TRIM)
    ({"agg_conviction": 4.4}, "TRIM"), ({"agg_conviction": 4.44}, "TRIM"),
    ({"agg_conviction": 4.45}, "HOLD"), ({"agg_conviction": 4.5}, "HOLD"),
    # DA broken -> SELL even at high conviction
    ({"da_verdict": "broken"}, "SELL"), ({"da_verdict": "damaged"}, "ADD"), ({"da_verdict": None}, "ADD"),
    # limit breach -> TRIM (before ADD)
    ({"limit_breach": True}, "TRIM"),
])
def test_holding_boundaries(over: dict, expected: str) -> None:
    action, _, trace = decide(**over)
    assert action == expected
    assert trace and all({"rule", "test", "result"} <= set(t) for t in trace)


def test_rule_trace_records_rounded_values() -> None:
    action, label, trace = decide(agg_conviction=6.95, rr=1.995)
    assert (action, label) == ("ADD", "add")
    conv = next(t for t in trace if t["rule"] == "R5.add.conviction")
    rr = next(t for t in trace if t["rule"] == "R5.add.rr")
    assert conv["values"]["agg_conviction"] == 7.0 and "7.0 >= 7" in conv["test"]
    assert rr["values"]["rr"] == 2.0


@pytest.mark.parametrize("over,expected,label", [
    ({"agg_evidence": 4.94, "agg_conviction": 2.0}, "HOLD", "insufficient_evidence"),
    ({"agg_evidence": 4.94, "agg_conviction": 2.0, "limit_breach": True}, "TRIM", "insufficient_evidence"),
    ({"agg_evidence": 4.9, "agg_conviction": 9.0}, "HOLD", "insufficient_evidence"),
    ({"agg_evidence": 4.95, "agg_conviction": 2.0}, "SELL", "sell_conviction"),     # 4.95 -> 5.0 passes
    ({"agg_evidence": 5.0, "agg_conviction": 9.0, "rr": 3.0}, "HOLD", "hold"),         # ADD needs ev >= 6
    ({"agg_evidence": None}, "HOLD", "insufficient_evidence"),
    ({"agg_conviction": None}, "HOLD", "insufficient_evidence"),
    ({"agg_conviction": None, "limit_breach": True}, "TRIM", "insufficient_evidence"),
    ({"agg_evidence": 4.9, "da_verdict": "broken"}, "HOLD", "insufficient_evidence"),
])
def test_insufficient_evidence_cap(over: dict, expected: str, label: str) -> None:
    action, lab, _ = decide(**over)
    assert (action, lab) == (expected, label)


@pytest.mark.parametrize("over,expected", [
    ({}, "BUY"),
    ({"rr": 2.5}, "BUY"), ({"rr": 2.495}, "BUY"), ({"rr": 2.49}, "PASS"), ({"rr": None}, "PASS"),
    ({"agg_conviction": 7.0}, "BUY"), ({"agg_conviction": 6.94}, "PASS"),
    ({"agg_evidence": 6.0}, "BUY"), ({"agg_evidence": 5.94}, "PASS"),
    ({"da_verdict": "broken"}, "PASS"), ({"da_verdict": "damaged"}, "BUY"),
    ({"unresolved_high": True}, "BUY"),          # block_on_unresolved_high_flag: false (default)
    ({"agg_conviction": None}, "PASS"),
    ({"limit_breach": True}, "BUY"),             # candidates are sized later; no breach rule
])
def test_candidate_boundaries(over: dict, expected: str) -> None:
    assert decide(kind="candidate", **over)[0] == expected


def test_candidate_flag_block_is_configurable() -> None:
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["actions"]["candidate"]["block_on_unresolved_high_flag"] = True
    assert agg.apply_rules("candidate", "stock", **dict(BASE, unresolved_high=True), cfg=cfg)[0] == "PASS"
    assert agg.apply_rules("candidate", "stock", **dict(BASE, unresolved_high=False), cfg=cfg)[0] == "BUY"


def test_thresholds_are_read_from_config() -> None:
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["actions"]["holding"]["add_conviction"] = 8.5
    assert agg.apply_rules("holding", "stock", **BASE, cfg=cfg)[0] == "HOLD"
    cfg["actions"]["holding"]["add_conviction"] = 7
    cfg["actions"]["holding"]["sell_max"] = 8.0
    cfg["actions"]["holding"]["trim_below"] = 9.0
    assert agg.apply_rules("holding", "stock", **BASE, cfg=cfg)[0] == "SELL"


def test_fund_rules_skip_rr() -> None:
    assert decide(asset_type="fund", rr=None, cap_pct=40.0, position_pct=20.0)[0] == "ADD"
    assert decide(kind="candidate", asset_type="fund", rr=None)[0] == "BUY"


# --------------------------------------------------------------------------- limit breaches
def test_limit_breach_trim_by_position_cap(desk: Desk) -> None:
    res = run_one(desk, add_ready(desk, position_pct=10.5))   # cap from profile: 10
    assert res["inputs"]["cap_pct"] == 10 and res["inputs"]["limit_breach"] is True
    assert labels(res) == ("TRIM", "limit_breach")
    res2 = run_one(desk, add_ready(desk, position_pct=10.0))
    assert res2["inputs"]["limit_breach"] is False
    assert labels(res2) == ("HOLD", "hold")                  # ADD fails position < cap
    res3 = run_one(desk, add_ready(desk, position_pct=10.004))  # rounds to 10.00 -> no breach
    assert res3["inputs"]["limit_breach"] is False


@pytest.mark.parametrize("sector_pct,cio_target,breach", [
    (31.01, 30.0, True),     # > min(35, 30) + 1
    (31.0, 30.0, False),
    (36.5, 40.0, True),      # > min(35, 40) + 1 = 36
    (36.0, 40.0, False),
    (36.5, None, True),      # no CIO target -> profile max_sector_pct only
])
def test_limit_breach_trim_by_sector(desk: Desk, sector_pct: float, cio_target: float | None, breach: bool) -> None:
    res = run_one(desk, add_ready(desk, sector_lookthrough_pct=sector_pct, cio_target_pct=cio_target))
    assert res["inputs"]["limit_breach"] is breach
    assert res["rule_action"] == ("TRIM" if breach else "ADD")
    if breach:
        assert any("sector look-through" in r for r in res["inputs"]["limit_breach_reasons"])


def test_sector_breach_ignored_for_funds_and_tolerance_from_config(desk: Desk) -> None:
    trace: list = []
    breach, reasons, _ = agg.compute_limit_breach(
        "fund", position_pct=20, cap_pct=40, sector_pct=80, cio_target_pct=30, max_sector_pct=35,
        tolerance_pp=1.0, trace=trace)
    assert breach is False and reasons == []
    assert any(t["rule"] == "limit_breach.sector" and t["result"] is False for t in trace)
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["actions"]["sector_breach_tolerance_pp"] = 3.0
    res = run_one(desk, add_ready(desk, sector_lookthrough_pct=32.5, cio_target_pct=30.0), cfg=cfg)
    assert res["inputs"]["limit_breach"] is False and res["inputs"]["sector_limit_pct"] == 33.0


# --------------------------------------------------------------------------- funds
def test_fund_holding_adds_without_rr_and_uses_fund_cap(desk: Desk) -> None:
    fund = Desk(desk.root.parent, "QQQ")
    reports = {"etf": fund.report("etf", conviction=8), "technical": fund.report("technical", conviction=8,
                                                                                 invalidation=90.0)}
    it = item(fund, reports, asset_type="fund", sector="funds", position_pct=20.0, sector_lookthrough_pct=60.0)
    res = run_one(fund, it)
    assert res["sector_class"] == "fund"
    assert res["rr"]["rr"] is None and res["rr"]["null_reason"] == "not_applicable"
    assert res["inputs"]["cap_pct"] == 40 and res["inputs"]["limit_breach"] is False
    assert labels(res) == ("ADD", "add")
    assert any(t["rule"] == "R5.add.rr" and "fund" in t["test"] for t in res["rule_trace"])
    over = run_one(fund, item(fund, reports, asset_type="fund", sector="funds", position_pct=41.0))
    assert labels(over) == ("TRIM", "limit_breach")


# --------------------------------------------------------------------------- flags
OSINT_HIGH = [{"id": "F1", "flag": "Pending litigation could force a large payment.", "severity": "high",
               "must_address": True, "source_ids": ["W2"]}]


def _flag_case(desk: Desk, responses: list | None, *, osint_flags: list | None = None, da_responses=None) -> dict:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, flag_responses=responses),
               "technical": desk.report("technical", conviction=8, invalidation=90.0),
               "osint": desk.report("osint", stance="neutral", conviction=5,
                                    red_flags=OSINT_HIGH if osint_flags is None else osint_flags)}
    return run_one(desk, item(desk, reports, da_path=desk.da("survives", flag_responses=da_responses)))


def test_unresolved_osint_high_flag_blocks_add(desk: Desk) -> None:
    res = _flag_case(desk, None)
    assert [f["flag_id"] for f in res["flags"]["unresolved"]] == ["osint:F1"]
    assert labels(res) == ("HOLD", "hold")
    assert any(t["rule"] == "R5.add.flags" and t["result"] is False for t in res["rule_trace"])


@pytest.mark.parametrize("status,resolved", [("mitigated", True), ("refuted", True), ("confirmed", False)])
def test_flag_responses_resolve_or_not(desk: Desk, status: str, resolved: bool) -> None:
    resp = [{"flag_ref": "osint:F1", "status": status, "note": "Filings show a reserve covering the exposure.",
             "source_ids": ["W1"]}]
    res = _flag_case(desk, resp)
    flag = next(f for f in res["flags"]["all"] if f["flag_id"] == "osint:F1")
    assert flag["resolved"] is resolved
    assert flag["responses"][0]["role"] == "fundamental"
    assert (res["flags"]["unresolved"] == []) is resolved
    assert res["rule_action"] == ("ADD" if resolved else "HOLD")


def test_da_response_also_resolves(desk: Desk) -> None:
    resp = [{"flag_ref": "osint:F1", "status": "refuted", "note": "Case was dismissed.", "source_ids": ["W1"]}]
    res = _flag_case(desk, None, da_responses=resp)
    assert res["flags"]["unresolved"] == [] and res["rule_action"] == "ADD"


def test_only_high_must_address_flags_of_configured_roles_are_unresolved(desk: Desk) -> None:
    flags = [{"id": "F1", "flag": "Medium issue.", "severity": "medium", "must_address": True, "source_ids": []},
             {"id": "F2", "flag": "High but informational.", "severity": "high", "must_address": False,
              "source_ids": []}]
    res = _flag_case(desk, None, osint_flags=flags)
    assert {f["flag_id"] for f in res["flags"]["all"]} == {"osint:F1", "osint:F2"}
    assert res["flags"]["unresolved"] == [] and res["rule_action"] == "ADD"
    # a high must_address flag from a non-OSINT role is collected but not "unresolved"
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, red_flags=OSINT_HIGH),
               "technical": desk.report("technical", conviction=8, invalidation=90.0)}
    res2 = run_one(desk, item(desk, reports))
    assert [f["flag_id"] for f in res2["flags"]["all"]] == ["fundamental:F1"]
    assert res2["flags"]["unresolved"] == []


def test_auto_invalidation_flag(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, invalidation=105.0),
               "technical": desk.report("technical", conviction=8, invalidation=90.0)}
    res = run_one(desk, item(desk, reports, da_path=desk.da()))
    auto = [f for f in res["flags"]["unresolved"] if f["flag_id"] == "auto:invalidation_breached"]
    assert len(auto) == 1 and auto[0]["severity"] == "high" and auto[0]["must_address"] is True
    assert "fundamental" in auto[0]["flag"]
    assert labels(res) == ("HOLD", "hold")          # blocks ADD
    # neutral at exactly the price also breaches
    reports["fundamental"] = desk.report("fundamental", stance="neutral", conviction=8, fv_base=130.0,
                                         invalidation=100.0)
    res2 = run_one(desk, item(desk, reports))
    assert any(f["flag_id"] == "auto:invalidation_breached" for f in res2["flags"]["unresolved"])


def test_bearish_invalidation_above_price_is_ignored(desk: Desk) -> None:
    tech = str(FIX / "technicals_TST.json")
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0),
               "technical": desk.report("technical", stance="bearish", conviction=3, invalidation=110.0)}
    res = run_one(desk, item(desk, reports, technicals_path=tech))
    assert not any(f["flag_id"] == "auto:invalidation_breached" for f in res["flags"]["all"])
    assert res["rr"]["invalidation_source"] == "technicals_stop_candidate"
    assert res["rr"]["invalidation"] == 94.0


# --------------------------------------------------------------------------- rr stop fallbacks
def test_stop_candidate_fallback(desk: Desk) -> None:
    tech = str(FIX / "technicals_TST.json")   # stops 93.5, 94.0, 101.0
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0),
               "technical": desk.failed("technical")}
    res = run_one(desk, item(desk, reports, technicals_path=tech))
    rr = res["rr"]
    assert rr["invalidation_source"] == "technicals_stop_candidate"
    assert rr["invalidation"] == 94.0 and rr["invalidation_ref"] == "STOP2"   # highest stop below 100
    assert rr["rr"] == 5.0                                                     # 30 / 6
    assert rr["upside_pct"] == 30.0 and rr["downside_pct"] == 6.0


def test_specialist_median_fallback_and_none(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, invalidation=92.0),
               "operator_ceo": desk.report("operator_ceo", conviction=7, invalidation=96.0),
               "osint": desk.report("osint", stance="bearish", conviction=6, invalidation=120.0)}
    res = run_one(desk, item(desk, reports))
    assert res["rr"]["invalidation_source"] == "specialist_median"
    assert res["rr"]["invalidation"] == 94.0     # median of 92 and 96 (bearish 120 above price ignored)
    assert not any(f["flag_id"] == "auto:invalidation_breached" for f in res["flags"]["all"])
    none = run_one(desk, item(desk, {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0)}))
    assert none["rr"]["invalidation_source"] == "none"
    assert none["rr"]["rr"] is None and none["rr"]["null_reason"] == "missing_input"


def test_rr_null_without_price_or_fair_value(desk: Desk) -> None:
    res = run_one(desk, add_ready(desk, price=None))
    assert res["rr"]["rr"] is None and res["rr"]["null_reason"] == "missing_input"
    assert res["inputs"]["price"] is None
    reports = {"fundamental": desk.failed("fundamental"),
               "technical": desk.report("technical", conviction=8, invalidation=90.0)}
    res2 = run_one(desk, item(desk, reports))
    assert res2["rr"]["fv_base"] is None and res2["rr"]["rr"] is None


# --------------------------------------------------------------------------- candidates, evidence lookup, run/CLI
def test_candidate_buy_and_pass(desk: Desk) -> None:
    it = add_ready(desk, kind="candidate", position_pct=None)
    res = run_one(desk, it)
    assert labels(res) == ("BUY", "buy")
    it["da_path"] = desk.da("broken")
    res2 = run_one(desk, it)
    assert res2["rule_action"] == "PASS"


def test_evidence_lookup_tolerates_path_forms_and_scores_inline(desk: Desk) -> None:
    it = add_ready(desk)
    # re-key evidence with backslash paths: still found
    desk.evidence["reports"] = {k.replace("/", "\\"): v for k, v in desk.evidence["reports"].items()}
    res = run_one(desk, it)
    assert res["agg_evidence"] == 10.0 and not any("inline" in n for n in res["notes"])
    # evidence missing entirely: scored inline from the report
    res2 = agg.run([it], evidence={}, as_of=RUN_DATE, profile=PROFILE)["tickers"]["TST"]
    assert any("scored inline" in n for n in res2["notes"])
    assert res2["agg_evidence"] is not None


def test_da_inside_reports_and_bare_price_are_accepted(desk: Desk) -> None:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0),
               "technical": desk.report("technical", conviction=8, invalidation=90.0),
               "devils_advocate": desk.da("damaged")}
    it = item(desk, reports)
    it["price"] = 100.0
    res = run_one(desk, it)
    assert res["da"]["verdict"] == "damaged" and res["agg_conviction"] == 7.0
    assert [s["role"] for s in res["specialists"]] == ["fundamental", "technical"]
    assert res["inputs"]["price"]["value"] == 100.0 and res["inputs"]["price"]["source_id"] == "D:PX:TST"
    assert res["rr"]["rr"] == 3.0


def test_run_writes_schema_valid_file_and_cli(desk: Desk, tmp_path: Path, capsys) -> None:
    other = Desk(tmp_path, "OTH")
    items = [add_ready(desk),
             item(other, {"fundamental": other.report("fundamental", conviction=8, fv_base=130.0),
                          "technical": other.failed("technical")},
                  da_path=other.da_failed(), technicals_path=str(FIX / "technicals_TST.json"))]
    evidence = {"schema": "evidence/v1", "as_of": RUN_DATE,
                "reports": {**desk.evidence["reports"], **other.evidence["reports"]}}
    out = tmp_path / "phase2" / "aggregate.json"
    doc = agg.run(items, out, evidence=evidence, as_of=RUN_DATE, profile=PROFILE)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert schema_errors(on_disk, "urn:agent-investors:data/aggregate") == []
    assert set(on_disk["tickers"]) == {"TST", "OTH"}
    assert on_disk["schema"] == "aggregate/v1" and on_disk["as_of"] == RUN_DATE
    assert any(f["code"] == "report_failed" for f in doc["flags"])
    # CLI
    items_path = tmp_path / "items.json"
    items_path.write_text(json.dumps(items), encoding="utf-8")
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence), encoding="utf-8")
    cli_out = tmp_path / "cli_aggregate.json"
    assert agg.main(["--items", str(items_path), "--evidence", str(ev_path), "--out", str(cli_out),
                     "--as-of", RUN_DATE]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["tickers"]["TST"]["rule_action"] == doc["tickers"]["TST"]["rule_action"]
    assert json.loads(cli_out.read_text(encoding="utf-8"))["tickers"]["OTH"]["rr"]["invalidation_source"] == \
        "technicals_stop_candidate"


# --------------------------------------------------------------------------- BUY gates: DA must have run, breached stop
def _candidate(desk: Desk, **over: Any) -> dict:
    return add_ready(desk, kind="candidate", position_pct=None, **over)


def _trace(res: dict, rule: str) -> dict:
    return next(t for t in res["rule_trace"] if t["rule"] == rule)


@pytest.mark.parametrize("da_verdict", [None, "", "unknown"])
def test_candidate_without_a_devils_advocate_verdict_is_pass_da_unavailable(da_verdict: Any) -> None:
    action, label, trace = agg.apply_rules("candidate", "stock", **dict(BASE, da_verdict=da_verdict))
    assert (action, label) == ("PASS", "da_unavailable")
    gate = next(t for t in trace if t["rule"] == "C2.buy.da_ran")
    assert gate["result"] is False and "stricter than SPEC" in gate["test"]
    # every other BUY test passed: the DA gate alone decided
    assert all(t["result"] for t in trace if t["rule"] != "C2.buy.da_ran")


def test_holding_rules_do_not_need_the_devils_advocate() -> None:
    assert decide(da_verdict=None)[:2] == ("ADD", "add")
    assert decide(asset_type="fund", da_verdict=None, rr=None, cap_pct=40.0)[0] == "ADD"
    assert not any(t["rule"].startswith("C2.") for t in decide(da_verdict=None)[2])


@pytest.mark.parametrize("how", ["not_run", "failed_stub", "missing_file", "no_verdict"])
def test_candidate_buy_needs_the_da_to_have_run_ok(desk: Desk, how: str) -> None:
    it = _candidate(desk)
    assert labels(run_one(desk, it)) == ("BUY", "buy")                  # control: DA survives
    if how == "not_run":
        it["da_path"] = None
    elif how == "failed_stub":
        it["da_path"] = desk.da_failed()
    elif how == "missing_file":
        it["da_path"] = str(desk.root / "devils_advocate_TST.json.missing")
    else:                                                                # status ok, verdict absent
        it["da_path"] = desk.da(None)
    res = run_one(desk, it)
    assert labels(res) == ("PASS", "da_unavailable")
    assert res["agg_conviction"] == 8.0 and res["rr"]["rr"] == 3.0       # every other BUY input is fine
    assert res["da"]["verdict"] is None
    gate = _trace(res, "C2.buy.da_ran")
    assert gate["result"] is False and gate["values"]["da_verdict"] is None


def test_candidate_with_a_da_buys_and_holdings_are_unaffected(desk: Desk) -> None:
    it = _candidate(desk)
    it["da_path"] = desk.da("damaged")                 # 8 - 1 = 7.0 still meets buy_conviction 7
    res = run_one(desk, it)
    assert labels(res) == ("BUY", "buy") and _trace(res, "C2.buy.da_ran")["result"] is True
    # holdings: a failed / absent DA adjusts 0 and never blocks ADD
    hold = add_ready(desk)
    hold["da_path"] = desk.da_failed()
    assert labels(run_one(desk, hold)) == ("ADD", "add")
    hold["da_path"] = None
    assert labels(run_one(desk, hold)) == ("ADD", "add")


def test_auto_invalidation_flag_always_blocks_candidate_buy(desk: Desk) -> None:
    # bullish fundamental stop (105) is above the price (100): thesis already invalidated
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0, invalidation=105.0),
               "technical": desk.report("technical", conviction=8, invalidation=90.0)}
    it = item(desk, reports, da_path=desk.da("survives"), kind="candidate", position_pct=None)
    res = run_one(desk, it)                       # default config: block_on_unresolved_high_flag false
    assert res["rr"]["rr"] == 3.0 and res["agg_conviction"] == 8.0 and res["agg_evidence"] == 10.0
    assert [f["flag_id"] for f in res["flags"]["unresolved"]] == ["auto:invalidation_breached"]
    assert labels(res) == ("PASS", "invalidation_breached")
    gate = _trace(res, "C2.buy.invalidation")
    assert gate["result"] is False and gate["values"]["invalidation_breached"] is True
    assert "always blocks BUY" in gate["test"]
    # the ordinary-flag switch stays off by default and does not matter here
    cfg = copy.deepcopy(common.committee_cfg())
    assert cfg["actions"]["candidate"]["block_on_unresolved_high_flag"] is False
    cfg["actions"]["candidate"]["block_on_unresolved_high_flag"] = True
    assert labels(run_one(desk, it, cfg=cfg)) == ("PASS", "invalidation_breached")
    # a neutral specialist at exactly the price also blocks
    reports["fundamental"] = desk.report("fundamental", stance="neutral", conviction=8, fv_base=130.0,
                                         invalidation=100.0)
    assert labels(run_one(desk, it)) == ("PASS", "invalidation_breached")


def test_apply_rules_invalidation_breached_blocks_regardless_of_the_flag_switch() -> None:
    for switch in (False, True):
        cfg = copy.deepcopy(common.committee_cfg())
        cfg["actions"]["candidate"]["block_on_unresolved_high_flag"] = switch
        action, label, trace = agg.apply_rules("candidate", "stock",
                                               **dict(BASE, unresolved_high=True, invalidation_breached=True), cfg=cfg)
        assert (action, label) == ("PASS", "invalidation_breached")
        assert next(t for t in trace if t["rule"] == "C2.buy.invalidation")["result"] is False
    # an ordinary unresolved high flag still only blocks when the switch is on
    off = copy.deepcopy(common.committee_cfg())
    assert agg.apply_rules("candidate", "stock", **dict(BASE, unresolved_high=True), cfg=off)[:2] == ("BUY", "buy")
    on = copy.deepcopy(common.committee_cfg())
    on["actions"]["candidate"]["block_on_unresolved_high_flag"] = True
    assert agg.apply_rules("candidate", "stock", **dict(BASE, unresolved_high=True), cfg=on)[:2] == ("PASS", "pass")
    # holdings: the breach is not a candidate gate (it already blocks ADD through the unresolved flag)
    assert decide(invalidation_breached=True)[0] == "ADD"
    assert decide(unresolved_high=True, invalidation_breached=True)[0] == "HOLD"


def test_candidate_label_priority_da_unavailable_then_invalidation_then_pass() -> None:
    both = dict(BASE, da_verdict=None, invalidation_breached=True)
    assert agg.apply_rules("candidate", "stock", **both)[:2] == ("PASS", "da_unavailable")
    assert agg.apply_rules("candidate", "stock", **dict(BASE, invalidation_breached=True))[:2] == \
        ("PASS", "invalidation_breached")
    assert agg.apply_rules("candidate", "stock", **dict(BASE, rr=1.0))[:2] == ("PASS", "pass")
    assert agg.apply_rules("candidate", "stock", **dict(BASE, da_verdict="broken"))[:2] == ("PASS", "pass")
    # a null conviction keeps its own label (checked before the BUY tests)
    assert agg.apply_rules("candidate", "stock", **dict(BASE, agg_conviction=None, da_verdict=None))[:2] == \
        ("PASS", "insufficient_evidence")


# --------------------------------------------------------------------------- rr: stop too tight
def _tight_case(desk: Desk, stop: float, **kw: Any) -> dict:
    reports = {"fundamental": desk.report("fundamental", conviction=8, fv_base=130.0),
               "technical": desk.report("technical", conviction=8, invalidation=stop)}
    return item(desk, reports, da_path=desk.da("survives"), **kw)


def test_rr_is_null_when_the_stop_is_closer_than_min_downside_pct(desk: Desk) -> None:
    res = run_one(desk, _tight_case(desk, 99.5, kind="candidate", position_pct=None))
    rr = res["rr"]
    assert rr["rr"] is None and rr["null_reason"] == "stop_too_tight"
    assert rr["upside_pct"] == 30.0 and rr["downside_pct"] == 0.5 and rr["invalidation"] == 99.5
    assert any("stop too tight" in n and "min_downside_pct" in n for n in res["notes"])
    assert labels(res) == ("PASS", "pass")                    # rr None fails the BUY rr test
    assert _trace(res, "C2.buy.rr")["result"] is False
    holding = run_one(desk, _tight_case(desk, 99.5))
    assert holding["rr"]["null_reason"] == "stop_too_tight" and labels(holding) == ("HOLD", "hold")


def test_rr_min_downside_boundary_inside_aggregate(desk: Desk) -> None:
    at = run_one(desk, _tight_case(desk, 99.0, kind="candidate", position_pct=None))     # exactly 1.0%
    assert at["rr"]["null_reason"] is None and at["rr"]["rr"] == 30.0 and at["rr"]["downside_pct"] == 1.0
    assert labels(at) == ("BUY", "buy")
    assert not any("stop too tight" in n for n in at["notes"])
    below = run_one(desk, _tight_case(desk, 99.01, kind="candidate", position_pct=None))  # 0.99%
    assert below["rr"]["rr"] is None and below["rr"]["null_reason"] == "stop_too_tight"
    assert labels(below) == ("PASS", "pass")


def test_rr_min_downside_is_read_from_config(desk: Desk) -> None:
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["risk_reward"]["min_downside_pct"] = 5.0
    res = run_one(desk, _tight_case(desk, 96.0), cfg=cfg)                                  # 4% < 5%
    assert res["rr"]["null_reason"] == "stop_too_tight"
    cfg["risk_reward"]["min_downside_pct"] = 0
    res0 = run_one(desk, _tight_case(desk, 99.999999), cfg=cfg)                            # guard disabled
    assert res0["rr"]["null_reason"] is None and res0["rr"]["rr"] == 30_000_000.0
    # a config without the key falls back to 1.0
    del cfg["risk_reward"]["min_downside_pct"]
    assert run_one(desk, _tight_case(desk, 99.5), cfg=cfg)["rr"]["null_reason"] == "stop_too_tight"
