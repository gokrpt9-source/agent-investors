"""Schema contracts (DESIGN 5): every schema compiles under draft 2020-12, every $ref resolves through a
registry built from all schema files (by $id), every shipped example validates, and the key structural
rules reject bad documents. Pure offline test: only jsonschema + referencing."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"
DATA_SCHEMAS_DIR = SCHEMAS_DIR / "data"
EXAMPLES_DIR = SCHEMAS_DIR / "examples"
URN = "urn:agent-investors:"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

AGENT_SCHEMAS = ["common_defs", "specialist_report", "devils_advocate_report", "etf_review",
                 "sector_verdict", "macro_view", "cio_allocation", "cio_final", "failed_report"]
DATA_SCHEMAS = ["prices", "technicals", "fundamentals", "valuation", "macro", "lookthrough", "screen",
                "evidence", "aggregate", "portfolio_plan", "manifest", "work_order"]
SPECIALIST_ROLES = ["fundamental", "technical", "operator_ceo", "industry_ops", "osint"]
EXPECTED_EXAMPLES = [f"specialist_report.{r}" for r in SPECIALIST_ROLES] + [
    "devils_advocate_report", "etf_review", "sector_verdict", "macro_view", "cio_allocation",
    "cio_final", "failed_report"]


# --------------------------------------------------------------------------- helpers
def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_files() -> list[Path]:
    return sorted(SCHEMAS_DIR.glob("*.json")) + sorted(DATA_SCHEMAS_DIR.glob("*.json"))


def _example_files() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.example.json"))


def _example_schema_name(path: Path) -> str:
    return path.name.split(".")[0]


def _iter_refs(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


@pytest.fixture(scope="module")
def schemas_by_id() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in _schema_files():
        schema = _load(path)
        sid = schema.get("$id")
        assert sid, f"{path.name}: missing $id"
        assert sid not in out, f"duplicate $id {sid} ({path.name})"
        out[sid] = schema
    return out


@pytest.fixture(scope="module")
def registry(schemas_by_id: dict[str, dict]) -> Registry:
    resources = [(sid, Resource.from_contents(s, default_specification=DRAFT202012))
                 for sid, s in schemas_by_id.items()]
    return Registry().with_resources(resources)


@pytest.fixture(scope="module")
def validate(schemas_by_id: dict[str, dict], registry: Registry) -> Callable[[Any, str], list[str]]:
    cache: dict[str, Draft202012Validator] = {}

    def _validate(doc: Any, name: str) -> list[str]:
        if name not in cache:
            cache[name] = Draft202012Validator(schemas_by_id[URN + name], registry=registry,
                                               format_checker=FormatChecker())
        errors = sorted(cache[name].iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
        return [f"{e.json_path}: {e.message}" for e in errors]

    return _validate


def example(name: str) -> dict:
    return copy.deepcopy(_load(EXAMPLES_DIR / f"{name}.example.json"))


# --------------------------------------------------------------------------- compile / ids / refs
def test_owned_schema_files_exist() -> None:
    missing = [n for n in AGENT_SCHEMAS if not (SCHEMAS_DIR / f"{n}.json").is_file()]
    missing += [f"data/{n}" for n in DATA_SCHEMAS if not (DATA_SCHEMAS_DIR / f"{n}.schema.json").is_file()]
    assert not missing, f"missing schema files: {missing}"


@pytest.mark.parametrize("path", _schema_files(), ids=lambda p: p.relative_to(SCHEMAS_DIR).as_posix())
def test_schema_compiles(path: Path) -> None:
    schema = _load(path)
    assert schema.get("$schema") == DRAFT, f"{path.name}: $schema must be draft 2020-12"
    assert str(schema.get("$id", "")).startswith(URN), f"{path.name}: $id must start with {URN}"
    Draft202012Validator.check_schema(schema)


def test_owned_ids_match_file_names(schemas_by_id: dict[str, dict]) -> None:
    for name in AGENT_SCHEMAS:
        assert _load(SCHEMAS_DIR / f"{name}.json")["$id"] == URN + name
    for name in DATA_SCHEMAS:
        assert _load(DATA_SCHEMAS_DIR / f"{name}.schema.json")["$id"] == f"{URN}data/{name}"


def test_every_ref_resolves(schemas_by_id: dict[str, dict], registry: Registry) -> None:
    checked = 0
    for sid, schema in schemas_by_id.items():
        resolver = registry.resolver(base_uri=sid)
        for ref in _iter_refs(schema):
            assert ref.startswith("#") or ref.startswith(URN), f"{sid}: external $ref {ref!r}"
            resolver.lookup(ref)  # raises referencing.exceptions.Unresolvable
            checked += 1
    assert checked > 50


def test_agent_schemas_reject_unknown_top_level_keys_but_allow_comment(
        schemas_by_id: dict[str, dict]) -> None:
    for name in AGENT_SCHEMAS:
        if name in ("common_defs", "failed_report"):
            continue
        schema = schemas_by_id[URN + name]
        assert schema.get("additionalProperties") is False, name
        assert "_comment" in schema["properties"], name


# --------------------------------------------------------------------------- examples
def test_expected_examples_exist() -> None:
    present = {p.name[: -len(".example.json")] for p in _example_files()}
    assert set(EXPECTED_EXAMPLES) <= present, sorted(set(EXPECTED_EXAMPLES) - present)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_validates(path: Path, validate) -> None:
    doc = _load(path)
    assert doc.get("_comment"), "examples must carry a _comment marking numbers as placeholders"
    errors = validate(doc, _example_schema_name(path))
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("role", SPECIALIST_ROLES)
def test_specialist_example_role_matches_file(role: str) -> None:
    doc = example(f"specialist_report.{role}")
    assert doc["agent_role"] == role and doc["ticker"] == "KO" and doc["sector"] == "consumer_staples"
    assert doc["run_id"] == "2026-10-03_1015"


def _cited_ids(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("evidence_refs", "source_ids") and isinstance(value, list):
                yield from value
            elif key == "source_id" and isinstance(value, str):
                yield value
            else:
                yield from _cited_ids(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cited_ids(item)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_example_citations_resolve_to_sources(path: Path) -> None:
    doc = _load(path)
    source_ids = [s["id"] for s in doc.get("sources", [])]
    assert len(source_ids) == len(set(source_ids)), "duplicate source ids"
    dangling = sorted(set(_cited_ids(doc)) - set(source_ids))
    assert not dangling, f"cited ids not in sources[]: {dangling}"


# --------------------------------------------------------------------------- negative / conditional cases
def _at(doc: Any, path: tuple):
    for key in path[:-1]:
        doc = doc[key]
    return doc, path[-1]


def setp(path: tuple, value: Any) -> Callable[[dict], None]:
    def _m(doc: dict) -> None:
        parent, key = _at(doc, path)
        parent[key] = copy.deepcopy(value)
    return _m


def delp(path: tuple) -> Callable[[dict], None]:
    def _m(doc: dict) -> None:
        parent, key = _at(doc, path)
        del parent[key]
    return _m


def both(*mutations: Callable[[dict], None]) -> Callable[[dict], None]:
    def _m(doc: dict) -> None:
        for m in mutations:
            m(doc)
    return _m


BUY_VERDICT = {
    "ticker": "MRK", "kind": "candidate", "action": "BUY", "rule_action": "BUY", "rule_label": None,
    "agg_conviction": 7.4, "agg_evidence": 7.0, "rr": 2.7,
    "rationale": "Rules pass with margin; DA verdict survives.", "dissents": [], "unresolved_red_flags": [],
    "rule_deviation": False, "thesis": "Placeholder thesis for a candidate.",
    "fair_value_low": 95.0, "fair_value_base": 118.0, "fair_value_high": 140.0, "invalidation_price": 88.0,
}
FUND = "specialist_report.fundamental"
TECH = "specialist_report.technical"
OPS = "specialist_report.operator_ceo"
IND = "specialist_report.industry_ops"
OSI = "specialist_report.osint"
SR = "specialist_report"
STRENGTH = {"point": "A strength.", "evidence_refs": ["D:SEC:KO:CF"]}
SHORT_JUST = "Because."

INVALID_CASES: list[tuple[str, str, str, Callable[[dict], None]]] = [
    # --- specialist report: shared rules
    ("strengths_4_items", FUND, SR, setp(("strengths",), [STRENGTH] * 4)),
    ("risks_4_items", FUND, SR, setp(("risks",), [{"point": "r", "evidence_refs": ["W1"], "severity": "low"}] * 4)),
    ("stance_very_bullish", FUND, SR, setp(("stance",), "very bullish")),
    ("conviction_11", FUND, SR, setp(("conviction",), 11)),
    ("conviction_0", FUND, SR, setp(("conviction",), 0)),
    ("conviction_not_integer", FUND, SR, setp(("conviction",), 6.5)),
    ("self_evidence_11", FUND, SR, setp(("self_evidence",), 11)),
    ("thesis_too_long", FUND, SR, setp(("thesis",), "word " * 200)),
    ("thesis_empty", FUND, SR, setp(("thesis",), "")),
    ("unknown_top_level_key", FUND, SR, setp(("foo",), 1)),
    ("missing_extensions", FUND, SR, delp(("extensions",))),
    ("bad_run_id", FUND, SR, setp(("run_id",), "20261003")),
    ("impossible_date", FUND, SR, setp(("as_of",), "2026-02-30")),
    ("malformed_date", FUND, SR, setp(("as_of",), "03/10/2026")),
    ("unknown_role", FUND, SR, setp(("agent_role",), "quant")),
    ("unknown_sector", FUND, SR, setp(("sector",), "tech")),
    ("lowercase_ticker", FUND, SR, setp(("ticker",), "ko")),
    ("empty_sources", FUND, SR, setp(("sources",), [])),
    ("source_ftp_url", FUND, SR, setp(("sources", 0, "url"), "ftp://example.com/file")),
    ("source_local_path_url", FUND, SR, setp(("sources", 0, "url"), "C:/data/fundamentals_KO.json")),
    ("source_bad_kind", FUND, SR, setp(("sources", 0, "kind"), "blog")),
    ("source_missing_accessed", FUND, SR, delp(("sources", 0, "accessed"))),
    ("risk_without_severity", FUND, SR, delp(("risks", 0, "severity"))),
    ("strength_without_evidence", FUND, SR, setp(("strengths", 0, "evidence_refs"), [])),
    ("key_figure_number_without_source", FUND, SR, setp(("key_figures", 0, "source_id"), None)),
    ("key_figure_number_without_as_of", FUND, SR, setp(("key_figures", 0, "as_of"), None)),
    ("key_figure_dataset_without_data_ref", FUND, SR, delp(("key_figures", 0, "data_ref"))),
    ("key_figure_bad_kind", FUND, SR, setp(("key_figures", 0, "kind"), "valuation")),
    ("key_figure_data_ref_without_pointer", FUND, SR,
     setp(("key_figures", 0, "data_ref"), "runs/x/phase2/data/fundamentals_KO.json")),
    ("invalidation_price_negative", FUND, SR, setp(("invalidation_price",), -5)),
    ("red_flag_bad_id", OSI, SR, setp(("red_flags", 0, "id"), "X1")),
    ("red_flag_must_address_string", OSI, SR, setp(("red_flags", 0, "must_address"), "yes")),
    ("red_flag_bad_severity", OSI, SR, setp(("red_flags", 0, "severity"), "critical")),
    ("flag_response_bad_ref", FUND, SR, setp(("flag_responses",), [
        {"flag_ref": "osint-F1", "status": "refuted", "note": "n", "source_ids": ["W1"]}])),
    ("flag_response_bad_status", FUND, SR, setp(("flag_responses",), [
        {"flag_ref": "osint:F1", "status": "ignored", "note": "n", "source_ids": ["W1"]}])),
    # --- specialist report: role extensions
    ("fundamental_without_valuation", FUND, SR, delp(("extensions", "valuation"))),
    ("fundamental_valuation_missing_cells_base", FUND, SR, delp(("extensions", "valuation", "cells_base"))),
    ("fundamental_fair_value_without_cells", FUND, SR, setp(("extensions", "valuation", "cells_base"), [])),
    ("fundamental_fair_value_string", FUND, SR, setp(("extensions", "valuation", "fair_value_base"), "68")),
    ("fundamental_missing_quality_notes", FUND, SR, delp(("extensions", "quality_notes"))),
    ("fundamental_with_technical_extension", FUND, SR,
     setp(("extensions",), example(TECH)["extensions"])),
    ("technical_bad_regime", TECH, SR, setp(("extensions", "trend_regime"), "sideways")),
    ("technical_missing_key_levels", TECH, SR, delp(("extensions", "key_levels"))),
    ("technical_invalidation_ref_null_with_price", TECH, SR, setp(("extensions", "invalidation_ref"), None)),
    ("technical_level_not_computed_id", TECH, SR, setp(("extensions", "key_levels", 0, "ref_id"), "SMA200")),
    ("technical_level_bad_type", TECH, SR, setp(("extensions", "key_levels", 0, "type"), "pivot")),
    ("technical_no_indicator_refs", TECH, SR, setp(("extensions", "indicator_refs"), [])),
    ("technical_with_valuation", TECH, SR,
     setp(("extensions", "valuation"), example(FUND)["extensions"]["valuation"])),
    ("operator_bad_net_direction", OPS, SR, setp(("extensions", "insider_activity", "net_direction"), "sold")),
    ("operator_missing_guidance", OPS, SR, delp(("extensions", "guidance_vs_delivery"))),
    ("operator_assessment_without_sources", OPS, SR, delp(("extensions", "incentives", "source_ids"))),
    ("industry_missing_moat", IND, SR, delp(("extensions", "operational_moat"))),
    ("osint_missing_signals", OSI, SR, delp(("extensions", "signals"))),
    ("osint_signal_without_sources", OSI, SR, setp(("extensions", "signals", 0, "source_ids"), [])),
    ("osint_signal_bad_trend", OSI, SR, setp(("extensions", "signals", 0, "trend"), "up")),
    # --- devil's advocate
    ("da_no_attacks", "devils_advocate_report", "devils_advocate_report", setp(("attacks",), [])),
    ("da_no_kill_criteria", "devils_advocate_report", "devils_advocate_report", setp(("kill_criteria",), [])),
    ("da_bear_price_without_ref", "devils_advocate_report", "devils_advocate_report", delp(("bear_case_ref",))),
    ("da_bear_price_null_ref", "devils_advocate_report", "devils_advocate_report", setp(("bear_case_ref",), None)),
    ("da_bad_verdict", "devils_advocate_report", "devils_advocate_report", setp(("verdict_on_thesis",), "dead")),
    ("da_attack_bad_target", "devils_advocate_report", "devils_advocate_report",
     setp(("attacks", 0, "target_agent"), "cio")),
    ("da_attack_without_sources", "devils_advocate_report", "devils_advocate_report",
     setp(("attacks", 0, "source_ids"), [])),
    ("da_wrong_role", "devils_advocate_report", "devils_advocate_report", setp(("agent_role",), "fundamental")),
    # --- etf review
    ("etf_buy_action", "etf_review", "etf_review", setp(("action",), "BUY")),
    ("etf_bad_role", "etf_review", "etf_review", setp(("role_in_portfolio",), "core-ish")),
    ("etf_negative_expense_ratio", "etf_review", "etf_review", setp(("expense_ratio_pct",), -1)),
    ("etf_missing_momentum", "etf_review", "etf_review", delp(("momentum_vs_benchmark",))),
    ("etf_bad_lookthrough_sector", "etf_review", "etf_review",
     setp(("lookthrough_summary", "top_sectors", 0, "sector"), "tech")),
    # --- sector verdict
    ("verdict_deviation_empty_justification", "sector_verdict", "sector_verdict",
     setp(("verdicts", 1, "deviation_justification"), "")),
    ("verdict_deviation_null_justification", "sector_verdict", "sector_verdict",
     setp(("verdicts", 1, "deviation_justification"), None)),
    ("verdict_deviation_missing_justification", "sector_verdict", "sector_verdict",
     delp(("verdicts", 1, "deviation_justification"))),
    ("verdict_deviation_short_justification", "sector_verdict", "sector_verdict",
     setp(("verdicts", 1, "deviation_justification"), SHORT_JUST)),
    ("verdict_action_differs_without_deviation", "sector_verdict", "sector_verdict",
     setp(("verdicts", 0, "action"), "TRIM")),
    ("verdict_holding_with_buy", "sector_verdict", "sector_verdict",
     lambda d: d["verdicts"][0].update(action="BUY", rule_action="BUY", thesis="Complete BUY fields.",
                                       fair_value_low=60.0, fair_value_base=75.0, fair_value_high=90.0,
                                       invalidation_price=62.0)),
    ("verdict_buy_empty_thesis", "sector_verdict", "sector_verdict",
     lambda d: d.update(phase=3, verdicts=[{**BUY_VERDICT, "thesis": ""}])),
    ("verdict_candidate_with_hold", "sector_verdict", "sector_verdict",
     setp(("verdicts", 0, "kind"), "candidate")),
    ("verdict_bad_phase", "sector_verdict", "sector_verdict", setp(("phase",), 4)),
    ("verdict_wrong_role", "sector_verdict", "sector_verdict", setp(("agent_role",), "cio")),
    ("verdict_conviction_out_of_range", "sector_verdict", "sector_verdict",
     setp(("verdicts", 0, "agg_conviction"), 12)),
    ("verdict_rationale_too_long", "sector_verdict", "sector_verdict",
     setp(("verdicts", 0, "rationale"), "word " * 200)),
    # --- macro view
    ("macro_probability_1_5", "macro_view", "macro_view", setp(("scenarios", 0, "probability"), 1.5)),
    ("macro_probability_negative", "macro_view", "macro_view", setp(("scenarios", 0, "probability"), -0.1)),
    ("macro_one_scenario", "macro_view", "macro_view", lambda d: d.__setitem__("scenarios", d["scenarios"][:1])),
    ("macro_six_scenarios", "macro_view", "macro_view",
     lambda d: d.__setitem__("scenarios", (d["scenarios"] * 2)[:6])),
    ("macro_one_driver", "macro_view", "macro_view", lambda d: d.__setitem__("drivers", d["drivers"][:1])),
    ("macro_min_weight_120", "macro_view", "macro_view", setp(("sector_constraints", 0, "min_weight"), 120)),
    ("macro_max_weight_negative", "macro_view", "macro_view", setp(("sector_constraints", 0, "max_weight"), -1)),
    ("macro_constraint_funds", "macro_view", "macro_view", setp(("sector_constraints", 0, "sector"), "funds")),
    ("macro_driver_without_sources", "macro_view", "macro_view", setp(("drivers", 0, "source_ids"), [])),
    ("macro_bad_direction", "macro_view", "macro_view", setp(("drivers", 0, "direction"), "up")),
    # --- cio allocation
    ("cio_alloc_bad_sector_key", "cio_allocation", "cio_allocation", setp(("target_sector_weights", "tech"), 5.0)),
    ("cio_alloc_weight_over_100", "cio_allocation", "cio_allocation",
     setp(("target_sector_weights", "financials"), 120)),
    ("cio_alloc_expand_funds", "cio_allocation", "cio_allocation", setp(("expand_sectors",), ["funds"])),
    ("cio_alloc_expand_duplicates", "cio_allocation", "cio_allocation",
     setp(("expand_sectors",), ["health_care", "health_care"])),
    ("cio_alloc_short_override_justification", "cio_allocation", "cio_allocation",
     setp(("constraint_overrides", 0, "justification"), SHORT_JUST)),
    ("cio_alloc_bad_bound", "cio_allocation", "cio_allocation",
     setp(("constraint_overrides", 0, "macro_bound"), "cap")),
    ("cio_alloc_wrong_mode", "cio_allocation", "cio_allocation", setp(("mode",), "reconciliation")),
    # --- cio final
    ("cio_final_six_bullets", "cio_final", "cio_final",
     lambda d: d.__setitem__("executive_summary", ["Bullet."] * 6)),
    ("cio_final_override_without_justification", "cio_final", "cio_final",
     delp(("final_actions", 2, "override_justification"))),
    ("cio_final_override_short_justification", "cio_final", "cio_final",
     setp(("final_actions", 2, "override_justification"), SHORT_JUST)),
    ("cio_final_action_differs_without_override", "cio_final", "cio_final",
     setp(("final_actions", 0, "action"), "SELL")),
    ("cio_final_six_risks", "cio_final", "cio_final", setp(("top_risks",), ["Risk."] * 6)),
    ("cio_final_no_reconciliation", "cio_final", "cio_final", setp(("reconciliation",), [])),
    ("cio_final_wrong_mode", "cio_final", "cio_final", setp(("mode",), "allocation")),
    # --- failed report
    ("failed_status_ok", "failed_report", "failed_report", setp(("status",), "ok")),
    ("failed_zero_attempts", "failed_report", "failed_report", setp(("attempts",), 0)),
    ("failed_missing_errors", "failed_report", "failed_report", delp(("errors",))),
    ("failed_bad_role", "failed_report", "failed_report", setp(("agent_role",), "trader")),
]


@pytest.mark.parametrize("case_id, example_name, schema_name, mutate", INVALID_CASES,
                         ids=[c[0] for c in INVALID_CASES])
def test_invalid_documents_are_rejected(case_id, example_name, schema_name, mutate, validate) -> None:
    doc = example(example_name)
    assert not validate(doc, schema_name), f"base example {example_name} must be valid"
    mutate(doc)
    assert validate(doc, schema_name), f"{case_id}: mutated document unexpectedly valid"


VALID_VARIANTS: list[tuple[str, str, str, Callable[[dict], None]]] = [
    ("empty_strengths_and_risks", FUND, SR, both(setp(("strengths",), []), setp(("risks",), []))),
    ("technical_without_invalidation", TECH, SR,
     both(setp(("invalidation_price",), None), setp(("extensions", "invalidation_ref"), None))),
    ("null_key_figure_listed_as_missing", OPS, SR, lambda d: None),
    ("config_sourced_figure_without_data_ref", FUND, SR, setp(("key_figures",), [
        {"name": "P/E band mid", "value": 22, "unit": "x", "source_id": "D:CFG:committee",
         "as_of": "2026-10-03", "kind": "other"}])),
    ("fair_values_null_without_cells", FUND, SR, lambda d: d["extensions"]["valuation"].update(
        fair_value_low=None, fair_value_base=None, fair_value_high=None,
        cells_low=[], cells_base=[], cells_high=[])),
    ("da_without_bear_price", "devils_advocate_report", "devils_advocate_report",
     both(setp(("bear_case_price",), None), delp(("bear_case_ref",)))),
    ("phase3_buy_verdict_complete", "sector_verdict", "sector_verdict",
     both(setp(("phase",), 3), setp(("sector",), "health_care"), setp(("verdicts",), [BUY_VERDICT]))),
    ("phase3_zero_proposals", "sector_verdict", "sector_verdict",
     both(setp(("phase",), 3), setp(("verdicts",), []))),
    ("verdict_empty_optional_prose_when_not_deviating", "sector_verdict", "sector_verdict",
     lambda d: d["verdicts"][0].update(deviation_justification="", da_rebuttal="", thesis="")),
    ("verdict_deviation_on_sell_rebuttal", "sector_verdict", "sector_verdict",
     lambda d: d["verdicts"][0].update(
         rule_action="SELL", rule_deviation=True,
         da_rebuttal="The DA's broken verdict rests on a one-off tax item already reserved.",
         deviation_justification=("The SELL comes only from the DA broken verdict, which the sector head "
                                  "rebuts because the cited exposure is reserved and non-recurring."))),
    ("cio_final_action_without_source_verdict", "cio_final", "cio_final",
     both(setp(("final_actions", 0, "source_verdict_action"), None), setp(("final_actions", 0, "action"), "TRIM"))),
    ("failed_report_extra_keys_allowed", "failed_report", "failed_report", setp(("written_at",), "x")),
]


@pytest.mark.parametrize("case_id, example_name, schema_name, mutate", VALID_VARIANTS,
                         ids=[c[0] for c in VALID_VARIANTS])
def test_valid_variants_are_accepted(case_id, example_name, schema_name, mutate, validate) -> None:
    doc = example(example_name)
    mutate(doc)
    errors = validate(doc, schema_name)
    assert not errors, f"{case_id}:\n" + "\n".join(errors)


BUY_MISSING = ["thesis", "fair_value_low", "fair_value_base", "fair_value_high", "invalidation_price"]


@pytest.mark.parametrize("field", BUY_MISSING)
def test_buy_verdict_requires_thesis_fair_values_and_invalidation(field: str, validate) -> None:
    doc = example("sector_verdict")
    verdict = copy.deepcopy(BUY_VERDICT)
    del verdict[field]
    doc.update(phase=3, sector="health_care", verdicts=[verdict])
    assert validate(doc, "sector_verdict"), f"BUY without {field} accepted"
    verdict2 = copy.deepcopy(BUY_VERDICT)
    if field != "thesis":
        verdict2[field] = None
        doc["verdicts"] = [verdict2]
        assert validate(doc, "sector_verdict"), f"BUY with null {field} accepted"


# --------------------------------------------------------------------------- data contracts
AS_OF = "2026-10-02"
TS = "2026-10-03T10:15:00+00:00"
DATA_SOURCE = {"id": "D:PX:KO", "url": "https://finance.yahoo.com/quote/KO/history/",
               "title": "KO daily price history (yfinance)", "accessed": "2026-10-03", "kind": "dataset"}


def fact(value: Any = 1.0, unit: str = "USD", source_id: str | None = "D:PX:KO", kind: str = "price",
         na_reason: str | None = None, **extra: Any) -> dict:
    if value is None and na_reason is None:
        na_reason = "missing"
    return {"value": value, "unit": unit, "source_id": source_id, "as_of": AS_OF, "kind": kind,
            "na_reason": na_reason, **extra}


def facts(*keys: str, unit: str = "USD", kind: str = "price") -> dict:
    return {k: fact(1.0, unit, kind=kind) for k in keys}


def env(schema: str, **payload: Any) -> dict:
    return {"schema": schema, "generated_at": TS, "as_of": AS_OF, "sources": [DATA_SOURCE],
            "missing_data": [], "flags": [{"code": "stale_cache", "severity": "high", "detail": "demo"}],
            **payload}


def data_doc(name: str) -> dict:
    if name == "prices":
        return env("prices/v1", tickers={"KO": {
            "last_close": fact(69.85, "USD/share"), "last_date": AS_OF, "first_date": "2023-10-02", "rows": 753,
            "provider": "yfinance", "stale": False, "adv_usd_63d": fact(1.1e9),
            "returns_pct": facts("1m", "3m", "6m", "12m", unit="pct"),
            "series_path": "data/cache/prices/prices_KO.csv"}},
            failed={"XYZ": "no provider returned data"},
            fx={"usdils": fact(3.7, "ILS/USD", "D:PX:ILS=X")})
    if name == "technicals":
        return env("technicals/v1", ticker="KO", benchmark="SPY", sector_etf="XLP", close=fact(69.85),
                   trend={**facts("sma20", "sma50", "sma200", "sma200_slope_20d_pct", "close_vs_sma200_pct"),
                          "regime": "range", "regime_tests": [{"test": "adx_lt_20", "value": 16.8, "pass": True}],
                          "structure": "transition"},
                   momentum={**facts("rsi14", "adx14", "pdi14", "ndi14"), "macd": facts("line", "signal", "hist")},
                   volatility=facts("atr14", "atr14_pct", "atr_pctile_252"),
                   range=facts("high_52w", "low_52w", "pct_from_high_52w", "drawdown_from_3y_peak_pct",
                               "max_drawdown_1y_pct"),
                   relative_strength={"vs_spy": facts("3m", "6m", "12m"), "vs_sector": facts("3m", "6m", "12m")},
                   volume={**facts("avg20", "avg50", "ratio_20_50", "adv_usd_63d"), "trend": "flat"},
                   levels={"supports": [{"id": "S1", "price": fact(66.8), "touches": 3, "last_date": AS_OF}],
                           "resistances": [{"id": "R1", "price": fact(73.1), "touches": 2, "last_date": AS_OF}]},
                   stop_candidates=[{"id": "STOP1", "price": fact(65.9), "basis": "below_support_0.5atr"}],
                   weekly_closes_52w=[["2026-09-25", 69.1], ["2026-10-02", 69.85]])
    if name == "fundamentals":
        f = lambda *k: facts(*k, kind="fundamental")  # noqa: E731
        return env("fundamentals/v1", ticker="KO", cik=21344, coverage="sec",
                   entity={"name": "COCA COLA CO", "sic": "2080", "sic_description": "Beverages",
                           "fiscal_year_end": "1231", "sector_class": "default"},
                   companyfacts_latest={"form": "10-Q", "filed": "2026-07-28", "period_end": "2026-06-27",
                                        "accn": "0000021344-26-000040"},
                   submissions_latest={"form": "10-Q", "filed": "2026-07-28", "report_date": "2026-06-27",
                                       "accn": "0000021344-26-000040"},
                   lag_detected=False, price=fact(69.85), shares=fact(4.3e9, "shares", kind="fundamental"),
                   market_cap=fact(3.0e11),
                   ttm=f("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "fcf",
                         "da", "ebitda", "pretax_income", "income_tax"),
                   fy=[{"fy": 2025, "period_end": "2025-12-31", **f("revenue", "net_income")}],
                   balance=f("cash", "st_investments", "total_debt", "net_debt", "equity", "goodwill",
                             "intangibles"),
                   margins_pct=f("gross", "operating", "net", "fcf"),
                   growth_pct=f("revenue_cagr_3y", "ni_cagr_3y", "fcf_cagr_3y", "revenue_yoy_ttm"),
                   valuation={**f("ev", "pe", "ev_ebit", "ev_sales", "fcf_yield_pct", "net_debt_ebitda",
                                  "p_tbv", "roe_pct", "roic_pct", "tax_rate_used"),
                              "p_b": fact(None, "x", kind="fundamental", na_reason="not_meaningful")},
                   tag_map={"revenue": {"tag": "Revenues", "stitched": False}})
    if name == "valuation":
        return env("valuation/v1", ticker="KO", price=fact(69.85), shares=fact(4.3e9, "shares"),
                   net_debt=fact(3.6e10), base_cf=facts("nopat", "fcf"),
                   reverse_dcf={"implied_growth_pct": fact(6.2, "pct"), "assumptions": {"r": 0.10, "exit": 15}},
                   cells=[{"id": "DCF-NOPAT-r10-g03", "method": "dcf_nopat", "params": {"r": 0.1, "g": 0.03},
                           "fv_per_share": 64.2, "tv_share": 0.71, "flags": [], "na_reason": None}],
                   methods_available=["dcf_nopat", "dcf_fcf", "pe", "ev_ebit"])
    if name == "macro":
        return env("macro/v1", series={"DFF": {
            "latest": fact(3.88, "pct", "D:FRED:DFF", "macro"), "observation_date": AS_OF, "frequency": "d",
            "chg_3m": fact(-0.25, "pp", "D:FRED:DFF", "macro"), "chg_12m": fact(-0.6, "pp", "D:FRED:DFF", "macro"),
            "yoy_pct": None, "history_tail": [["2026-09-01", 3.9], ["2026-10-01", None]]}})
    if name == "lookthrough":
        return env("lookthrough/v1", total_value_usd=fact(250000.0),
                   holdings=[{"ticker": "QQQ", "asset_type": "fund", "weight_pct": fact(40.0, "pct"),
                              "sector": "funds", "sector_split": {"information_technology": 51.8,
                                                                  "unclassified": 48.2},
                              "classification_source": "yf_funds", "flags": []}],
                   sector_weights_pct={"information_technology": 60.0, "cash": 5.0, "unclassified": 35.0},
                   funds={"QQQ": {"expense_ratio_pct": fact(0.2, "pct", "D:YF:QQQ:FUND", "other"),
                                  "top10": [{"symbol": "NVDA", "weight_pct": 9.1}],
                                  "top10_concentration_pct": fact(52.7, "pct", "D:YF:QQQ:FUND", "other"),
                                  "source_id": "D:YF:QQQ:FUND"}},
                   overlap={"pairs": [{"a": "QQQ", "b": "SPMO", "overlap_pct": 38.5, "basis": "top10"}],
                            "direct_overlap": []})
    if name == "screen":
        comp = {"score": 0.6, "inputs": {"op_margin": 0.3}}
        return env("screen/v1", sector="health_care", benchmark_etf="XLV",
                   universe=[{"ticker": "MRK", "origin": "etf_top10", "etf_weight_pct": 4.2}],
                   excluded=[{"ticker": "JNJ", "reason": "held"}], weights={"quality": 0.4, "valuation": 0.3,
                                                                           "momentum": 0.3},
                   ranked=[{"ticker": "MRK", "rank": 1, "composite": 0.63,
                            "components": {"quality": comp, "valuation": comp, "momentum": comp},
                            "raw": {"mcap": fact(2.5e11), "adv_usd": fact(1.2e9)}, "imputed": []}],
                   shortlist=["MRK"])
    if name == "evidence":
        return env("evidence/v1", reports={"runs/x/phase2/reports/fundamental_KO.json": {
            "agent_role": "fundamental", "ticker": "KO", "status": "ok", "score": 7, "start": 10, "floor": 1,
            "deductions": [{"rule": "few_sources", "detail": "2 distinct URLs", "points": -2},
                           {"rule": "missing_data", "detail": "1 item", "points": -1}]}})
    if name == "aggregate":
        return env("aggregate/v1", tickers={"KO": {
            "kind": "holding", "asset_type": "stock", "sector": "consumer_staples", "sector_class": "default",
            "inputs": {"price": fact(69.85), "position_pct": 8.0, "cap_pct": 10.0, "sector_lookthrough_pct": 22.0,
                       "cio_target_pct": 18.0, "limit_breach": False},
            "specialists": [{"role": "fundamental", "path": "runs/x/phase2/reports/fundamental_KO.json",
                             "status": "ok", "stance": "neutral", "conviction": 6, "effective_conviction": 5,
                             "evidence": 7, "in_mean": True}],
            "conviction_mean": 5.6, "da": {"path": "runs/x/da.json", "verdict": "damaged", "adj": -1},
            "agg_conviction": 4.6, "agg_evidence": 7.2,
            "rr": {"price": 69.85, "fv_base": 67.9, "invalidation": 65.9, "invalidation_source": "technical",
                   "upside_pct": -2.8, "downside_pct": 5.7, "rr": -0.49, "null_reason": None},
            "flags": {"all": [{"flag_id": "osint:F1", "severity": "high"}],
                      "unresolved": [{"flag_id": "osint:F1", "severity": "high"}]},
            "rule_action": "HOLD", "rule_label": None,
            "rule_trace": [{"rule": "sell", "test": "conv <= 3.0", "result": False}]}})
    if name == "portfolio_plan":
        return env("portfolio_plan/v1", base_currency="ILS", fx_usdils=fact(3.7, "ILS/USD", "D:PX:ILS=X"),
                   total_value_usd=fact(250000.0),
                   cash={"current_pct": 5.0, "target_pct": 5.0, "post_pct": 5.2},
                   positions=[{"ticker": "JPM", "asset_type": "stock", "sector": "financials", "action": "TRIM",
                               "action_source": "cio_override", "price": 300.0, "shares_cur": 80,
                               "value_usd_cur": 24000.0, "weight_cur_pct": 9.6, "weight_tgt_pct": 8.0,
                               "trade": {"side": "SELL", "shares": 13, "value_usd": 3900.0, "value_ils": 14430.0},
                               "dropped_reason": None,
                               "tax": {"applicable": True, "est_gain_usd": 1200.0, "est_tax_usd": 300.0,
                                       "note": "Israeli tax applies to real (CPI/FX-adjusted) gains."}}],
                   sectors=[{"sector": "financials", "cur_lookthrough_pct": 25.0, "post_lookthrough_pct": 23.4,
                             "target_pct": 23.0, "max_pct": 35.0, "gap_pp": 0.4, "breach": False}],
                   constraint_checks=[{"check": "max_sector_pct", "status": "pass", "detail": "all <= 35%"}],
                   notes=["Shares rounded down to whole shares."])
    if name == "manifest":
        return {"run_id": "2026-10-03_1015", "run_dir": "runs/2026-10-03_1015", "mode": {"dry_run": False, "quick": False, "sectors": None, "holding": None},
                "as_of": "2026-10-03", "created_at": TS, "sectors": None, "holding": None,
                "config_sha256": {"committee.yaml": "a" * 64}, "steps": {"p1.data": "done"}}
    if name == "work_order":
        return {"order_id": "p2-a-fundamental-KO", "run_id": "2026-10-03_1015", "step": "p2.wave_a",
                "agent_type": "fundamental-analyst", "agent_role": "fundamental", "mode": None, "ticker": "KO",
                "sector": "consumer_staples", "sector_class": "default", "attempt": 1, "as_of": "2026-10-03",
                "inputs": [{"path": "C:/x/phase2/data/fundamentals_KO.json", "purpose": "SEC fundamentals"}],
                "output": "C:/x/phase2/reports/fundamental_KO.json", "schema": "C:/x/schemas/specialist_report.json",
                "seed_sources": [DATA_SOURCE], "required_fields": ["extensions.valuation.fair_value_base"],
                "prior_errors": [], "word_limits": {"thesis": 60}}
    raise KeyError(name)


@pytest.mark.parametrize("name", DATA_SCHEMAS)
def test_minimal_data_documents_validate(name: str, validate) -> None:
    errors = validate(data_doc(name), f"data/{name}")
    assert not errors, "\n".join(errors)


DATA_INVALID: list[tuple[str, str, Callable[[dict], None]]] = [
    ("fact_null_without_na_reason", "prices", lambda d: d["tickers"]["KO"]["last_close"].update(
        value=None, na_reason=None)),
    ("fact_unknown_na_reason", "prices", lambda d: d["tickers"]["KO"]["last_close"].update(
        value=None, na_reason="bogus")),
    ("fact_missing_kind", "prices", lambda d: d["tickers"]["KO"]["last_close"].pop("kind")),
    ("fact_value_string", "prices", lambda d: d["tickers"]["KO"]["last_close"].update(value="69.85")),
    ("envelope_missing_generated_at", "prices", lambda d: d.pop("generated_at")),
    ("envelope_bad_timestamp", "prices", lambda d: d.update(generated_at="2026-10-03 10:15")),
    ("envelope_flag_bad_severity", "prices", lambda d: d["flags"][0].update(severity="fatal")),
    ("wrong_schema_name", "prices", lambda d: d.update(schema="prices/v2")),
    ("prices_bad_provider", "prices", lambda d: d["tickers"]["KO"].update(provider="bloomberg")),
    ("technicals_bad_regime", "technicals", lambda d: d["trend"].update(regime="bullish")),
    ("technicals_bad_support_id", "technicals", lambda d: d["levels"]["supports"][0].update(id="R9")),
    ("technicals_missing_rsi", "technicals", lambda d: d["momentum"].pop("rsi14")),
    ("fundamentals_missing_ttm_revenue", "fundamentals", lambda d: d["ttm"].pop("revenue")),
    ("fundamentals_bad_coverage", "fundamentals", lambda d: d.update(coverage="full")),
    ("valuation_bad_method", "valuation", lambda d: d["cells"][0].update(method="dcf")),
    ("macro_bad_frequency", "macro", lambda d: d["series"]["DFF"].update(frequency="q")),
    ("lookthrough_bad_sector_key", "lookthrough", lambda d: d["sector_weights_pct"].update(tech=1.0)),
    ("screen_bad_exclusion_reason", "screen", lambda d: d["excluded"][0].update(reason="other")),
    ("evidence_bad_status", "evidence", lambda d: next(iter(d["reports"].values())).update(status="partial")),
    ("evidence_positive_deduction", "evidence",
     lambda d: next(iter(d["reports"].values()))["deductions"][0].update(points=2)),
    ("aggregate_bad_rule_action", "aggregate", lambda d: d["tickers"]["KO"].update(rule_action="BUY_MORE")),
    ("aggregate_flag_without_id", "aggregate", lambda d: d["tickers"]["KO"]["flags"]["unresolved"][0].pop("flag_id")),
    ("portfolio_bad_trade_side", "portfolio_plan", lambda d: d["positions"][0]["trade"].update(side="HOLD")),
    ("portfolio_bad_check_status", "portfolio_plan", lambda d: d["constraint_checks"][0].update(status="ok")),
    ("manifest_missing_run_id", "manifest", lambda d: d.pop("run_id")),
    ("manifest_bad_sha", "manifest", lambda d: d.update(config_sha256="xyz")),
    ("work_order_bad_agent_type", "work_order", lambda d: d.update(agent_type="random-agent")),
    ("work_order_bad_mode", "work_order", lambda d: d.update(mode="full")),
    ("work_order_zero_attempt", "work_order", lambda d: d.update(attempt=0)),
]


@pytest.mark.parametrize("case_id, name, mutate", DATA_INVALID, ids=[c[0] for c in DATA_INVALID])
def test_invalid_data_documents_are_rejected(case_id, name, mutate, validate) -> None:
    doc = data_doc(name)
    mutate(doc)
    assert validate(doc, f"data/{name}"), f"{case_id}: mutated data document unexpectedly valid"
