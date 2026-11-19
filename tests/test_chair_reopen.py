"""Chair review machinery (DESIGN §10): ``committee.py reopen`` / ``audit`` / ``chair-save``.

The heavy tests drive the REAL pipeline offline: one pristine ``dry-run`` per session is cloned
(paths in the copied run are rewritten) for every test, a phase-2 order is reopened with feedback,
and the normal ``next`` loop plays the agents again from the dry-run fixtures until ``done``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT
from scripts import committee, common, ledger

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "dryrun"
KO_FUND = "p2-a-fundamental-KO"
_PRISTINE: dict[str, Any] = {}
TEXT_SUFFIXES = {".json", ".md", ".html", ".csv", ".yaml", ".txt", ".log"}


# =====================================================================================
# helpers
# =====================================================================================


@pytest.fixture
def pristine(tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture) -> dict:
    """One finished dry run per session (≈15 s); tests clone it."""
    if "res" not in _PRISTINE:
        root = tmp_path_factory.mktemp("chair_pristine")
        capsys.readouterr()
        rc = committee.main(["dry-run", "--runs-dir", str(root / "runs"), "--cache-dir", str(root / "cache")])
        res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0 and res["type"] == "done", res
        _PRISTINE["res"] = res
        _PRISTINE["run_dir"] = Path(res["run_dir"])
    return _PRISTINE


@pytest.fixture
def run(pristine: dict, tmp_path: Path) -> Path:
    """A private copy of the finished dry run (absolute paths rewritten to the copy)."""
    src: Path = pristine["run_dir"]
    dst = tmp_path / "runs" / src.name
    shutil.copytree(src, dst)
    old, new = src.as_posix().encode(), dst.as_posix().encode()
    for f in dst.rglob("*"):
        if f.is_file() and f.suffix in TEXT_SUFFIXES:
            data = f.read_bytes()
            if old in data:
                f.write_bytes(data.replace(old, new))
    return dst


def cli(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    rc = committee.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return rc, json.loads(out[-1]) if out else {}


def manifest(run: Path) -> dict:
    return committee.load_manifest(run)


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def step_status(run: Path) -> dict[str, str]:
    return {s["name"]: s["status"] for s in manifest(run)["steps"]}


def drive(run: Path, *, limit: int = 200) -> tuple[dict, list[dict]]:
    """Play the normal loop: ``next`` until done; agent batches come from the dry-run fixtures."""
    batches: list[dict] = []
    for _ in range(limit):
        res = committee.run_next(str(run), ignore_circuit_breaker=True)
        if res["type"] == "agents":
            batches.append(res)
            sim = committee.simulate_agents(run, res, FIXTURES)
            assert not sim["missing"], sim
        elif res["type"] != "continue":
            return res, batches
    raise AssertionError("the run did not finish")


def ledger_keys(run: Path) -> list[tuple[str, str, str]]:
    rows = ledger.load_rows(run / "phase4" / "ledger_preview.csv")
    return [(r["run_id"], r["ticker"], r["role"]) for r in rows]


def reopen(capsys: pytest.CaptureFixture, run: Path, *orders: str, feedback: str = "Reconcile the fair value "
           "with the market multiples.\nState the main risk in one sentence.") -> tuple[int, dict]:
    argv = ["reopen", "--run", str(run)]
    for o in orders:
        argv += ["--order", o]
    return cli(capsys, *argv, "--feedback", feedback)


def reopen_and_play(capsys: pytest.CaptureFixture, run: Path, *orders: str, **kw: Any) -> dict:
    """``reopen`` + the chair launching the listed orders (the agents answer from the fixtures)."""
    rc, payload = reopen(capsys, run, *orders, **kw)
    assert rc == 0, payload
    sim = committee.simulate_agents(run, payload, FIXTURES)
    assert not sim["missing"], sim
    return payload


# =====================================================================================
# pure helpers
# =====================================================================================


def test_chair_feedback_lines_prefix_split_and_never_truncate() -> None:
    long = " ".join(["word"] * 400)
    lines = committee.chair_feedback_lines(f"first point\n\n  second   point  \n{long}")
    assert lines[0] == "CHAIR FEEDBACK: first point" and lines[1] == "CHAIR FEEDBACK: second point"
    assert all(l.startswith(committee.CHAIR_PREFIX) and len(l) <= committee.ERROR_TEXT_MAX for l in lines)
    assert len(lines) > 3
    assert " ".join(l[len(committee.CHAIR_PREFIX):].strip() for l in lines[2:]) == long
    assert committee.chair_feedback_lines("  \n ") == []
    chair, changed, validator = committee.split_prior_errors(
        lines[:1] + [committee.inputs_changed_text(["a", "b"]), "$.x: bad"])
    assert chair == ["first point"] and validator == ["$.x: bad"]
    assert changed == ["inputs changed after chair re-run of a, b; re-check your conclusions against the updated reports"]


def test_step_dependents_follow_the_dependency_table() -> None:
    from scripts import _steps

    names = list(_steps.STEP_NAMES)
    assert committee._step_dependents(names, ["p2.wave_a"]) == [
        "p2.wave_b", "p2.aggregate", "p2.sector_heads", "p4.preview", "p4.cio", "p4.finalize"]
    assert committee._step_dependents(names, ["p3.da"]) == [
        "p3.aggregate", "p3.sector_heads", "p4.preview", "p4.cio", "p4.finalize"]
    assert committee._step_dependents(names, ["p4.cio"]) == ["p4.finalize"]
    macro = committee._step_dependents(names, ["p1.macro"])
    assert "p1.cio" in macro and "p3.screen" in macro and "p2.aggregate" in macro
    assert not {"p2.data", "p2.wave_a", "p2.wave_b"} & set(macro)
    # a reopened step downstream of another reopened step is reset too
    assert "p2.sector_heads" in committee._step_dependents(names, ["p2.wave_a", "p2.sector_heads"])
    assert committee._step_dependents(names, ["p2.wave_a", "p3.light"])[0] == "p2.wave_b"
    # unknown step names depend on everything before them
    assert committee._step_dependents(["a", "b", "c"], ["a"]) == ["b", "c"]
    assert committee._step_dependents(["a", "b", "c"], ["b"]) == ["c"]
    # every table entry only references steps that exist
    assert {d for deps in committee.STEP_DEPENDS.values() for d in deps} <= set(names)
    assert set(committee.STEP_DEPENDS) <= set(names)


def test_find_agent_fixture_reuses_the_original_for_retries_and_reopened_attempts(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent_outputs"
    agent_dir.mkdir()
    (agent_dir / "o1.json").write_text("{}", encoding="utf-8")
    rec = {"attempt": 2, "output": str(tmp_path / "x.r2.json"), "base_output": str(tmp_path / "x.json")}
    assert committee._find_agent_fixture(agent_dir, "o1", rec).name == "o1.json"
    rec["reopen_round"] = 1
    assert committee._find_agent_fixture(agent_dir, "o1", rec).name == "o1.json"
    (agent_dir / "o1.chair1.json").write_text("{}", encoding="utf-8")
    assert committee._find_agent_fixture(agent_dir, "o1", rec).name == "o1.chair1.json"
    assert committee._find_agent_fixture(agent_dir, "o1", {"attempt": 1, "output": "o.json"}).name == "o1.json"


def _row(run_id: str, ticker: str, role: str = "cio", action: str = "HOLD") -> dict:
    row = {c: "" for c in ledger.LEDGER_COLUMNS}
    row.update(run_id=run_id, rec_date="2026-10-02", price_date="2026-10-02", ticker=ticker,
               asset_type="stock", sector="financials", kind="holding", role=role, action=action)
    return row


def test_ledger_append_can_replace_the_rows_of_one_run(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    a, b = "2026-10-01_1000", "2026-10-02_1000"
    assert ledger.append([_row(a, "KO"), _row(a, "PG"), _row(b, "KO")], ledger_path=path) == 3
    # plain append dedupes on (run_id, ticker, role): the new action would be silently ignored
    assert ledger.append([_row(b, "KO", action="SELL")], ledger_path=path) == 0
    assert {r["action"] for r in ledger.load_rows(path) if r["run_id"] == b} == {"HOLD"}
    n = ledger.append([_row(b, "KO", action="SELL"), _row(b, "JPM")], ledger_path=path, replace_run_ids=[b])
    assert n == 2
    rows = ledger.load_rows(path)
    assert [(r["run_id"], r["ticker"], r["action"]) for r in rows] == [
        (a, "KO", "HOLD"), (a, "PG", "HOLD"), (b, "KO", "SELL"), (b, "JPM", "HOLD")]
    # replacing with the very same rows leaves exactly one copy of each
    ledger.append([_row(b, "KO", action="SELL"), _row(b, "JPM")], ledger_path=path, replace_run_ids=[b])
    assert len(ledger.load_rows(path)) == 4
    # replacing a run that has no rows is a plain append
    assert ledger.append([_row("2026-10-03_1000", "KO")], ledger_path=path, replace_run_ids=["2026-10-03_1000"]) == 1


# =====================================================================================
# reopen: the plan (no agents involved)
# =====================================================================================


def test_reopen_resets_exactly_the_downstream_steps_and_writes_the_feedback_order(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    before = manifest(run)
    untouched = {p: sha(run / p) for p in (
        "phase1/macro_view.json", "phase1/cio_allocation.json", "phase3/health_care_verdict.json",
        "phase3/aggregate.json", "phase2/reports/devils_advocate_PG.json",
        "phase2/financials_verdict.json", "phase2/funds_verdict.json")}
    original_output = run / "phase2" / "reports" / "fundamental_KO.json"
    assert original_output.is_file()

    rc, payload = reopen(capsys, run, KO_FUND)
    assert rc == 0, payload
    assert payload["type"] == "agents" and payload["reopened"] is True and payload["round"] == 1
    assert [o["order_id"] for o in payload["orders"]] == [KO_FUND]
    assert payload["step"] == "p2.wave_a" and payload["rounds_left"] == 1
    assert payload["invalidated_steps"] == ["p2.wave_b", "p2.aggregate", "p2.sector_heads",
                                            "p4.preview", "p4.cio", "p4.finalize"]

    m = manifest(run)
    assert m["status"] == "reopened" and "finished_at" not in m
    st = step_status(run)
    for name in ("p1.data", "p1.macro", "p1.cio", "p2.data", "p3.screen", "p3.light", "p3.finalists",
                 "p3.full", "p3.da", "p3.aggregate", "p3.sector_heads"):
        assert st[name] == "done", (name, st)
    assert st["p2.wave_a"] == "dispatched"
    for name in ("p2.wave_b", "p2.aggregate", "p2.sector_heads", "p4.preview", "p4.cio", "p4.finalize"):
        assert st[name] == "pending", (name, st)

    # the reopened order: same id / order file, attempt + 1, new output path, original kept
    rec = m["orders"][KO_FUND]
    assert rec["attempt"] == 2 and rec["status"] == "dispatched" and rec["reopened_by"] == "chair"
    assert rec["reopen_round"] == 1 and rec["output"].endswith("fundamental_KO.r2.json")
    assert rec["base_output"] == before["orders"][KO_FUND]["base_output"]
    assert rec["order_path"] == before["orders"][KO_FUND]["order_path"]
    assert rec["attempt_limit"] == 3 and original_output.is_file()
    text = Path(rec["order_path"]).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["attempt"] == 2 and fm["output"] == rec["output"]
    assert fm["prior_errors"] == ["CHAIR FEEDBACK: Reconcile the fair value with the market multiples.",
                                  "CHAIR FEEDBACK: State the main risk in one sentence."]
    assert "## Chair feedback - address every point" in text
    assert "- Reconcile the fair value with the market multiples." in text
    assert "## Retry" not in text           # it was not a validator rejection
    assert committee.schema_errors(fm, "work_order") == []

    # only the touched ticker / sector is invalidated in the per-ticker / per-sector steps
    assert KO_FUND in m["orders"] and "p2-b-devils_advocate-KO" not in m["orders"]
    for kept in ("p2-b-devils_advocate-PG", "p2-b-devils_advocate-JPM", "p2-b-devils_advocate-BRK-B",
                 "p2-sh-financials", "p2-sh-funds"):
        assert m["orders"][kept] == before["orders"][kept], kept
    assert "p2-sh-consumer_staples" not in m["orders"] and "p4-cio" not in m["orders"]
    # archived, not deleted
    sup = run / "_superseded" / "round1"
    assert (sup / "phase4" / "memo.md").is_file() and (sup / "phase4" / "memo.html").is_file()
    assert (sup / "phase2" / "reports" / "devils_advocate_KO.json").is_file()
    assert (sup / "phase2" / "consumer_staples_verdict.json").is_file()
    assert (sup / "phase4" / "cio_final.json").is_file() and (sup / "phase2" / "aggregate.json").is_file()
    assert (sup / "orders" / "p4-cio.md").is_file()
    assert not (run / "phase4" / "memo.md").exists() and not (run / "phase2" / "aggregate.json").exists()
    assert (run / "phase4" / "ledger_preview.csv").is_file()        # shared ledger file stays in place
    for p, digest in untouched.items():
        assert sha(run / p) == digest, p                              # unrelated work is byte-identical

    # bookkeeping
    (entry,) = m["chair_rounds"]
    assert entry["round"] == 1 and entry["orders"] == [KO_FUND] and len(entry["feedback_sha"]) == 64
    assert entry["reason"].startswith("Reconcile the fair value")
    assert entry["invalidated_steps"] == payload["invalidated_steps"] and entry["reopened_steps"] == ["p2.wave_a"]
    assert set(entry["superseded_orders"]) == {"p2-b-devils_advocate-KO", "p2-sh-consumer_staples", "p4-cio"}
    fb_doc = common.read_json(run / entry["feedback_file"])
    assert fb_doc["items"] == [{"order_id": KO_FUND, "feedback": "Reconcile the fair value with the market "
                                                                  "multiples.\nState the main risk in one sentence."}]
    log = (run / "run_log.md").read_text(encoding="utf-8")
    assert "CHAIR REOPEN round 1/2" in log and KO_FUND in log and "State the main risk" in log


def test_status_and_redispatch_show_the_reopened_run(run: Path, capsys: pytest.CaptureFixture) -> None:
    assert reopen(capsys, run, KO_FUND)[0] == 0
    rc, status = cli(capsys, "status", "--run", str(run))
    assert rc == 0 and status["status"] == "reopened" and status["current_step"] == "p2.wave_a"
    assert status["pending_orders"] == [KO_FUND] and status["chair_round"] == 1
    assert status["orders"]["dispatched"] == 1 and "chair reopen in progress" in status["hint"]
    rc, again = cli(capsys, "redispatch", "--run", str(run))
    assert [o["order_id"] for o in again["orders"]] == [KO_FUND] and again["redispatch"] is True
    # re-listing does not count an attempt
    assert manifest(run)["orders"][KO_FUND]["attempt"] == 2


def test_reopen_of_a_phase1_order_resets_every_dependent_step(run: Path, capsys: pytest.CaptureFixture) -> None:
    rc, payload = reopen(capsys, run, "p1-macro")
    assert rc == 0, payload
    st = step_status(run)
    assert st["p1.macro"] == "dispatched"
    for name in ("p1.data", "p2.data", "p2.wave_a", "p2.wave_b"):
        assert st[name] == "done", name
    for name in ("p1.cio", "p2.aggregate", "p2.sector_heads", "p3.screen", "p3.light", "p3.finalists", "p3.full",
                 "p3.da", "p3.aggregate", "p3.sector_heads", "p4.preview", "p4.cio", "p4.finalize"):
        assert st[name] == "pending", name
    m = manifest(run)
    assert "p1-cio" not in m["orders"] and "p3-sh-health_care" not in m["orders"]
    assert m["orders"]["p2-a-fundamental-KO"]["status"] == "done"
    inv = {s["name"]: s.get("invalidated") for s in m["steps"] if s.get("invalidated")}
    assert all(v["keep"] == [] for v in inv.values()), "a global change keeps nothing in the agent steps"
    assert inv["p1.cio"]["text"].startswith("inputs changed after chair re-run of p1-macro; re-check")


def test_reopen_in_phase3_light_resets_the_order_set_changing_steps_without_scoping(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    rc, payload = reopen(capsys, run, "p3-light-fundamental-ABBV")
    assert rc == 0, payload
    assert payload["invalidated_steps"] == ["p3.finalists", "p3.full", "p3.da", "p3.aggregate",
                                            "p3.sector_heads", "p4.preview", "p4.cio", "p4.finalize"]
    m = manifest(run)
    assert step_status(run)["p2.sector_heads"] == "done"      # phase 2 is not affected by a phase 3 reopen
    inv = {s["name"]: s for s in m["steps"]}
    assert inv["p3.full"]["invalidated"]["keep"] == [] and inv["p3.da"]["invalidated"]["keep"] == []
    assert not any(o.startswith("p3-full-") for o in m["orders"])


def test_reopening_a_report_and_a_later_sector_head_carries_the_feedback_through_the_reset(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    rc, payload = cli(capsys, "reopen", "--run", str(run), "--order", KO_FUND, "--order", "p2-sh-financials",
                      "--feedback", "common point")
    assert rc == 0, payload
    assert [o["order_id"] for o in payload["orders"]] == [KO_FUND]      # only the earliest step is launched now
    assert not committee.simulate_agents(run, payload, FIXTURES)["missing"]
    m = manifest(run)
    sh_step = next(s for s in m["steps"] if s["name"] == "p2.sector_heads")
    assert sh_step["status"] == "pending" and sh_step["orders"] == ["p2-sh-funds"]
    assert set(sh_step["invalidated"]["carry"]) == {"p2-sh-financials"}
    # play the loop: everything reruns; the financials head comes back as attempt 2 with both texts
    res, batches = drive(run)
    assert res["type"] == "done" and res["chair_rounds"] == 1
    sh_batch = next(b for b in batches if b["step"] == "p2.sector_heads")
    assert {o["order_id"] for o in sh_batch["orders"]} == {"p2-sh-financials", "p2-sh-consumer_staples"}
    m = manifest(run)
    fin = m["orders"]["p2-sh-financials"]
    assert fin["attempt"] == 2 and fin["reopened_by"] == "chair" and fin["output"].endswith("financials_verdict.r2.json")
    fm = yaml.safe_load(Path(fin["order_path"]).read_text(encoding="utf-8").split("---")[1])
    assert fm["prior_errors"][0].startswith("inputs changed after chair re-run of")
    assert "CHAIR FEEDBACK: common point" in fm["prior_errors"]
    cs = m["orders"]["p2-sh-consumer_staples"]
    assert cs["attempt"] == 1 and "reopened_by" not in cs and cs["rechecked_round"] == 1
    assert m["orders"]["p2-sh-funds"]["attempt"] == 1 and "rechecked_round" not in m["orders"]["p2-sh-funds"]


# =====================================================================================
# reopen: the whole loop
# =====================================================================================


def test_full_reopen_loop_regenerates_downstream_and_the_memo_and_does_not_duplicate_ledger_rows(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    before = manifest(run)
    rows_before = ledger_keys(run)
    assert rows_before and len(rows_before) == len(set(rows_before))
    old_memo = (run / "phase4" / "memo.md").read_text(encoding="utf-8")
    keep_hashes = {p: sha(run / p) for p in (
        "phase2/reports/devils_advocate_PG.json", "phase2/financials_verdict.json",
        "phase2/funds_verdict.json", "phase3/health_care_verdict.json", "phase1/cio_allocation.json")}
    keep_orders = {o: copy.deepcopy(before["orders"][o]) for o in
                   ("p2-b-devils_advocate-PG", "p2-b-devils_advocate-JPM", "p2-sh-financials", "p3-sh-health_care")}

    reopen_and_play(capsys, run, KO_FUND, feedback="Show the FCF yield next to the fair value.")
    res, batches = drive(run)

    assert res["type"] == "done" and res["chair_rounds"] == 1
    # exactly the affected agents were launched again, in dependency order
    assert [(b["step"], sorted(o["order_id"] for o in b["orders"])) for b in batches] == [
        ("p2.wave_b", ["p2-b-devils_advocate-KO"]),
        ("p2.sector_heads", ["p2-sh-consumer_staples"]),
        ("p4.cio", ["p4-cio"])]
    m = manifest(run)
    assert m["status"] == "done" and "finished_at" in m
    assert all(s["status"] == "done" for s in m["steps"])
    assert all(o["status"] == "done" for o in m["orders"].values())
    assert set(m["orders"]) == set(before["orders"])
    # the reopened order was collected on attempt 2 from its new output file
    ko = m["orders"][KO_FUND]
    assert ko["attempt"] == 2 and ko["resolved_output"].endswith("fundamental_KO.r2.json")
    assert (run / "phase2" / "reports" / "fundamental_KO.r2.json").is_file()
    assert (run / "phase2" / "reports" / "fundamental_KO.json").is_file()       # the original is kept
    # downstream agents were re-dispatched with the standard "inputs changed" text
    for oid in ("p2-b-devils_advocate-KO", "p2-sh-consumer_staples", "p4-cio"):
        rec = m["orders"][oid]
        assert rec["attempt"] == 1 and rec["rechecked_round"] == 1
        fm = yaml.safe_load(Path(rec["order_path"]).read_text(encoding="utf-8").split("---")[1])
        assert fm["prior_errors"] == [f"inputs changed after chair re-run of {KO_FUND}; "
                                      "re-check your conclusions against the updated reports"]
        assert "## Inputs changed" in Path(rec["order_path"]).read_text(encoding="utf-8")
    # unaffected agents: record and output untouched
    for oid, rec in keep_orders.items():
        assert m["orders"][oid] == rec, oid
    for p, digest in keep_hashes.items():
        assert sha(run / p) == digest, p
    # the memo is regenerated (and the old one archived, not lost)
    memo = run / "phase4" / "memo.md"
    assert memo.is_file() and (run / "phase4" / "memo.html").is_file() and (run / "phase4" / "summary.json").is_file()
    assert res["memo_md"] == memo.as_posix()
    assert (run / "_superseded" / "round1" / "phase4" / "memo.md").read_text(encoding="utf-8") == old_memo
    # the ledger has one row per (run, ticker, role) - re-finishing replaced, never duplicated
    rows_after = ledger_keys(run)
    assert len(rows_after) == len(set(rows_after)) and sorted(rows_after) == sorted(rows_before)
    summary = common.read_json(run / "phase4" / "summary.json")
    assert summary["ledger"]["rows_appended"] == len(rows_before)
    # status after done
    rc, status = cli(capsys, "status", "--run", str(run))
    assert status["status"] == "done" and status["chair_round"] == 1 and "pending_orders" not in status
    log = (run / "run_log.md").read_text(encoding="utf-8")
    assert "CHAIR REOPEN round 1/2" in log and "run done" in log


def test_a_reopened_order_that_returns_invalid_output_gets_one_more_attempt_with_the_chair_feedback(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    assert reopen(capsys, run, KO_FUND, feedback="Point one.")[0] == 0
    out = Path(manifest(run)["orders"][KO_FUND]["output"])
    out.write_text('{"not": "a report"}', encoding="utf-8")
    res = committee.run_next(str(run))
    assert res["type"] == "agents" and [o["order_id"] for o in res["orders"]] == [KO_FUND]
    rec = manifest(run)["orders"][KO_FUND]
    assert rec["attempt"] == 3 and rec["output"].endswith("fundamental_KO.r3.json") and rec["status"] == "dispatched"
    text = Path(rec["order_path"]).read_text(encoding="utf-8")
    assert "## Chair feedback - address every point" in text and "- Point one." in text
    assert "## Retry — attempt 3" in text            # the validator errors are shown separately
    # the cap of this round's retries (attempt_limit 3) is honoured: a second invalid output fails the order
    Path(rec["output"]).write_text('{"not": "a report"}', encoding="utf-8")
    committee.run_next(str(run))
    assert manifest(run)["orders"][KO_FUND]["status"] == "failed"


# =====================================================================================
# reopen: refusals and caps
# =====================================================================================


def _refused(capsys: pytest.CaptureFixture, run: Path, *argv: str) -> dict:
    digest = sha(run / "manifest.json")
    rc, res = cli(capsys, "reopen", "--run", str(run), *argv)
    assert rc == 2 and res["type"] == "error" and res["step"] == "reopen", res
    assert sha(run / "manifest.json") == digest, "a refused reopen must not touch the manifest"
    assert not (run / "_superseded").exists()
    return res


def test_reopen_refusals_leave_the_run_untouched(run: Path, capsys: pytest.CaptureFixture) -> None:
    res = _refused(capsys, run, "--order", "no-such-order", "--feedback", "x")
    assert any("no-such-order: no such order" in d for d in res["details"])
    res = _refused(capsys, run, "--order", KO_FUND)
    assert any("feedback is empty" in d for d in res["details"])
    res = _refused(capsys, run, "--feedback", "x")
    assert any("no orders to reopen" in d for d in res["details"])
    res = _refused(capsys, run, "--order", KO_FUND, "--feedback", "x" * 7000)
    assert any("characters" in d for d in res["details"])

    # an order that is still pending an agent cannot be reopened
    m = manifest(run)
    m["orders"][KO_FUND]["status"] = "dispatched"
    committee.save_manifest(run, m)
    res = _refused(capsys, run, "--order", KO_FUND, "--feedback", "x")
    assert any("still pending an agent" in d for d in res["details"])
    m["orders"][KO_FUND]["status"] = "done"
    committee.save_manifest(run, m)

    # an unfinished run cannot be reopened
    m["status"] = "in_progress"
    committee.save_manifest(run, m)
    res = _refused(capsys, run, "--order", KO_FUND, "--feedback", "x")
    assert "only a finished run" in res["message"] or any("only a finished run" in d for d in res["details"])


def test_no_second_reopen_while_the_first_round_is_still_running(run: Path, capsys: pytest.CaptureFixture) -> None:
    assert reopen(capsys, run, KO_FUND)[0] == 0
    rc, res = reopen(capsys, run, "p2-a-technical-KO")
    assert rc == 2 and "reopened" in json.dumps(res) and "finish the current reopen round" in json.dumps(res)


def test_round_cap_is_enforced_from_the_config_with_a_clear_error(run: Path, capsys: pytest.CaptureFixture) -> None:
    snap = run / "inputs" / "committee.yaml"
    cfg = yaml.safe_load(snap.read_text(encoding="utf-8"))
    cfg["orchestration"]["max_chair_rounds"] = 1
    snap.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    reopen_and_play(capsys, run, KO_FUND)
    res, _ = drive(run)
    assert res["type"] == "done"
    err = _refused_after_done(capsys, run, "p2-a-technical-PG")
    assert "chair round limit reached: 1 of 1" in json.dumps(err)
    assert manifest(run)["chair_rounds"][0]["round"] == 1 and len(manifest(run)["chair_rounds"]) == 1


def _refused_after_done(capsys: pytest.CaptureFixture, run: Path, order: str) -> dict:
    digest = sha(run / "manifest.json")
    rc, res = reopen(capsys, run, order)
    assert rc == 2 and res["type"] == "error", res
    assert sha(run / "manifest.json") == digest
    return res


def test_default_round_cap_is_two_and_the_per_order_cap_is_max_attempts_plus_two(
        run: Path, capsys: pytest.CaptureFixture) -> None:
    # default: 2 rounds (the dry-run config has no max_chair_rounds key)
    assert committee._max_chair_rounds({}) == 2
    assert committee._max_chair_rounds({"orchestration": {"max_chair_rounds": 5}}) == 5
    snap = run / "inputs" / "committee.yaml"
    cfg = yaml.safe_load(snap.read_text(encoding="utf-8"))
    assert "max_chair_rounds" not in cfg["orchestration"]
    cfg["orchestration"].update(max_attempts=1, max_chair_rounds=5)       # per-order cap = 1 + 2 = 3 attempts
    snap.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    for expected_attempt in (2, 3):
        reopen_and_play(capsys, run, "p4-cio")
        assert manifest(run)["orders"]["p4-cio"]["attempt"] == expected_attempt
        res, _ = drive(run)
        assert res["type"] == "done"
    err = _refused_after_done(capsys, run, "p4-cio")
    assert "attempt cap reached (3 of 3" in json.dumps(err)
    m = manifest(run)
    assert len(m["chair_rounds"]) == 2 and m["orders"]["p4-cio"]["attempt"] == 3
    assert [r["round"] for r in m["chair_rounds"]] == [1, 2]
    assert m["orders"]["p4-cio"]["reopen_history"][-1]["round"] == 2
    # reopening the last agent step resets only the python finalize step
    assert m["chair_rounds"][0]["invalidated_steps"] == ["p4.finalize"]
    assert len(ledger_keys(run)) == len(set(ledger_keys(run)))


def test_spec_file_gives_each_order_its_own_feedback(run: Path, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps([{"order_id": KO_FUND, "feedback": "alpha"},
                                {"order_id": "p2-a-technical-PG", "feedback": "beta"}]), encoding="utf-8")
    fb_file = tmp_path / "fb.txt"
    fb_file.write_text("from file\n", encoding="utf-8")
    rc, payload = cli(capsys, "reopen", "--run", str(run), "--spec-file", str(spec),
                      "--order", "p2-a-fundamental-PG", "--feedback", "shared", "--feedback-file", str(fb_file),
                      "--reason", "two things")
    assert rc == 0, payload
    m = manifest(run)
    assert sorted(o["order_id"] for o in payload["orders"]) == [KO_FUND, "p2-a-fundamental-PG", "p2-a-technical-PG"]

    def prior(oid: str) -> list[str]:
        return yaml.safe_load(Path(m["orders"][oid]["order_path"]).read_text(encoding="utf-8").split("---")[1])["prior_errors"]

    assert prior(KO_FUND) == ["CHAIR FEEDBACK: alpha"]
    assert prior("p2-a-technical-PG") == ["CHAIR FEEDBACK: beta"]
    assert prior("p2-a-fundamental-PG") == ["CHAIR FEEDBACK: shared", "CHAIR FEEDBACK: from file"]
    assert m["chair_rounds"][0]["reason"] == "two things"
    # a bad spec file is a clean error
    rc, res = cli(capsys, "reopen", "--run", str(run), "--spec-file", str(fb_file))
    assert rc == 2 and res["type"] == "error"


# =====================================================================================
# audit wrapper
# =====================================================================================


def test_audit_subcommand_wraps_scripts_audit_lazily(run: Path, capsys: pytest.CaptureFixture,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    fake = types.ModuleType("scripts.audit")
    fake.run = lambda run_dir: (seen.append(Path(run_dir)), {"type": "audit", "findings": [], "n": 0})[1]
    monkeypatch.setitem(sys.modules, "scripts.audit", fake)
    rc, res = cli(capsys, "audit", "--run", str(run))
    assert rc == 0 and res == {"type": "audit", "findings": [], "n": 0}
    assert seen == [run.resolve()] or seen == [run]
    # not importable -> a clear JSON error, exit 1
    monkeypatch.setitem(sys.modules, "scripts.audit", None)
    rc, res = cli(capsys, "audit", "--run", str(run))
    assert rc == 1 and res["type"] == "error" and res["step"] == "audit" and "audit.py is not available" in res["message"]


# =====================================================================================
# chair-save
# =====================================================================================


def _review(run_id: str, **over: Any) -> dict:
    doc = {
        "schema": "chair_review/1", "run_id": run_id, "round": 1, "reviewed_at": "2026-10-02T12:00:00+00:00",
        "verdict": "approved_with_notes", "headline": "Keep the portfolio as is; one trim is noise.",
        "executive_summary": ["Do nothing urgent.", "Trim KO only if you want the tax loss.", "Re-run next month."],
        "decisions": [{"ticker": "KO", "action": "HOLD",
                       "chair_view": "At 24 times earnings against a fair value near the price, KO is fairly priced; "
                                     "the main risk is slower volume growth.",
                       "confidence": "medium", "what_would_change_it": "A price above 72 or a broken dividend."}],
        "changes_made": [{"finding_id": None, "what": "Sent the KO fundamental back.", "how": "agent_rerun"}],
        "open_issues": ["Fair value model ignores buybacks."],
        "user_actions": ["Confirm the KO decision."],
        "disclaimer_confirmed": True,
    }
    doc.update(over)
    return doc


def _save(capsys: pytest.CaptureFixture, run: Path, tmp_path: Path, doc: Any, name: str = "review.json") -> tuple[int, dict]:
    f = tmp_path / name
    f.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return cli(capsys, "chair-save", "--run", str(run), "--file", str(f))


def test_chair_save_accepts_a_valid_review_and_keeps_previous_rounds(
        run: Path, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    rid = manifest(run)["run_id"]
    target = run / "phase4" / "chair_review.json"
    rc, res = _save(capsys, run, tmp_path, _review(rid))
    assert rc == 0 and res["type"] == "chair_saved" and res["round"] == 1 and res["previous"] is None
    assert common.read_json(target)["verdict"] == "approved_with_notes"
    rc, res = _save(capsys, run, tmp_path, _review(rid, round=2, verdict="approved"), "r2.json")
    assert rc == 0 and res["previous"].endswith("chair_review.r1.json")
    assert common.read_json(target)["round"] == 2
    assert common.read_json(run / "phase4" / "chair_review.r1.json")["verdict"] == "approved_with_notes"
    # the same round again keeps the earlier copy under a free name
    rc, res = _save(capsys, run, tmp_path, _review(rid, round=2, verdict="needs_rework"), "r2b.json")
    assert rc == 0 and res["previous"].endswith("chair_review.r2.json")
    # a round that goes backwards is refused and nothing changes
    digest = sha(target)
    rc, res = _save(capsys, run, tmp_path, _review(rid, round=1), "old.json")
    assert rc == 2 and any("older than the saved review" in d for d in res["details"]) and sha(target) == digest
    assert not list(run.joinpath("phase4").glob(".*.tmp"))


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.update(extra_key=1), "extra_key"),
    (lambda d: d["decisions"][0].update(note="x"), "note"),
    (lambda d: d.update(verdict="fine"), "verdict"),
    (lambda d: d.update(executive_summary=["a", "b"]), "executive_summary"),
    (lambda d: d.update(executive_summary=["a"] * 8), "executive_summary"),
    (lambda d: d.update(headline=" ".join(["w"] * 41)), "headline: 41 words (limit 40)"),
    (lambda d: d.update(executive_summary=["a", "b", " ".join(["w"] * 46)]), "executive_summary[2]: 46 words (limit 45)"),
    (lambda d: d["decisions"][0].update(chair_view=" ".join(["w"] * 91)), "chair_view: 91 words (limit 90)"),
    (lambda d: d["decisions"][0].update(what_would_change_it=" ".join(["w"] * 41)), "what_would_change_it: 41 words"),
    (lambda d: d["decisions"][0].update(action="WATCH"), "action"),
    (lambda d: d["decisions"][0].update(confidence="certain"), "confidence"),
    (lambda d: d["decisions"].append(copy.deepcopy(d["decisions"][0])), "duplicate decision for KO"),
    (lambda d: d["changes_made"][0].update(how="magic"), "how"),
    (lambda d: d.update(disclaimer_confirmed=False), "disclaimer_confirmed"),
    (lambda d: d.update(round=0), "round"),
    (lambda d: d.update(schema="chair_review/2"), "schema"),
    (lambda d: d.update(run_id="2020-01-01_0000"), "expected"),
    (lambda d: d.update(reviewed_at="yesterday"), "reviewed_at"),
    (lambda d: d.pop("user_actions"), "user_actions"),
    (lambda d: d["decisions"][0].pop("what_would_change_it"), "what_would_change_it"),
])
def test_chair_save_rejects_invalid_reviews(run: Path, capsys: pytest.CaptureFixture, tmp_path: Path,
                                            mutate: Any, needle: str) -> None:
    doc = _review(manifest(run)["run_id"])
    mutate(doc)
    rc, res = _save(capsys, run, tmp_path, doc)
    assert rc == 2 and res["type"] == "error" and res["step"] == "chair-save", res
    assert needle in json.dumps(res["details"]), res["details"]
    assert not (run / "phase4" / "chair_review.json").exists()


def test_chair_save_rejects_unreadable_input(run: Path, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    rc, res = _save(capsys, run, tmp_path, "{not json")
    assert rc == 2 and "not valid JSON" in res["message"]
    rc, res = cli(capsys, "chair-save", "--run", str(run), "--file", str(tmp_path / "missing.json"))
    assert rc == 2 and "not found" in res["message"]
    rc, res = _save(capsys, run, tmp_path, [1, 2, 3])
    assert rc == 2 and res["type"] == "error"


def test_chair_review_schema_is_a_valid_draft_2020_12_schema_with_closed_objects() -> None:
    from jsonschema import Draft202012Validator

    schema = common.read_json(REPO_ROOT / "schemas" / "chair_review.schema.json")
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "urn:agent-investors:chair_review" and schema["additionalProperties"] is False
    assert schema["properties"]["decisions"]["items"]["additionalProperties"] is False
    assert schema["properties"]["changes_made"]["items"]["additionalProperties"] is False
    assert not committee.chair_review_errors(_review("2026-10-02_1200"), run_id="2026-10-02_1200")
