"""committee.py: CLI + run state machine (DESIGN §2.1–§2.3), driven by a FAKE ``_steps`` module.

Fake run: ``p1.data`` (python) -> ``p2.wave_a`` (agents, 2 orders) -> ``p4.finalize`` (python).
``validate`` is replaced by a fake whose rule is: a document is valid iff ``doc["valid"] is True``.

Documented order semantics under test:
* the first ``next`` that reaches an agents step builds and dispatches its orders (attempt 1);
* the following ``next`` checks the outputs — calling it without outputs counts a failed attempt
  (missing output), so the same order ids come back as attempt 2 with ``.r2.json`` outputs and
  ``prior_errors``; one more ``next`` writes ``.failed.json`` stubs and moves on;
* ``redispatch`` re-emits the dispatched orders unchanged (no attempt counted).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import common, committee

NOW = "2026-10-02T10:15:00+00:00"
RUN_ID = "2026-10-02_1015"
REPO = Path(__file__).resolve().parents[1]


# =====================================================================================
# Fakes
# =====================================================================================


class FakeCtx:
    def __init__(self, run_dir: Path, manifest: dict) -> None:
        self.run_dir = Path(run_dir)
        self.manifest = manifest

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]

    @property
    def as_of(self) -> str:
        return self.manifest["as_of"]

    @property
    def mode(self) -> dict:
        return self.manifest["mode"]

    def p(self, *parts: str) -> Path:
        return self.run_dir.joinpath(*parts)

    def log(self, msg: str) -> None:
        committee.log(self.run_dir, msg)


def _spec(ctx: FakeCtx, role: str) -> dict:
    """Fundamental: schema given as a name and notes as a string. Technical: the real _steps shape
    (schema = absolute schema path + schema_name, notes = list of strings)."""
    prices = ctx.p("phase1", "prices.json")
    if role == "technical":
        return dict(_spec(ctx, "fundamental"), order_id="p2-a-technical-KO", agent_role="technical",
                    agent_type=common.AGENT_TYPES["technical"],
                    output=str(ctx.p("phase2", "reports", "technical_KO.json")),
                    schema=(REPO / "schemas" / "specialist_report.json").as_posix(),
                    schema_name="specialist_report",
                    notes=["Technical lens only.", "Use the computed levels."])
    return {
        "order_id": f"p2-a-{role}-KO", "step": "p2.wave_a", "agent_type": common.AGENT_TYPES[role],
        "agent_role": role, "mode": None, "ticker": "KO", "sector": "consumer_staples",
        "sector_class": "default", "phase": 2,
        "inputs": [{"path": str(prices), "purpose": "price data"}],
        "output": str(ctx.p("phase2", "reports", f"{role}_KO.json")),
        "schema": "specialist_report",
        "seed_sources": [{"id": "D:PX:KO", "url": "https://finance.yahoo.com/quote/KO/history/",
                          "title": "KO price history", "accessed": "2026-10-02", "kind": "dataset",
                          "path": str(prices)}],
        "required_fields": ["conviction"],
        "word_limits": {"thesis": 60},
        "notes": "Phase 2 holding review of KO.",
    }


class FakeSteps:
    def __init__(self) -> None:
        self.RunContext = FakeCtx
        self.sequence = [("p1.data", "python"), ("p2.wave_a", "agents"), ("p4.finalize", "python")]
        self.python_calls: list[str] = []
        self.build_calls: list[str] = []
        self.context_calls: list[dict] = []
        self.fail_on: str | None = None
        self.roles = ["fundamental", "technical"]

    def step_sequence(self, ctx: FakeCtx) -> list[tuple[str, str]]:
        return list(self.sequence)

    def run_python_step(self, name: str, ctx: FakeCtx) -> dict:
        self.python_calls.append(name)
        if self.fail_on == name:
            raise RuntimeError(f"boom in {name}")
        if name == "p1.data":
            out = common.write_json(ctx.p("phase1", "prices.json"), {"tickers": {"KO": {"last_close": 61.0}}})
            ctx.manifest["holdings"] = [{"ticker": "KO", "asset_type": "stock", "sector": "consumer_staples",
                                         "sector_class": "default", "classification_source": "test",
                                         "position_pct": 5.0}]
            return {"summary": "prices for 1 ticker", "outputs": [out], "warnings": ["KO price is stale"],
                    "skip_steps": []}
        if name == "p4.finalize":
            md = common.write_text(ctx.p("phase4", "memo.md"), "# memo\n")
            html = common.write_text(ctx.p("phase4", "memo.html"), "<html></html>\n")
            common.write_json(ctx.p("phase4", "summary.json"), {"summary": "KO: HOLD (fake run)"})
            return {"summary": "memo rendered", "outputs": [md.as_posix(), html.as_posix()],
                    "warnings": [], "skip_steps": []}
        return {"summary": f"{name} ok", "outputs": [], "warnings": [], "skip_steps": []}

    def build_orders(self, name: str, ctx: FakeCtx) -> list[dict]:
        self.build_calls.append(name)
        return [_spec(ctx, r) for r in self.roles]

    def validation_context(self, ctx: FakeCtx, spec: dict) -> dict:
        self.context_calls.append(spec)
        return {"order_id": spec["order_id"], "output": spec["output"]}


class FakeValidate:
    ERROR = "$.valid: must be true (got false)"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def validate_doc(self, doc: Any, schema_name: str, *, context: Any = None) -> list[str]:
        self.calls.append((schema_name, context))
        if isinstance(doc, dict) and doc.get("valid") is True:
            return []
        return [self.ERROR]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    steps = FakeSteps()
    validate = FakeValidate()
    monkeypatch.setattr(committee, "_steps_module", lambda: steps)
    monkeypatch.setattr(committee, "_validate_module", lambda: validate)
    return SimpleNamespace(steps=steps, validate=validate)


@pytest.fixture
def env(tmp_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    tmp_env.use_sample_portfolio()
    monkeypatch.setenv(common.ENV_NOW, NOW)
    return tmp_env


@pytest.fixture(scope="module")
def registry() -> Registry:
    resources = []
    for path in sorted((REPO / "schemas").glob("*.json")) + sorted((REPO / "schemas" / "data").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    return Registry().with_resources(resources)


def _schema_errors(registry: Registry, doc: Any, rel: str) -> list[str]:
    schema = json.loads((REPO / "schemas" / rel).read_text(encoding="utf-8"))
    v = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    return [e.message for e in v.iter_errors(doc)]


#: ``manifest["mode"]`` per the MANIFEST SHAPE integration contract.
MANIFEST_MODE_SCHEMA = {
    "type": "object",
    "required": ["dry_run", "quick", "sectors", "holding"],
    "properties": {
        "dry_run": {"type": "boolean"},
        "quick": {"type": "boolean"},
        "sectors": {"type": ["array", "null"],
                    "items": {"$ref": "urn:agent-investors:common_defs#/$defs/sector_key"}},
        "holding": {"$ref": "urn:agent-investors:common_defs#/$defs/ticker_or_null"},
    },
}


def _manifest_errors(registry: Registry, doc: Any) -> list[str]:
    """Errors of a written manifest against schemas/data/manifest.schema.json. ``mode`` is an
    object per the MANIFEST SHAPE contract; while that schema still pins ``mode`` to a string
    (reported to the lead — committee.py does not own the schema) the object shape is checked
    instead, so every other manifest key is still validated against the real schema."""
    schema = json.loads((REPO / "schemas" / "data" / "manifest.schema.json").read_text(encoding="utf-8"))
    mode_schema = schema.get("properties", {}).get("mode", {})
    if mode_schema.get("type") == "string":
        schema["properties"]["mode"] = MANIFEST_MODE_SCHEMA
    v = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    errs = [f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(doc)]
    errs += [f"mode: {e.message}" for e in
             Draft202012Validator(MANIFEST_MODE_SCHEMA, registry=registry).iter_errors(doc.get("mode"))]
    return errs


# =====================================================================================
# Helpers
# =====================================================================================


def run_cli(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, Any]:
    rc = committee.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    assert out, f"no output for {argv}"
    try:
        return rc, json.loads(out[-1])
    except json.JSONDecodeError:
        return rc, "\n".join(out)


def init_run(capsys: pytest.CaptureFixture, *extra: str) -> tuple[str, Path]:
    rc, res = run_cli(capsys, "init", *extra)
    assert rc == 0, res
    return res["run_id"], Path(res["run_dir"])


def manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def parse_order(path: Path) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm_text, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(fm_text), body


def write_report(path: str | Path, *, valid: bool = True, **extra: Any) -> None:
    common.write_json(path, {"valid": valid, **extra})


def dispatch(capsys: pytest.CaptureFixture, run_id: str) -> dict:
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0, res
    assert res["type"] == "agents", res
    return res


# =====================================================================================
# init
# =====================================================================================


def test_committee_imports_without_other_units() -> None:
    assert callable(committee.main)
    assert committee.RUN_ID_RE.match(RUN_ID)


def test_init_fails_on_prefilled_template(tmp_env: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    rc, res = run_cli(capsys, "init")
    assert rc == 2
    assert "/committee-setup" in res["error"]
    assert res["details"] and all(isinstance(d, str) for d in res["details"])
    assert any(d.startswith("portfolio.yaml:") for d in res["details"])
    assert not any(tmp_env.runs_dir.iterdir()), "no run dir may be created on config errors"


def test_init_success_snapshots_configs(env: SimpleNamespace, fakes: SimpleNamespace,
                                        capsys: pytest.CaptureFixture, registry: Registry) -> None:
    rc, res = run_cli(capsys, "init")
    assert rc == 0
    assert res["run_id"] == RUN_ID
    assert res["mode"] == {"dry_run": False, "quick": False, "sectors": None, "holding": None}
    run_dir = Path(res["run_dir"])
    assert run_dir == env.runs_dir / RUN_ID
    for sub in ("inputs", "orders", "phase1", "phase2/data", "phase2/reports", "phase3/reports", "phase4"):
        assert (run_dir / sub).is_dir(), sub
    m = manifest(run_dir)
    assert m["run_id"] == RUN_ID and m["as_of"] == "2026-10-02" and m["created_at"] == NOW
    assert m["mode_label"] == "full"
    assert _manifest_errors(registry, m) == []
    assert "env" in m and common.ENV_OFFLINE not in m["env"], "only dry runs are forced offline"
    assert set(m["config_sha256"]) == {p.name for p in env.config_dir.glob("*.yaml")}
    for name, sha in m["config_sha256"].items():
        assert committee._sha256(run_dir / "inputs" / name) == sha
        assert (run_dir / "inputs" / name).read_bytes() == (env.config_dir / name).read_bytes()
    assert [(s["name"], s["kind"], s["status"]) for s in m["steps"]] == [
        ("p1.data", "python", "pending"), ("p2.wave_a", "agents", "pending"),
        ("p4.finalize", "python", "pending")]
    assert m["orders"] == {} and m["holdings"] == [] and m["warnings"] == []
    assert m["paths"]["run_dir"] == run_dir.as_posix()
    assert "python" in m["versions"]
    log_text = (run_dir / "run_log.md").read_text(encoding="utf-8")
    assert log_text.startswith(f"# Run log — {RUN_ID}") and "init: mode=full" in log_text


def test_init_run_id_suffixes_and_modes(env: SimpleNamespace, fakes: SimpleNamespace,
                                        capsys: pytest.CaptureFixture, registry: Registry,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    first, _ = init_run(capsys)
    second, _ = init_run(capsys, "--quick", "--sectors", "health_care,Financials")
    dry, dry_dir = init_run(capsys, "--dry-run")
    assert (first, second, dry) == (RUN_ID, RUN_ID + "_2", RUN_ID + "_dryrun")
    m2 = manifest(env.runs_dir / second)
    assert m2["mode"] == {"dry_run": False, "quick": True, "sectors": ["health_care", "financials"],
                          "holding": None}
    assert m2["mode_label"] == "quick"
    assert _manifest_errors(registry, m2) == []
    md = manifest(dry_dir)
    assert md["paths"]["cache_dir"] == (dry_dir / ".cache").as_posix()
    assert _manifest_errors(registry, md) == []

    # `init --dry-run` without a fixtures dir: stored offline, so later `next` calls are cache-only
    assert md["env"][common.ENV_OFFLINE] == "1" and common.ENV_FIXTURES_DIR not in md["env"]
    assert any("cache-only" in w for w in md["warnings"])
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    committee.apply_run_env(md, dry_dir)
    assert common.is_offline(), "a dry run never reaches the network"

    rc, res = run_cli(capsys, "holding", "brk.b")
    assert rc == 0 and res["mode"]["holding"] == "BRK-B" and res["mode_label"] == "holding"
    assert _manifest_errors(registry, manifest(Path(res["run_dir"]))) == []
    rc, res = run_cli(capsys, "holding", "KO", "--dry-run")
    assert rc == 0 and manifest(Path(res["run_dir"]))["env"][common.ENV_OFFLINE] == "1"

    rc, res = run_cli(capsys, "init", "--sectors", "tech")
    assert rc == 2 and "unknown sector" in res["error"]

    rc, res = run_cli(capsys, "init", "--run-id", RUN_ID)
    assert rc == 2 and "already exists" in res["error"]


# =====================================================================================
# next: progression, retries, failures
# =====================================================================================


def test_step_progression_to_done(env: SimpleNamespace, fakes: SimpleNamespace,
                                  capsys: pytest.CaptureFixture, registry: Registry) -> None:
    run_id, run_dir = init_run(capsys)
    res = dispatch(capsys, run_id)
    assert fakes.steps.python_calls == ["p1.data"]
    assert res["step"] == "p2.wave_a" and res["run_id"] == run_id
    assert res["concurrency"] == env.read_config("committee")["concurrency"]
    assert [o["order_id"] for o in res["orders"]] == ["p2-a-fundamental-KO", "p2-a-technical-KO"]
    assert [o["agent_type"] for o in res["orders"]] == ["fundamental-analyst", "technical-analyst"]
    assert res["summary"] == "p2.wave_a: 2 orders (KO x2)"
    for o in res["orders"]:
        assert Path(o["order_path"]).is_file()
        assert o["order_path"] == (run_dir / "orders" / f"{o['order_id']}.md").as_posix()

    m = manifest(run_dir)
    assert m["holdings"][0]["ticker"] == "KO", "python steps may mutate the manifest"
    assert m["warnings"] == ["p1.data: KO price is stale"]
    step1 = m["steps"][0]
    assert step1["status"] == "done" and step1["summary"] == "prices for 1 ticker"
    assert step1["outputs"] == [(run_dir / "phase1" / "prices.json").as_posix()]
    rec = m["orders"]["p2-a-fundamental-KO"]
    assert rec["status"] == "dispatched" and rec["attempt"] == 1 and rec["phase"] == 2
    assert rec["output"] == (run_dir / "phase2" / "reports" / "fundamental_KO.json").as_posix()
    assert rec["resolved_output"] is None and rec["errors"] == []
    index = json.loads((run_dir / "orders" / "index.json").read_text(encoding="utf-8"))
    assert set(index["orders"]) == {"p2-a-fundamental-KO", "p2-a-technical-KO"}

    for o in res["orders"]:
        write_report(m["orders"][o["order_id"]]["output"], run_id=run_id, order_id=o["order_id"])
    rc, done = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and done["type"] == "done"
    assert done["memo_md"] == (run_dir / "phase4" / "memo.md").as_posix()
    assert done["memo_html"] == (run_dir / "phase4" / "memo.html").as_posix()
    assert done["summary"] == "KO: HOLD (fake run)"
    assert done["orders"] == {"done": 2, "failed": 0}
    assert fakes.steps.python_calls == ["p1.data", "p4.finalize"]
    assert fakes.steps.build_calls == ["p2.wave_a"], "orders are built once"
    assert [c[0] for c in fakes.validate.calls] == ["specialist_report", "specialist_report"]
    assert fakes.validate.calls[0][1] == {"order_id": "p2-a-fundamental-KO",
                                          "output": rec["output"]}, "context from _steps.validation_context"

    m = manifest(run_dir)
    assert m["status"] == "done"
    assert _manifest_errors(registry, m) == [], "the finished manifest still matches its schema"
    assert all(s["status"] == "done" for s in m["steps"])
    assert m["orders"]["p2-a-technical-KO"]["resolved_output"] == m["orders"]["p2-a-technical-KO"]["output"]
    assert m["steps"][1]["summary"] == "2 done, 0 failed (2 orders)"

    rc, again = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and again["type"] == "done", "next after done is idempotent"
    assert fakes.steps.python_calls == ["p1.data", "p4.finalize"]


def test_next_without_outputs_counts_attempts_then_writes_failed_stubs(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture, registry: Registry) -> None:
    run_id, run_dir = init_run(capsys)
    first = dispatch(capsys, run_id)

    # second next with no outputs: same orders, attempt 2, .r2.json outputs, prior_errors
    second = dispatch(capsys, run_id)
    assert [o["order_id"] for o in second["orders"]] == [o["order_id"] for o in first["orders"]]
    assert [o["order_path"] for o in second["orders"]] == [o["order_path"] for o in first["orders"]]
    assert second["summary"].startswith("p2.wave_a retry (attempt 2): 2 orders")
    m = manifest(run_dir)
    for o in second["orders"]:
        rec = m["orders"][o["order_id"]]
        assert rec["attempt"] == 2 and rec["status"] == "dispatched"
        assert rec["output"].endswith(".r2.json")
        assert rec["base_output"].endswith("_KO.json")
        assert rec["errors"][0].startswith("attempt 1: output file missing")
        fm, body = parse_order(Path(o["order_path"]))
        assert fm["attempt"] == 2 and fm["output"] == rec["output"]
        assert len(fm["prior_errors"]) == 1 and fm["prior_errors"][0].startswith("output file missing")
        assert "## Retry — attempt 2" in body
    assert fakes.steps.build_calls == ["p2.wave_a"], "a retry never rebuilds the step's orders"

    # third next with still no outputs: failed stubs, step done, run continues to the end
    rc, done = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and done["type"] == "done"
    assert done["orders"] == {"done": 0, "failed": 2}
    m = manifest(run_dir)
    assert m["steps"][1]["summary"] == "0 done, 2 failed (2 orders)"
    for oid in ("p2-a-fundamental-KO", "p2-a-technical-KO"):
        rec = m["orders"][oid]
        assert rec["status"] == "failed" and rec["attempt"] == 2
        stub_path = Path(rec["resolved_output"])
        assert stub_path.name.endswith("_KO.failed.json")
        stub = json.loads(stub_path.read_text(encoding="utf-8"))
        assert _schema_errors(registry, stub, "failed_report.json") == []
        assert stub["status"] == "failed" and stub["attempts"] == 2 and stub["order_id"] == oid
        assert stub["run_id"] == run_id and stub["ticker"] == "KO" and stub["sector"] == "consumer_staples"
        assert [e.split(":")[0] for e in stub["errors"]] == ["attempt 1", "attempt 2"]
        assert m["steps"][1]["outputs"].count(rec["resolved_output"]) == 1


def test_invalid_output_retries_with_prior_errors_and_r2(env: SimpleNamespace, fakes: SimpleNamespace,
                                                         capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    m = manifest(run_dir)
    write_report(m["orders"]["p2-a-fundamental-KO"]["output"], valid=False)
    write_report(m["orders"]["p2-a-technical-KO"]["output"])

    retry = dispatch(capsys, run_id)
    assert [o["order_id"] for o in retry["orders"]] == ["p2-a-fundamental-KO"], "only the invalid one"
    m = manifest(run_dir)
    fund = m["orders"]["p2-a-fundamental-KO"]
    tech = m["orders"]["p2-a-technical-KO"]
    assert tech["status"] == "done" and tech["resolved_output"].endswith("technical_KO.json")
    assert fund["attempt"] == 2 and fund["output"].endswith("fundamental_KO.r2.json")
    assert fund["errors"] == [f"attempt 1: {FakeValidate.ERROR}"]
    fm, _ = parse_order(Path(fund["order_path"]))
    assert fm["prior_errors"] == [FakeValidate.ERROR]
    assert fm["output"] == fund["output"]

    write_report(fund["output"])
    rc, done = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and done["type"] == "done"
    fund = manifest(run_dir)["orders"]["p2-a-fundamental-KO"]
    assert fund["status"] == "done" and fund["resolved_output"] == fund["output"]
    assert fund["resolved_output"].endswith(".r2.json")
    # the retry was validated with a context built for the attempt-2 output
    assert fakes.steps.context_calls[-1]["output"] == fund["output"]
    assert fakes.steps.context_calls[-1]["attempt"] == 2


def test_mismatched_run_id_in_output_is_rejected(env: SimpleNamespace, fakes: SimpleNamespace,
                                                 capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    m = manifest(run_dir)
    write_report(m["orders"]["p2-a-fundamental-KO"]["output"], run_id="2026-01-01_0000")
    write_report(m["orders"]["p2-a-technical-KO"]["output"], order_id="p2-a-technical-KO")
    retry = dispatch(capsys, run_id)
    assert [o["order_id"] for o in retry["orders"]] == ["p2-a-fundamental-KO"]
    fm, _ = parse_order(Path(retry["orders"][0]["order_path"]))
    assert any(e.startswith("run_id: expected") for e in fm["prior_errors"])


def test_collect_is_all_or_nothing_when_validation_crashes(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash part-way through collecting (validator unavailable for order B after order A was
    already judged missing) must not persist A as a retry that was never emitted."""
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    before = manifest(run_dir)
    fund_path = Path(before["orders"]["p2-a-fundamental-KO"]["order_path"])
    fund_order_before = fund_path.read_text(encoding="utf-8")
    write_report(before["orders"]["p2-a-technical-KO"]["output"])  # B valid, A missing

    def broken() -> Any:
        raise ImportError("scripts.validate is broken")

    monkeypatch.setattr(committee, "_validate_module", broken)
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 1 and res["type"] == "error" and res["step"] == "p2.wave_a"
    after = manifest(run_dir)
    assert after["orders"] == before["orders"], "no order state committed on a partial collection"
    assert after["steps"][1]["status"] == "dispatched" and "ImportError" in after["steps"][1]["error"]
    assert fund_path.read_text(encoding="utf-8") == fund_order_before, "no retry order written"
    assert not list((run_dir / "phase2" / "reports").glob("*.failed.json"))

    monkeypatch.setattr(committee, "_validate_module", lambda: fakes.validate)
    retry = dispatch(capsys, run_id)
    assert [o["order_id"] for o in retry["orders"]] == ["p2-a-fundamental-KO"], "A's retry is emitted"
    m = manifest(run_dir)
    fund = m["orders"]["p2-a-fundamental-KO"]
    assert fund["attempt"] == 2 and fund["status"] == "dispatched"
    assert fund["errors"] == [f"attempt 1: output file missing: {before['orders']['p2-a-fundamental-KO']['output']}"]
    assert m["orders"]["p2-a-technical-KO"]["status"] == "done"
    fm, _ = parse_order(fund_path)
    assert fm["attempt"] == 2 and fm["output"].endswith("fundamental_KO.r2.json")


