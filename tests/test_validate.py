"""scripts/validate.py (DESIGN §4.13): semantic checks (positive + negative), context handling,
work-order based validation and the CLI. Context fixtures live in tests/fixtures/validate/run/
(a miniature run directory whose data files match the shipped schema examples)."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import common, validate

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "tests" / "fixtures" / "validate" / "run"
REL = "tests/fixtures/validate/run/"
RUN_ID = "2026-10-03_1015"
SEMANTIC_MSG = re.compile(r"^\$[^ ]*: .+; expected .+, got .+$")

DATA = {
    "technicals": RUN / "phase2" / "data" / "technicals_KO.json",
    "fundamentals": RUN / "phase2" / "data" / "fundamentals_KO.json",
    "valuation": RUN / "phase2" / "data" / "valuation_KO.json",
    "aggregate": RUN / "phase2" / "aggregate.json",
    "macro": RUN / "phase1" / "macro.json",
    "macro_view": RUN / "phase1" / "macro_view.json",
    "lookthrough": RUN / "phase1" / "lookthrough.json",
}
VERDICTS = [RUN / "phase2" / "consumer_staples_verdict.json", RUN / "phase2" / "financials_verdict.json",
            RUN / "phase2" / "funds_verdict.json", RUN / "phase3" / "health_care_verdict.json"]
REPORTS = {
    "fundamental": ("phase2/reports/fundamental_KO.json", "specialist_report"),
    "technical": ("phase2/reports/technical_KO.json", "specialist_report"),
    "operator_ceo": ("phase2/reports/operator_ceo_KO.json", "specialist_report"),
    "industry_ops": ("phase2/reports/industry_ops_KO.json", "specialist_report"),
    "osint": ("phase2/reports/osint_KO.json", "specialist_report"),
    "devils_advocate": ("phase2/reports/devils_advocate_KO.json", "devils_advocate_report"),
    "etf": ("phase2/reports/etf_QQQ.json", "etf_review"),
    "macro_view": ("phase1/macro_view.json", "macro_view"),
    "cio_allocation": ("phase1/cio_allocation.json", "cio_allocation"),
    "sector_verdict": ("phase2/consumer_staples_verdict.json", "sector_verdict"),
    "cio_final": ("phase4/cio_final.json", "cio_final"),
}
JUST = ("This justification is long enough to satisfy the minimum because it explains the reasoning "
        "in plain and specific language.")


# =====================================================================================
# helpers
# =====================================================================================


def load(rel: str) -> dict:
    return json.loads((RUN / rel).read_text(encoding="utf-8"))


def report(name: str) -> dict:
    return load(REPORTS[name][0])


def ctx(**over: Any) -> dict:
    c: dict[str, Any] = {k: str(v) for k, v in DATA.items()}
    c["verdicts"] = [str(p) for p in VERDICTS]
    if over.get("technicals") == "QQQ":
        over["technicals"] = str(RUN / "phase2" / "data" / "technicals_QQQ.json")
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def errs(doc: dict, name: str, **over: Any) -> list[str]:
    schema = REPORTS[name][1] if name in REPORTS else name
    return validate.validate_doc(doc, schema, context=ctx(**over))


def rep(doc: dict, name: str, **over: Any) -> validate.Report:
    schema = REPORTS[name][1] if name in REPORTS else name
    return validate.validate_doc_report(doc, schema, context=ctx(**over))


def has(errors: list[str], *fragments: str) -> bool:
    return any(all(f in e for f in fragments) for e in errors)


def cfg_with(**sections: dict) -> dict:
    cfg = common.committee_cfg()
    for section, values in sections.items():
        cfg.setdefault(section, {})
        for k, v in values.items():
            if isinstance(v, dict) and isinstance(cfg[section].get(k), dict):
                cfg[section][k] = {**cfg[section][k], **v}
            else:
                cfg[section][k] = v
    return cfg


def words(n: int, w: str = "word") -> str:
    return " ".join([w] * n)


# =====================================================================================
# baseline: every fixture report is valid with full context (strict refs too)
# =====================================================================================


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_fixture_reports_are_valid_with_full_context(name: str) -> None:
    extra = {"technicals": "QQQ"} if name == "etf" else {}
    r = rep(report(name), name, strict_refs=True, **extra)
    assert r.errors == [], "\n".join(r.errors)


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_fixture_reports_validate_through_validate_file(name: str) -> None:
    path = RUN / REPORTS[name][0]
    extra = {"technicals": "QQQ"} if name == "etf" else {}
    assert validate.validate_file(path, context=ctx(**extra)) == []


# =====================================================================================
# word limits
# =====================================================================================


def test_thesis_over_the_word_limit() -> None:
    doc = report("fundamental")
    doc["thesis"] = words(61)
    assert "$.thesis: too long; expected <= 60 words, got 61" in errs(doc, "fundamental")
    doc["thesis"] = words(60)
    assert not has(errs(doc, "fundamental"), "$.thesis")


def test_configured_word_limit_replaces_the_schema_annotation() -> None:
    doc = report("fundamental")
    doc["thesis"] = words(30)
    tight = cfg_with(validation={"word_limits": {"thesis": 10}})
    assert has(errs(doc, "fundamental", committee_cfg=tight), "$.thesis: too long; expected <= 10 words, got 30")
    doc["thesis"] = words(70)
    loose = cfg_with(validation={"word_limits": {"thesis": 100}})
    assert not has(errs(doc, "fundamental", committee_cfg=loose), "$.thesis")
    assert has(errs(doc, "fundamental"), "$.thesis: too long; expected <= 60 words")


def test_configured_limit_replaces_the_coarse_character_guard() -> None:
    doc = report("fundamental")
    doc["thesis"] = words(90, "longword")  # 90 words, 809 characters (schema maxLength 480)
    assert [e for e in errs(doc, "fundamental") if e.startswith("$.thesis")] == [
        "$.thesis: too long; expected <= 60 words, got 90"]
    loose = cfg_with(validation={"word_limits": {"thesis": 100}})
    assert not has(errs(doc, "fundamental", committee_cfg=loose), "$.thesis")
    doc["thesis"] = words(50, "x" * 25)
    assert has(errs(doc, "fundamental", committee_cfg=loose), "$.thesis: too long; expected <= 1000 characters")


def test_work_order_word_limits_win_over_config() -> None:
    doc = report("fundamental")
    doc["thesis"] = words(25)
    assert has(errs(doc, "fundamental", word_limits={"thesis": 20}), "expected <= 20 words, got 25")


@pytest.mark.parametrize("name, path, limit", [
    ("technical", ("extensions", "setup"), 40),
    ("operator_ceo", ("extensions", "capital_allocation", "assessment"), 60),
    ("sector_verdict", ("verdicts", 0, "rationale"), 80),
    ("sector_verdict", ("summary",), 80),
    ("macro_view", ("regime_summary",), 80),
    ("macro_view", ("scenarios", 1, "description"), 60),
    ("cio_allocation", ("rationale",), 150),
    ("cio_final", ("dissent_summary",), 120),
    ("cio_final", ("executive_summary", 0), 40),
    ("osint", ("red_flags", 0, "flag"), 40),
    ("devils_advocate", ("kill_criteria", 0), 40),
])
def test_word_limits_per_field(name: str, path: tuple, limit: int) -> None:
    doc = report(name)
    node = doc
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = words(limit + 1, "ok")
    jp = validate._jp(path)
    assert has(errs(doc, name), f"{jp}: too long; expected <= {limit} words, got {limit + 1}")
    node[path[-1]] = words(limit, "ok")
    assert not has(errs(doc, name), f"{jp}: too long")


def test_regime_label_word_annotation() -> None:
    doc = report("macro_view")
    doc["regime"] = words(9, "late")
    assert has(errs(doc, "macro_view"), "$.regime: too long; expected <= 8 words, got 9")


def test_justification_min_words_and_configured_minimum() -> None:
    doc = report("cio_allocation")
    doc["constraint_overrides"][0]["justification"] = "Selling the core funds now would trigger taxable gains, so wait."
    assert has(errs(doc, "cio_allocation"), "$.constraint_overrides[0].justification: too short; "
                                             "expected >= 15 words, got 11")
    relaxed = cfg_with(validation={"min_justification_words": 8})
    assert not has(errs(doc, "cio_allocation", committee_cfg=relaxed), "justification")


def test_nullable_justification_is_not_min_checked_when_not_deviating() -> None:
    doc = report("sector_verdict")
    doc["verdicts"][0]["deviation_justification"] = "n/a"
    assert errs(doc, "sector_verdict") == []
    doc["verdicts"][1]["deviation_justification"] = "The CIO already plans a staples cut."
    assert has(errs(doc, "sector_verdict"), "$.verdicts[1].deviation_justification: too short; expected >= 15 words")


def test_executive_summary_count_from_config() -> None:
    doc = report("cio_final")
    assert errs(doc, "cio_final") == []
    tight = cfg_with(validation={"max_executive_summary_items": 3, "max_top_risks": 2})
    e = errs(doc, "cio_final", committee_cfg=tight)
    assert has(e, "$.executive_summary: too many items; expected <= 3, got 4")
    assert has(e, "$.top_risks: too many items; expected <= 2, got 3")


# =====================================================================================
# sources
# =====================================================================================


def test_unknown_evidence_ref_is_rejected() -> None:
    doc = report("fundamental")
    doc["strengths"][0]["evidence_refs"] = ["D:SEC:KO:CF", "W9"]
    e = errs(doc, "fundamental")
    assert has(e, "$.strengths[0].evidence_refs[1]: unknown source id; expected an id from sources[]", 'got "W9"')


@pytest.mark.parametrize("name, path", [
    ("osint", ("red_flags", 0, "source_ids")),
    ("osint", ("extensions", "signals", 1, "source_ids")),
    ("operator_ceo", ("extensions", "insider_activity", "source_ids")),
    ("devils_advocate", ("flag_responses", 0, "source_ids")),
    ("macro_view", ("drivers", 0, "source_ids")),
])
def test_unknown_source_ids_are_rejected(name: str, path: tuple) -> None:
    doc = report(name)
    node = doc
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = ["W99"]
    assert has(errs(doc, name), f"{validate._jp(path)}[0]: unknown source id", '"W99"')


def test_key_figure_source_id_must_resolve() -> None:
    doc = report("operator_ceo")
    doc["key_figures"][0]["source_id"] = "W8"
    assert has(errs(doc, "operator_ceo"), "$.key_figures[0].source_id: unknown source id")


def test_duplicate_source_ids_are_rejected() -> None:
    doc = report("technical")
    doc["sources"].append(copy.deepcopy(doc["sources"][1]))
    assert has(errs(doc, "technical"), "$.sources[3].id: duplicate source id", '"D:PX:SPY"')


def test_seed_sources_must_be_copied_verbatim() -> None:
    doc = report("fundamental")
    seeds = [copy.deepcopy(s) for s in doc["sources"] if s["id"].startswith("D:")]
    assert errs(doc, "fundamental", seed_sources=seeds) == []
    seeds[0]["url"] = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"
    assert has(errs(doc, "fundamental", seed_sources=seeds),
               "$.sources[0].url: dataset source D:SEC:KO:CF must be copied verbatim")


# =====================================================================================
# data_ref
# =====================================================================================


def test_data_ref_value_mismatch() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["value"] = 50_000_000_000
    e = errs(doc, "fundamental")
    assert has(e, "$.key_figures[0].value: does not match the data at", "fundamentals_KO.json#/ttm/revenue",
               "expected 4.79e+10 (+/-0.5%)", "got 5e+10")


def test_data_ref_within_tolerance_passes() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["value"] = 47_900_000_000 * 1.004
    assert errs(doc, "fundamental") == []
    doc["key_figures"][0]["value"] = 47_900_000_000 * 1.006
    assert has(errs(doc, "fundamental"), "$.key_figures[0].value")
    loose = cfg_with(validation={"data_ref_tolerance_pct": 1.0})
    assert errs(doc, "fundamental", committee_cfg=loose) == []


def test_data_ref_pointer_must_resolve() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["data_ref"] = REL + "phase2/data/fundamentals_KO.json#/ttm/revenues"
    assert has(errs(doc, "fundamental"), "$.key_figures[0].data_ref: JSON pointer does not resolve in "
                                         "fundamentals_KO.json", "#/ttm/revenues")


def test_data_ref_must_point_at_a_fact_or_number() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["data_ref"] = REL + "phase2/data/fundamentals_KO.json#/ttm"
    assert has(errs(doc, "fundamental"), "$.key_figures[0].data_ref: data_ref must point at a Fact or a number")


def test_data_ref_to_bare_number_and_list_index() -> None:
    doc = report("devils_advocate")
    assert doc["key_figures"][0]["data_ref"].endswith("valuation_KO.json#/cells/0/fv_per_share")
    assert errs(doc, "devils_advocate") == []
    doc["key_figures"][0]["value"] = 61.0
    assert has(errs(doc, "devils_advocate"), "$.key_figures[0].value: does not match", "expected 58.4")


def test_data_ref_to_null_fact_with_a_value() -> None:
    doc = report("fundamental")
    doc["key_figures"][3]["data_ref"] = REL + "phase2/data/fundamentals_KO.json#/valuation/p_tbv"
    assert has(errs(doc, "fundamental"), "$.key_figures[3].value: the referenced data value is null "
                                         "(na_reason negative_denominator)")


def test_missing_data_ref_file_is_a_note_unless_strict() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["data_ref"] = "runs/1999-01-01_0000/nowhere/fundamentals_KO.json#/ttm/revenue"
    lenient = rep(doc, "fundamental")
    assert lenient.errors == []
    assert any("data_ref file not found" in n for n in lenient.notes)
    strict = rep(doc, "fundamental", strict_refs=True)
    assert has(strict.errors, "$.key_figures[0].data_ref: data_ref file not found")


def test_absolute_data_ref_paths_resolve() -> None:
    doc = report("fundamental")
    doc["key_figures"][0]["data_ref"] = (RUN / "phase2/data/fundamentals_KO.json").as_posix() + "#/ttm/revenue"
    assert errs(doc, "fundamental", strict_refs=True) == []


def test_data_ref_into_another_run_is_reanchored_on_the_current_run(tmp_path: Path) -> None:
    run = tmp_path / "runs" / RUN_ID
    shutil.copytree(RUN, run)
    data = json.loads((run / "phase2/data/fundamentals_KO.json").read_text(encoding="utf-8"))
    data["ttm"]["revenue"]["value"] = 60_000_000_000
    common.write_json(run / "phase2/data/fundamentals_KO.json", data)
    doc = report("fundamental")  # refs name tests/fixtures/validate/run/phase2/... (the repo copy)
    assert errs(doc, "fundamental") == []
    e = validate.validate_doc(doc, "specialist_report", context={"run_dir": str(run), "strict_refs": True})
    assert has(e, "$.key_figures[0].value: does not match", "expected 6e+10")


def test_indicator_refs_and_driver_refs_must_resolve() -> None:
    doc = report("technical")
    doc["extensions"]["indicator_refs"][0] = REL + "phase2/data/technicals_KO.json#/trend/regime_label"
    assert has(errs(doc, "technical"), "$.extensions.indicator_refs[0]: JSON pointer does not resolve")
    mv = report("macro_view")
    mv["drivers"][1]["data_refs"][0] = REL + "phase1/macro.json#/series/CPILFESL/yoy_pct"
    assert has(errs(mv, "macro_view"), "$.drivers[1].data_refs[0]: JSON pointer does not resolve")


def test_key_level_data_ref_compares_the_price() -> None:
    doc = report("technical")
    doc["extensions"]["key_levels"][0]["data_ref"] = REL + "phase2/data/technicals_KO.json#/levels/supports/1/price"
    assert has(errs(doc, "technical"), "$.extensions.key_levels[0].price: does not match the data at")


# =====================================================================================
# key figures with null values
# =====================================================================================


def test_null_key_figure_must_be_listed_in_missing_data() -> None:
    doc = report("operator_ceo")
    assert errs(doc, "operator_ceo") == []
    doc["missing_data"] = ["Something else entirely"]
    assert has(errs(doc, "operator_ceo"), "$.key_figures[1].name: value is null but the figure is not listed in "
                                          "missing_data", "Share of CEO target pay that is performance-based")


def test_null_key_figure_name_may_appear_inside_a_longer_entry() -> None:
    doc = report("operator_ceo")
    doc["missing_data"] = ["share of ceo target pay that is performance-based (not disclosed in the 2026 proxy)"]
    assert errs(doc, "operator_ceo") == []


# =====================================================================================
# fundamental lens
# =====================================================================================


def test_fair_value_must_equal_mean_of_cited_cells() -> None:
    doc = report("fundamental")
    doc["extensions"]["valuation"]["fair_value_base"] = 70.0
    e = errs(doc, "fundamental")
    assert has(e, "$.extensions.valuation.fair_value_base: does not equal the mean fair value of cells_base",
               "DCF-NOPAT-r10-g03 66.9", "PE-mid 68.9", "expected 67.9 (+/-0.5%)", "got 70")


def test_unknown_cell_id() -> None:
    doc = report("fundamental")
    doc["extensions"]["valuation"]["cells_high"][1] = "PE-max"
    assert has(errs(doc, "fundamental"), "$.extensions.valuation.cells_high[1]: unknown valuation cell", '"PE-max"')


def test_cell_without_fair_value() -> None:
    doc = report("fundamental")
    doc["extensions"]["valuation"]["cells_high"] = ["DCF-FCF-r10-g15"]
    assert has(errs(doc, "fundamental"), "$.extensions.valuation.cells_high[0]: cell DCF-FCF-r10-g15 has no fair "
                                         "value (na_reason tv_dominates)")


def test_fair_values_must_be_ordered() -> None:
    doc = report("fundamental")
    doc["extensions"]["valuation"]["fair_value_high"] = 60.0
    assert has(validate.validate_doc(doc, "specialist_report"), "$.extensions.valuation: fair values out of order")


def test_cells_base_must_span_method_families_when_available() -> None:
    doc = report("fundamental")
    val = doc["extensions"]["valuation"]
    val["cells_base"] = ["DCF-NOPAT-r10-g03", "DCF-FCF-r10-g03"]
    val["fair_value_base"] = 64.95
    assert has(errs(doc, "fundamental"), "$.extensions.valuation.cells_base: cells_base must span the available "
                                         "valuation method families", "expected >= 2 families (dcf, multiple)",
               "got 1 (dcf)")
    vdoc = load("phase2/data/valuation_KO.json")
    vdoc["cells"] = [c for c in vdoc["cells"] if c["method"].startswith("dcf")]
    doc["extensions"]["valuation"]["cells_low"] = ["DCF-NOPAT-r10-g00"]
    doc["extensions"]["valuation"]["cells_high"] = ["DCF-NOPAT-r09-g06"]
    val["fair_value_high"] = 77.6
    assert errs(doc, "fundamental", valuation=vdoc) == []


def test_valuation_context_is_discovered_from_the_reports_own_refs() -> None:
    doc = report("fundamental")
    doc["extensions"]["valuation"]["fair_value_base"] = 70.0
    r = rep(doc, "fundamental", valuation=None)
    assert has(r.errors, "$.extensions.valuation.fair_value_base: does not equal the mean")
    assert any("taken from the document's own data_ref" in n for n in r.notes)


def test_valuation_context_for_another_ticker_is_skipped() -> None:
    doc = report("fundamental")
    vdoc = load("phase2/data/valuation_KO.json")
    vdoc["ticker"] = "PG"
    doc["extensions"]["valuation"]["fair_value_base"] = 70.0
    r = rep(doc, "fundamental", valuation=vdoc)
    assert not has(r.errors, "mean fair value")
    assert any("valuation context is for PG" in n for n in r.notes)


# =====================================================================================
# technical lens
# =====================================================================================


def test_trend_regime_must_match_computed_regime() -> None:
    doc = report("technical")
    doc["extensions"]["trend_regime"] = "trend_up"
    assert ('$.extensions.trend_regime: does not match the computed regime in technicals_<T>.json; '
            'expected "range", got "trend_up"') in errs(doc, "technical")


def test_invalidation_must_equal_the_referenced_level() -> None:
    doc = report("technical")
    doc["invalidation_price"] = 64.0
    assert has(errs(doc, "technical"), "$.invalidation_price: does not equal the computed price of STOP1",
               "expected 65.9 (+/-0.5%)", "got 64")
    doc["invalidation_price"] = 65.9 * 1.004
    assert errs(doc, "technical") == []
    doc["invalidation_price"] = 66.8
    doc["extensions"]["invalidation_ref"] = "S1"
    assert errs(doc, "technical") == []


def test_invalidation_ref_must_exist() -> None:
    doc = report("technical")
    doc["extensions"]["invalidation_ref"] = "STOP9"
    assert has(errs(doc, "technical"), "$.extensions.invalidation_ref: unknown level id",
               "S1, S2, R1, STOP1, STOP2", '"STOP9"')


def test_key_levels_must_match_computed_levels() -> None:
    doc = report("technical")
    doc["extensions"]["key_levels"][1]["price"] = 75.0
    doc["extensions"]["key_levels"][0]["type"] = "resistance"
    e = errs(doc, "technical")
    assert has(e, "$.extensions.key_levels[1].price: does not equal the computed price of R1", "expected 73.1")
    assert has(e, "$.extensions.key_levels[0].type: wrong type for S1", 'expected "support"')
    doc = report("technical")
    doc["extensions"]["key_levels"][0]["ref_id"] = "S7"
    assert has(errs(doc, "technical"), "$.extensions.key_levels[0].ref_id: unknown level id")


def test_without_technicals_context_invalidation_is_checked_against_key_levels() -> None:
    doc = report("technical")
    doc["invalidation_price"] = 60.0
    r = rep(doc, "technical", technicals=str(RUN / "missing" / "technicals_KO.json"))
    assert has(r.errors, "$.invalidation_price: does not equal the price of key level STOP1")
    assert any("technicals context file not found" in n for n in r.notes)


# =====================================================================================
# devil's advocate
# =====================================================================================


def test_bear_case_ref_must_be_a_known_cell_or_level() -> None:
    doc = report("devils_advocate")
    doc["bear_case_ref"] = "DCF-NOPAT-r12-g00"
    assert has(errs(doc, "devils_advocate"), "$.bear_case_ref: unknown reference",
               "a valuation cell id from valuation_<T>.json")


def test_bear_case_price_must_match_the_cell() -> None:
    doc = report("devils_advocate")
    doc["bear_case_price"] = 50.0
    doc["key_figures"] = []
    assert has(errs(doc, "devils_advocate"), "$.bear_case_price: does not equal the fair value of cell "
                                             "DCF-NOPAT-r10-g00", "expected 58.4")


def test_bear_case_ref_may_be_a_technical_level() -> None:
    doc = report("devils_advocate")
    doc["key_figures"] = []
    doc["bear_case_ref"], doc["bear_case_price"] = "STOP1", 65.9
    assert errs(doc, "devils_advocate") == []
    doc["bear_case_price"] = 60.0
    assert has(errs(doc, "devils_advocate"), "$.bear_case_price: does not equal the computed price of STOP1")
    doc["bear_case_ref"] = "R5"
    assert has(errs(doc, "devils_advocate"), "$.bear_case_ref: unknown technical level id")


# =====================================================================================
# ETF review
# =====================================================================================


def test_etf_figures_must_match_lookthrough_and_technicals() -> None:
    doc = report("etf")
    doc["expense_ratio_pct"] = 0.35
    doc["momentum_vs_benchmark"]["rs_12m"] = 7.0
    e = errs(doc, "etf", technicals="QQQ")
    assert has(e, "$.expense_ratio_pct: does not match lookthrough.json", "expected 0.2")
    assert has(e, "$.momentum_vs_benchmark.rs_12m: does not match technicals_<T>.json", "expected 4.9")


def _qqq_technicals(tmp_path: Path, **over: Any) -> str:
    tdoc = load("phase2/data/technicals_QQQ.json")
    tdoc.update(over)
    out = tmp_path / "technicals_QQQ.json"
    common.write_json(out, tdoc)
    return str(out)


def test_etf_momentum_vs_sector_etf_is_checked_against_vs_sector(tmp_path: Path) -> None:
    tdoc_path = _qqq_technicals(tmp_path, sector_etf="XLK")
    tdoc = json.loads(Path(tdoc_path).read_text(encoding="utf-8"))
    tdoc["relative_strength"]["vs_sector"]["6m"]["value"] = -2.5
    common.write_json(Path(tdoc_path), tdoc)
    doc = report("etf")
    doc["momentum_vs_benchmark"]["benchmark"] = "XLK"
    doc["momentum_vs_benchmark"]["rs_6m"] = 3.2  # the vs_spy figure, not vs_sector
    e = errs(doc, "etf", technicals=tdoc_path)
    assert has(e, "$.momentum_vs_benchmark.rs_6m: does not match technicals_<T>.json", "expected -2.5")
    doc["momentum_vs_benchmark"]["rs_6m"] = -2.5
    assert not has(errs(doc, "etf", technicals=tdoc_path), "$.momentum_vs_benchmark")


def test_etf_momentum_with_unknown_benchmark_is_not_silently_skipped(tmp_path: Path) -> None:
    doc = report("etf")
    doc["momentum_vs_benchmark"]["benchmark"] = "XLK"  # technicals_QQQ has no sector_etf
    doc["momentum_vs_benchmark"]["rs_3m"] = 99.0
    e = errs(doc, "etf", technicals="QQQ")
    assert has(e, "$.momentum_vs_benchmark.benchmark: is not a benchmark", "expected SPY", "got \"XLK\"")
    e = errs(doc, "etf", technicals=_qqq_technicals(tmp_path, sector_etf="XLC"))
    assert has(e, "$.momentum_vs_benchmark.benchmark:", "expected SPY or XLC", "got \"XLK\"")


# =====================================================================================
# macro view
# =====================================================================================


def test_scenario_probabilities_must_sum_to_one() -> None:
    doc = report("macro_view")
    doc["scenarios"][2]["probability"] = 0.15
    assert ("$.scenarios: scenario probabilities must sum to 1; expected 1 +/- 0.01, got 0.95"
            in errs(doc, "macro_view"))
    doc["scenarios"][2]["probability"] = 0.205
    assert errs(doc, "macro_view") == []


def test_sector_constraint_bounds() -> None:
    doc = report("macro_view")
    doc["sector_constraints"][1]["min_weight"] = 30
    doc["sector_constraints"][3]["sector"] = "information_technology"
    e = errs(doc, "macro_view")
    assert has(e, "$.sector_constraints[1]: min_weight is greater than max_weight", "got 30 > 25")
    assert has(e, "$.sector_constraints[3].sector: duplicate constraint for sector")


def test_sector_floors_must_be_feasible() -> None:
    doc = report("macro_view")
    for c in doc["sector_constraints"]:
        c["min_weight"], c["max_weight"] = 30, 40
    assert has(errs(doc, "macro_view"), "$.sector_constraints: the sector floors (min_weight) cannot all be met")


def test_driver_series_must_exist_in_macro_json() -> None:
    doc = report("macro_view")
    doc["drivers"][0]["series"] = ["DFF", "SOFR"]
    assert has(errs(doc, "macro_view"), "$.drivers[0].series[1]: series not in macro.json", '"SOFR"')
    r = rep(doc, "macro_view", macro=None)
    assert not has(r.errors, "series not in macro.json")
    assert any("macro context not provided" in n for n in r.notes)


# =====================================================================================
# CIO allocation
# =====================================================================================


def test_weight_maps_must_sum_to_100() -> None:
    doc = report("cio_allocation")
    doc["target_sector_weights"]["cash"] = 3.0
    assert "$.target_sector_weights: weights must sum to 100; expected 100 +/- 0.5, got 98" in errs(doc, "cio_allocation")
    doc["target_sector_weights"]["cash"] = 4.7
    assert not has(errs(doc, "cio_allocation"), "weights must sum")


def test_target_outside_macro_bound_needs_an_override() -> None:
    doc = report("cio_allocation")
    doc["constraint_overrides"] = []
    assert has(errs(doc, "cio_allocation"), "$.target_sector_weights.information_technology: target is outside "
                                             "the macro max bound without a constraint_overrides entry",
               "macro_bound: max", "got 24")
    r = rep(doc, "cio_allocation", macro_view=None)
    assert r.errors == []
    assert any("macro_view context not provided" in n for n in r.notes)


def test_target_below_macro_floor_needs_an_override() -> None:
    doc = report("cio_allocation")
    doc["target_sector_weights"]["health_care"] = 4.0
    doc["target_sector_weights"]["cash"] = 9.0
    assert has(errs(doc, "cio_allocation"), "$.target_sector_weights.health_care: target is outside the macro min "
                                             "bound", "expected >= 5")


def test_override_must_match_bound_and_target() -> None:
    doc = report("cio_allocation")
    doc["constraint_overrides"][0]["macro_value"] = 20
    doc["constraint_overrides"][0]["cio_target"] = 25.0
    e = errs(doc, "cio_allocation")
    assert has(e, "$.constraint_overrides[0].macro_value: does not match macro_view max_weight for "
                  "information_technology", "expected 22", "got 20")
    assert has(e, "$.constraint_overrides[0].cio_target: does not match target_sector_weights.information_technology",
               "expected 24", "got 25")


def test_unneeded_override_and_unknown_override_sector() -> None:
    doc = report("cio_allocation")
    doc["target_sector_weights"]["information_technology"] = 22.0
    doc["target_sector_weights"]["cash"] = 7.0
    doc["constraint_overrides"][0]["cio_target"] = 22.0
    assert has(errs(doc, "cio_allocation"), "$.constraint_overrides[0]: override not needed: the target is within "
                                             "the macro max bound")
    doc = report("cio_allocation")
    doc["constraint_overrides"].append({**doc["constraint_overrides"][0], "sector": "energy"})
    assert has(errs(doc, "cio_allocation"), "$.constraint_overrides[1].sector: no macro_view sector constraint "
                                             "exists for this sector")


def test_expand_sectors_capped_by_config() -> None:
    doc = report("cio_allocation")
    doc["expand_sectors"] = ["health_care", "industrials", "energy"]
    assert has(errs(doc, "cio_allocation"), "$.expand_sectors: too many expand sectors; expected <= 2")
    roomy = cfg_with(phase3={"max_expand_sectors": 3})
    assert not has(errs(doc, "cio_allocation", committee_cfg=roomy), "expand")


def test_current_weights_must_match_lookthrough() -> None:
    doc = report("cio_allocation")
    doc["current_sector_weights"]["financials"] = 23.0
    doc["current_sector_weights"]["cash"] = 7.0
    e = errs(doc, "cio_allocation")
    assert has(e, "$.current_sector_weights.financials: does not match lookthrough.json sector_weights_pct",
               "expected 25 (+/-0.5 pp)", "got 23")
    assert has(e, "$.current_sector_weights.cash: does not match lookthrough.json")
    doc = report("cio_allocation")
    doc["current_sector_weights"]["financials"] = 25.4
    doc["current_sector_weights"]["cash"] = 4.6
    assert errs(doc, "cio_allocation") == []
    del doc["current_sector_weights"]["unclassified"]
    doc["current_sector_weights"]["cash"] = 5.6
    assert has(errs(doc, "cio_allocation"), "$.current_sector_weights.unclassified", "got missing (0)")


# =====================================================================================
# sector verdict vs aggregate
# =====================================================================================


def test_verdict_values_must_equal_aggregate() -> None:
    doc = report("sector_verdict")
    v = doc["verdicts"][0]
    v.update(action="TRIM", rule_action="TRIM", agg_conviction=5.7, agg_evidence=7.0, rr=1.2)
    e = errs(doc, "sector_verdict")
    assert has(e, '$.verdicts[0].rule_action: does not match aggregate.json for KO; expected "HOLD", got "TRIM"')
    assert has(e, "$.verdicts[0].agg_conviction: does not match aggregate.json for KO; expected 5.6, got 5.7")
    assert has(e, "$.verdicts[0].agg_evidence: does not match aggregate.json")
    assert has(e, "$.verdicts[0].rr: does not match aggregate.json for KO; expected 0.9, got 1.2")


def test_verdict_tickers_must_cover_the_sector_in_aggregate() -> None:
    doc = report("sector_verdict")
    doc["verdicts"][1]["ticker"] = "XYZ"
    e = errs(doc, "sector_verdict")
    assert has(e, '$.verdicts[1].ticker: ticker not found in aggregate.json; expected one of KO, PG, got "XYZ"')
    assert has(e, "$.verdicts: missing verdict for PG")


def test_verdict_for_ticker_of_another_sector() -> None:
    doc = report("sector_verdict")
    doc["verdicts"].append({**copy.deepcopy(doc["verdicts"][0]), "ticker": "JPM", "agg_conviction": 6.1,
                            "agg_evidence": 7.0, "rr": 1.4, "unresolved_red_flags": []})
    assert has(errs(doc, "sector_verdict"), "$.verdicts[2].ticker: ticker belongs to another sector in aggregate.json",
               'expected "consumer_staples", got "financials"')


def test_unresolved_flags_must_be_carried_from_aggregate() -> None:
    doc = report("sector_verdict")
    doc["verdicts"][0]["unresolved_red_flags"] = []
    assert has(errs(doc, "sector_verdict"), "$.verdicts[0].unresolved_red_flags: must list every unresolved flag "
                                            "from aggregate.json", "osint:F1")
    doc["verdicts"][0]["unresolved_red_flags"] = ["osint:F1", "osint:F9"]
    assert has(errs(doc, "sector_verdict"), "$.verdicts[0].unresolved_red_flags: lists flags that are not in "
                                            "aggregate.json", "osint:F9")


def test_buy_cap_per_sector() -> None:
    buy = {"kind": "candidate", "action": "BUY", "rule_action": "BUY", "rule_label": None, "agg_conviction": 7.4,
           "agg_evidence": 7.0, "rr": 2.7, "rationale": "Rules pass with margin.", "dissents": [],
           "unresolved_red_flags": [], "rule_deviation": False, "thesis": "Candidate thesis.",
           "fair_value_low": 95.0, "fair_value_base": 118.0, "fair_value_high": 140.0, "invalidation_price": 88.0}
    doc = load("phase3/health_care_verdict.json")
    doc["verdicts"] = [{**buy, "ticker": t} for t in ("MRK", "ABT")]
    agg = {"schema": "aggregate/v1", "tickers": {t: {"kind": "candidate", "sector": "health_care",
                                                     "rule_action": "BUY", "agg_conviction": 7.4,
                                                     "agg_evidence": 7.0, "rr": {"rr": 2.7},
                                                     "flags": {"all": [], "unresolved": []}}
                                                 for t in ("MRK", "ABT")}}
    assert errs(doc, "sector_verdict", aggregate=agg) == []
    one = cfg_with(phase3={"max_buys_per_sector": 1})
    assert has(errs(doc, "sector_verdict", aggregate=agg, committee_cfg=one),
               "$.verdicts: too many BUY verdicts for one sector; expected <= 1 BUY", "got 2 (MRK, ABT)")


# =====================================================================================
# CIO final vs sector verdicts
# =====================================================================================


def test_source_verdict_action_must_match_the_verdict_file() -> None:
    doc = report("cio_final")
    doc["final_actions"][0].update(action="ADD", source_verdict_action="ADD")
    e = errs(doc, "cio_final")
    assert has(e, '$.final_actions[0].source_verdict_action: does not match the sector verdict; expected "HOLD" '
                  '(from consumer_staples_verdict.json), got "ADD"')
    assert has(e, "$.final_actions[0].override: action", "differs from the sector verdict", "without an override")


def test_override_true_with_same_action() -> None:
    doc = report("cio_final")
    doc["final_actions"][1].update(override=True, override_justification=JUST)
    assert has(errs(doc, "cio_final"), "$.final_actions[1].override: override is true but action equals the sector "
                                       "verdict action")


def test_override_with_justification_is_accepted() -> None:
    doc = report("cio_final")
    assert doc["final_actions"][2]["override"] is True
    assert errs(doc, "cio_final") == []


def test_every_holding_and_buy_needs_a_final_action() -> None:
    doc = report("cio_final")
    doc["final_actions"] = [fa for fa in doc["final_actions"] if fa["ticker"] not in ("BRK-B", "MRK")]
    e = errs(doc, "cio_final")
    assert has(e, "$.final_actions: missing final action for BRK-B")
    assert has(e, "$.final_actions: missing final action for MRK")


def test_final_action_without_any_verdict() -> None:
    doc = report("cio_final")
    doc["final_actions"].append({"ticker": "XYZ", "action": "HOLD", "source_verdict_action": "HOLD",
                                 "override": False})
    assert has(errs(doc, "cio_final"), "$.final_actions[7].source_verdict_action: no sector verdict exists for "
                                       "this ticker; expected null")


def test_cio_final_targets_vs_macro_bounds() -> None:
    doc = report("cio_final")
    doc["constraint_overrides"] = []
    assert has(errs(doc, "cio_final"), "$.final_target_sector_weights.information_technology: target is outside "
                                       "the macro max bound")
    r = rep(doc, "cio_final", verdicts=[], macro_view=None)
    assert r.errors == []
    assert any("no sector verdict files" in n for n in r.notes)


# =====================================================================================
# copyright guard
# =====================================================================================


@pytest.mark.parametrize("open_q, close_q", [('"', '"'), ("\u201c", "\u201d"), ("\u00ab", "\u00bb")])
def test_quoted_spans_over_the_limit_are_rejected(open_q: str, close_q: str) -> None:
    doc = report("osint")
    doc["extensions"]["signals"][0]["signal"] = f"A reviewer wrote {open_q}{words(26, 'tasty')}{close_q} online."
    assert has(errs(doc, "osint"), "$.extensions.signals[0].signal: quoted span of 26 words breaks the copyright "
                                   "guard", "expected <= 25 words inside quotation marks")
    doc["extensions"]["signals"][0]["signal"] = f"A reviewer wrote {open_q}{words(25, 'tasty')}{close_q} online."
    assert errs(doc, "osint") == []


def test_quote_limit_comes_from_config() -> None:
    doc = report("osint")
    doc["thesis"] = 'Management said "demand is steady across all of our regions this year" on the call.'
    assert errs(doc, "osint") == []
    strict = cfg_with(validation={"max_quote_words": 5})
    assert has(errs(doc, "osint", committee_cfg=strict), "$.thesis: quoted span of 10 words")


# =====================================================================================
# identity vs work order
# =====================================================================================


def test_identity_must_match_the_work_order() -> None:
    doc = report("fundamental")
    expect = {"order_id": "p2-a-fundamental-KO", "run_id": RUN_ID, "agent_role": "fundamental", "ticker": "KO",
              "sector": "consumer_staples", "mode": None}
    assert errs(doc, "fundamental", expect=expect) == []
    e = errs(doc, "fundamental", expect={**expect, "ticker": "PG", "order_id": "p2-a-fundamental-PG"})
    assert has(e, '$.ticker: does not match the work order; expected "PG", got "KO"')
    assert has(e, "$.order_id: does not match the work order")


def test_mode_and_phase_must_match_the_work_order() -> None:
    sv = report("sector_verdict")
    assert has(errs(sv, "sector_verdict", expect={"mode": "phase3"}), "$.phase: does not match the work order mode")
    assert errs(sv, "sector_verdict", expect={"mode": "phase2", "sector": "consumer_staples"}) == []


# =====================================================================================
# robustness: missing context, inference, file errors, message format
# =====================================================================================


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_missing_context_files_never_crash(name: str, tmp_path: Path) -> None:
    gone = {k: str(tmp_path / f"missing_{k}.json") for k in DATA}
    r = validate.validate_doc_report(report(name), REPORTS[name][1],
                                     context={**gone, "verdicts": [str(tmp_path / "x_verdict.json")]})
    assert r.errors == [], r.errors
    if name in ("technical", "sector_verdict", "cio_allocation", "cio_final"):
        assert any("context file not found" in n for n in r.notes), r.notes


def test_semantic_check_exceptions_become_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(run: Any) -> None:
        raise RuntimeError("kaboom")
    monkeypatch.setattr(validate, "_check_quotes", boom)
    r = validate.validate_doc_report(report("osint"), "specialist_report")
    assert r.errors == []
    assert any("skipped (RuntimeError: kaboom)" in n for n in r.notes)


def test_semantic_messages_follow_the_path_expected_got_format() -> None:
    doc = report("cio_allocation")
    doc["target_sector_weights"]["cash"] = 3.0
    doc["expand_sectors"] = ["health_care", "industrials", "energy"]
    doc["current_sector_weights"]["financials"] = 20.0
    e = errs(doc, "cio_allocation")
    assert e
    for msg in e:
        assert SEMANTIC_MSG.match(msg), msg


def test_error_list_is_capped() -> None:
    doc = report("fundamental")
    doc["strengths"] = []
    doc["key_figures"] = [{"name": f"f{i}", "value": None, "unit": "x", "source_id": f"W{i + 10}", "as_of": None,
                           "kind": "other"} for i in range(25)]
    doc["missing_data"] = []
    doc["red_flags"] = [{"id": "F1", "flag": "x", "severity": "low", "must_address": False,
                         "source_ids": [f"W{i + 50}" for i in range(10)]}] * 10
    e = errs(doc, "fundamental")
    assert len(e) == validate.MAX_ERRORS
    assert "more errors not shown" in e[-1]


def test_infer_schema() -> None:
    assert validate.infer_schema({"schema": "technicals/v1"}) == "technicals"
    assert validate.infer_schema({"agent_role": "fundamental"}) == "specialist_report"
    assert validate.infer_schema({"agent_role": "cio", "mode": "allocation"}) == "cio_allocation"
    assert validate.infer_schema({"agent_role": "cio", "final_actions": []}) == "cio_final"
    assert validate.infer_schema({"agent_role": "etf", "status": "failed"}) == "failed_report"
    assert validate.infer_schema({}, "runs/x/phase2/reports/osint_KO.failed.json") == "failed_report"
    assert validate.infer_schema({"order_id": "p1-macro", "agent_type": "cio", "agent_role": "cio",
                                  "inputs": []}) == "work_order"
    assert validate.infer_schema({"foo": 1}) is None
    e = validate.validate_doc({"foo": 1}, None)
    assert len(e) == 1 and e[0].startswith("$: cannot infer the schema")


@pytest.mark.parametrize("doc", [
    {"agent_role": ["x"]},
    {"agent_role": {"r": "fundamental"}},
    {"agent_role": "cio", "mode": ["allocation"]},
    {"agent_role": "cio", "mode": {"m": 1}},
], ids=["role_list", "role_object", "cio_mode_list", "cio_mode_object"])
def test_unhashable_role_or_mode_is_an_error_not_a_crash(doc: dict, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert validate.infer_schema(doc) is None
    e = validate.validate_doc(doc, None)
    assert len(e) == 1 and e[0].startswith("$: cannot infer the schema")
    f = tmp_path / "out.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    assert validate.validate_file(f)[0].startswith("$: cannot infer the schema")
    assert validate.main(["--file", str(f)]) == 1
    assert "cannot infer the schema" in capsys.readouterr().out


def test_validate_file_errors(tmp_path: Path) -> None:
    missing = tmp_path / "fundamental_KO.json"
    assert "file not found" in validate.validate_file(missing, "specialist_report")[0]
    missing.write_text('{"agent_role": "fundamental",', encoding="utf-8")
    e = validate.validate_file(missing, "specialist_report")
    assert len(e) == 1 and "invalid JSON at line 1" in e[0]
    bom = tmp_path / "osint_KO.json"
    bom.write_bytes(b"\xef\xbb\xbf" + (RUN / REPORTS["osint"][0]).read_bytes())
    assert validate.validate_file(bom) == []


# =====================================================================================
# work orders
# =====================================================================================


def _seed_sources(doc: dict) -> list[dict]:
    return [copy.deepcopy(s) for s in doc["sources"] if s["id"].startswith("D:")]


@pytest.fixture
def run_copy(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / RUN_ID
    shutil.copytree(RUN, run)
    return run


def write_order(run: Path, order_id: str, *, role: str, agent_type: str, ticker: str | None, sector: str | None,
                inputs: list[str], output: str, schema: str, mode: str | None = None,
                seed_sources: list | None = None, word_limits: dict | None = None) -> Path:
    front = {
        "order_id": order_id, "run_id": RUN_ID, "step": "p2.wave_a", "agent_type": agent_type, "agent_role": role,
        "mode": mode, "ticker": ticker, "sector": sector, "sector_class": "default" if ticker else None,
        "attempt": 1, "as_of": "2026-10-03",
        "inputs": [{"path": (run / p).as_posix(), "purpose": "input"} for p in inputs],
        "output": (run / output).as_posix(), "schema": (REPO / "schemas" / f"{schema}.json").as_posix(),
        "seed_sources": seed_sources or [], "required_fields": [], "prior_errors": [],
        "word_limits": word_limits or {"thesis": 60},
    }
    path = run / "orders" / f"{order_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"# Work order - {agent_type}\n\nRead only the listed inputs.\n"
    path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body, encoding="utf-8")
    return path


def fundamental_order(run: Path, **kw: Any) -> Path:
    doc = json.loads((run / REPORTS["fundamental"][0]).read_text(encoding="utf-8"))
    return write_order(run, "p2-a-fundamental-KO", role="fundamental", agent_type="fundamental-analyst",
                       ticker="KO", sector="consumer_staples",
                       inputs=["phase2/data/fundamentals_KO.json", "phase2/data/valuation_KO.json",
                               "phase1/prices.json"],
                       output=REPORTS["fundamental"][0], schema="specialist_report",
                       seed_sources=_seed_sources(doc), **kw)


def test_parse_order_md_keeps_tickers_and_dates_as_strings(tmp_path: Path) -> None:
    p = tmp_path / "order.md"
    p.write_text("---\norder_id: p2-a-technical-ON\nticker: ON\nas_of: 2026-10-03\nattempt: 1\n"
                 "inputs:\n  - {path: C:/x/phase2/data/technicals_ON.json, purpose: data}\n---\n# body\n---\n",
                 encoding="utf-8")
    order = validate.parse_order_md(p)
    assert order["ticker"] == "ON" and order["as_of"] == "2026-10-03" and order["attempt"] == 1
    assert order["inputs"][0]["path"] == "C:/x/phase2/data/technicals_ON.json"


@pytest.mark.parametrize("text", ["# no front matter\n", "---\norder_id: x\n", "---\n- a\n- b\n---\n"])
def test_parse_order_md_rejects_bad_front_matter(tmp_path: Path, text: str) -> None:
    p = tmp_path / "bad.md"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        validate.parse_order_md(p)


def test_build_order_context_detects_inputs_by_name(run_copy: Path) -> None:
    order_path = write_order(
        run_copy, "p2-b-devils_advocate-KO", role="devils_advocate", agent_type="devils-advocate", ticker="KO",
        sector="consumer_staples",
        inputs=["phase2/reports/fundamental_KO.json", "phase2/data/technicals_KO.json",
                "phase2/data/technicals_QQQ.json", "phase2/data/valuation_KO.json",
                "phase2/data/fundamentals_KO.json", "phase2/aggregate.json", "phase1/macro.json",
                "phase1/lookthrough.json", "phase1/macro_view.json", "phase1/cio_allocation.json",
                "phase2/consumer_staples_verdict.json", "phase3/health_care_verdict.json"],
        output=REPORTS["devils_advocate"][0], schema="devils_advocate_report")
    c = validate.build_order_context(validate.parse_order_md(order_path))
    assert c["technicals"].endswith("technicals_KO.json")
    for key, tail in (("valuation", "valuation_KO.json"), ("fundamentals", "fundamentals_KO.json"),
                      ("aggregate", "phase2/aggregate.json"), ("macro", "macro.json"),
                      ("lookthrough", "lookthrough.json"), ("macro_view", "macro_view.json"),
                      ("cio_allocation", "cio_allocation.json")):
        assert c[key].endswith(tail), key
    assert [Path(p).name for p in c["verdicts"]] == ["consumer_staples_verdict.json", "health_care_verdict.json"]
    assert Path(c["run_dir"]) == run_copy
    assert c["strict_refs"] is True and c["expect"]["order_id"] == "p2-b-devils_advocate-KO"
    assert isinstance(c["committee_cfg"], dict) and "validation" in c["committee_cfg"]


def test_validate_order_output_valid(run_copy: Path) -> None:
    order_path = fundamental_order(run_copy)
    assert validate.validate_order_output(validate.parse_order_md(order_path), None) == []
    assert validate.validate_order_output(order_path) == []


def test_validate_order_output_uses_the_runs_own_data(run_copy: Path) -> None:
    data_path = run_copy / "phase2/data/valuation_KO.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["cells"][6]["fv_per_share"] = 75.0  # PE-mid
    common.write_json(data_path, data)
    e = validate.validate_order_output(fundamental_order(run_copy))
    assert has(e, "$.extensions.valuation.fair_value_base: does not equal the mean fair value of cells_base",
               "PE-mid 75")


def test_validate_order_output_identity_and_retry_path(run_copy: Path) -> None:
    order_path = fundamental_order(run_copy)
    doc = json.loads((run_copy / REPORTS["fundamental"][0]).read_text(encoding="utf-8"))
    doc["order_id"] = "p2-a-fundamental-PG"
    r2 = run_copy / "phase2/reports/fundamental_KO.r2.json"
    common.write_json(r2, doc)
    e = validate.validate_order_output(order_path, r2)
    assert e == ['$.order_id: does not match the work order; expected "p2-a-fundamental-KO", '
                 'got "p2-a-fundamental-PG"']


def test_validate_order_output_missing_and_strict_refs(run_copy: Path) -> None:
    order_path = fundamental_order(run_copy)
    (run_copy / REPORTS["fundamental"][0]).unlink()
    e = validate.validate_order_output(order_path)
    assert len(e) == 1 and "file not found" in e[0]
    doc = report("fundamental")
    doc["key_figures"][0]["data_ref"] = "C:/elsewhere/fundamentals_KO.json#/ttm/revenue"
    common.write_json(run_copy / REPORTS["fundamental"][0], doc)
    assert has(validate.validate_order_output(order_path), "$.key_figures[0].data_ref: data_ref file not found")


def test_validate_order_output_word_limits_from_order(run_copy: Path) -> None:
    order_path = fundamental_order(run_copy, word_limits={"thesis": 20})
    assert has(validate.validate_order_output(order_path), "$.thesis: too long; expected <= 20 words")


def test_validate_order_output_seed_sources(run_copy: Path) -> None:
    order_path = fundamental_order(run_copy)
    order = validate.parse_order_md(order_path)
    order["seed_sources"][1]["url"] = "https://example.org/other-valuation"
    assert has(validate.validate_order_output(order), "$.sources[1].url: dataset source D:VAL:KO must be copied")


def test_validate_order_output_sector_head(run_copy: Path) -> None:
    order_path = write_order(run_copy, "p2-sh-consumer_staples", role="sector_head", agent_type="sector-head",
                             ticker=None, sector="consumer_staples", mode="phase2",
                             inputs=["phase2/aggregate.json", "phase2/reports/fundamental_KO.json"],
                             output="phase2/consumer_staples_verdict.json", schema="sector_verdict")
    assert validate.validate_order_output(order_path) == []
    agg_path = run_copy / "phase2/aggregate.json"
    agg = json.loads(agg_path.read_text(encoding="utf-8"))
    agg["tickers"]["PG"]["rule_action"] = "HOLD"
    common.write_json(agg_path, agg)
    assert has(validate.validate_order_output(order_path),
               '$.verdicts[1].rule_action: does not match aggregate.json for PG; expected "HOLD", got "TRIM"')


def test_validate_order_output_cio_reconciliation(run_copy: Path) -> None:
    order_path = write_order(run_copy, "p4-cio", role="cio", agent_type="cio", ticker=None, sector=None,
                             mode="reconciliation",
                             inputs=["phase1/macro_view.json", "phase1/cio_allocation.json",
                                     "phase2/consumer_staples_verdict.json", "phase2/financials_verdict.json",
                                     "phase2/funds_verdict.json", "phase3/health_care_verdict.json"],
                             output="phase4/cio_final.json", schema="cio_final")
    assert validate.validate_order_output(order_path) == []
    order = validate.parse_order_md(order_path)
    order["mode"] = "allocation"
    order["schema"] = (REPO / "schemas" / "cio_final.json").as_posix()
    assert has(validate.validate_order_output(order), '$.mode: does not match the work order; expected "allocation"')


def test_unreadable_order_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("no front matter", encoding="utf-8")
    e = validate.validate_order_output(p)
    assert len(e) == 1 and "cannot read work order" in e[0]


# =====================================================================================
# CLI
# =====================================================================================


def test_cli_valid_file(capsys: pytest.CaptureFixture[str]) -> None:
    code = validate.main(["--file", str(RUN / REPORTS["osint"][0])])
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("OK ") and "specialist_report" in out


def test_cli_invalid_file_prints_one_error_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    bad = REPO / "tests/fixtures/schemas/invalid/macro_view/probabilities_sum_0_9.json"
    code = validate.main(["--file", str(bad), "--schema", "macro_view"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 1
    assert out == ["$.scenarios: scenario probabilities must sum to 1; expected 1 +/- 0.01, got 0.9"]


def test_cli_schema_flag_and_unknown_schema(capsys: pytest.CaptureFixture[str]) -> None:
    good = str(RUN / "phase2/data/technicals_KO.json")
    assert validate.main(["--file", good, "--schema", "technicals"]) == 0
    assert validate.main(["--file", good, "--schema", "specialist_report"]) == 1
    assert validate.main(["--file", good, "--schema", "bogus"]) == 1
    assert "unknown schema" in capsys.readouterr().out


def test_cli_order(run_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    order_path = fundamental_order(run_copy)
    assert validate.main(["--order", str(order_path)]) == 0
    doc = report("fundamental")
    doc["extensions"]["valuation"]["fair_value_low"] = 61.0
    r2 = run_copy / "phase2/reports/fundamental_KO.r2.json"
    common.write_json(r2, doc)
    capsys.readouterr()
    assert validate.main(["--order", str(order_path), "--file", str(r2)]) == 1
    assert "$.extensions.valuation.fair_value_low: does not equal the mean" in capsys.readouterr().out


def test_cli_requires_file_or_order() -> None:
    with pytest.raises(SystemExit) as exc:
        validate.main([])
    assert exc.value.code == 2


def test_cli_runs_as_a_script() -> None:
    env = dict(os.environ)
    good = subprocess.run([sys.executable, str(REPO / "scripts" / "validate.py"), "--file",
                           str(RUN / REPORTS["technical"][0])], capture_output=True, text=True, env=env,
                          cwd=str(REPO), encoding="utf-8")
    assert good.returncode == 0, good.stdout + good.stderr
    bad = subprocess.run([sys.executable, str(REPO / "scripts" / "validate.py"), "--file",
                          str(REPO / "tests/fixtures/schemas/invalid/specialist_report/conviction_11.json")],
                         capture_output=True, text=True, env=env, cwd=str(REPO), encoding="utf-8")
    assert bad.returncode == 1
    assert "$.conviction: out of range; expected <= 10, got 11" in bad.stdout


# =====================================================================================
# data_ref provenance: a key figure carries the as_of / kind / unit / filed of the Fact it cites
# =====================================================================================

LAST_CLOSE_REF = "tests/fixtures/validate/run/phase1/prices.json#/tickers/KO/last_close"


def test_key_figure_as_of_must_equal_the_fact_as_of() -> None:
    doc = report("fundamental")
    fig = doc["key_figures"][4]
    assert fig["name"] == "Last close" and fig["data_ref"].endswith("prices.json#/tickers/KO/last_close")
    fig["as_of"] = "2026-01-02"
    e = errs(doc, "fundamental")
    assert has(e, "$.key_figures[4].as_of", "does not match the Fact at", LAST_CLOSE_REF,
               "expected 2026-10-02 (from data), got 2026-01-02")
    assert len([m for m in e if "$.key_figures[4]" in m]) == 1          # only the as_of differs
    fig["as_of"] = "2026-10-02"
    assert errs(doc, "fundamental") == []


def test_key_figure_kind_must_equal_the_fact_kind() -> None:
    # the evidence score applies a kind-specific freshness rule to the AGENT's kind: "other" is never checked
    doc = report("fundamental")
    doc["key_figures"][4]["kind"] = "other"
    assert has(errs(doc, "fundamental"), "$.key_figures[4].kind", "expected price (from data), got other")
    doc["key_figures"][0]["kind"] = "macro"
    assert has(errs(doc, "fundamental"), "$.key_figures[0].kind", "expected fundamental (from data), got macro")


def test_free_hand_kind_and_as_of_would_change_the_evidence_score() -> None:
    # why the check exists: with the Fact's own provenance the figure is stale, with the agent's it is not
    from scripts import evidence_score
    ev_cfg, holidays = evidence_score.resolve_cfg(common.committee_cfg())
    run_date = common.parse_date("2026-10-02")
    honest = {"name": "x", "value": 1.0, "kind": "price", "as_of": "2026-09-01"}
    gamed = {**honest, "kind": "other"}
    assert evidence_score.stale_reason(honest, run_date, ev_cfg, holidays) is not None
    assert evidence_score.stale_reason(gamed, run_date, ev_cfg, holidays) is None


def test_key_figure_filed_must_equal_the_fact_filed() -> None:
    doc = report("fundamental")
    assert doc["key_figures"][0]["filed"] == "2026-07-28"
    doc["key_figures"][0]["filed"] = "2026-09-30"                       # a fresher filing date than the data's
    assert has(errs(doc, "fundamental"), "$.key_figures[0].filed", "expected 2026-07-28 (from data), got 2026-09-30")
    del doc["key_figures"][0]["filed"]                                  # omitted: the 45-day lag would apply
    assert has(errs(doc, "fundamental"), "$.key_figures[0].filed", "expected 2026-07-28 (from data), got null")
    doc["key_figures"][0]["filed"] = None
    assert has(errs(doc, "fundamental"), "$.key_figures[0].filed", "got null")


def test_key_figure_filed_invented_when_the_fact_has_none() -> None:
    doc = report("fundamental")
    assert "filed" not in doc["key_figures"][4] or doc["key_figures"][4]["filed"] is None
    doc["key_figures"][4]["filed"] = "2026-10-01"
    e = errs(doc, "fundamental")
    assert has(e, "$.key_figures[4].filed", "the data has none", "got 2026-10-01")


def test_key_figure_unit_must_equal_the_fact_unit() -> None:
    doc = report("fundamental")
    doc["key_figures"][1]["unit"] = "x"                                  # operating margin is a pct
    assert has(errs(doc, "fundamental"), "$.key_figures[1].unit", "expected pct (from data), got x")


def test_several_provenance_fields_are_all_reported() -> None:
    doc = report("fundamental")
    fig = doc["key_figures"][0]
    fig.update({"as_of": "2025-01-01", "kind": "other", "filed": "2025-02-02"})
    e = errs(doc, "fundamental")
    for field_name, want in (("as_of", "2026-06-27"), ("kind", "fundamental"), ("filed", "2026-07-28")):
        assert has(e, f"$.key_figures[0].{field_name}", f"expected {want} (from data)"), e


def test_bare_number_pointers_skip_the_provenance_check() -> None:
    doc = report("devils_advocate")
    fig = doc["key_figures"][0]
    assert fig["data_ref"].endswith("valuation_KO.json#/cells/0/fv_per_share")      # a bare number, not a Fact
    fig.update({"as_of": "2026-09-30", "kind": "price", "unit": "USD", "filed": "2026-01-01"})
    assert errs(doc, "devils_advocate") == []


def test_provenance_not_checked_for_figures_without_a_number_or_a_data_ref() -> None:
    doc = report("operator_ceo")                                         # web figures: no data_ref
    doc["key_figures"][0]["as_of"] = "2020-01-01"
    assert errs(doc, "operator_ceo") == []
    doc = report("fundamental")
    doc["key_figures"][0].update({"value": None, "as_of": None, "kind": "other"})  # null value: nothing to date
    doc["missing_data"].append("Revenue TTM was not available")
    assert not has(errs(doc, "fundamental"), "key_figures[0].kind", "key_figures[0].as_of")


def test_provenance_applies_to_every_report_schema_that_cites_facts() -> None:
    doc = report("etf")
    doc["key_figures"][0]["as_of"] = "2026-10-03"                        # the data says 2026-10-02
    assert has(errs(doc, "etf", technicals="QQQ"), "$.key_figures[0].as_of", "expected 2026-10-02 (from data), got 2026-10-03")
    doc = report("technical")
    doc["key_figures"][2]["kind"] = "other"
    assert has(errs(doc, "technical"), "$.key_figures[2].kind", "expected price (from data), got other")


def test_same_date_text_helper() -> None:
    assert validate._same_date_text("2026-10-02", "2026-10-02")
    assert validate._same_date_text("2026-10-02T00:00:00+00:00", "2026-10-02")
    assert validate._same_date_text(None, None)
    assert not validate._same_date_text(None, "2026-10-02") and not validate._same_date_text("2026-10-02", None)
    assert not validate._same_date_text("2026-10-02", "2026-10-03")
    assert validate._same_date_text("not a date", "not a date") and not validate._same_date_text("x", "y")


# =====================================================================================
# proxy-priced funds: order ticker (TASE id) != the ticker of its (only) technicals file
# =====================================================================================

PROXY_TICKER = "1186063"        # e.g. a TASE fund priced from QQQ


def proxy_etf_doc() -> dict:
    doc = report("etf")
    doc["ticker"] = PROXY_TICKER
    doc["order_id"] = f"p2-a-etf-{PROXY_TICKER}"
    return doc


def test_proxy_priced_fund_is_checked_against_its_only_technicals_file() -> None:
    doc = proxy_etf_doc()
    ok = rep(doc, "etf", technicals="QQQ", sole_context=["technicals"])
    assert ok.errors == [], ok.errors
    assert any("the order's only technicals file" in n and "QQQ" in n for n in ok.notes)
    doc["momentum_vs_benchmark"]["rs_12m"] = 9.9                         # computed: 4.9
    e = errs(doc, "etf", technicals="QQQ", sole_context=["technicals"])
    assert has(e, "$.momentum_vs_benchmark.rs_12m", "does not match technicals_<T>.json relative_strength", "4.9")
    doc["momentum_vs_benchmark"].update({"rs_12m": 4.9, "benchmark": "XLK"})
    assert has(errs(doc, "etf", technicals="QQQ", sole_context=["technicals"]),
               "$.momentum_vs_benchmark.benchmark", "is not a benchmark technicals_<T>.json computed")


def test_other_ticker_context_is_still_skipped_without_the_sole_file_marker() -> None:
    doc = proxy_etf_doc()
    doc["momentum_vs_benchmark"]["rs_12m"] = 9.9
    r = rep(doc, "etf", technicals="QQQ")                                # several files / unknown: mismatch stands
    assert not has(r.errors, "momentum_vs_benchmark")
    assert any("technicals context is for QQQ, not 1186063" in n for n in r.notes)
    # the marker names kinds: a sole *valuation* file does not unlock technicals
    r2 = rep(doc, "etf", technicals="QQQ", sole_context=["valuation"])
    assert not has(r2.errors, "momentum_vs_benchmark")
    assert any("not 1186063" in n for n in r2.notes)


def test_proxy_technical_report_regime_and_levels_are_checked() -> None:
    # stand-in for a proxy fund: the report is for order ticker 1186063, its only technicals file is KO's
    doc = report("technical")
    doc["ticker"] = PROXY_TICKER
    doc["extensions"]["trend_regime"] = "trend_up"                        # computed: range
    sole = {"sole_context": ["technicals"]}
    e = errs(doc, "technical", **sole)
    assert has(e, "$.extensions.trend_regime", "does not match the computed regime", '"range"', '"trend_up"')
    doc["extensions"]["trend_regime"] = "range"
    doc["extensions"]["key_levels"][0]["price"] = 60.0                    # S1 computed at 66.8
    assert has(errs(doc, "technical", **sole), "$.extensions.key_levels[0].price", "66.8")
    doc["extensions"]["key_levels"][0]["price"] = 66.8
    assert errs(doc, "technical", **sole) == []
    # DA bear_case_ref level / valuation cell checks use the same rule
    da = report("devils_advocate")
    da["ticker"] = PROXY_TICKER
    da["bear_case_ref"], da["bear_case_price"] = "S1", 60.0
    assert has(errs(da, "devils_advocate", **sole), "$.bear_case_price", "does not equal the computed price of S1")
    assert not has(errs(da, "devils_advocate"), "bear_case_price")        # without the marker: skipped


def _proxy_order(run: Path, inputs: list[str], doc: dict | None = None) -> Path:
    order_path = write_order(run, f"p2-a-etf-{PROXY_TICKER}", role="etf", agent_type="etf-analyst",
                             ticker=PROXY_TICKER, sector="funds", inputs=inputs,
                             output=f"phase2/reports/etf_{PROXY_TICKER}.json", schema="etf_review")
    doc = doc or proxy_etf_doc()
    doc["sector"] = "funds"
    common.write_json(run / f"phase2/reports/etf_{PROXY_TICKER}.json", doc)
    return order_path


def test_build_order_context_records_the_sole_technicals_file_of_a_proxy_fund(run_copy: Path) -> None:
    order_path = _proxy_order(run_copy, ["phase1/lookthrough.json", "phase2/data/technicals_QQQ.json"])
    c = validate.build_order_context(validate.parse_order_md(order_path))
    assert c["technicals"].endswith("technicals_QQQ.json") and c["sole_context"] == ["technicals"]
    assert validate.validate_order_output(order_path) == []
    doc = proxy_etf_doc()
    doc["momentum_vs_benchmark"]["rs_12m"] = 9.9
    _proxy_order(run_copy, ["phase1/lookthrough.json", "phase2/data/technicals_QQQ.json"], doc)
    assert has(validate.validate_order_output(order_path), "$.momentum_vs_benchmark.rs_12m", "4.9")


def test_several_technicals_files_and_none_for_the_order_ticker_keep_the_mismatch(run_copy: Path) -> None:
    shutil.copy(run_copy / "phase2/data/technicals_QQQ.json", run_copy / "phase2/data/technicals_SPY.json")
    doc = proxy_etf_doc()
    doc["momentum_vs_benchmark"]["rs_12m"] = 9.9
    inputs = ["phase1/lookthrough.json", "phase2/data/technicals_QQQ.json", "phase2/data/technicals_SPY.json"]
    order_path = _proxy_order(run_copy, inputs, doc)
    c = validate.build_order_context(validate.parse_order_md(order_path))
    assert "technicals" not in c and c["sole_context"] == []
    rep_ = validate.validate_order_output_report(order_path)
    assert not has(rep_.errors, "momentum_vs_benchmark")           # no unambiguous technicals file: skipped
    # an order for QQQ itself with two technicals files picks its own
    own = validate.build_order_context({"ticker": "QQQ", "inputs": inputs})
    assert own["technicals"].endswith("technicals_QQQ.json") and own["sole_context"] == []


def test_sole_file_is_derived_from_the_order_markdown_when_the_context_has_no_marker(run_copy: Path) -> None:
    # the orchestrator builds its own context (technicals, run_dir, expect) without `sole_context`
    inputs = ["phase1/lookthrough.json", "phase2/data/technicals_QQQ.json"]
    _proxy_order(run_copy, inputs)
    doc = proxy_etf_doc()
    doc["sector"] = "funds"
    doc["momentum_vs_benchmark"]["rs_12m"] = 9.9
    context = {"technicals": str(run_copy / "phase2/data/technicals_QQQ.json"), "run_dir": str(run_copy),
               "expect": {"order_id": f"p2-a-etf-{PROXY_TICKER}", "ticker": PROXY_TICKER}}
    assert has(validate.validate_doc(doc, "etf_review", context=context), "$.momentum_vs_benchmark.rs_12m")
    # two technicals inputs in the order: the mismatch stands
    shutil.copy(run_copy / "phase2/data/technicals_QQQ.json", run_copy / "phase2/data/technicals_SPY.json")
    _proxy_order(run_copy, inputs + ["phase2/data/technicals_SPY.json"])
    assert not has(validate.validate_doc(doc, "etf_review", context=context), "momentum_vs_benchmark")
    # no order file on disk: no marker, mismatch stands
    (run_copy / "orders" / f"p2-a-etf-{PROXY_TICKER}.md").unlink()
    assert not has(validate.validate_doc(doc, "etf_review", context=context), "momentum_vs_benchmark")
