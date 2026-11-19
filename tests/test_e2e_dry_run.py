"""End-to-end dry run (SPEC §10): ``committee.py dry-run`` with the default fixtures in
``tests/fixtures/dryrun`` drives the REAL ``_steps`` / ``validate`` / ``aggregate`` / ``portfolio`` /
``render_memo`` / ``ledger`` code fully offline to a complete memo.

The pipeline runs once per session (``_dry_run``) into a temp runs dir; the tests then inspect
the run directory. Regenerate the fixtures with ``python tests/fixtures/dryrun/build_fixtures.py``.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT
from scripts import committee, common, render_memo

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "dryrun"
AGENT_OUTPUTS = FIXTURES / "agent_outputs"
REAL_LEDGER = REPO_ROOT / "ledger" / "recommendations.csv"
TITLES = [str(t) for t in render_memo.SECTION_TITLES.values()]

_CACHE: dict[str, Any] = {}


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@pytest.fixture
def dry(tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture) -> dict:
    """Run ``committee.py dry-run`` once (hermetic env of the first test that asks for it)."""
    if "result" not in _CACHE:
        root = tmp_path_factory.mktemp("e2e_dry_run")
        before = _sha(REAL_LEDGER)
        capsys.readouterr()
        rc = committee.main(["dry-run", "--runs-dir", str(root / "runs"), "--cache-dir", str(root / "cache")])
        out = capsys.readouterr().out.strip().splitlines()
        res = json.loads(out[-1])
        run_dir = Path(res["run_dir"])
        _CACHE["result"] = {
            "rc": rc, "res": res, "root": root, "run_dir": run_dir,
            "ledger_before": before, "ledger_after": _sha(REAL_LEDGER),
            "md": common.read_text(res["memo_md"]) if res.get("memo_md") else "",
            "html": common.read_text(res["memo_html"]) if res.get("memo_html") else "",
            "manifest": committee.load_manifest(run_dir),
        }
    return _CACHE["result"]


def _section(md: str, num: int) -> str:
    start = md.index(f"## {TITLES[num - 1]}")
    nxt = md.find("\n## ", start + 3)
    return md[start: nxt if nxt != -1 else len(md)]


def _fixture(oid: str) -> dict:
    return json.loads(common.read_text(AGENT_OUTPUTS / f"{oid}.json"))


# =====================================================================================
# the run itself
# =====================================================================================


def test_dry_run_completes_with_zero_failed_orders(dry: dict) -> None:
    res = dry["res"]
    assert dry["rc"] == 0, res
    assert res["type"] == "done" and res["ok"] is True and res["dry_run"] is True
    assert res["fixtures_missing"] == []
    assert res["orders"]["failed"] == 0 and res["orders"]["done"] > 0
    orders = dry["manifest"]["orders"]
    assert orders and all(o["status"] == "done" and o["attempt"] == 1 for o in orders.values()), \
        {k: (o["status"], o["attempt"]) for k, o in orders.items() if o["status"] != "done" or o["attempt"] != 1}
    assert dry["manifest"]["status"] == "done"
    steps = {s["name"]: s["status"] for s in dry["manifest"]["steps"]}
    assert all(v == "done" for v in steps.values()), steps
    # one fixture per order, nothing unused
    assert {p.stem for p in AGENT_OUTPUTS.glob("*.json")} == set(orders)


def test_run_lives_under_the_requested_runs_dir_with_isolated_cache(dry: dict) -> None:
    run_dir = dry["run_dir"]
    assert run_dir.parent == (dry["root"] / "runs").resolve()
    assert run_dir.name.endswith("_dryrun")
    paths = dry["manifest"]["paths"]
    assert Path(paths["cache_dir"]) == run_dir / ".cache"
    assert Path(paths["ledger"]) == run_dir / "phase4" / "ledger_preview.csv"
    assert dry["manifest"]["env"].get(common.ENV_OFFLINE) == "1"


def test_phase3_expansion_produced_a_buy_and_passes(dry: dict) -> None:
    m = dry["manifest"]
    assert m["expand_sectors"] == ["health_care"]
    assert len(m["candidates"]["health_care"]) >= 4 and m["finalists"]["health_care"]
    verdict = common.read_json(dry["run_dir"] / "phase3" / "health_care_verdict.json")
    actions = {v["ticker"]: v["action"] for v in verdict["verdicts"]}
    assert "BUY" in actions.values() and "PASS" in actions.values()


# =====================================================================================
# memo
# =====================================================================================


def test_memo_files_exist_with_all_sections_in_order(dry: dict) -> None:
    md, page = dry["md"], dry["html"]
    assert Path(dry["res"]["memo_md"]).is_file() and Path(dry["res"]["memo_html"]).is_file()
    assert Path(dry["res"]["memo_md"]).parent == dry["run_dir"] / "phase4"
    pos = [md.find(f"## {t}") for t in TITLES]
    assert all(p >= 0 for p in pos), dict(zip(TITLES, pos))
    assert pos == sorted(pos)
    hpos = []
    for t in TITLES:
        cands = [page.find(f"<h2>{x}</h2>") for x in (t, html.escape(t), html.escape(t, quote=False))]
        hpos.append(max(cands))
    assert all(p >= 0 for p in hpos), dict(zip(TITLES, hpos))
    assert hpos == sorted(hpos)


def test_disclaimer_comes_first(dry: dict) -> None:
    md = dry["md"]
    first_h2 = re.search(r"^## (.+)$", md, re.M)
    assert first_h2 and first_h2.group(1) == TITLES[0]
    assert md.index(common.DISCLAIMER) < md.index(f"## {TITLES[1]}")
    body = dry["html"]
    assert body.index(TITLES[0]) < body.index(html.escape(TITLES[1], quote=False))


def test_every_section_is_populated(dry: dict) -> None:
    md = dry["md"]
    for num in range(1, 12):
        sec = _section(md, num)
        lines = [ln for ln in sec.splitlines()[1:] if ln.strip()]
        assert len(lines) >= 3, f"section {num} is (nearly) empty"
    assert "| QQQ |" in _section(md, 5) and "| PG |" in _section(md, 5)
    trades = _section(md, 7)
    assert re.search(r"\| \S+ \| BUY \| [1-9]", trades) and re.search(r"\| \S+ \| SELL \| [1-9]", trades)
    assert "broken" in _section(md, 8) and "damaged" in _section(md, 8)
    assert "Low-evidence" in _section(md, 10) or "Missing data" in _section(md, 10)
    assert "fred.stlouisfed.org" in _section(md, 11) and "data.sec.gov" in _section(md, 11)


def test_section_6_has_a_buy_card(dry: dict) -> None:
    sec = _section(dry["md"], 6)
    cards = re.findall(r"^### (\S+) — .+ · BUY$", sec, re.M)
    assert cards, sec[:500]
    assert "Fair value (low / base / high)" in sec and "Risk/reward" in sec


def test_section_9_lists_the_rule_deviation_and_the_cio_override(dry: dict) -> None:
    sec = _section(dry["md"], 9)
    verdict = _fixture("p2-sh-consumer_staples")
    devs = [v for v in verdict["verdicts"] if v["rule_deviation"]]
    assert devs, "the fixture must contain a sector-head rule deviation"
    for v in devs:
        assert len(v["deviation_justification"].split()) >= 15
        assert v["deviation_justification"] in sec
        assert v["da_rebuttal"] and v["da_rebuttal"] in sec
    cio = _fixture("p4-cio")
    alloc = _fixture("p1-cio")
    assert alloc["constraint_overrides"] and cio["constraint_overrides"]
    for o in cio["constraint_overrides"] + alloc["constraint_overrides"]:
        assert len(o["justification"].split()) >= 15
        assert o["justification"] in sec


# =====================================================================================
# ledger, fixtures hygiene
# =====================================================================================


def test_ledger_preview_has_rows_and_real_ledger_is_untouched(dry: dict) -> None:
    preview = dry["run_dir"] / "phase4" / "ledger_preview.csv"
    assert preview.is_file()
    lines = [ln for ln in common.read_text(preview).splitlines() if ln.strip()]
    assert len(lines) > 1 and lines[0].startswith("run_id,rec_date")
    assert all(ln.startswith(dry["res"]["run_id"]) for ln in lines[1:])
    assert dry["ledger_before"] == dry["ledger_after"]


def test_agent_output_fixtures_are_portable() -> None:
    """Fixtures carry ${RUN_DIR} / ${REPO_ROOT} tokens, never machine-specific paths."""
    files = sorted(AGENT_OUTPUTS.glob("*.json"))
    assert files
    bad = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/tmp/|/home/|/Users/|AppData")
    for f in files:
        text = common.read_text(f)
        assert not bad.search(text), f.name
        json.loads(text)
    assert any("${RUN_DIR}" in common.read_text(f) for f in files)


# =====================================================================================
# first-live-run review: summary digest, memo columns/labels, config edited mid-run
# =====================================================================================


def test_summary_json_lists_sources_and_missing_data_from_the_memo_context(dry: dict) -> None:
    summary = common.read_json(dry["run_dir"] / "phase4" / "summary.json")
    urls = [s["url"] for s in summary["sources"]]
    assert urls and len(urls) == len(set(urls)), "sources de-duplicated by URL"
    assert summary["sources"][0]["kind"] == "dataset" and summary["sources"][0]["ids"]
    assert any(s["kind"] == "web" for s in summary["sources"]), "the agents' web sources are included"
    # the same URLs the memo's appendix lists
    appendix = _section(dry["md"], 11)
    assert all(u in appendix for u in urls[:10])
    assert summary["missing_data_count"] >= len(summary["missing_data"]) > 0
    assert len(summary["missing_data"]) <= 20 and all(isinstance(m, str) and ": " in m for m in summary["missing_data"])
    assert summary["config_drift"] == []


def test_memo_shows_fair_value_and_stop_columns_and_separate_cash_labels(dry: dict) -> None:
    s5, s4 = _section(dry["md"], 5), _section(dry["md"], 4)
    header = next(ln for ln in s5.splitlines() if ln.startswith("| Ticker |"))
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert cols[cols.index("R/R"):cols.index("R/R") + 3] == ["R/R", "Fair value (base)", "Stop"]
    assert "Cash floor (profile)" in s4 and "Cash target (CIO)" in s4
    assert "Cash (current / target / post-trade)" not in s4
    # no stray negative ratio: those are shown as n/m with the raw value in the footnote
    for line in s5.splitlines():
        if line.startswith("| ") and not line.startswith("| Ticker") and not line.startswith("| ---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            assert not re.fullmatch(r"-\d+(\.\d+)?", cells[cols.index("R/R")]), line
    s7 = _section(dry["md"], 7)
    # the old "USD-denominated = 100 - cash" line is replaced by an exposure breakdown by listing currency
    assert "USD-denominated positions" not in s7
    assert "USD-listed holdings" in s7 and "Economic USD exposure (securities)" in s7


def test_unchanged_config_raises_no_drift_warning(dry: dict) -> None:
    assert "config_drift" not in dry["manifest"]
    assert not any("changed after this run started" in w for w in dry["manifest"]["warnings"])
    assert "Configuration changed during the run" not in dry["md"]


def test_a_config_edited_mid_run_is_reported_in_the_manifest_log_memo_and_summary(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The first live run started before the investor profile was edited and nobody noticed: the
    agents had decided on the old profile. The run is not aborted, but it must say so."""
    import shutil

    cfg = tmp_path / "live_config"
    shutil.copytree(FIXTURES / "config", cfg)
    original = committee.simulate_agents
    edited: list[bool] = []

    def edit_then_simulate(run_dir, payload, fixtures_root):
        if not edited:                       # after init, while the run is in flight
            (cfg / "investor_profile.yaml").write_text(
                (cfg / "investor_profile.yaml").read_text(encoding="utf-8") + "\n# edited mid-run\n",
                encoding="utf-8")
            edited.append(True)
        return original(run_dir, payload, fixtures_root)

    monkeypatch.setattr(committee, "simulate_agents", edit_then_simulate)
    capsys.readouterr()
    rc = committee.main(["dry-run", "--runs-dir", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache"),
                         "--config-dir", str(cfg)])
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and res["ok"] is True, res            # not aborted

    run_dir = Path(res["run_dir"])
    msg = ("config investor_profile.yaml changed after this run started — decisions used the snapshot in "
           "inputs/")
    m = committee.load_manifest(run_dir)
    assert [d["file"] for d in m["config_drift"]] == ["investor_profile.yaml"]
    assert any(w.endswith(msg) for w in m["warnings"]), m["warnings"]
    assert f"WARNING {msg}" in common.read_text(run_dir / "run_log.md")
    memo = common.read_text(run_dir / "phase4" / "memo.md")
    s10 = memo[memo.index("## " + TITLES[9]):memo.index("## " + TITLES[10])]
    assert "### Configuration changed during the run" in s10 and f"- {msg}" in s10
    assert msg in html.unescape(common.read_text(run_dir / "phase4" / "memo.html"))
    assert common.read_json(run_dir / "phase4" / "summary.json")["config_drift"] == [msg]
    # the snapshot, not the edited file, is what the agents saw
    assert "edited mid-run" not in common.read_text(run_dir / "inputs" / "investor_profile.yaml")
    # status reports it too
    rc = committee.main(["status", "--run", res["run_id"], "--runs-dir", str(tmp_path / "runs")])
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and [d["file"] for d in st["config_drift"]] == ["investor_profile.yaml"]
