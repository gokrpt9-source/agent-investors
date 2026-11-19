"""Fixture-based schema tests (U6): every agent-output schema, the failed-report stub and every
data contract has valid and invalid fixture files under ``tests/fixtures/schemas/``:

    valid/<schema>/<case>.json      must produce no validation errors
    invalid/<schema>/<defect>.json  must produce at least one error containing the fragment
                                    listed for it in invalid/expected.json

Validation goes through ``scripts.validate`` (registry built from schemas/*.json, Draft 2020-12 +
FormatChecker, x-max-words / x-min-words, and the semantic checks that need no context), so the
invalid set covers both pure schema defects and context-free semantic defects (word counts,
quoted spans, unknown source ids, probability / weight sums, ...)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import common, validate

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "schemas"
VALID_DIR = FIXTURES / "valid"
INVALID_DIR = FIXTURES / "invalid"
EXPECTED = json.loads((INVALID_DIR / "expected.json").read_text(encoding="utf-8"))

AGENT_OUTPUT_SCHEMAS = ["specialist_report", "devils_advocate_report", "etf_review", "sector_verdict",
                        "macro_view", "cio_allocation", "cio_final", "failed_report"]
DATA_SCHEMAS = list(common.DATA_SCHEMA_NAMES)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.json") if p.name != "expected.json")


def _case_id(p: Path) -> str:
    return f"{p.parent.name}/{p.stem}"


def _load(p: Path) -> object:
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- coverage of the fixture set
def test_every_schema_has_fixtures() -> None:
    missing = []
    for name in AGENT_OUTPUT_SCHEMAS:
        n_valid = len(list((VALID_DIR / name).glob("*.json")))
        n_invalid = len(list((INVALID_DIR / name).glob("*.json")))
        if n_valid < 1 or n_invalid < 2:
            missing.append(f"{name}: {n_valid} valid / {n_invalid} invalid (need >= 1 / >= 2)")
    for name in DATA_SCHEMAS:
        n_valid = len(list((VALID_DIR / name).glob("*.json")))
        n_invalid = len(list((INVALID_DIR / name).glob("*.json")))
        if n_valid < 1 or n_invalid < 1:
            missing.append(f"{name}: {n_valid} valid / {n_invalid} invalid (need >= 1 / >= 1)")
    assert not missing, "\n".join(missing)


def test_fixture_dirs_are_known_schemas() -> None:
    for root in (VALID_DIR, INVALID_DIR):
        for d in root.iterdir():
            if d.is_dir():
                assert validate.resolve_schema_name(d.name) == d.name


def test_every_invalid_fixture_has_an_expected_fragment() -> None:
    keys = {f"{p.parent.name}/{p.name}" for p in _files(INVALID_DIR)}
    listed = {k for k in EXPECTED if not k.startswith("_")}
    assert keys == listed, f"unlisted: {sorted(keys - listed)}; stale: {sorted(listed - keys)}"


def test_specialist_fixtures_cover_every_role() -> None:
    roles = {_load(p)["agent_role"] for p in (VALID_DIR / "specialist_report").glob("*.json")}
    assert roles == set(common.SPECIALIST_ROLES)


# --------------------------------------------------------------------------- known schema gaps
def _manifest_mode_is_still_a_string() -> bool:
    """schemas/data/manifest.schema.json types ``mode`` as a string, while the integration
    MANIFEST SHAPE (written by committee.py) is ``{"dry_run","quick","sectors","holding"}``."""
    schema = json.loads(common.schema_path("manifest").read_text(encoding="utf-8"))
    return (schema.get("properties", {}).get("mode") or {}).get("type") == "string"


# case id -> (gap still open?, reason). While a gap is open the affected fixture xfails (listed in
# the pytest summary, never silently green); once the schema owner fixes the schema the fixture
# runs for real and must pass, with no edit needed here.
KNOWN_SCHEMA_GAPS = {
    "manifest/full_run": (_manifest_mode_is_still_a_string,
                          "manifest.schema.json types mode as string; the integration manifest mode is an object"),
    "manifest/mode_string": (_manifest_mode_is_still_a_string,
                             "manifest.schema.json still accepts the legacy string mode"),
}


def _xfail_if_known_gap(path: Path) -> None:
    gap = KNOWN_SCHEMA_GAPS.get(_case_id(path))
    if gap is not None and gap[0]():
        pytest.xfail(f"known schema gap (U0 / lead): {gap[1]}")


def test_known_schema_gaps_name_real_fixtures() -> None:
    cases = {_case_id(p) for p in _files(VALID_DIR) + _files(INVALID_DIR)}
    assert set(KNOWN_SCHEMA_GAPS) <= cases


def test_manifest_fixtures_use_the_integration_mode_object() -> None:
    """Pin the manifest fixtures to the integration MANIFEST SHAPE (committee.py writes it)."""
    for p in list((VALID_DIR / "manifest").glob("*.json")) + list((INVALID_DIR / "manifest").glob("*.json")):
        mode = _load(p)["mode"]
        if p.stem == "mode_string":
            assert isinstance(mode, str)
            continue
        assert isinstance(mode, dict) and set(mode) == {"dry_run", "quick", "sectors", "holding"}, p.name
    full = _load(VALID_DIR / "manifest" / "full_run.json")
    assert full["orders"] and {s["status"] for s in full["steps"]} >= {"done", "dispatched"}


# --------------------------------------------------------------------------- valid / invalid
@pytest.mark.parametrize("path", _files(VALID_DIR), ids=_case_id)
def test_valid_fixture_has_no_errors(path: Path) -> None:
    errors = validate.validate_file(path, path.parent.name)
    if errors:
        _xfail_if_known_gap(path)
    assert errors == [], "\n".join(errors)


@pytest.mark.parametrize("path", _files(INVALID_DIR), ids=_case_id)
def test_invalid_fixture_is_rejected_for_the_named_defect(path: Path) -> None:
    errors = validate.validate_file(path, path.parent.name)
    if not errors:
        _xfail_if_known_gap(path)
    assert errors, f"{_case_id(path)} unexpectedly valid"
    fragment = EXPECTED[f"{path.parent.name}/{path.name}"]
    assert any(fragment in e for e in errors), f"no error mentions {fragment!r}:\n" + "\n".join(errors)


@pytest.mark.parametrize("path", _files(INVALID_DIR), ids=_case_id)
def test_error_messages_start_with_a_json_path(path: Path) -> None:
    for e in validate.validate_file(path, path.parent.name):
        assert e.startswith("$"), e
        assert ": " in e, e


@pytest.mark.parametrize("path", _files(VALID_DIR), ids=_case_id)
def test_schema_is_inferred_from_the_document(path: Path) -> None:
    assert validate.infer_schema(_load(path), path) == path.parent.name


# --------------------------------------------------------------------------- registry / names
@pytest.mark.parametrize("name, expected", [
    ("specialist_report", "specialist_report"),
    ("specialist_report.json", "specialist_report"),
    ("data/prices", "prices"),
    ("prices/v1", "prices"),
    ("urn:agent-investors:data/aggregate", "aggregate"),
    ("urn:agent-investors:cio_final", "cio_final"),
    ("C:/x/agent-investors/schemas/data/technicals.schema.json", "technicals"),
    ("C:\\x\\agent-investors\\schemas\\sector_verdict.json", "sector_verdict"),
    ("config_committee", "config_committee"),
])
def test_resolve_schema_name(name: str, expected: str) -> None:
    assert validate.resolve_schema_name(name) == expected


def test_unknown_schema_name_is_an_error_not_a_crash() -> None:
    with pytest.raises(KeyError):
        validate.resolve_schema_name("no_such_schema")
    errors = validate.validate_doc({}, "no_such_schema")
    assert len(errors) == 1 and "unknown schema" in errors[0]


def test_every_schema_compiles_through_the_registry() -> None:
    for name in common.SCHEMA_FILES:
        if name == "common_defs":
            continue
        assert validate.get_validator(name) is validate.get_validator(name)  # cached


def test_cross_file_refs_resolve_via_registry() -> None:
    """A $ref into common_defs (source.url pattern) is enforced inside an agent schema."""
    doc = _load(VALID_DIR / "specialist_report" / "fundamental_example.json")
    doc["sources"][0]["url"] = "file:///C:/data/x.json"
    errors = validate.schema_errors(doc, "specialist_report")
    assert any(e.startswith("$.sources[0].url:") for e in errors), errors


def test_schema_errors_only_skips_semantic_checks() -> None:
    doc = _load(INVALID_DIR / "macro_view" / "probabilities_sum_0_9.json")
    assert validate.schema_errors(doc, "macro_view") == []
    assert validate.validate_doc(doc, "macro_view")


def test_config_schemas_are_reachable() -> None:
    cfg = common.load_config("committee")
    assert validate.validate_doc(cfg, "config_committee") == []


def test_committee_schema_pins_risk_reward_min_downside_pct() -> None:
    import copy
    cfg = copy.deepcopy(common.load_config("committee"))
    assert cfg["risk_reward"]["min_downside_pct"] == 1.0
    assert validate.validate_doc(cfg, "config_committee") == []
    assert common.validate_config("committee", cfg) == []
    for bad in (-0.5, 101, "1.0", None, True):
        cfg["risk_reward"]["min_downside_pct"] = bad
        errors = validate.validate_doc(cfg, "config_committee")
        assert any("risk_reward.min_downside_pct" in e for e in errors), (bad, errors)
    for ok in (0, 0.5, 1, 2.5, 100):                    # 0 disables the guard
        cfg["risk_reward"]["min_downside_pct"] = ok
        assert validate.validate_doc(cfg, "config_committee") == [], ok
    del cfg["risk_reward"]["min_downside_pct"]          # optional: the code falls back to 1.0
    assert validate.validate_doc(cfg, "config_committee") == []
