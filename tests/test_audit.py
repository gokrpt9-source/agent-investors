"""scripts/audit.py - the chair's deterministic quality audit.

Every check has a passing case (the ``clean`` fixture run, or a pure-function input) and a failing case (a copy of
the clean run with one defect edited in, or the ``flawed`` fixture). The real run ``runs/2026-10-28_1002`` is the
acceptance fixture (skipped when the directory is absent). Offline; nothing under ``runs/`` is written.
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from jsonschema import Draft202012Validator

from conftest import REPO_ROOT, FIXTURES_DIR
from scripts import audit, common

FX = FIXTURES_DIR / "audit"
NOW = "2026-10-02"
REAL_RUN = REPO_ROOT / "runs" / "2026-10-28_1002"


# --------------------------------------------------------------------------- helpers
def _sha_manifest(run: Path) -> None:
    import hashlib
    m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    m["config_sha256"]["portfolio.yaml"] = hashlib.sha256((run / "inputs" / "portfolio.yaml").read_bytes()).hexdigest()
    (run / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")


def fresh(tmp_path: Path, name: str = "clean") -> tuple[Path, Path]:
    """Copy of a fixture run + a 'live' portfolio.yaml identical to the run's snapshot."""
    run = tmp_path / name
    shutil.copytree(FX / name, run)
    _sha_manifest(run)
    live = tmp_path / "live_portfolio.yaml"
    shutil.copyfile(run / "inputs" / "portfolio.yaml", live)
    return run, live


def edit(run: Path, rel: str, fn: Callable[[Any], Any]) -> None:
    p = run / rel
    doc = json.loads(p.read_text(encoding="utf-8"))
    out = fn(doc)
    p.write_text(json.dumps(out if isinstance(out, (dict, list)) else doc, indent=2), encoding="utf-8")


def go(run: Path, live: Path, now: str = NOW) -> dict:
    return audit.run(run, now=now, portfolio_path=live, write=False)


def of(doc: dict, code: str, ticker: str | None = "__any__") -> list[dict]:
    return [f for f in doc["findings"] if f["code"] == code and (ticker == "__any__" or f["ticker"] == ticker)]


def plan_pos(doc: dict, t: str) -> dict:
    return next(p for p in doc["positions"] if p["ticker"] == t)


def mk_ctx(**kw: Any) -> audit.Ctx:
    ctx = audit.Ctx(**kw)
    if ctx.now is None:
        ctx.now = date(2026, 10, 2)
    if ctx.as_of is None:
        ctx.as_of = date(2026, 10, 2)
    return ctx


def agg(price: float, fv: float | None, *, kind: str = "holding", asset_type: str = "stock", light: bool = False,
        sector_class: str = "default", **extra: Any) -> dict:
    return {"kind": kind, "asset_type": asset_type, "sector": "energy", "sector_class": sector_class, "_light": light,
            "inputs": {"price": {"value": price}}, "rr": {"price": price, "fv_base": fv, "rr": None, "null_reason": None},
            "specialists": [], "da": {"status": "ok"}, "flags": {"all": [], "unresolved": []}, **extra}


