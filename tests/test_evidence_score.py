"""evidence_score.py (DESIGN 4.8, SPEC 5 + 10): freshness (trading days incl. weekend + holiday,
fundamental filing lag, macro 45 d), null required fields per role / sector class, distinct source
URLs, missing_data cap, floor, failed stubs, evidence.json output."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import common, evidence_score as es

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "aggregate"
RUN_DATE = "2026-10-02"  # a Friday


# --------------------------------------------------------------------------- helpers
def _registry() -> Registry:
    resources = []
    for p in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    return Registry().with_resources(resources)


def schema_errors(doc: Any, schema_id: str) -> list[str]:
    reg = _registry()
    validator = Draft202012Validator(reg.contents(schema_id), registry=reg, format_checker=FormatChecker())
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(doc)]


def template(role: str) -> dict:
    return json.loads((FIX / f"template_{role}.json").read_text(encoding="utf-8"))


def with_figures(report: dict, *figures: dict) -> dict:
    r = copy.deepcopy(report)
    r["key_figures"] = list(figures)
    return r


def fig(kind: str, as_of: str | None, value: float | None = 1.0, **extra: Any) -> dict:
    d = {"name": f"{kind} figure", "value": value, "unit": "x", "source_id": "W1", "as_of": as_of, "kind": kind}
    d.update(extra)
    return d


def score(report: Any, role: str = "technical", sector_class: str = "default", run_date: str = RUN_DATE,
          cfg: Any = None) -> dict:
    return es.score_report(report, role=role, sector_class=sector_class, run_date=run_date, cfg=cfg)


def rules(res: dict) -> list[str]:
    return [d["rule"] for d in res["deductions"]]


# --------------------------------------------------------------------------- baseline
def test_clean_report_scores_start() -> None:
    res = score(template("technical"))
    assert res["score"] == 10
    assert res["status"] == "ok"
    assert res["deductions"] == []
    assert res["start"] == 10 and res["floor"] == 1


# --------------------------------------------------------------------------- price freshness
@pytest.mark.parametrize("as_of,run_date,stale", [
    ("2026-09-29", "2026-10-02", False),   # Tue -> Fri: 3 trading days
    ("2026-09-28", "2026-10-02", True),    # Mon -> Fri: 4 trading days
    ("2026-09-30", "2026-10-05", False),   # Wed -> Mon across a weekend: 3 trading days (6 calendar)
    ("2026-09-29", "2026-10-05", True),    # Tue -> Mon: 4 trading days
    ("2026-09-03", "2026-09-09", False),   # Thu -> Wed across weekend + Labor Day (09-07): 3
    ("2026-09-02", "2026-09-09", True),    # Wed -> Wed over Labor Day: 4
    ("2026-11-24", "2026-11-30", False),   # Tue -> Mon across Thanksgiving (11-26) + weekend: 3
    ("2026-10-02", "2026-10-02", False),   # same day
])
def test_price_staleness_counts_nyse_trading_days(as_of, run_date, stale) -> None:
    res = score(with_figures(template("technical"), fig("price", as_of)), run_date=run_date)
    assert (rules(res) == ["stale"]) is stale
    assert res["score"] == (9 if stale else 10)


def test_holiday_list_comes_from_config() -> None:
    report = with_figures(template("technical"), fig("price", "2026-09-03"))
    cfg = common.committee_cfg()
    assert score(report, run_date="2026-09-09", cfg=cfg)["score"] == 10          # Labor Day excluded
    cfg_no_holidays = dict(cfg, market_holidays=[])
    res = score(report, run_date="2026-09-09", cfg=cfg_no_holidays)
    assert rules(res) == ["stale"]                                               # now 4 trading days
    assert "4 trading days" in res["deductions"][0]["detail"]


# --------------------------------------------------------------------------- fundamental freshness
@pytest.mark.parametrize("figure,stale", [
    (fig("fundamental", "2026-03-31", filed="2026-06-04"), False),   # 120 d after filing
    (fig("fundamental", "2026-03-31", filed="2026-06-03"), True),    # 121 d
    (fig("fundamental", "2026-04-20"), False),                       # as_of + 45 = 06-04 -> 120 d
    (fig("fundamental", "2026-04-19"), True),                        # as_of + 45 = 06-03 -> 121 d
    (fig("fundamental", "2025-12-31", filed="2026-07-30"), False),   # filed wins over an old period end
    (fig("fundamental", "2026-06-30", filed="2026-01-15"), True),    # filed date drives it
])
def test_fundamental_staleness_filed_vs_as_of_plus_lag(figure, stale) -> None:
    res = score(with_figures(template("fundamental"), figure), role="fundamental")
    assert ("stale" in rules(res)) is stale


# --------------------------------------------------------------------------- macro + other kinds
@pytest.mark.parametrize("as_of,stale", [("2026-08-18", False), ("2026-08-17", True)])
def test_macro_staleness_at_45_days(as_of, stale) -> None:
    res = score(with_figures(template("technical"), fig("macro", as_of)))
    assert (rules(res) == ["stale"]) is stale


def test_other_kind_and_null_values_are_not_checked() -> None:
    report = with_figures(template("technical"),
                          fig("other", "2019-01-01"),
                          fig("price", "2020-01-01", value=None),
                          fig("fundamental", "2015-12-31", value=None))
    assert score(report)["score"] == 10


def test_numeric_figure_without_as_of_counts_as_stale() -> None:
    res = score(with_figures(template("technical"), fig("price", None)))
    assert rules(res) == ["stale"]


def test_each_stale_figure_costs_one_point() -> None:
    report = with_figures(template("technical"), fig("price", "2026-09-01"), fig("macro", "2026-01-01"),
                          fig("price", "2026-10-01"))
    res = score(report)
    assert rules(res) == ["stale", "stale"]
    assert res["score"] == 8
    assert all(d["points"] == -1 for d in res["deductions"])


# --------------------------------------------------------------------------- required fields
def test_technical_null_required_fields() -> None:
    r = template("technical")
    r["invalidation_price"] = None
    r["extensions"]["invalidation_ref"] = None
    res = score(r)
    assert rules(res) == ["null_required", "null_required"]
    assert res["score"] == 6
    assert {d["points"] for d in res["deductions"]} == {-2}


def test_fundamental_null_and_missing_required_fields() -> None:
    r = template("fundamental")
    r["extensions"]["valuation"]["fair_value_base"] = None
    res = score(r, role="fundamental")
    assert res["score"] == 10 - 2 - 1   # null base fv (-2); the template's 1 missing_data item (-1)
    r2 = template("fundamental")
    del r2["extensions"]["valuation"]
    res2 = score(r2, role="fundamental")
    assert rules(res2).count("null_required") == 3  # low, base, high gone; conviction present
    # invalidation_price is NOT required for the fundamental lens
    r3 = template("fundamental")
    r3["invalidation_price"] = None
    assert "null_required" not in rules(score(r3, role="fundamental"))


def test_required_fields_depend_on_sector_class() -> None:
    r = template("industry_ops")
    r["missing_data"] = []
    del r["extensions"]["supply_chain"]
    r["extensions"]["capacity_backlog"] = None
    default = score(r, role="industry_ops", sector_class="default")
    fin = score(r, role="industry_ops", sector_class="financials")
    assert rules(default).count("null_required") == 2
    assert "null_required" not in rules(fin)   # financials list omits supply chain + capacity/backlog
    # unknown sector class falls back to the default list
    assert rules(score(r, role="industry_ops", sector_class="weird")).count("null_required") == 2


@pytest.mark.parametrize("role,mutate,expected", [
    ("osint", lambda r: r["extensions"].__setitem__("signals", []), 1),
    ("osint", lambda r: r["extensions"].pop("signals"), 1),
    ("devils_advocate", lambda r: r.__setitem__("bear_case_price", None), 1),
    ("devils_advocate", lambda r: r.pop("verdict_on_thesis"), 1),
    ("etf", lambda r: r.__setitem__("expense_ratio_pct", None), 1),
    ("etf", lambda r: r.__setitem__("action", ""), 1),
    ("operator_ceo", lambda r: r["extensions"]["insider_activity"].pop("assessment"), 1),
])
def test_null_required_per_role(role, mutate, expected) -> None:
    r = template(role)
    mutate(r)
    sc = "fund" if role == "etf" else "default"
    assert rules(score(r, role=role, sector_class=sc)).count("null_required") == expected


def test_required_fields_lookup() -> None:
    cfg = common.committee_cfg()
    ev_cfg, _ = es.resolve_cfg(cfg)
    assert es.required_fields_for("technical", "default", ev_cfg) == [
        "extensions.trend_regime", "invalidation_price", "extensions.invalidation_ref"]
    assert es.required_fields_for("macro", "default", ev_cfg) == []
    # the evidence section alone is accepted as cfg too
    ev_only, hol = es.resolve_cfg(cfg["evidence"])
    assert ev_only["start"] == 10 and hol is None


# --------------------------------------------------------------------------- sources
def test_fewer_than_three_distinct_source_urls() -> None:
    r = template("technical")
    same = r["sources"][0]["url"]
    r["sources"][1]["url"] = same
    res = score(r)
    assert rules(res) == ["few_sources"]
    assert res["score"] == 8
    assert "2 distinct" in res["deductions"][0]["detail"]


def test_url_normalization_and_exact_threshold() -> None:
    r = template("technical")
    assert len(es.distinct_source_urls(r)) == 3 and score(r)["score"] == 10
    # scheme/host case, a trailing slash and a fragment do not make a different source
    r["sources"][1]["url"] = "HTTPS://FINANCE.YAHOO.COM/quote/KO/history#top"
    assert r["sources"][0]["url"] == "https://finance.yahoo.com/quote/KO/history/"
    assert len(es.distinct_source_urls(r)) == 2
    # two ids sharing one URL (D:SEC:..:CF and D:VAL:.. both point at companyfacts) count once
    da = template("devils_advocate")
    urls = es.distinct_source_urls(da)
    assert len(urls) < len(da["sources"])


@pytest.mark.parametrize("a,b,same", [
    # paths are case-sensitive: two distinct pages are two sources (regression: whole-URL lower())
    ("https://x.com/AbC", "https://x.com/abc", False),
    ("https://x.com/a?Id=1", "https://x.com/a?id=1", False),
    ("https://x.com/a/B", "https://x.com/a/b", False),
    # scheme and host are case-insensitive
    ("HTTPS://X.COM/AbC", "https://x.com/AbC", True),
    ("https://News.Example.org/Story", "https://news.example.ORG/Story", True),
    # trailing slash and fragment are dropped
    ("https://x.com/AbC/", "https://x.com/AbC", True),
    ("https://x.com/AbC#section-2", "https://x.com/AbC", True),
    ("https://x.com/", "https://x.com", True),
    ("  https://x.com/AbC  ", "https://x.com/AbC", True),
    # nothing else is normalized
    ("http://x.com/AbC", "https://x.com/AbC", False),
    ("https://www.x.com/AbC", "https://x.com/AbC", False),
])
def test_norm_url_lowercases_only_scheme_and_host(a: str, b: str, same: bool) -> None:
    assert (es._norm_url(a) == es._norm_url(b)) is same


def test_case_distinct_urls_are_not_merged_into_a_false_few_sources_penalty() -> None:
    r = template("technical")
    r["sources"] = [{**r["sources"][0], "url": "https://x.com/AbC"},
                    {**r["sources"][1], "url": "https://x.com/abc"},
                    {**r["sources"][2], "url": "https://x.com/ABC"}]
    assert len(es.distinct_source_urls(r)) == 3
    res = score(r)
    assert "few_sources" not in rules(res) and res["score"] == 10
    # genuinely identical sources (differing only in host case / slash) still count once
    r["sources"][1]["url"] = "https://X.com/AbC/"
    assert len(es.distinct_source_urls(r)) == 2
    assert "few_sources" in rules(score(r))


def test_norm_url_ignores_non_strings() -> None:
    assert es._norm_url(None) is None and es._norm_url(5) is None and es._norm_url("   ") is None


# --------------------------------------------------------------------------- missing_data, floor, failed
@pytest.mark.parametrize("n,points", [(1, -1), (2, -2), (4, -4), (6, -4), (12, -4)])
def test_missing_data_capped_at_minus_four(n, points) -> None:
    r = template("technical")
    r["missing_data"] = [f"item {i}" for i in range(n)]
    res = score(r)
    assert rules(res) == ["missing_data"]
    assert res["deductions"][0]["points"] == points
    assert res["score"] == 10 + points


def test_floor_is_one() -> None:
    r = template("technical")
    r["invalidation_price"] = None
    r["extensions"]["invalidation_ref"] = None
    r["extensions"]["trend_regime"] = None
    r["sources"] = r["sources"][:1]
    r["missing_data"] = ["a", "b", "c", "d", "e"]
    r["key_figures"] = [fig("price", "2026-01-02"), fig("macro", "2026-01-02")]
    res = score(r)
    assert res["raw_score"] == 10 - 6 - 2 - 2 - 4  # = -4
    assert res["score"] == 1


def test_failed_stub_scores_one() -> None:
    res = score(template("failed"), role="operator_ceo")
    assert res["score"] == 1
    assert res["status"] == "failed"
    assert res["deductions"][0]["rule"] == "failed_report"
    assert res["deductions"][0]["points"] == -9
    res2 = score(None, role="technical")
    assert res2["score"] == 1 and res2["status"] == "failed"
    assert res2["deductions"][0]["rule"] == "unreadable_report"


# --------------------------------------------------------------------------- evidence.json
def test_score_reports_writes_valid_evidence_json(tmp_path: Path) -> None:
    good = tmp_path / "reports" / "technical_TST.json"
    good.parent.mkdir(parents=True)
    good.write_text(json.dumps(template("technical")), encoding="utf-8")
    stub = tmp_path / "reports" / "operator_ceo_TST.failed.json"
    stub.write_text(json.dumps(template("failed")), encoding="utf-8")
    fund = tmp_path / "reports" / "fundamental_TST.json"
    fund.write_text(json.dumps(template("fundamental")), encoding="utf-8")
    gone = tmp_path / "reports" / "osint_TST.json"
    items = [
        {"path": str(good), "role": "technical", "ticker": "TST", "sector_class": "default"},
        {"path": stub, "role": "operator_ceo", "ticker": "tst", "sector_class": "default"},
        {"path": str(fund), "role": "fundamental", "ticker": "TST", "sector_class": "default"},
        {"path": str(gone), "role": "osint", "ticker": "TST", "sector_class": "default"},
    ]
    out = tmp_path / "evidence.json"
    doc = es.score_reports(items, out, run_date=RUN_DATE)
    assert out.exists()
    assert schema_errors(json.loads(out.read_text(encoding="utf-8")), "urn:agent-investors:data/evidence") == []
    assert doc["schema"] == "evidence/v1" and doc["as_of"] == RUN_DATE
    reps = doc["reports"]
    assert set(reps) == {Path(str(p)).as_posix() for p in (good, stub, fund, gone)}
    assert reps[good.as_posix()]["score"] == 10
    assert reps[stub.as_posix()]["status"] == "failed" and reps[stub.as_posix()]["score"] == 1
    assert reps[stub.as_posix()]["ticker"] == "TST"
    assert reps[fund.as_posix()]["score"] == 9  # template has one missing_data item
    assert reps[gone.as_posix()]["status"] == "failed"
    assert any(f["code"] == "report_unreadable" for f in doc["flags"])
    assert es.score_run_reports is es.score_reports


def test_score_reports_role_falls_back_to_filename_and_never_null(tmp_path: Path) -> None:
    rdir = tmp_path / "reports"
    rdir.mkdir()
    # Missing file, no item role: role comes from the filename prefix.
    gone = rdir / "fundamental_KO.json"
    # Readable stub without agent_role and no item role: filename prefix (multi-word role).
    stub = rdir / "operator_ceo_KO.failed.json"
    failed = template("failed")
    failed.pop("agent_role", None)
    stub.write_text(json.dumps(failed), encoding="utf-8")
    # Retry file with a bogus item role: falls back to the report's agent_role, flagged.
    r2 = rdir / "technical_KO.r2.json"
    r2.write_text(json.dumps(template("technical")), encoding="utf-8")
    # No role anywhere: skipped with flag + missing_data (no null-role entry).
    anon = tmp_path / "nope" / "mystery.json"
    items = [
        {"path": str(gone), "ticker": "KO"},
        {"path": str(stub), "ticker": "KO"},
        {"path": str(r2), "role": "bogus", "ticker": "KO"},
        {"path": str(anon), "ticker": "KO"},
    ]
    doc = es.score_reports(items, run_date=RUN_DATE)
    assert schema_errors(doc, "urn:agent-investors:data/evidence") == []
    reps = doc["reports"]
    assert reps[gone.as_posix()]["agent_role"] == "fundamental"
    assert reps[gone.as_posix()]["status"] == "failed"
    assert reps[stub.as_posix()]["agent_role"] == "operator_ceo"
    assert reps[r2.as_posix()]["agent_role"] == "technical"
    assert anon.as_posix() not in reps
    assert all(v["agent_role"] in common.ROLE_KEYS for v in reps.values())
    codes = [f["code"] for f in doc["flags"]]
    assert "report_role_unknown" in codes and "report_role_invalid" in codes
    assert any("role unknown" in m and anon.as_posix() in m for m in doc["missing_data"])


def test_role_from_filename() -> None:
    assert es.role_from_filename("x/operator_ceo_KO.failed.json") == "operator_ceo"
    assert es.role_from_filename("devils_advocate_BRK-B.r2.json") == "devils_advocate"
    assert es.role_from_filename("sector_head_tech.json") == "sector_head"
    assert es.role_from_filename("evidence.json") is None


def test_cli_single_report_and_items(tmp_path: Path, capsys) -> None:
    rep = tmp_path / "technical_TST.json"
    r = template("technical")
    r["missing_data"] = ["x"]
    rep.write_text(json.dumps(r), encoding="utf-8")
    assert es.main(["--report", str(rep), "--role", "technical", "--sector-class", "default",
                    "--run-date", RUN_DATE]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["score"] == 9
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"path": str(rep), "role": "technical", "ticker": "TST"}]), encoding="utf-8")
    ev_out = tmp_path / "evidence.json"
    assert es.main(["--items", str(items), "--run-date", RUN_DATE, "--out", str(ev_out)]) == 0
    capsys.readouterr()
    doc = json.loads(ev_out.read_text(encoding="utf-8"))
    assert doc["reports"][rep.as_posix()]["score"] == 9
    assert es.main(["--report", str(rep), "--run-date", "not-a-date"]) == 2


def test_not_assessed_placeholder_counts_as_null_required_field():
    from scripts import evidence_score
    assert evidence_score._is_null("Not assessed: proxy statement unreachable")
    assert not evidence_score._is_null("Assessed: strong capital allocation record")