def test_max_attempts_one_fails_without_retry(env: SimpleNamespace, fakes: SimpleNamespace,
                                              capsys: pytest.CaptureFixture) -> None:
    env.update_config("committee", lambda c: c["orchestration"].update(max_attempts=1))
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and res["type"] == "done" and res["orders"]["failed"] == 2


def test_time_budget_returns_continue(env: SimpleNamespace, fakes: SimpleNamespace,
                                      capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes.steps.sequence = [("p1.data", "python"), ("p1.extra", "python"), ("p2.wave_a", "agents"),
                            ("p4.finalize", "python")]
    run_id, run_dir = init_run(capsys)
    ticks = iter(range(0, 10**6, 1000))  # every clock read advances 1000 s (budget is 90 s)
    monkeypatch.setattr(committee, "_clock", lambda: float(next(ticks)))

    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and res["type"] == "continue" and res["next_step"] == "p1.extra"
    assert fakes.steps.python_calls == ["p1.data"], "one step always runs (progress guarantee)"
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert res["type"] == "continue" and res["next_step"] == "p2.wave_a"
    assert fakes.steps.python_calls == ["p1.data", "p1.extra"]
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert res["type"] == "agents"
    assert [s["status"] for s in manifest(run_dir)["steps"]] == ["done", "done", "dispatched", "pending"]


def test_skip_steps_marks_later_steps_skipped(env: SimpleNamespace, fakes: SimpleNamespace,
                                              capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = fakes.steps.run_python_step

    def run_python_step(name: str, ctx: FakeCtx) -> dict:
        res = original(name, ctx)
        if name == "p1.data":
            res["skip_steps"] = ["p2.wave_a"]
        return res

    monkeypatch.setattr(fakes.steps, "run_python_step", run_python_step)
    run_id, run_dir = init_run(capsys)
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and res["type"] == "done"
    steps = manifest(run_dir)["steps"]
    assert [s["status"] for s in steps] == ["done", "skipped", "done"]
    assert steps[1]["summary"] == "skipped by p1.data"
    assert fakes.steps.build_calls == []


def test_agents_step_without_orders_is_done(env: SimpleNamespace, fakes: SimpleNamespace,
                                            capsys: pytest.CaptureFixture) -> None:
    fakes.steps.roles = []
    run_id, run_dir = init_run(capsys)
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and res["type"] == "done"
    assert manifest(run_dir)["steps"][1]["summary"] == "no orders"


# =====================================================================================
# work-order files
# =====================================================================================


def test_order_front_matter_parses_and_validates(env: SimpleNamespace, fakes: SimpleNamespace,
                                                 capsys: pytest.CaptureFixture, registry: Registry) -> None:
    run_id, run_dir = init_run(capsys)
    res = dispatch(capsys, run_id)
    order = res["orders"][0]
    fm, body = parse_order(Path(order["order_path"]))
    assert _schema_errors(registry, fm, "data/work_order.schema.json") == []
    assert list(fm)[:18] == ["order_id", "run_id", "step", "agent_type", "agent_role", "mode", "ticker",
                             "sector", "sector_class", "attempt", "as_of", "inputs", "output", "schema",
                             "schema_name", "seed_sources", "required_fields", "prior_errors"]
    assert fm["order_id"] == "p2-a-fundamental-KO" and fm["run_id"] == run_id
    assert fm["as_of"] == "2026-10-02" and isinstance(fm["as_of"], str), "dates stay strings"
    assert fm["attempt"] == 1 and fm["prior_errors"] == [] and fm["mode"] is None
    assert fm["seed_sources"][0]["accessed"] == "2026-10-02"
    assert fm["schema_name"] == "specialist_report"
    assert fm["schema"] == (REPO / "schemas" / "specialist_report.json").as_posix()
    for p in [fm["output"], fm["schema"], fm["seed_sources"][0]["path"]] + [i["path"] for i in fm["inputs"]]:
        assert "\\" not in p and Path(p).is_absolute(), p
    assert fm["inputs"] == [{"path": (run_dir / "phase1" / "prices.json").as_posix(), "purpose": "price data"}]
    assert fm["required_fields"] == ["conviction"] and fm["word_limits"] == {"thesis": 60}

    assert "# Work order — fundamental-analyst on KO (consumer_staples)" in body
    assert f"Execute work order {order['order_path']}" in body
    assert f"`DONE {fm['output']}`" in body and "FAILED <" in body
    assert "Phase 2 holding review of KO." in body
    assert "## Role notes — fundamental" in body and "cells_base" in body
    assert "data_ref" in body and "missing_data" in body and "never pad" in body
    assert f"at most {env.read_config('committee')['validation']['max_quote_words']} words" in body
    assert "## Retry" not in body
    assert "{{" not in body and "{%" not in body
    assert "## Assignment\n\n- Phase 2 holding review of KO.\n" in body
    tech_fm, tech_body = parse_order(Path(res["orders"][1]["order_path"]))
    assert _schema_errors(registry, tech_fm, "data/work_order.schema.json") == []
    assert tech_fm["schema_name"] == "specialist_report" and tech_fm["schema"] == fm["schema"]
    assert "## Role notes — technical" in tech_body and "trend_regime" in tech_body
    assert "- Technical lens only.\n- Use the computed levels.\n" in tech_body
    m = manifest(run_dir)
    assert {r["schema"] for r in m["orders"].values()} == {"specialist_report"}


def test_invalid_front_matter_is_an_error(env: SimpleNamespace, fakes: SimpleNamespace,
                                          capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_orders(name: str, ctx: FakeCtx) -> list[dict]:
        spec = _spec(ctx, "fundamental")
        spec["sector"] = "not_a_sector"
        return [spec]

    monkeypatch.setattr(fakes.steps, "build_orders", bad_orders)
    run_id, run_dir = init_run(capsys)
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 1 and res["type"] == "error" and res["step"] == "p2.wave_a"
    assert "front matter invalid" in res["message"]
    m = manifest(run_dir)
    assert m["orders"] == {}, "nothing is committed when an order fails to render"
    assert m["steps"][1]["status"] == "failed"


# =====================================================================================
# redispatch / status
# =====================================================================================


def test_redispatch_and_status(env: SimpleNamespace, fakes: SimpleNamespace,
                               capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)

    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert rc == 0 and st["type"] == "status" and st["current_step"] == "p1.data"
    assert st["steps"] == {"p1.data": "pending", "p2.wave_a": "pending", "p4.finalize": "pending"}
    rc, res = run_cli(capsys, "redispatch", "--run", run_id)
    assert rc == 0 and res["type"] == "continue", "nothing dispatched yet"

    first = dispatch(capsys, run_id)
    before = manifest(run_dir)["orders"]
    Path(first["orders"][1]["order_path"]).unlink()  # crash lost an order file

    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert rc == 0 and red["type"] == "agents" and red["redispatch"] is True
    assert red["orders"] == first["orders"] and red["step"] == "p2.wave_a"
    assert Path(first["orders"][1]["order_path"]).is_file(), "missing order file re-rendered"
    after = manifest(run_dir)["orders"]
    assert {k: v["attempt"] for k, v in after.items()} == {k: 1 for k in before}
    assert after == before, "redispatch never changes order state"

    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert st["current_step"] == "p2.wave_a" and st["steps"]["p1.data"] == "done"
    assert st["steps"]["p2.wave_a"] == "dispatched"
    assert st["orders"]["dispatched"] == 2 and st["orders"]["total"] == 2
    assert st["mode"] == "full" and st["warnings"] == 1 and "hint" in st

    for o in first["orders"]:
        write_report(after[o["order_id"]]["output"])
    run_cli(capsys, "next", "--run", run_id)
    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert st["status"] == "done" and st["current_step"] is None
    assert st["memo_md"].endswith("phase4/memo.md")
    rc, st_latest = run_cli(capsys, "status", "--run", "latest")
    assert st_latest["run_id"] == run_id


def test_redispatch_after_retry_keeps_attempt(env: SimpleNamespace, fakes: SimpleNamespace,
                                              capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    dispatch(capsys, run_id)  # no outputs -> attempt 2
    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert red["type"] == "agents" and "retry (attempt 2)" in red["summary"]
    assert {r["attempt"] for r in manifest(run_dir)["orders"].values()} == {2}


FOUR_ROLES = ["fundamental", "technical", "osint", "operator_ceo"]


def _attempts(run_dir: Path) -> dict[str, int]:
    return {k: v["attempt"] for k, v in manifest(run_dir)["orders"].items()}


def test_redispatch_skips_orders_whose_output_is_already_valid(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    """After a stop `next` has not run, so every order still reads `dispatched`; redispatch must
    only re-emit the ones that still need an agent (a re-run would redo finished work and its agent
    could not Write over a file it never read)."""
    run_id, run_dir = init_run(capsys)
    first = dispatch(capsys, run_id)
    recs = manifest(run_dir)["orders"]
    done_id, todo_id = first["orders"][0]["order_id"], first["orders"][1]["order_id"]
    write_report(recs[done_id]["output"], run_id=run_id, order_id=done_id)

    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert rc == 0 and red["type"] == "agents" and red["redispatch"] is True
    assert [o["order_id"] for o in red["orders"]] == [todo_id]
    assert red["skipped_valid"] == [done_id] and red["summary"] == "p2.wave_a: 1 order (KO)"
    m = manifest(run_dir)
    assert m["orders"][done_id]["status"] == "done"
    assert m["orders"][done_id]["resolved_output"] == m["orders"][done_id]["output"]
    assert m["orders"][todo_id]["status"] == "dispatched"
    assert _attempts(run_dir) == {done_id: 1, todo_id: 1}, "redispatch never counts an attempt"
    assert "already have a valid output" in (run_dir / "run_log.md").read_text(encoding="utf-8")

    # the last agent finishes: nothing is left to launch -> `continue`, then next completes the run
    write_report(recs[todo_id]["output"], run_id=run_id, order_id=todo_id)
    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert rc == 0 and red["type"] == "continue" and red["skipped_valid"] == [todo_id]
    rc, done = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and done["type"] == "done" and done["orders"] == {"done": 2, "failed": 0}
    assert set(_attempts(run_dir).values()) == {1}


def test_redispatch_sets_aside_an_invalid_output_and_re_emits_the_order(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    first = dispatch(capsys, run_id)
    recs = manifest(run_dir)["orders"]
    bad_id = first["orders"][0]["order_id"]
    bad_out = Path(recs[bad_id]["output"])
    write_report(bad_out, valid=False, run_id=run_id, order_id=bad_id)

    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert rc == 0 and red["type"] == "agents"
    assert [o["order_id"] for o in red["orders"]] == [o["order_id"] for o in first["orders"]]
    assert red["set_aside"] == [bad_id]
    assert not bad_out.exists(), "the agent must be able to Write the path again"
    assert bad_out.with_name(bad_out.name + ".invalid").is_file()
    assert set(_attempts(run_dir).values()) == {1}


def test_circuit_breaker_stops_next_without_burning_attempts(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    fakes.steps.roles = FOUR_ROLES
    run_id, run_dir = init_run(capsys)
    first = dispatch(capsys, run_id)
    assert len(first["orders"]) == 4

    # every agent call failed (usage limit / outage): no output file at all
    for _ in range(2):                                   # and calling next again changes nothing
        rc, res = run_cli(capsys, "next", "--run", run_id)
        assert rc == 1 and res["type"] == "error" and res["step"] == "p2.wave_a"
        assert res["message"] == (f"4 of 4 agent orders produced no output - likely a usage limit or outage; "
                                  f"re-run `redispatch --run {run_id}` then `next` once agents are available")
        assert res["circuit_breaker"]["no_output"] == 4 and res["circuit_breaker"]["orders"] == 4
        assert _attempts(run_dir) == {o["order_id"]: 1 for o in first["orders"]}, "no attempt may be burned"
    m = manifest(run_dir)
    assert {r["status"] for r in m["orders"].values()} == {"dispatched"}
    assert all(r["errors"] == [] for r in m["orders"].values()), "no failed attempt was recorded"
    assert m["steps"][1]["status"] == "dispatched" and m["steps"][1]["circuit_breaker"]["no_output"] == 4
    assert not list((run_dir / "phase2" / "reports").glob("*.failed.json"))
    assert "CIRCUIT BREAKER" in (run_dir / "run_log.md").read_text(encoding="utf-8")

    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert st["circuit_breaker"]["no_output"] == 4 and "circuit breaker" in st["hint"]
    assert st["last_error"].startswith("4 of 4 agent orders produced no output")

    # half of the agents finished: still >= 50% missing -> still stops; redispatch lists only the rest
    for o in first["orders"][:2]:
        write_report(m["orders"][o["order_id"]]["output"], run_id=run_id, order_id=o["order_id"])
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 1 and res["message"].startswith("2 of 4 agent orders produced no output")
    rc, red = run_cli(capsys, "redispatch", "--run", run_id)
    assert [o["order_id"] for o in red["orders"]] == [o["order_id"] for o in first["orders"][2:]]
    assert red["skipped_valid"] == [o["order_id"] for o in first["orders"][:2]]

    for o in first["orders"][2:]:
        write_report(m["orders"][o["order_id"]]["output"], run_id=run_id, order_id=o["order_id"])
    rc, done = run_cli(capsys, "next", "--run", run_id)
    assert rc == 0 and done["type"] == "done" and done["orders"] == {"done": 4, "failed": 0}
    assert set(_attempts(run_dir).values()) == {1}
    st = manifest(run_dir)["steps"][1]
    assert "circuit_breaker" not in st and "error" not in st, "the breaker state is cleared once the step passes"


def test_circuit_breaker_needs_enough_orders_and_a_majority(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    # fewer than 3 orders (the default fixture has 2): never trips, a missing output is an attempt
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    second = dispatch(capsys, run_id)
    assert "retry (attempt 2)" in second["summary"]

    # 4 orders, only one missing (25% < 50%): ordinary retry of that order
    fakes.steps.roles = FOUR_ROLES
    run_id2, run_dir2 = init_run(capsys, "--run-id", "2026-10-02_1100", "--config-dir", str(env.config_dir))
    first = dispatch(capsys, run_id2)
    recs = manifest(run_dir2)["orders"]
    for o in first["orders"][:3]:
        write_report(recs[o["order_id"]]["output"], run_id=run_id2, order_id=o["order_id"])
    retry = dispatch(capsys, run_id2)
    assert [o["order_id"] for o in retry["orders"]] == [first["orders"][3]["order_id"]]
    assert "retry (attempt 2)" in retry["summary"]

    # retries are never affected: the retried order is still missing -> failed stub, not an error
    rc, done = run_cli(capsys, "next", "--run", run_id2)
    assert rc == 0 and done["type"] == "done" and done["orders"] == {"done": 3, "failed": 1}


def test_circuit_breaker_is_config_driven_and_can_be_ignored(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    fakes.steps.roles = FOUR_ROLES
    # a fraction above 1 disables it
    env.update_config("committee", lambda c: c["orchestration"].update(circuit_breaker_fraction=1.5))
    run_id, run_dir = init_run(capsys)
    dispatch(capsys, run_id)
    assert "retry (attempt 2)" in dispatch(capsys, run_id)["summary"]

    # a higher minimum order count also keeps it quiet; the CLI flag accepts the failures
    env.update_config("committee", lambda c: c["orchestration"].update(
        circuit_breaker_fraction=0.5, circuit_breaker_min_orders=10))
    run_id2, _ = init_run(capsys, "--run-id", "2026-10-02_1101", "--config-dir", str(env.config_dir))
    dispatch(capsys, run_id2)
    assert "retry (attempt 2)" in dispatch(capsys, run_id2)["summary"]

    env.update_config("committee", lambda c: c["orchestration"].pop("circuit_breaker_min_orders"))
    run_id3, run_dir3 = init_run(capsys, "--run-id", "2026-10-02_1102", "--config-dir", str(env.config_dir))
    dispatch(capsys, run_id3)
    rc, res = run_cli(capsys, "next", "--run", run_id3)
    assert rc == 1 and res["type"] == "error"
    rc, res = run_cli(capsys, "next", "--run", run_id3, "--ignore-circuit-breaker")
    assert rc == 0 and res["type"] == "agents" and "retry (attempt 2)" in res["summary"]
    assert set(_attempts(run_dir3).values()) == {2}


def test_status_reports_a_config_edited_after_the_run_started(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert "config_drift" not in st

    env.update_config("investor_profile", lambda c: c.update(risk_tolerance="aggressive", horizon_years=55))
    rc, st = run_cli(capsys, "status", "--run", run_id)
    assert rc == 0
    assert [d["file"] for d in st["config_drift"]] == ["investor_profile.yaml"]
    assert st["config_drift"][0]["change"] == "changed"
    assert st["config_drift"][0]["message"] == ("config investor_profile.yaml changed after this run started "
                                                "\u2014 decisions used the snapshot in inputs/")
    assert "inputs/ snapshot" in st["hint"]
    # the run itself keeps using its snapshot and is not aborted
    assert (run_dir / "inputs" / "investor_profile.yaml").read_bytes() != \
        (env.config_dir / "investor_profile.yaml").read_bytes()
    res = dispatch(capsys, run_id)
    assert res["type"] == "agents"

    (env.config_dir / "universe.yaml").unlink()
    rc, st = run_cli(capsys, "status", "--run", run_id)
    changes = {d["file"]: d["change"] for d in st["config_drift"]}
    assert changes == {"investor_profile.yaml": "changed", "universe.yaml": "deleted"}


# =====================================================================================
# errors
# =====================================================================================


def test_exception_becomes_error_json_and_run_resumes(env: SimpleNamespace, fakes: SimpleNamespace,
                                                      capsys: pytest.CaptureFixture) -> None:
    run_id, run_dir = init_run(capsys)
    fakes.steps.fail_on = "p1.data"
    rc, res = run_cli(capsys, "next", "--run", run_id)
    assert rc == 1
    assert res["type"] == "error" and res["step"] == "p1.data" and res["run_id"] == run_id
    assert "RuntimeError: boom in p1.data" in res["message"]
    m = manifest(run_dir)
    assert m["steps"][0]["status"] == "failed" and "boom" in m["steps"][0]["error"]
    log_text = (run_dir / "run_log.md").read_text(encoding="utf-8")
    assert "ERROR [p1.data] RuntimeError: boom in p1.data" in log_text and "Traceback" in log_text

    fakes.steps.fail_on = None
    res = dispatch(capsys, run_id)  # resumable: the failed python step is re-run
    assert res["step"] == "p2.wave_a"
    m = manifest(run_dir)
    assert m["steps"][0]["status"] == "done" and "error" not in m["steps"][0]


def test_unknown_run_is_error_json(env: SimpleNamespace, fakes: SimpleNamespace,
                                   capsys: pytest.CaptureFixture) -> None:
    rc, res = run_cli(capsys, "next", "--run", "2026-01-01_0000")
    assert rc == 1 and res["type"] == "error" and res["step"] is None
    assert "run not found" in res["message"]


# =====================================================================================
# dry-run
# =====================================================================================


def _make_fixtures(tmp_path: Path, env: SimpleNamespace, *, technical: bool = True) -> Path:
    fx = tmp_path / "fx"
    (fx / "config").mkdir(parents=True)
    for f in env.config_dir.glob("*.yaml"):
        shutil.copy2(f, fx / "config" / f.name)
    (fx / "data").mkdir()
    agents = fx / "agent_outputs"
    agents.mkdir()
    common.write_text(agents / "p2-a-fundamental-KO.json", json.dumps({
        "valid": True, "run_id": "OLD", "order_id": "OLD", "as_of": "1999-01-01",
        "key_figures": [{"data_ref": "${RUN_DIR}/phase1/prices.json#/tickers/KO/last_close"}],
        "repo": "${REPO_ROOT}/schemas"}))
    if technical:  # fallback lookup by output file name
        common.write_text(agents / "technical_KO.json", json.dumps({"valid": True}))
    return fx


def test_dry_run_copies_fixtures_with_token_substitution(env: SimpleNamespace, fakes: SimpleNamespace,
                                                         capsys: pytest.CaptureFixture, tmp_path: Path,
                                                         registry: Registry) -> None:
    fx = _make_fixtures(tmp_path, env)
    runs = tmp_path / "dry_runs"
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(runs))
    assert rc == 0, res
    assert res["type"] == "done" and res["dry_run"] is True and res["agent_batches"] == 1
    assert res["ok"] is True and res["fixtures_missing"] == []
    run_dir = Path(res["run_dir"])
    assert run_dir.parent == runs.resolve() and run_dir.name == "2026-10-02_1200_dryrun"
    assert res["memo_md"] == (run_dir / "phase4" / "memo.md").as_posix()
    assert res["memo_html"] == (run_dir / "phase4" / "memo.html").as_posix()

    assert Path(os.environ[common.ENV_FIXTURES_DIR]) == fx / "data"
    assert os.environ[common.ENV_AS_OF] == "2026-10-02"
    assert Path(os.environ[common.ENV_CACHE_DIR]) == run_dir / ".cache"
    assert Path(os.environ[common.ENV_CONFIG_DIR]) == run_dir / "inputs", "next runs on the snapshot"
    m = manifest(run_dir)
    assert _manifest_errors(registry, m) == []
    assert m["mode"]["dry_run"] is True and m["as_of"] == "2026-10-02"
    assert m["created_at"] == "2026-10-02T12:00:00+00:00"
    assert m["env"][common.ENV_FIXTURES_DIR] == (fx / "data").as_posix()
    assert m["env"][common.ENV_OFFLINE] == "1" and os.environ[common.ENV_OFFLINE] == "1"
    assert not any("cache-only" in w for w in m["warnings"])
    assert m["paths"]["cache_dir"] == (run_dir / ".cache").as_posix()
    assert m["paths"]["ledger"] == (run_dir / "phase4" / "ledger_preview.csv").as_posix()
    assert Path(os.environ[common.ENV_LEDGER]) == run_dir / "phase4" / "ledger_preview.csv"
    assert {r["status"] for r in m["orders"].values()} == {"done"}

    fund = json.loads(Path(m["orders"]["p2-a-fundamental-KO"]["output"]).read_text(encoding="utf-8"))
    assert fund["run_id"] == m["run_id"] and fund["order_id"] == "p2-a-fundamental-KO"
    assert fund["as_of"] == "2026-10-02"
    assert fund["key_figures"][0]["data_ref"] == (
        f"{run_dir.as_posix()}/phase1/prices.json#/tickers/KO/last_close")
    assert fund["repo"] == f"{REPO.as_posix()}/schemas"
    tech = json.loads(Path(m["orders"]["p2-a-technical-KO"]["output"]).read_text(encoding="utf-8"))
    assert tech["order_id"] == "p2-a-technical-KO" and tech["valid"] is True
    assert "[dry-run] simulated 2 agent output(s)" in (run_dir / "run_log.md").read_text(encoding="utf-8")


def test_dry_run_missing_fixture_goes_through_retry_and_failure(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    fx = _make_fixtures(tmp_path, env, technical=False)
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"),
                      "--as-of", "2026-10-01")
    # ANY failed order makes the dry run fail (it used to need every order to fail)
    assert rc == 1, res
    assert res["type"] == "done" and res["agent_batches"] == 2 and res["orders"] == {"done": 1, "failed": 1}
    assert res["ok"] is False and res["fixtures_missing"] == ["p2-a-technical-KO"]
    assert res["message"].startswith("1 of 2 agent orders failed")
    run_dir = Path(res["run_dir"])
    assert run_dir.name == "2026-10-01_1200_dryrun"
    m = manifest(run_dir)
    tech = m["orders"]["p2-a-technical-KO"]
    assert tech["status"] == "failed" and tech["resolved_output"].endswith("technical_KO.failed.json")
    assert Path(tech["resolved_output"]).is_file()
    assert not Path(tech["output"]).exists()


def test_dry_run_every_order_failed_is_nonzero(env: SimpleNamespace, fakes: SimpleNamespace,
                                               capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    fx = _make_fixtures(tmp_path, env, technical=False)
    (fx / "agent_outputs" / "p2-a-fundamental-KO.json").unlink()
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"))
    assert rc == 1, res
    assert res["type"] == "done" and res["ok"] is False and res["orders"] == {"done": 0, "failed": 2}
    assert sorted(res["fixtures_missing"]) == ["p2-a-fundamental-KO", "p2-a-technical-KO"]
    assert "every agent order failed" in res["message"]


@pytest.mark.parametrize("layout", ["absent", "no_agent_outputs", "no_data", "no_config"])
def test_dry_run_rejects_missing_or_incomplete_fixtures(layout: str, env: SimpleNamespace,
                                                        fakes: SimpleNamespace, capsys: pytest.CaptureFixture,
                                                        tmp_path: Path) -> None:
    if layout == "absent":
        fx = tmp_path / "does_not_exist"
    else:
        fx = _make_fixtures(tmp_path, env)
        shutil.rmtree(fx / {"no_agent_outputs": "agent_outputs", "no_data": "data", "no_config": "config"}[layout])
    runs = tmp_path / "r"
    config_before = os.environ[common.ENV_CONFIG_DIR]
    fixtures_before = os.environ.get(common.ENV_FIXTURES_DIR)
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(runs))
    assert rc == 2 and res["type"] == "error" and res["step"] == "init", res
    if layout == "absent":
        assert res["error"].startswith("fixtures dir not found:")
    else:
        assert res["error"].startswith("fixtures dir incomplete:")
    assert any("agent_outputs/" in d for d in res["details"])
    assert not runs.exists() or not any(runs.iterdir()), "nothing is created"
    assert os.environ[common.ENV_CONFIG_DIR] == config_before, "the real config dir is never picked up"
    assert os.environ.get(common.ENV_FIXTURES_DIR) == fixtures_before


def test_dry_run_explicit_config_dir_replaces_fixture_config(env: SimpleNamespace, fakes: SimpleNamespace,
                                                             capsys: pytest.CaptureFixture,
                                                             tmp_path: Path) -> None:
    fx = _make_fixtures(tmp_path, env)
    shutil.rmtree(fx / "config")
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"),
                      "--config-dir", str(env.config_dir))
    assert rc == 0 and res["type"] == "done", res
    assert manifest(Path(res["run_dir"]))["paths"]["config_dir"] == env.config_dir.resolve().as_posix()


def test_dry_run_reports_init_errors(env: SimpleNamespace, fakes: SimpleNamespace,
                                     capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    fx = _make_fixtures(tmp_path, env)
    shutil.copy2(REPO / "tests" / "fixtures" / "config_template" / "portfolio.yaml",
                 fx / "config" / "portfolio.yaml")  # null template
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"))
    assert rc == 2 and res["type"] == "error" and "/committee-setup" in res["error"]


@pytest.mark.parametrize("flags,named", [
    (["--quick"], "--quick"),
    (["--sectors", "health_care"], "--sectors"),
    (["--holding", "KO"], "--holding"),
    (["--quick", "--sectors", "health_care"], "--quick, --sectors"),
])
def test_dry_run_rejects_modes_the_fixtures_do_not_support(
        flags: list[str], named: str, env: SimpleNamespace, fakes: SimpleNamespace,
        capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    """A quick / sector-limited dry run used to start and fail half way with a broken memo."""
    fx = _make_fixtures(tmp_path, env)
    runs = tmp_path / "r"
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(runs), *flags)
    assert rc == 2, res
    assert res["type"] == "error" and res["step"] == "init"
    assert res["error"].startswith(f"dry-run does not support {named}:")
    assert "full pipeline only" in res["error"]
    assert not runs.exists() or not any(runs.iterdir()), "nothing is created"
    assert not fakes.steps.python_calls


def test_dry_run_exit_code_is_nonzero_when_any_order_failed_even_quietly(
        env: SimpleNamespace, fakes: SimpleNamespace, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    fx = _make_fixtures(tmp_path, env, technical=False)
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"))
    assert rc == 1 and res["ok"] is False and res["orders"]["failed"] == 1 and res["orders"]["done"] == 1


def test_dry_run_never_trips_the_circuit_breaker(env: SimpleNamespace, fakes: SimpleNamespace,
                                                 capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    """The agents of a dry run are simulated, so a missing fixture is a missing fixture, not an
    outage: it goes through the retry / failed-stub path even when most orders have none."""
    fakes.steps.roles = FOUR_ROLES
    fx = _make_fixtures(tmp_path, env, technical=False)            # 1 of 4 orders has a fixture
    rc, res = run_cli(capsys, "dry-run", "--fixtures-dir", str(fx), "--runs-dir", str(tmp_path / "r"))
    assert rc == 1 and res["type"] == "done" and res["ok"] is False
    assert res["orders"] == {"done": 1, "failed": 3} and res["agent_batches"] == 2


DRYRUN_FIXTURES = REPO / "tests" / "fixtures" / "dryrun"


def test_dry_run_end_to_end_with_real_steps(tmp_env: SimpleNamespace, capsys: pytest.CaptureFixture,
                                            tmp_path: Path) -> None:
    """SPEC §10: the default fixtures drive the REAL _steps / validate / render_memo to a memo."""
    assert (DRYRUN_FIXTURES / "agent_outputs").is_dir(), "tests/fixtures/dryrun is missing"
    rc, res = run_cli(capsys, "dry-run", "--runs-dir", str(tmp_path / "runs"))
    assert rc == 0, res
    assert res["type"] == "done" and res["ok"] is True, res
    assert res["fixtures_missing"] == [], res["fixtures_missing"]
    assert res["memo_md"] and Path(res["memo_md"]).is_file()
    assert res["memo_html"] and Path(res["memo_html"]).is_file()


# =====================================================================================
# validate-config / scorecard
# =====================================================================================


def test_validate_config_ok_and_errors(tmp_env: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    rc, out = run_cli(capsys, "validate-config")
    assert rc == 1 and "portfolio.yaml" in out and "/committee-setup" in out
    rc, out = run_cli(capsys, "validate-config", "--allow-template")
    assert rc == 0 and out == "ok"
    tmp_env.use_sample_portfolio()
    rc, out = run_cli(capsys, "validate-config")
    assert rc == 0 and out == "ok"
    rc, out = run_cli(capsys, "validate-config", "--json")
    assert rc == 0 and out["ok"] is True and out["errors"] == []


def test_scorecard_writes_markdown(tmp_env: SimpleNamespace, capsys: pytest.CaptureFixture,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def score(ledger_path: Any = None, **kw: Any) -> dict:
        calls.append((ledger_path, kw))
        return {"as_of": "2026-10-02", "rows": [{"ticker": "KO"}, {"ticker": "PG"}],
                "summary": {"fundamental": {"BUY": {"n": 3, "hit_rate": 0.67, "mean_excess_spy_pp": 2.5,
                                                    "mean_excess_sector_pp": -1.25}},
                            "committee": {"SELL": {"n": 1, "hit_rate": 1.0, "mean_excess_spy_pp": -4.0,
                                                   "mean_excess_sector_pp": None}}}}

    monkeypatch.setattr(committee, "_ledger_module", lambda: SimpleNamespace(score=score))
    rc, res = run_cli(capsys, "scorecard")
    assert rc == 0 and res["type"] == "scorecard" and res["n_rows"] == 2
    md_path = tmp_env.ledger.parent / "scorecard_2026-10-02.md"
    assert res["scorecard_md"] == md_path.as_posix()
    assert Path(calls[0][0]) == tmp_env.ledger
    md = md_path.read_text(encoding="utf-8")
    assert md.startswith("# Recommendation scorecard — 2026-10-02")
    assert "| Role | Action | N | Hit rate |" in md
    assert "| fundamental | BUY | 3 | 67% | 2.5 | -1.25 |" in md
    assert "| committee | SELL | 1 | 100% | -4 | – |" in md
    assert md.index("| committee |") < md.index("| fundamental |")


def test_scorecard_prefers_ledger_renderer(tmp_env: SimpleNamespace, capsys: pytest.CaptureFixture,
                                           monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = SimpleNamespace(score=lambda path=None, **kw: {"summary": {}, "rows": []},
                           render_scorecard_md=lambda result: "# from ledger\n")
    monkeypatch.setattr(committee, "_ledger_module", lambda: fake)
    out = tmp_path / "card.md"
    rc, res = run_cli(capsys, "scorecard", "--out", str(out))
    assert rc == 0 and res["scorecard_md"] == out.as_posix() and res["n_rows"] == 0
    assert out.read_text(encoding="utf-8") == "# from ledger\n"


def test_script_runs_as_cli(tmp_env: SimpleNamespace) -> None:
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "committee.py"), "validate-config",
                           "--allow-template", "--config-dir", str(tmp_env.config_dir)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_sector_head_order_mentions_phase(env: SimpleNamespace) -> None:
    spec = {"order_id": "p3-sh-health_care", "step": "p3.sector_heads", "agent_type": "sector-head",
            "agent_role": "sector_head", "mode": "phase3", "ticker": None, "sector": "health_care",
            "sector_class": None, "inputs": [], "output": "C:/r/phase3/health_care_verdict.json",
            "schema": "sector_verdict", "seed_sources": [], "required_fields": [], "word_limits": {},
            "notes": ""}
    rec = {"attempt": 1, "output": spec["output"], "order_path": "C:/r/orders/p3-sh-health_care.md"}
    fm = committee.front_matter(spec, rec, run_id=RUN_ID, as_of="2026-10-02", prior_errors=[])
    assert committee.schema_errors(fm, "work_order") == []
    cfg = common.committee_cfg()
    text = committee.render_order_text(fm, spec, cfg, order_path=rec["order_path"])
    assert "set `phase` to 3" in text
    assert f"At most {cfg['phase3']['max_buys_per_sector']} BUY verdicts" in text
    assert "`mode`" not in text.split("## Output", 1)[1].split("## Rules", 1)[0]


def test_scorecard_rows_accepts_flat_and_list_shapes() -> None:
    flat = committee.scorecard_rows({"summary": {"technical|ADD": {"n": 2, "hit_rate": 0.5}}})
    assert flat == [{"role": "technical", "action": "ADD", "n": 2, "hit_rate": 0.5}]
    listed = committee.scorecard_rows({"summary": [{"role": "osint", "action": "bearish", "n": 1}]})
    assert listed == [{"role": "osint", "action": "bearish", "n": 1}]
    assert committee.scorecard_rows({}) == []