# --------------------------------------------------------------------------- whole-run behaviour
def test_clean_run_has_no_findings_and_passes(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    doc = go(run, live)
    assert doc["findings"] == []
    assert doc["summary"] == {"high": 0, "medium": 0, "low": 0, "verdict": "pass"}
    assert doc["schema"] == "audit/1" and doc["run_id"] == "2026-10-02_0900" and doc["as_of"] == "2026-10-02"
    assert "check_errors" not in doc


def test_run_writes_phase4_audit_json_atomically(tmp_path: Path) -> None:
    run, live = fresh(tmp_path, "flawed")
    doc = audit.run(run, now=NOW, portfolio_path=live)
    path = run / "phase4" / "audit.json"
    assert json.loads(path.read_text(encoding="utf-8")) == doc
    assert not list((run / "phase4").glob("*.tmp")) and not list((run / "phase4").glob(".*"))
    before = path.read_text(encoding="utf-8")
    path.unlink()
    audit.run(run, now=NOW, portfolio_path=live, write=False)
    assert not path.exists()
    assert audit.run(run, now=NOW, portfolio_path=live)["findings"] == json.loads(before)["findings"]


def test_flawed_run_validates_against_schema_and_is_ordered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGINV_NOW", "2026-10-02T09:00:00+00:00")
    run, live = fresh(tmp_path, "flawed")
    doc = go(run, live)
    schema = json.loads((REPO_ROOT / "schemas" / "audit.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(doc))
    assert not errors, [e.message for e in errors]
    fs = doc["findings"]
    assert [f["id"] for f in fs] == [f"F{i:02d}" for i in range(1, len(fs) + 1)]
    rank = {"high": 0, "medium": 1, "low": 2}
    keys = [(rank[f["severity"]], f["code"], f["ticker"] or "") for f in fs]
    assert keys == sorted(keys)
    s = doc["summary"]
    assert s["high"] == sum(f["severity"] == "high" for f in fs) and s["verdict"] == "needs_rework"
    assert s["medium"] + s["low"] + s["high"] == len(fs)
    assert doc["generated_at"] == "2026-10-02T09:00:00+00:00"
    assert go(run, live) == doc  # deterministic


def test_flawed_fixture_carries_every_planted_defect(tmp_path: Path) -> None:
    run, live = fresh(tmp_path, "flawed")
    codes = {f["code"] for f in go(run, live)["findings"]}
    assert {"text_vs_plan_contradiction", "trim_became_full_exit", "valuation_outlier", "action_vs_trade",
            "captive_or_leverage_flag", "failed_reports", "limit_inconsistency", "low_evidence",
            "plan_does_not_cure_breach", "thin_review", "unresolved_high_flags", "cash_drag", "pending_event_risk",
            "weak_why"} <= codes


def test_every_fix_is_self_describing(tmp_path: Path) -> None:
    run, live = fresh(tmp_path, "flawed")
    for f in go(run, live)["findings"]:
        assert f["message"] and f["evidence"], f["code"]
        fix = f["fix"]
        assert fix["type"] in audit.FIX_TYPES
        assert fix["feedback"].strip(), f["code"]
        if fix["type"] == "rerun_agents":
            assert fix["roles"], f["code"]


def test_a_crashing_check_is_reported_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, live = fresh(tmp_path)

    def boom(ctx: audit.Ctx) -> list:
        raise RuntimeError("kaput")

    monkeypatch.setattr(audit, "CHECKS", (("cash_drag", boom),) + audit.CHECKS[:1])
    doc = go(run, live)
    assert doc["check_errors"] == [{"check": "cash_drag", "error": "RuntimeError: kaput"}]
    schema = json.loads((REPO_ROOT / "schemas" / "audit.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(doc))


def test_missing_run_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        audit.run(tmp_path / "nope", write=False)


def test_run_survives_an_empty_run_directory(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    doc = audit.run(tmp_path / "r", now=NOW, portfolio_path=tmp_path / "none.yaml", write=False)
    assert doc["findings"] == [] and doc["summary"]["verdict"] == "pass"


# --------------------------------------------------------------------------- config
def test_audit_cfg_defaults_overrides_and_min_evidence() -> None:
    cfg = audit.audit_cfg({})
    assert cfg["outlier_high_low"] == 0.35 and cfg["min_evidence"] == 5.0 and cfg["bias_min_n"] == 6
    cfg = audit.audit_cfg({"actions": {"min_evidence": 6}, "audit": {"outlier_medium_low": 0.5, "bias_min_n": 4,
                                                                      "event_window_days": "x", "bogus": 1}})
    assert cfg["min_evidence"] == 6.0 and cfg["outlier_medium_low"] == 0.5 and cfg["bias_min_n"] == 4
    assert cfg["event_window_days"] == 3 and "bogus" not in cfg
    assert audit.audit_cfg({"audit": {"min_evidence": 4}})["min_evidence"] == 4.0


def test_committee_yaml_audit_block_matches_defaults_and_validates() -> None:
    cfg = common.committee_cfg()
    block = cfg["audit"]
    assert set(block) <= set(audit.DEFAULTS)
    live = audit.audit_cfg(cfg)
    for k, v in audit.DEFAULTS.items():
        if v is not None and k in block:
            assert live[k] == pytest.approx(v), k


# --------------------------------------------------------------------------- 1 stale_portfolio
def test_stale_portfolio_pass_when_live_equals_snapshot(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "stale_portfolio") == []


def test_stale_portfolio_shares_changed_is_high_new_run(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    shutil.copyfile(FX / "live_portfolio_changed.yaml", live)
    (f,) = of(go(run, live), "stale_portfolio")
    assert f["severity"] == "high" and f["section"] == "run" and f["ticker"] is None
    assert f["fix"]["type"] == "new_run" and f["fix"]["tickers"] == ["AAA"]
    assert "AAA: shares 10 in the run, 6 now" in f["evidence"]
    assert "sha256" in f["evidence"]


def test_stale_portfolio_cost_basis_new_and_removed_tickers(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    doc = yaml.safe_load(live.read_text(encoding="utf-8"))
    doc["holdings"][1]["cost_basis"] = 250
    doc["holdings"] = [h for h in doc["holdings"] if h["ticker"] != "FND"] + [
        {"ticker": "NEW", "asset_type": "stock", "shares": 2}]
    live.write_text(yaml.safe_dump(doc), encoding="utf-8")
    (f,) = of(go(run, live), "stale_portfolio")
    assert "BBB: average cost 190 in the run, 250 now" in f["evidence"]
    assert "FND: in the run" in f["evidence"] and "NEW: new in portfolio.yaml" in f["evidence"]


def test_stale_portfolio_cash_change_only_counts_above_threshold(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    doc = yaml.safe_load(live.read_text(encoding="utf-8"))
    doc["cash_usd"] = 450
    live.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert [f for f in go(run, live)["findings"] if f["severity"] == "high"] == []
    doc["cash_usd"] = 1400
    live.write_text(yaml.safe_dump(doc), encoding="utf-8")
    (f,) = of(go(run, live), "stale_portfolio")
    assert "cash_usd" in f["evidence"] and f["severity"] == "high"


def test_stale_portfolio_metadata_only_edit_is_low(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    live.write_text(live.read_text(encoding="utf-8").replace('as_of: "2026-10-01"', 'as_of: "2026-10-02"'),
                    encoding="utf-8")
    (f,) = of(go(run, live), "stale_portfolio")
    assert f["severity"] == "low" and f["fix"]["type"] == "note_only"
    assert go(run, live)["summary"]["verdict"] == "pass"


def test_stale_portfolio_prices_older_than_three_trading_days(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live, now="2026-10-06"), "stale_portfolio") == []      # Oct 1 -> Oct 6 = 3 trading days
    (f,) = of(go(run, live, now="2026-10-07"), "stale_portfolio")            # 4 trading days
    assert f["severity"] == "high" and f["fix"]["type"] == "new_run" and "2026-10-01" in f["evidence"]


def test_stale_portfolio_without_snapshot_uses_plan_shares_and_sha(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    (run / "inputs" / "portfolio.yaml").unlink()
    assert of(go(run, live), "stale_portfolio") == []                        # same shares, same sha as manifest
    shutil.copyfile(FX / "live_portfolio_changed.yaml", live)
    (f,) = of(go(run, live), "stale_portfolio")
    assert f["severity"] == "high" and "AAA" in f["evidence"]
    live.write_text(live.read_text(encoding="utf-8").replace("shares: 6", "shares: 10"), encoding="utf-8")
    (f,) = of(go(run, live), "stale_portfolio")                              # sha differs, nothing comparable
    assert f["severity"] == "high" and "inputs/portfolio.yaml missing" in f["evidence"]


def test_portfolio_diffs_pure() -> None:
    a = {"holdings": [{"ticker": "x", "shares": 5, "cost_basis": 10}, {"ticker": "Y", "shares": 1.0}]}
    assert audit.portfolio_diffs(a, a) == []
    b = {"holdings": [{"ticker": "X", "shares": 5.0000000001, "cost_basis": 10.001}, {"ticker": "Y", "shares": 2}]}
    d = audit.portfolio_diffs(a, b)
    assert d == ["Y: shares 1 in the run, 2 now"]
    assert audit.portfolio_diffs(None, None) == []


# --------------------------------------------------------------------------- 2 action_vs_trade
def _bbb_trim_without_trade(run: Path, *, effective: str | None = None) -> None:
    edit(run, "phase4/cio_final.json", lambda d: d["final_actions"][1].update(action="TRIM"))

    def plan(d: dict) -> None:
        p = plan_pos(d, "BBB")
        p.update(action="TRIM", dropped_reason="rounds_to_zero_shares")
        p["trade"]["proposed_shares"] = 0.4
        if effective:
            p["effective_action"] = effective
    edit(run, "phase4/portfolio_plan.json", plan)


def test_action_vs_trade_pass_when_every_action_has_a_trade(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "action_vs_trade") == []


def test_action_vs_trade_dropped_trade_is_medium_note_only(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    _bbb_trim_without_trade(run)
    (f,) = of(go(run, live), "action_vs_trade")
    assert f["severity"] == "medium" and f["ticker"] == "BBB" and f["section"] == "7"
    assert f["fix"]["type"] == "note_only"
    assert "rounds down to zero whole shares" in f["message"] and "0.40 share(s)" in f["message"]
    assert "dropped_reason=rounds_to_zero_shares" in f["evidence"]


def test_action_vs_trade_effective_action_means_rerender(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    _bbb_trim_without_trade(run, effective="HOLD")
    (f,) = of(go(run, live), "action_vs_trade")
    assert f["fix"]["type"] == "rerender" and "effective_action=HOLD" in f["evidence"]


def test_action_vs_trade_buy_without_position_and_non_trade_actions_ignored(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/cio_final.json", lambda d: d["final_actions"][3].update(action="BUY"))
    (f,) = of(go(run, live), "action_vs_trade", "CCC")
    assert "not bought" in f["message"] and "the plan has no position for it" in f["evidence"]
    edit(run, "phase4/cio_final.json", lambda d: d["final_actions"][3].update(action="PASS"))
    assert of(go(run, live), "action_vs_trade") == []


# --------------------------------------------------------------------------- 3 trim_became_full_exit
def _aaa_full_exit(d: dict) -> None:
    p = plan_pos(d, "AAA")
    p["trade"].update(shares=10, value_usd=1000.0)
    p["full_exit"] = True
    p["weight_tgt_pct"] = 0.0


def test_trim_became_full_exit_pass_for_partial_trim(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "trim_became_full_exit") == []


def test_trim_became_full_exit_fails_high_and_asks_for_replan(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", _aaa_full_exit)
    (f,) = of(go(run, live), "trim_became_full_exit")
    assert f["severity"] == "high" and f["ticker"] == "AAA" and f["fix"]["type"] == "replan"
    assert "SELL 10 share(s) of 10 held (full exit" in f["evidence"] and "partial trim" in f["fix"]["feedback"]


def test_trim_full_exit_is_computed_when_exit_kind_and_flag_are_absent(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)

    def plan(d: dict) -> None:
        _aaa_full_exit(d)
        del plan_pos(d, "AAA")["full_exit"]
    edit(run, "phase4/portfolio_plan.json", plan)
    assert of(go(run, live), "trim_became_full_exit", "AAA")


def test_trim_exit_kind_field_wins_and_sell_verdict_is_fine(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)

    def plan(d: dict) -> None:
        plan_pos(d, "AAA")["exit_kind"] = "full_exit_confirmed"
    edit(run, "phase4/portfolio_plan.json", plan)
    assert of(go(run, live), "trim_became_full_exit", "AAA")
    edit(run, "phase4/portfolio_plan.json", lambda d: plan_pos(d, "AAA").update(exit_kind="partial"))
    assert of(go(run, live), "trim_became_full_exit") == []
    edit(run, "phase4/portfolio_plan.json", lambda d: (_aaa_full_exit(d), plan_pos(d, "AAA").pop("exit_kind"),
                                                       plan_pos(d, "AAA").update(action="SELL")))
    edit(run, "phase4/cio_final.json", lambda d: d["final_actions"][0].update(action="SELL"))
    assert of(go(run, live), "trim_became_full_exit") == []


def test_plan_exit_kind_pure() -> None:
    assert audit.plan_exit_kind(None) == "none"
    assert audit.plan_exit_kind({"trade": {"side": "SELL", "shares": 2}, "shares_cur": 8}) == "partial"
    assert audit.plan_exit_kind({"trade": {"side": "SELL", "shares": 8}, "shares_cur": 8}) == "full"
    assert audit.plan_exit_kind({"trade": {"side": "NONE", "shares": 0}, "shares_cur": 8}) == "none"
    assert audit.plan_exit_kind({"full_exit": True}) == "full"
    assert audit.plan_exit_kind({"exit_kind": "none", "trade": {"shares": 3, "side": "SELL"}, "shares_cur": 3}) == "none"


# --------------------------------------------------------------------------- 4 text_vs_plan_contradiction
PLAN_FULL = {"ticker": "PLTR", "shares_cur": 12, "weight_cur_pct": 6.45, "weight_tgt_pct": 0.0, "full_exit": True,
             "trade": {"side": "SELL", "shares": 12}}
TICKERS = ["PLTR", "QCOM", "BRK-B", "CAT"]


def claims(text: str, default: str | None = None) -> list[tuple[str, str]]:
    return [(c["ticker"], c["kind"]) for c in audit.find_text_claims(text, TICKERS, default)]


def test_find_text_claims_exit_phrases() -> None:
    assert claims("PLTR and QCOM TRIM verdicts size to full exits in the preview.") == [("PLTR", "exit"), ("QCOM", "exit")]
    assert claims("Exit follows the sector limit. Keep the TRIM verdict, flag as full exit.", "PLTR") == [
        ("PLTR", "exit"), ("PLTR", "exit")]
    assert claims("Exits of PLTR and QCOM realise estimated gains.") == [("PLTR", "exit"), ("QCOM", "exit")]
    assert claims("PLTR will be sold out entirely.") == [("PLTR", "exit")]
    assert claims("The exit multiple for PLTR is 15x.") == []                   # valuation jargon
    assert claims("CAT might become a full exit if results disappoint.") == []  # hypothetical
    assert claims("PLTR is not a full exit.") == [("PLTR", "stay")]             # negated exit = stay claim
    assert claims("No full exit is planned for PLTR.") == []


def test_find_text_claims_stay_phrases() -> None:
    t = "The stock stays in the portfolio; only the sector excess is reduced."
    assert claims(t, "PLTR") == [("PLTR", "stay")]
    assert claims("The position is only reduced, not exited.", "QCOM") == [("QCOM", "stay")]
    assert claims("QCOM remains in the portfolio after a partial trim.") == [("QCOM", "stay")]
    assert claims("PLTR no longer stays in the portfolio.") == []
    assert claims("The PLTR exit exceeds a partial trim and is flagged.") == [("PLTR", "exit")]


def test_find_text_claims_attaches_to_the_right_ticker() -> None:
    assert claims("After the PLTR and QCOM exits the remaining excess is held through SPMO and CAT.") == [
        ("PLTR", "exit"), ("QCOM", "exit")]
    assert claims("Exit PLTR; trim CAT.") == [("PLTR", "exit")]
    assert claims("PLTR (sell 10 shares) and QCOM (sell 9) bring tech down.") == [("PLTR", "shares")]
    assert claims("TRIM sells 2 shares of BRK-B.") == [("BRK-B", "shares")]
    # a sentence that names no ticker uses the unit's default ticker, one that names another does not
    assert claims("It stays in the portfolio.", "CAT") == [("CAT", "stay")]
    assert claims("QCOM is sold elsewhere; it stays in the portfolio.", "CAT") == []


def test_find_text_claims_share_and_percent_values() -> None:
    (c,) = audit.find_text_claims("PLTR: SELL 12 shares, est tax 135.", TICKERS)
    assert c["kind"] == "shares" and c["value"] == 12
    (c,) = audit.find_text_claims("PLTR trim by 33% of the position.", TICKERS)
    assert c["kind"] == "pct" and c["value"] == 33
    assert audit.find_text_claims("Trimming PLTR and QCOM brings the preview to 35.64%.", TICKERS) == []


def _set_text(run: Path, rel: str, fn: Callable[[Any], Any]) -> None:
    edit(run, rel, fn)


def test_text_pass_when_text_matches_plan(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "text_vs_plan_contradiction") == []


def test_text_stays_in_portfolio_but_plan_exits_fully(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", _aaa_full_exit)
    doc = go(run, live)
    (f,) = of(doc, "text_vs_plan_contradiction", "AAA")
    assert f["severity"] == "high" and f["fix"]["type"] == "rerun_agents" and "sector_head" in f["fix"]["roles"]
    assert "rest stays in the portfolio" in f["evidence"] and "SELL 10 share(s) of 10 held" in f["fix"]["feedback"]
    # the CIO line "sells 3 shares" now disagrees with the plan's 10 shares as well
    assert set(f["fix"]["roles"]) == {"cio", "sector_head"} and f["section"] == "2"
    assert "quotes 3 shares" in f["evidence"]


def test_text_says_exit_but_plan_is_partial(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/cio_final.json",
         lambda d: d["executive_summary"].append("AAA is a full exit in the preview and needs confirmation."))
    (f,) = of(go(run, live), "text_vs_plan_contradiction", "AAA")
    assert f["severity"] == "high" and f["fix"]["roles"] == ["cio"] and f["section"] == "2"
    assert "speaks of an exit" in f["message"]
    assert "AAA is a full exit" in f["fix"]["feedback"] and "SELL 3 share(s) of 10 held (partial trim" in f["fix"]["feedback"]


def test_text_pltr_qcom_case_exit_text_is_fine_but_trim_became_full_exit_fires(tmp_path: Path) -> None:
    """runs/2026-10-28_1002 says 'PLTR and QCOM TRIM verdicts size to full exits': true to the plan, wrong as a TRIM."""
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", _aaa_full_exit)
    edit(run, "phase4/cio_final.json", lambda d: (d["executive_summary"].__setitem__(
        0, "AAA and BBB TRIM verdicts size to full exits in the preview, which needs explicit confirmation."),
        d["reconciliation"][0].update(findings="AAA TRIM sells 10 shares of the 10 held.")))
    edit(run, "phase2/information_technology_verdict.json", lambda d: d["verdicts"][0].update(
        rationale="The sector limit drives the trim and the business is sound; the preview exits the whole position."))
    memo = run / "phase4" / "memo.md"
    memo.write_text(memo.read_text(encoding="utf-8").replace("the rest stays in the portfolio", "the preview exits it"),
                    encoding="utf-8")
    doc = go(run, live)
    assert of(doc, "text_vs_plan_contradiction", "AAA") == []
    assert of(doc, "trim_became_full_exit", "AAA")
    (f,) = of(doc, "text_vs_plan_contradiction", "BBB") or [None]
    assert f is not None and "exit" in f["message"]          # BBB is a HOLD with no trade: 'full exit' is false for it


def test_text_negated_exit_is_not_flagged(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/cio_final.json",
         lambda d: d["executive_summary"].append("There is no full exit planned for AAA."))
    assert of(go(run, live), "text_vs_plan_contradiction") == []


def test_text_share_count_mismatch_and_percent_mismatch(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/cio_final.json",
         lambda d: d["reconciliation"][0].update(findings="AAA TRIM sells 5 shares of the 10 held."))
    (f,) = of(go(run, live), "text_vs_plan_contradiction", "AAA")
    assert f["severity"] == "high" and "quotes 5 shares" in f["evidence"]
    edit(run, "phase4/cio_final.json", lambda d: d["reconciliation"][0].update(
        findings="AAA TRIM sells 3 shares and cuts the position by 55% in total."))
    (f,) = of(go(run, live), "text_vs_plan_contradiction", "AAA")
    assert f["severity"] == "medium" and "quotes 55%" in f["evidence"]
    edit(run, "phase4/cio_final.json", lambda d: d["reconciliation"][0].update(
        findings="AAA TRIM sells 3 shares and cuts the position by 30% in total."))
    assert of(go(run, live), "text_vs_plan_contradiction") == []


def test_text_only_in_memo_means_rerender(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", _aaa_full_exit)
    edit(run, "phase2/information_technology_verdict.json", lambda d: d["verdicts"][0].update(
        rationale="The sector limit drives the sale of the whole holding."))
    edit(run, "phase4/cio_final.json", lambda d: d["reconciliation"][0].update(
        findings="AAA is sold out completely, 10 shares."))
    (f,) = of(go(run, live), "text_vs_plan_contradiction", "AAA")
    assert f["fix"]["type"] == "rerender" and f["fix"]["roles"] == [] and f["section"] == "5"
    assert "memo.md" in f["evidence"]


# --------------------------------------------------------------------------- 5 / 6 valuation
def _val_ctx(price: float, fv: float | None, **kw: Any) -> audit.Ctx:
    return mk_ctx(aggregates={(2, "ZZZ"): agg(price, fv, **kw)})


@pytest.mark.parametrize("fv,expected", [(100, None), (61, None), (59, "medium"), (40, "medium"), (35.1, "medium"),
                                          (34, "high"), (13, "high"), (159, None), (161, "medium"),
                                          (249, "medium"), (251, "high")])
def test_valuation_outlier_thresholds(fv: float, expected: str | None) -> None:
    out = audit.check_valuation_outliers(_val_ctx(100.0, float(fv)))
    assert [f["severity"] for f in out] == ([expected] if expected else [])


def test_valuation_outlier_message_and_fix() -> None:
    (f,) = audit.check_valuation_outliers(_val_ctx(198.84, 26.75))
    assert f["code"] == "valuation_outlier" and f["severity"] == "high" and f["ticker"] == "ZZZ"
    assert "worth 13% of its price" in f["message"] and "verify against market multiples" in f["message"]
    assert f["fix"]["type"] == "rerun_agents" and f["fix"]["roles"] == ["fundamental"] and f["fix"]["tickers"] == ["ZZZ"]
    fb = f["fix"]["feedback"]
    for needle in ("P/E", "consensus", "net-debt", "captive", "cyclical peak", "model or", "limitation of the model"):
        assert needle in fb or needle == "model or", needle


def test_valuation_outlier_candidates_funds_and_not_meaningful_are_handled() -> None:
    ctx = mk_ctx(aggregates={(3, "CND"): agg(100, 20, kind="candidate"),
                             (2, "FUN"): agg(100, 20, asset_type="fund", sector_class="fund"),
                             (2, "NEG"): agg(100, -5), (2, "NUL"): agg(100, None),
                             (2, "NMF"): agg(100, 20)},
                 reports={(2, "fundamental", "NMF"): {"extensions": {"valuation": {"not_meaningful": True}}}})
    out = audit.check_valuation_outliers(ctx)
    assert [(f["ticker"], f["section"]) for f in out] == [("CND", "6")]


def test_valuation_outlier_uses_report_fair_value_when_aggregate_lacks_it() -> None:
    ctx = mk_ctx(aggregates={(2, "ZZZ"): agg(100, None)},
                 reports={(2, "fundamental", "ZZZ"): {"extensions": {"valuation": {"fair_value_base": 30}}}})
    assert [f["ticker"] for f in audit.check_valuation_outliers(ctx)] == ["ZZZ"]


def test_valuation_outlier_clean_run_passes_and_flawed_fails(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "valuation_outlier") == []
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["AAA"]["rr"].update(fv_base=50.0))
    (f,) = of(go(run, live), "valuation_outlier")
    assert f["ticker"] == "AAA" and f["severity"] == "medium" and "reverse-DCF implied growth 8.0%" in f["evidence"]
    assert "committee action TRIM" in f["evidence"]


def _bias_ctx(n_below: int, n_above: int) -> audit.Ctx:
    aggs = {}
    for i in range(n_below):
        aggs[(2, f"B{i}")] = agg(100, 80)
    for i in range(n_above):
        aggs[(2, f"A{i}")] = agg(100, 120)
    return mk_ctx(aggregates=aggs)


def test_systematic_bias_fires_at_75_percent_with_six_or_more() -> None:
    (f,) = audit.check_systematic_bias(_bias_ctx(6, 2))
    assert f["code"] == "systematic_valuation_bias" and f["severity"] == "high" and f["ticker"] is None
    assert f["section"] == "run" and f["fix"]["type"] == "note_only"
    assert "6 of the 8 stocks" in f["message"] and "too harsh" in f["message"]
    assert audit.check_systematic_bias(_bias_ctx(5, 3)) == []       # 62.5%
    assert audit.check_systematic_bias(_bias_ctx(5, 0)) == []       # all below but n < 6
    assert len(audit.check_systematic_bias(_bias_ctx(6, 0))) == 1


def test_systematic_bias_ignores_funds_and_dedups_light_aggregates() -> None:
    aggs = {(2, f"B{i}"): agg(100, 80) for i in range(5)}
    aggs[(2, "FUN")] = agg(100, 80, asset_type="fund", sector_class="fund")
    aggs[(3, "B0")] = agg(100, 80, kind="candidate", light=True)
    assert audit.check_systematic_bias(mk_ctx(aggregates=aggs)) == []


# --------------------------------------------------------------------------- 7 captive / leverage
@pytest.mark.parametrize("code", ["captive_finance_debt_excluded", "captive_finance_override", "possible_captive_finance"])
def test_captive_flag_in_valuation_or_fundamentals_is_medium(code: str) -> None:
    ctx = mk_ctx(valuation={"CAT": {"flags": [{"code": code, "severity": "low", "detail": "net debt 64 per share"}]}})
    (f,) = audit.check_captive_or_leverage(ctx)
    assert f["severity"] == "medium" and f["ticker"] == "CAT" and f["fix"]["roles"] == ["fundamental"]
    assert code in f["evidence"] and "10-K" in f["fix"]["feedback"]
    ctx = mk_ctx(fundamentals={"CAT": {"flags": [{"code": code, "severity": "low", "detail": "x"}]}})
    assert len(audit.check_captive_or_leverage(ctx)) == 1


def test_captive_absence_and_other_flags_are_fine() -> None:
    ctx = mk_ctx(valuation={"CAT": {"flags": [{"code": "mixed_instants", "severity": "medium", "detail": "x"}]}},
                 fundamentals={"CAT": {"flags": []}})
    assert audit.check_captive_or_leverage(ctx) == []
    assert audit.check_captive_or_leverage(mk_ctx()) == []


def test_captive_text_mention_in_a_report_counts_unless_negated() -> None:
    rep = {"risks": [{"point": "Net debt likely includes captive-finance debt netted against industrial EBIT."}]}
    ctx = mk_ctx(reports={(2, "devils_advocate", "CAT"): rep})
    (f,) = audit.check_captive_or_leverage(ctx)
    assert "devils_advocate report" in f["evidence"]
    neg = {"risks": [{"point": "There is no captive finance arm to adjust for."}]}
    assert audit.check_captive_or_leverage(mk_ctx(reports={(2, "fundamental", "X"): neg})) == []


def test_captive_flag_end_to_end(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "captive_or_leverage_flag") == []
    edit(run, "phase2/data/fundamentals_AAA.json", lambda d: d["flags"].append(
        {"code": "possible_captive_finance", "severity": "low", "detail": "ratio 0.4"}))
    (f,) = of(go(run, live), "captive_or_leverage_flag")
    assert f["ticker"] == "AAA"


# --------------------------------------------------------------------------- 8 plan_does_not_cure_breach
def test_breach_pass_when_every_sector_is_under_the_cap(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "plan_does_not_cure_breach") == []


def test_breach_computed_from_plan_sectors_with_fund_drivers(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", lambda d: d["sectors"][0].update(post_lookthrough_pct=36.5, breach=True))
    (f,) = of(go(run, live), "plan_does_not_cure_breach")
    assert f["severity"] == "medium" and f["section"] == "4" and f["fix"]["type"] == "note_only"
    assert "36.50%" in f["message"] and "35%" in f["message"] and "Stock sales cannot fix it" in f["message"]
    assert "FND 24.0 pp" in f["message"]
    assert "new contributions" in f["fix"]["feedback"]


def test_breach_uses_plan_residual_breaches_when_present(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", lambda d: d.update(
        residual_breaches=[{"sector": "health_care", "post_lookthrough_pct": 38.0, "max_pct": 35.0}]))
    (f,) = of(go(run, live), "plan_does_not_cure_breach")
    assert "Health Care is still 38.00%" in f["message"]


def test_breach_ignores_cash_and_unclassified(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", lambda d: d["sectors"][2].update(post_lookthrough_pct=60.0, max_pct=None))
    assert of(go(run, live), "plan_does_not_cure_breach") == []


# --------------------------------------------------------------------------- 9 limit_inconsistency
def test_quoted_limits_extracts_sector_limit_numbers() -> None:
    q = audit.quoted_limits("The sector is 46.46% of the portfolio, above the 33.00% sector limit.", "information_technology")
    assert [(s, v) for s, v, _ in q] == [("information_technology", 33.0)]
    q = audit.quoted_limits("Information Technology stays 3.64 pp above its limit (32%) after trimming.")
    assert [(s, v) for s, v, _ in q] == [("information_technology", 32.0)]
    q = audit.quoted_limits("reduced 6.45 pp for the information_technology sector limit 32%")
    assert [(s, v) for s, v, _ in q] == [("information_technology", 32.0)]
    assert audit.quoted_limits("The 2.91% position is far below its 10.00% cap; limit, while 5% matters.", "financials") == []
    assert audit.quoted_limits("single-holding limit of 10%", None) == []                       # no sector named
    assert audit.quoted_limits("above the 10.00% position limit", "financials") == []           # not a sector limit


def test_limit_inconsistency_pass_and_fail(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "limit_inconsistency") == []
    memo = run / "phase4" / "memo.md"
    memo.write_text(memo.read_text(encoding="utf-8").replace("32.00% sector limit", "33.00% sector limit"), encoding="utf-8")
    (f,) = of(go(run, live), "limit_inconsistency")
    assert f["severity"] == "medium" and f["fix"]["type"] == "rerender" and f["ticker"] is None
    assert "32% and 33%" in f["message"] and "Information Technology" in f["message"]
    assert "memo section 5 Why (AAA)" in f["evidence"] and "plan note 1" in f["evidence"]


def test_limit_inconsistency_counts_the_plans_own_sector_limit(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", lambda d: d.update(sector_limit={"information_technology": 34.0}))
    (f,) = of(go(run, live), "limit_inconsistency")
    assert "plan sector_limit" in f["evidence"] and "34% and 32%" in f["message"].replace("32% and 34%", "34% and 32%")


def test_limit_inconsistency_same_number_everywhere_is_fine(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/portfolio_plan.json", lambda d: d.update(sector_limit={"information_technology": 32.0}))
    assert of(go(run, live), "limit_inconsistency") == []


# --------------------------------------------------------------------------- 10 weak_why
@pytest.mark.parametrize("text,bad", [
    ("Hold: conviction 5.0/10 is neither strong enough to add nor weak enough to trim, and no limit is breached.", False),
    ("Hold.", True),
    ("Trim because the rule R4.trim.limit_breach says so and the sector is above its limit by quite a lot of points here.", True),
    ("The rule fired because the position is far above the cap and the committee wants it lower than it is today now.", True),
    ("", True),
])
def test_weak_why_reason(text: str, bad: bool) -> None:
    assert bool(audit.weak_why_reason(text)) is bad


def test_weak_why_ignores_trailing_flag_note_and_counts_words() -> None:
    base = " ".join(["word"] * 18)
    assert audit.weak_why_reason(base + " [CIO override; sector-head deviation]") is None
    assert "only 17 words" in audit.weak_why_reason(" ".join(["word"] * 17) + " [sector-head deviation]")
    assert audit.weak_why_reason(" ".join(["word"] * 10), min_words=10) is None


def test_weak_why_clean_run_passes_and_flawed_flags(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "weak_why") == []
    memo = run / "phase4" / "memo.md"
    memo.write_text(memo.read_text(encoding="utf-8").replace(
        "Hold: conviction 5.2/10 is neither strong enough to add nor weak enough to trim, and no limit is breached.",
        "Hold, R4.trim fired."), encoding="utf-8")
    (f,) = of(go(run, live), "weak_why")
    assert f["severity"] == "low" and f["ticker"] == "BBB" and f["fix"]["roles"] == ["sector_head"]
    assert "R4.trim" in f["evidence"] and "fired" in f["evidence"]


def test_weak_why_falls_back_to_the_verdict_rationale_without_a_memo(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    (run / "phase4" / "memo.md").unlink()
    out = of(go(run, live), "weak_why")
    assert {f["ticker"] for f in out} == {"BBB", "FND"}                     # their rationales are short
    assert all("sector verdict rationale" in f["evidence"] for f in out)


# --------------------------------------------------------------------------- 11 flags / failed / evidence
def test_flags_failed_and_low_evidence_pass_on_clean_run(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    doc = go(run, live)
    for code in ("unresolved_high_flags", "failed_reports", "low_evidence"):
        assert of(doc, code) == []


def test_unresolved_high_flag(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    flag = {"flag_id": "osint:F1", "role": "osint", "id": "F1", "severity": "high", "must_address": True,
            "flag": "Regulator opened an investigation."}
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["BBB"]["flags"].update(unresolved=[flag]))
    (f,) = of(go(run, live), "unresolved_high_flags")
    assert f["severity"] == "medium" and f["ticker"] == "BBB" and f["section"] == "10"
    assert set(f["fix"]["roles"]) == {"devils_advocate", "sector_head"} and "osint:F1" in f["fix"]["feedback"]
    flag["severity"] = "medium"
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["BBB"]["flags"].update(unresolved=[flag]))
    assert of(go(run, live), "unresolved_high_flags") == []


def test_failed_report_names_the_failed_role(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["BBB"]["specialists"][3].update(status="failed"))
    (f,) = of(go(run, live), "failed_reports")
    assert f["fix"]["roles"] == ["operator_ceo"] and "operator_ceo" in f["message"]
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["BBB"]["da"].update(status="failed"))
    (f,) = of(go(run, live), "failed_reports")
    assert set(f["fix"]["roles"]) == {"operator_ceo", "devils_advocate"}


def test_low_evidence_uses_min_evidence_and_skips_failed_stubs(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["AAA"]["specialists"][1].update(evidence=4.9))
    (f,) = of(go(run, live), "low_evidence")
    assert f["ticker"] == "AAA" and f["fix"]["roles"] == ["technical"] and "technical 4.9/10" in f["evidence"]
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["AAA"]["specialists"][1].update(evidence=5.0))
    assert of(go(run, live), "low_evidence") == []
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["AAA"]["specialists"][1].update(evidence=1.0, status="failed"))
    doc = go(run, live)
    assert of(doc, "low_evidence") == [] and of(doc, "failed_reports")


def test_low_evidence_threshold_follows_the_run_config(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["AAA"]["specialists"][1].update(evidence=6.0))
    assert of(go(run, live), "low_evidence") == []
    (run / "inputs" / "committee.yaml").write_text("actions:\n  min_evidence: 7\n", encoding="utf-8")
    # the live repo config has no audit.min_evidence override, so the run snapshot's actions.min_evidence applies
    assert of(go(run, live), "low_evidence", "AAA")


# --------------------------------------------------------------------------- 12 thin_review
def test_thin_review_pass_and_funds_without_da_are_fine(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "thin_review") == []


def test_thin_review_missing_devils_advocate(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase2/aggregate.json", lambda d: d["tickers"]["BBB"]["da"].update(status="not_run", verdict=None))
    (f,) = of(go(run, live), "thin_review")
    assert f["severity"] == "medium" and f["ticker"] == "BBB"
    assert set(f["fix"]["roles"]) == {"sector_head", "devils_advocate"} and "Devil's Advocate" in f["message"]


def test_thin_review_lenses_disagree_with_the_action(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)

    def bullish(d: dict) -> None:
        for s in d["tickers"]["AAA"]["specialists"][:2]:
            s["stance"] = "bullish"
    edit(run, "phase2/aggregate.json", bullish)
    (f,) = of(go(run, live), "thin_review", "AAA")
    assert "both the fundamental and technical reviews are bullish, yet the action is TRIM" in f["message"]

    def bearish_add(d: dict) -> None:
        for s in d["tickers"]["AAA"]["specialists"][:2]:
            s["stance"] = "bearish"
    edit(run, "phase2/aggregate.json", bearish_add)
    assert of(go(run, live), "thin_review") == []                             # bearish + TRIM is consistent
    edit(run, "phase4/cio_final.json", lambda d: d["final_actions"][0].update(action="ADD"))
    (f,) = of(go(run, live), "thin_review", "AAA")
    assert "both the fundamental and technical reviews are bearish, yet the action is ADD" in f["message"]


# --------------------------------------------------------------------------- 13 pending_event_risk
D = date(2026, 10, 28)


def test_find_pending_events_dates_and_windows() -> None:
    hit = audit.find_pending_events("Third-quarter results due October 29.", D, 3)
    assert [d for d, _ in hit] == [date(2026, 10, 29)]
    assert audit.find_pending_events("Q3 results are due Oct. 31, 2026.", D, 3)[0][0] == date(2026, 10, 31)
    assert audit.find_pending_events("Earnings on 30 October.", D, 3)[0][0] == date(2026, 10, 30)
    assert audit.find_pending_events("Results due 2026-10-29 after the close.", D, 3)[0][0] == date(2026, 10, 29)
    assert audit.find_pending_events("The ruling is expected tomorrow.", D, 3)[0][0] == date(2026, 10, 29)
    assert audit.find_pending_events("Results due November 5.", D, 3) == []                       # outside the window
    assert audit.find_pending_events("Results due October 20.", D, 3) == []                       # already past
    assert audit.find_pending_events("The company reported results on October 28.", D, 3) == []   # past tense
    assert audit.find_pending_events("Dividend is due October 29.", D, 3) == []                    # not an event keyword
    assert audit.find_pending_events("Results due in early November.", D, 3) == []                 # no parseable date
    assert audit.find_pending_events("Results due January 2.", date(2026, 12, 30), 3)[0][0] == date(2027, 1, 2)


def test_pending_event_pass_and_fail_end_to_end(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "pending_event_risk") == []
    edit(run, "phase2/reports/fundamental_BBB.json", lambda d: d["risks"].append(
        {"point": "Third-quarter results are due October 3 and could move the stock.", "severity": "medium"}))
    (f,) = of(go(run, live), "pending_event_risk")
    assert f["severity"] == "low" and f["ticker"] == "BBB" and f["fix"]["type"] == "note_only"
    assert "2026-10-03" in f["message"] and "consider waiting" in f["message"].lower()


def test_pending_event_in_cio_reconciliation_is_assigned_to_the_topic_ticker(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    edit(run, "phase4/cio_final.json", lambda d: d["reconciliation"].append(
        {"topic": "BBB earnings pending", "phase1_position": "-", "findings": "Results due October 3.",
         "resolution": "Wait."}))
    (f,) = of(go(run, live), "pending_event_risk")
    assert f["ticker"] == "BBB" and "cio reconciliation" in f["evidence"]


# --------------------------------------------------------------------------- 14 cash_drag
def test_cash_drag_pass_and_fail(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)
    assert of(go(run, live), "cash_drag") == []
    edit(run, "phase4/portfolio_plan.json", lambda d: d["cash"].update(post_pct=10.0))
    assert of(go(run, live), "cash_drag") == []                                 # exactly floor + 5: not "far above"
    edit(run, "phase4/portfolio_plan.json", lambda d: d["cash"].update(post_pct=13.8, post_usd=1380.0))
    (f,) = of(go(run, live), "cash_drag")
    assert f["severity"] == "low" and f["fix"]["type"] == "note_only" and f["section"] == "7"
    assert "13.8%" in f["message"] and "floor 5%" in f["message"]


def test_cash_drag_not_flagged_when_the_plan_buys(tmp_path: Path) -> None:
    run, live = fresh(tmp_path)

    def plan(d: dict) -> None:
        d["cash"].update(post_pct=20.0)
        plan_pos(d, "BBB")["trade"] = {"side": "BUY", "shares": 1}
    edit(run, "phase4/portfolio_plan.json", plan)
    assert of(go(run, live), "cash_drag") == []


# --------------------------------------------------------------------------- memo parsing
def test_memo_section_and_table_rows() -> None:
    text = "## 4. A\nx\n## 5. Holding Verdicts\n\n| Ticker | Why |\n| --- | --- |\n| brk.b | a \\| b |\n| CAT | c |\n\n## 6. B\n"
    sec = audit.memo_section(text, 5)
    assert "## 6" not in sec and "Holding Verdicts" not in sec
    rows = audit.memo_table_rows(sec)
    assert rows["BRK-B"]["Why"] == "a | b" and rows["CAT"]["Why"] == "c"
    assert audit.memo_section(text, 9) == "" and audit.memo_table_rows("") == {}


# --------------------------------------------------------------------------- CLI
def test_cli_summary_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, live = fresh(tmp_path, "flawed")
    assert audit.main(["--run-dir", str(run), "--portfolio", str(live), "--as-of", NOW, "--no-write"]) == 0
    out = capsys.readouterr().out
    assert "NEEDS_REWORK" in out and "F01 HIGH" in out and not (run / "phase4" / "audit.json").exists()
    assert audit.main(["--run-dir", str(run), "--portfolio", str(live), "--as-of", NOW, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"]["verdict"] == "needs_rework"
    assert json.loads((run / "phase4" / "audit.json").read_text(encoding="utf-8")) == doc


def test_cli_missing_run_dir_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert audit.main(["--run-dir", str(tmp_path / "nope")]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_cli_by_run_id_uses_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    shutil.copytree(FX / "clean", runs / "clean")
    monkeypatch.setenv("AGINV_RUNS_DIR", str(runs))
    assert audit.main(["--run", "clean", "--portfolio", str(FX / "live_portfolio_same.yaml"), "--no-write"]) == 0
    assert "PASS" in capsys.readouterr().out


# --------------------------------------------------------------------------- real run (acceptance)
@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="runs/2026-10-28_1002 not present")
def test_acceptance_on_real_run_2026_10_28_1002(tmp_path: Path) -> None:
    before = sorted(p.relative_to(REAL_RUN).as_posix() for p in REAL_RUN.rglob("*") if p.is_file())
    # the live portfolio "since sold": BRK-B 8 -> 6 shares, everything else as in the run
    live = tmp_path / "portfolio.yaml"
    live.write_text((REAL_RUN / "inputs" / "portfolio.yaml").read_text(encoding="utf-8").replace(
        "ticker: \"BRK-B\"\n    asset_type: stock\n    shares: 8", "ticker: \"BRK-B\"\n    asset_type: stock\n    shares: 6"),
        encoding="utf-8")
    doc = audit.run(REAL_RUN, now="2026-10-29", portfolio_path=live, write=False)
    assert sorted(p.relative_to(REAL_RUN).as_posix() for p in REAL_RUN.rglob("*") if p.is_file()) == before
    schema = json.loads((REPO_ROOT / "schemas" / "audit.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(doc))
    by = {(f["code"], f["ticker"]) for f in doc["findings"]}
    assert doc["summary"]["verdict"] == "needs_rework"
    reopened = bool(json.loads((REAL_RUN / "manifest.json").read_text(encoding="utf-8")).get("chair_rounds"))
    if not reopened:
        # PLTR / QCOM: a TRIM verdict the plan sizes as a full exit, and text that says they stay in the portfolio
        for t in ("PLTR", "QCOM"):
            assert ("trim_became_full_exit", t) in by
        assert ("text_vs_plan_contradiction", "PLTR") in by
    else:   # the chair re-ran the CIO against the partial-trim plan: those findings must be gone
        assert not [c for c in by if c[0] in ("trim_became_full_exit", "text_vs_plan_contradiction")], by
    assert ("valuation_outlier", "CAT") in by and ("valuation_outlier", "PLTR") in by
    assert ("systematic_valuation_bias", None) in by
    stale = [f for f in doc["findings"] if f["code"] == "stale_portfolio"]
    assert stale and stale[0]["severity"] == "high" and "BRK-B: shares 8 in the run, 6 now" in stale[0]["evidence"]
    assert ("action_vs_trade", "CAT") in by
    pltr = next(f for f in doc["findings"] if f["code"] == "valuation_outlier" and f["ticker"] == "PLTR")
    assert pltr["severity"] == "high" and pltr["fix"]["roles"] == ["fundamental"]
    # same portfolio as the run -> no stale finding
    same = audit.run(REAL_RUN, now="2026-10-29", portfolio_path=REAL_RUN / "inputs" / "portfolio.yaml", write=False)
    assert not [f for f in same["findings"] if f["code"] == "stale_portfolio"]


def test_audit_reads_the_newest_cio_variant(tmp_path):
    """A chair re-run writes cio_final.r<n>.json; the audit must judge that one, not the superseded original."""
    p4 = tmp_path / "phase4"
    p4.mkdir()
    (p4 / "cio_final.json").write_text('{"v": "old"}', encoding="utf-8")
    (p4 / "cio_final.r3.json").write_text('{"v": "new"}', encoding="utf-8")
    assert audit._read(audit._pick_variants(p4)["cio_final"]) == {"v": "new"}


@pytest.mark.parametrize("text", [
    "Plan trims are partial: PLTR exit_kind partial (trade_pct_of_position 0.5).",
    "Earlier wording that PLTR and QCOM size to full exits is withdrawn; the preview records partial trims.",
    "The trims are partial, never exits, for PLTR.",
])
def test_exit_claim_ignores_field_names_negations_and_withdrawn_wording(text):
    claims = audit.find_text_claims(text, ["PLTR", "QCOM"])
    assert not [c for c in claims if c["kind"] == "exit"], claims
