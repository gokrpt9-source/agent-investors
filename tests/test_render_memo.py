"""Tests for scripts/render_memo.py (DESIGN §4.14).

Fully offline: every test renders a copy of ``tests/fixtures/memo_run`` (a complete minimal run
built from ``schemas/examples`` and the DESIGN payload shapes, with a manifest ``orders`` map)
or a stripped-down run directory under ``tmp_path``. Fixture data is illustrative only.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR
from scripts import common, render_memo

FIXTURE_RUN = FIXTURES_DIR / "memo_run"
TITLES = list(render_memo.SECTION_TITLES.values())
NOW = "2026-10-03T10:45:00+00:00"


# =====================================================================================
# helpers
# =====================================================================================


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of the fixture run (outputs never land in tests/fixtures)."""
    dst = tmp_path / "runs" / "2026-10-03_1015"
    shutil.copytree(FIXTURE_RUN, dst)
    monkeypatch.setenv(common.ENV_NOW, NOW)
    return dst


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _positions(text: str, needles: list[str]) -> list[int]:
    return [text.find(n) for n in needles]


def _assert_in_order(text: str, needles: list[str]) -> None:
    pos = _positions(text, needles)
    missing = [n for n, p in zip(needles, pos) if p < 0]
    assert not missing, f"missing: {missing}"
    assert pos == sorted(pos), f"out of order: {list(zip(needles, pos))}"


def _md_section(md: str, num: int) -> str:
    start = md.index(f"## {render_memo.SECTION_TITLES[num]}")
    nxt = md.find("\n## ", start + 1)
    return md[start: nxt if nxt >= 0 else len(md)]


def _html_section(html: str, num: int) -> str:
    m = re.search(rf'<section id="s{num}">(.*?)</section>', html, re.S)
    assert m, f"section s{num} missing"
    return m.group(1)


def _md_tables(md: str) -> list[list[str]]:
    tables, cur = [], []
    for line in md.splitlines():
        if line.startswith("|"):
            cur.append(line)
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    return tables


def _unescaped_pipes(line: str) -> int:
    return len(re.findall(r"(?<!\\)\|", line))


def _fixture_json(rel: str) -> dict:
    return json.loads((FIXTURE_RUN / rel).read_text(encoding="utf-8"))


# =====================================================================================
# full memo
# =====================================================================================


def test_full_render_writes_md_and_html_with_all_11_headings_in_order(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    assert md_path == run_dir / "phase4" / "memo.md"
    assert html_path == run_dir / "phase4" / "memo.html"
    md, html = _read(md_path), _read(html_path)
    _assert_in_order(md, [f"## {t}" for t in TITLES])
    # raw HTML (titles are trusted constants, valid HTML5 text) and the unescaped text
    _assert_in_order(html, [f"<h2>{t}</h2>" for t in TITLES])
    _assert_in_order(htmllib.unescape(html), TITLES)
    assert md.count("\n## ") == 11


def test_disclaimer_comes_first_in_both_formats(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    md, html = _read(md_path), _read(html_path)
    first_h2 = re.search(r"^## (.+)$", md, re.M)
    assert first_h2 and first_h2.group(1) == TITLES[0]
    assert md.index(common.DISCLAIMER) < md.index(f"## {TITLES[1]}")
    assert md.index(f"## {TITLES[0]}") < md.index(common.DISCLAIMER)
    first_html_h2 = re.search(r"<h2>(.*?)</h2>", html)
    assert first_html_h2 and first_html_h2.group(1) == TITLES[0]
    assert common.DISCLAIMER in _html_section(html, 1)
    assert html.index(common.DISCLAIMER) < html.index(TITLES[1])


def test_deviation_and_override_justifications_appear_in_section_9(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    md9, html9 = _md_section(_read(md_path), 9), htmllib.unescape(_html_section(_read(html_path), 9))
    pg = next(v for v in _fixture_json("phase2/consumer_staples_verdict.json")["verdicts"] if v["ticker"] == "PG")
    jpm = next(a for a in _fixture_json("phase4/cio_final.json")["final_actions"] if a["ticker"] == "JPM")
    co = _fixture_json("phase1/cio_allocation.json")["constraint_overrides"][0]
    for text in (pg["deviation_justification"], jpm["override_justification"], co["justification"]):
        assert text in md9
        assert text in html9
    assert "Sector-head rule deviations" in md9 and "CIO action overrides" in md9
    assert "CIO macro-bound overrides" in md9
    # the identical allocation/final constraint override is listed once, with both stages
    assert md9.count(co["justification"]) == 1
    assert "Phase 1 allocation, Phase 4 final" in md9


def test_html_is_self_contained_and_escaped(run_dir: Path) -> None:
    _, html_path = render_memo.render(run_dir)
    html = _read(html_path)
    low = html.lower()
    assert "<script" not in low
    assert "<link" not in low
    assert "@import" not in low and "url(" not in low
    assert " src=" not in low
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html and "prefers-color-scheme: dark" in html and "@media print" in html
    # hostile agent text (a source title carrying a script tag) is escaped, never executed
    assert "&lt;script&gt;" in html
    # every hyperlink is an in-page anchor or a plain http(s) URL
    for href in re.findall(r'href="([^"]*)"', html):
        assert href.startswith("#") or re.match(r"https?://", href), href


def test_markdown_table_cells_escape_pipes_and_newlines(run_dir: Path) -> None:
    def rationale(doc):
        for v in doc["verdicts"]:
            if v["ticker"] == "KO":
                v["rationale"] = "Distribution coverage | cash conversion looks thin on a full-year view."

    _mutate(run_dir, "phase2/consumer_staples_verdict.json", rationale)
    md_path, _ = render_memo.render(run_dir)
    md = _read(md_path)
    # the sector head's own sentence contains a literal "|" -> escaped in the cell; the labelled
    # parts of the Why cell are separate lines (<br>) inside ONE table row
    ko_row = next(line for line in _md_section(md, 5).splitlines() if line.startswith("| KO |"))
    assert "coverage \\| cash conversion" in ko_row
    assert "**Call:**" in ko_row and "<br>**Why:**" in ko_row and "<br>**Watch:**" in ko_row
    # a missing_data item containing a newline stays on one table row
    assert "| Unit case volume by segment for the latest quarter (not in companyfacts) |" in md
    # every row of every table has the same number of unescaped column separators
    tables = _md_tables(md)
    assert len(tables) >= 15
    for table in tables:
        counts = {_unescaped_pipes(line) for line in table}
        assert len(counts) == 1, table


def test_section_content_from_every_phase(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    md = _read(md_path)
    for item in _fixture_json("phase4/cio_final.json")["executive_summary"]:
        assert item in _md_section(md, 2)
    s3 = _md_section(md, 3)
    assert "late-cycle disinflation" in s3 and "| Soft landing | 50% |" in s3
    assert "| Information Technology | 10.0% | 22.0% |" in s3
    assert "DFF — Effective Federal Funds Rate | 3.88% | 2026-10-02 | -0.25 pp" in s3
    s4 = _md_section(md, 4)
    assert "$52,000 (as of 2026-10-02)" in s4
    assert "| JPM | stock | Financials | 13.8% | 9.2% | -4.6 pp | TRIM |" in s4
    s5 = _md_section(md, 5)
    for t in ("QQQ", "KO", "PG", "JPM", "BRK-B"):
        assert f"| {t} |" in s5
    assert "| JPM | Financials | TRIM | HOLD | 6.1 | 7.0 | 1.47 |" in s5
    assert "| MRK |" not in s5  # candidates belong to section 6
    s6 = _md_section(md, 6)
    assert "### MRK — Health Care · BUY" in s6
    assert "$96.00 / $128.00 / $150.00" in s6
    assert "Course NOPAT DCF" in s6  # method from the phase-3 fundamental report
    assert "2.56 (upside 30.0%, downside 11.7%)" in s6  # 29.95 -> 30.0 (ROUND_HALF_UP)
    assert "Patent-cliff timing" in s6  # dissent
    # a light-review non-finalist (phase3/aggregate_light.json) is listed, never given a verdict
    assert ("| ABBV | Health Care | — | PASS | 5.5 | 8.5 | 0.67 | $190.00 | $170.00 | "
            "Light review only — not selected as a finalist. |") in s6
    s7 = _md_section(md, 7)
    assert "| JPM | SELL | 10 | $2,400 | ILS 8,910 | TRIM | partial | 33% | \u2014 |" in s7
    assert "below min_trade_usd (USD 150 < 200)" in s7
    # a trim whose trade was dropped is a HOLD in the plan, everywhere an action is shown
    assert "| BRK-B | SELL | 0 | $0 | ILS 0 | TRIM -> HOLD (below the minimum trade size) |" in s7
    assert "TRIM -> HOLD (below the minimum trade size)" in _md_section(md, 4)
    assert _row(s5, "BRK-B")[2] == "HOLD", "section 5 shows the CIO's final action; the plan also holds"
    assert "| max_sector_pct | warn |" in s7
    assert "| JPM | $900 | $225 |" in s7
    assert "flat 25%" in s7 and "real gain" in s7
    assert "3.7123 ILS/USD (as of 2026-10-02)" in s7
    assert "- MRK: scaled pro-rata x0.9333 to keep cash >= 5%" in s7  # per-position plan notes
    s8 = _md_section(md, 8)
    assert "thesis damaged" in s8 and "Kill criteria" in s8
    assert "Free cash flow covers the dividend" in s8  # the high-severity attack on KO
    assert "Capital-return record justifies a higher conviction." in s8  # sector-head dissent
    assert "No dissent changed a final action." in s8  # CIO dissent summary
    # identical figures in both formats
    html = htmllib.unescape(_read(html_path))
    for figure in ("$52,000 (as of 2026-10-02)", "ILS 8,910", "2.56 (upside 30.0%, downside 11.7%)"):
        assert figure in html


def test_reports_are_located_through_manifest_orders(run_dir: Path) -> None:
    ctx = render_memo.build_context(run_dir)
    idx = ctx["report_index"]
    # an absolute output path written on another machine is rebased onto this run dir
    da = idx[(2, "devils_advocate", "KO")]
    assert da["status"] == "ok" and da["rel"] == "phase2/reports/devils_advocate_KO.json"
    # retry output wins over the invalid attempt-1 file
    assert idx[(2, "fundamental", "JPM")]["rel"] == "phase2/reports/fundamental_JPM.r2.json"
    # failed stub (resolved_output) is reported as failed with its errors
    failed = idx[(2, "operator_ceo", "PG")]
    assert failed["status"] == "failed" and failed["stub"]["attempts"] == 2
    assert idx[(4, "cio", "reconciliation")]["status"] == "ok"
    assert set(ctx["verdicts"][2]) == {"consumer_staples", "financials", "funds"}
    assert set(ctx["verdicts"][3]) == {"health_care"}
    assert list(ctx["holdings"]) == ["QQQ", "KO", "PG", "JPM", "BRK-B"]


def test_reports_found_by_directory_scan_without_orders(run_dir: Path) -> None:
    manifest = json.loads(_read(run_dir / "manifest.json"))
    manifest.pop("orders")
    common.write_json(run_dir / "manifest.json", manifest)
    ctx = render_memo.build_context(run_dir)
    idx = ctx["report_index"]
    assert idx[(2, "fundamental", "JPM")]["rel"] == "phase2/reports/fundamental_JPM.r2.json"
    assert idx[(2, "operator_ceo", "PG")]["status"] == "failed"
    assert idx[(3, "devils_advocate", "MRK")]["status"] == "ok"
    assert ctx["phase1"]["macro_view"] is not None and ctx["cio_final"] is not None


def test_data_gaps_section_lists_failures_low_evidence_flags_and_missing_data(run_dir: Path) -> None:
    md_path, _ = render_memo.render(run_dir)
    s10 = _md_section(_read(md_path), 10)
    assert "| operator_ceo | PG | phase 2 | failed | 2 | attempt 2: output file not written |" in s10
    assert "Low-evidence reports (score below 5)" in s10  # threshold from committee.yaml
    assert "| osint | KO | 4.0 | ok |" in s10
    assert "| operator_ceo | PG | 1.0 | failed |" in s10
    assert "| BRK-B | phase 2 | 4.0 | HOLD | insufficient_evidence |" in s10
    assert "osint:F1" in s10
    assert "companyfacts_lag" in s10 and "price_fetch_failed" in s10
    assert "tag_mix" not in s10 and "1 low-severity data flags not listed." in s10
    assert "Amount reserved for the transfer-pricing case in the latest 10-Q" in s10
    assert "not yet in companyfacts" in s10  # data-envelope missing_data
    assert "industrials dropped from expansion" in s10  # manifest warning


def test_sources_appendix_dedupes_by_url_and_groups_by_kind(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    s11 = _md_section(_read(md_path), 11)
    assert "### Datasets fetched by scripts" in s11 and "### Web sources cited by agents" in s11
    assert s11.index("### Datasets") < s11.index("### Web sources")
    ko_cf = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000021344.json"
    assert s11.count(ko_cf) == 1
    assert "D:SEC:KO:CF, D:VAL:KO" in s11  # ids sharing one URL are merged
    assert s11.count("https://finance.yahoo.com/quote/SPY/history/") == 1
    html11 = _html_section(_read(html_path), 11)
    assert f'href="{ko_cf}"' in html11


def test_collect_sources_unit() -> None:
    docs = [("a", {"sources": [{"id": "W1", "url": "https://x.org/1", "title": "One", "accessed": "2026-10-01",
                                "kind": "web"},
                               {"id": "D:PX:KO", "url": "https://finance.yahoo.com/quote/KO/history/",
                                "title": "KO", "accessed": "2026-10-02", "kind": "dataset"}]}),
            ("b", {"sources": [{"id": "W3", "url": "https://x.org/1", "title": "One again", "accessed": "2026-10-03"},
                               {"id": "bad", "url": "", "title": "no url"}, "not-a-dict"]}),
            ("c", {"no_sources": True})]
    groups = render_memo.collect_sources(docs)
    assert [e["url"] for e in groups["dataset"]] == ["https://finance.yahoo.com/quote/KO/history/"]
    assert len(groups["web"]) == 1
    web = groups["web"][0]
    assert web["title"] == "One" and web["accessed"] == "2026-10-03" and web["cited_by"] == ["a", "b"]


def test_thresholds_and_tax_rate_come_from_config(run_dir: Path) -> None:
    ctx = render_memo.build_context(run_dir, cfg={"actions": {"min_evidence": 9.5}, "aggregation": {"round_dp": 2},
                                                  "risk_reward": {"round_dp": 1}},
                                    profile={"capital_gains_tax_pct": 30, "base_currency": "ILS"})
    md, _ = render_memo.render_strings(render_memo.build_memo(ctx))
    s10 = _md_section(md, 10)
    assert "Low-evidence reports (score below 9.5)" in s10
    assert "| fundamental | KO | 9.00 | ok |" in s10  # score_dp from aggregation.round_dp
    assert "| JPM | Financials | TRIM | HOLD | 6.10 | 7.00 | 1.5 |" in _md_section(md, 5)  # rr dp 1
    assert "flat 30%" in _md_section(md, 7)
    # malformed values fall back to the documented defaults instead of raising
    bad = render_memo.build_context(run_dir, cfg={"actions": {"min_evidence": "lots"}, "aggregation": {"round_dp": None}},
                                    profile={"capital_gains_tax_pct": "n/a"})
    assert bad["settings"]["min_evidence"] == render_memo.DEFAULT_MIN_EVIDENCE
    assert bad["settings"]["score_dp"] == render_memo.DEFAULT_SCORE_DP
    assert bad["settings"]["tax_pct"] == render_memo.DEFAULT_CAPITAL_GAINS_TAX_PCT
    # the repo committee.yaml / investor_profile.yaml drive the defaults in a normal run
    repo = render_memo.build_context(run_dir)["settings"]
    assert repo["min_evidence"] == common.committee_cfg()["actions"]["min_evidence"]
    assert repo["tax_pct"] == common.load_config("investor_profile")["capital_gains_tax_pct"]


# =====================================================================================
# degraded inputs
# =====================================================================================


def test_run_dir_with_only_manifest_renders_all_11_headings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(common.ENV_NOW, NOW)
    rd = tmp_path / "2026-10-03_1015"
    rd.mkdir()
    shutil.copy2(FIXTURE_RUN / "manifest.json", rd / "manifest.json")
    md_path, html_path = render_memo.render(rd)
    md, html = _read(md_path), _read(html_path)
    _assert_in_order(md, [f"## {t}" for t in TITLES])
    _assert_in_order(htmllib.unescape(html), TITLES)
    assert md.index(common.DISCLAIMER) < md.index(TITLES[1])
    for n in (2, 3, 4, 7, 11):
        assert render_memo.NA_TEXT in _md_section(md, n)
    # holdings come from the manifest, but nothing is fabricated for them
    assert re.search(r"\| KO \| Consumer Staples \| n/a \| n/a \| n/a \| n/a \| n/a \| n/a \| n/a \|( [^|]+ \|)? "
                     r"No verdict in this run\. \|", _md_section(md, 5))
    # orders marked done whose outputs are absent are reported as data gaps
    assert "| fundamental | KO | phase 2 | missing |" in _md_section(md, 10)


def test_empty_run_dir_renders_without_manifest(tmp_path: Path) -> None:
    rd = tmp_path / "empty_run"
    rd.mkdir()
    md_path, html_path = render_memo.render(rd)
    md = _read(md_path)
    _assert_in_order(md, [f"## {t}" for t in TITLES])
    assert md.count(render_memo.NA_TEXT) >= 8
    assert "<script" not in _read(html_path).lower()


def test_missing_run_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        render_memo.render(tmp_path / "nope")


def test_corrupt_json_is_reported_not_fatal(run_dir: Path) -> None:
    (run_dir / "phase4" / "cio_final.json").write_text("{not json", encoding="utf-8")
    md_path, _ = render_memo.render(run_dir)
    md = _read(md_path)
    assert render_memo.NA_TEXT in _md_section(md, 2)
    assert "phase4/cio_final.json: unreadable (JSONDecodeError)" in _md_section(md, 10)
    _assert_in_order(md, [f"## {t}" for t in TITLES])


def test_preview_plan_is_used_when_final_plan_is_missing(run_dir: Path) -> None:
    plan = json.loads(_read(run_dir / "phase4" / "portfolio_plan.json"))
    plan["schema"] = "portfolio_preview/v1"
    common.write_json(run_dir / "phase4" / "portfolio_preview.json", plan)
    (run_dir / "phase4" / "portfolio_plan.json").unlink()
    md_path, _ = render_memo.render(run_dir)
    s7 = _md_section(_read(md_path), 7)
    assert "pre-CIO preview" in s7 and "| JPM | SELL | 10 |" in s7


def test_hostile_urls_are_never_linkified(run_dir: Path) -> None:
    path = run_dir / "phase2" / "reports" / "osint_KO.json"
    doc = json.loads(_read(path))
    doc["sources"] += [
        {"id": "W8", "url": "javascript:alert(1)", "title": "js link", "accessed": "2026-10-03", "kind": "web"},
        {"id": "W9", "url": 'https://x.org/a"onmouseover="alert(1)', "title": "quote", "accessed": "2026-10-03",
         "kind": "web"}]
    common.write_json(path, doc)
    _, html_path = render_memo.render(run_dir)
    html = _read(html_path)
    assert 'href="javascript' not in html
    assert "onmouseover=\"" not in html
    assert "javascript:alert(1)" in html  # still listed as plain (escaped) text
    for href in re.findall(r'href="([^"]*)"', html):
        assert href.startswith("#") or re.match(r"https?://[^\s\"<>']+$", href), href


def test_a_failing_section_builder_is_contained(run_dir: Path, monkeypatch) -> None:
    def boom(ctx, t):
        raise KeyError("synthetic")
    monkeypatch.setitem(render_memo.SECTION_BUILDERS, 6, boom)
    md_path, _ = render_memo.render(run_dir)
    md = _read(md_path)
    _assert_in_order(md, [f"## {t}" for t in TITLES])
    assert "Section could not be built" in _md_section(md, 6)


def test_quick_mode_without_phase3_explains_no_proposals(tmp_path: Path) -> None:
    rd = tmp_path / "2026-10-03_1100"
    rd.mkdir()
    common.write_json(rd / "manifest.json", {"run_id": "2026-10-03_1100", "as_of": "2026-10-03",
                                             "mode": {"dry_run": False, "quick": True, "sectors": None,
                                                      "holding": None}, "orders": {}})
    md_path, _ = render_memo.render(rd)
    assert "skipped in quick mode" in _md_section(_read(md_path), 6)


# =====================================================================================
# trial mode
# =====================================================================================


def test_trial_mode_renders_sections_1_5_8_9_10_11_for_one_ticker(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir, mode="trial", ticker="ko")
    assert md_path == run_dir / "phase4" / "trial_KO.md"
    assert html_path == run_dir / "phase4" / "trial_KO.html"
    md, html = _read(md_path), _read(html_path)
    wanted = [render_memo.SECTION_TITLES[n] for n in render_memo.TRIAL_SECTIONS]
    _assert_in_order(md, [f"## {t}" for t in wanted])
    _assert_in_order(htmllib.unescape(html), wanted)
    for n in (2, 3, 4, 6, 7):
        assert render_memo.SECTION_TITLES[n] not in md
        assert render_memo.SECTION_TITLES[n] not in htmllib.unescape(html)
    assert md.startswith("# Holding Trial — KO")
    assert md.index(common.DISCLAIMER) < md.index(f"## {render_memo.SECTION_TITLES[5]}")
    s5 = _md_section(md, 5)
    # a negative R/R (fair value below price) is shown as n/m with the anchors beside it; the raw
    # number stays in the detail line and the footnote
    assert "| KO | Consumer Staples | HOLD | HOLD | 5.6 | 7.2 | n/m (FV below price) | $67.90 | $65.90 |" in s5
    assert "Risk/reward:** -0.49" in s5  # one R/R value per ticker across the whole memo
    assert "0.90" not in md
    assert "| PG |" not in s5 and "| JPM |" not in s5
    assert "### Specialist views" in s5 and "### Rule trace" in s5
    s8 = _md_section(md, 8)
    assert "KO — Consumer Staples (phase 2)" in s8 and "PG —" not in s8
    assert "No overrides or rule deviations for KO in this run." in _md_section(md, 9)
    s10 = _md_section(md, 10)
    assert "| osint | KO | 4.0 |" in s10 and "| operator_ceo | PG |" not in s10
    s11 = _md_section(md, 11)
    assert "D:PX:KO" in s11 and "D:PX:JPM" not in s11


def test_trial_mode_for_a_ticker_with_a_deviation(run_dir: Path) -> None:
    md_path, _ = render_memo.render(run_dir, mode="trial", ticker="PG")
    md = _read(md_path)
    s9 = _md_section(md, 9)
    pg = next(v for v in _fixture_json("phase2/consumer_staples_verdict.json")["verdicts"] if v["ticker"] == "PG")
    assert pg["deviation_justification"] in s9
    assert "| operator_ceo | PG | phase 2 | failed |" in _md_section(md, 10)


def test_trial_mode_takes_ticker_from_manifest_holding(run_dir: Path) -> None:
    manifest = json.loads(_read(run_dir / "manifest.json"))
    manifest["mode"]["holding"] = "JPM"
    common.write_json(run_dir / "manifest.json", manifest)
    md_path, _ = render_memo.render(run_dir, mode="trial")
    assert md_path.name == "trial_JPM.md"
    assert "| JPM | Financials |" in _md_section(_read(md_path), 5)


def test_trial_mode_without_ticker_raises(run_dir: Path) -> None:
    with pytest.raises(ValueError):
        render_memo.render(run_dir, mode="trial")
    with pytest.raises(ValueError):
        render_memo.render(run_dir, mode="sideways")


# =====================================================================================
# CLI + formatting helpers
# =====================================================================================


def test_cli_full_and_trial(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert render_memo.main(["--run-dir", str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["memo_md"]).is_file() and Path(out["memo_html"]).is_file()
    assert render_memo.main(["--run-dir", str(run_dir), "--mode", "trial", "--ticker", "KO"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["memo_md"].endswith("phase4/trial_KO.md")
    custom = run_dir / "custom.md"
    assert render_memo.main(["--run-dir", str(run_dir), "--out-md", str(custom),
                             "--out-html", str(run_dir / "custom.html")]) == 0
    assert custom.is_file()
    capsys.readouterr()
    assert render_memo.main(["--run-dir", str(run_dir / "missing")]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_cli_run_id_under_runs_dir(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert render_memo.main(["--run", run_dir.name, "--runs-dir", str(run_dir.parent)]) == 0
    assert json.loads(capsys.readouterr().out)["memo_md"].endswith("phase4/memo.md")


def test_number_formatting_helpers() -> None:
    assert render_memo.fmt_num(2.345, 2) == "2.35"  # ROUND_HALF_UP, like aggregate.py
    assert render_memo.fmt_num(None) == "n/a"
    assert render_memo.fmt_num(float("nan")) == "n/a"
    assert render_memo.fmt_num(-0.004, 2) == "0.00"  # never "-0.00"
    assert render_memo.fmt_num(1234567.891, 2) == "1,234,567.89"
    assert render_memo.fmt_pct(12.345) == "12.3%"
    assert render_memo.fmt_pp(-4.6) == "-4.6 pp" and render_memo.fmt_pp(2.0) == "+2.0 pp"
    assert render_memo.fmt_usd(-1234.5) == "-$1,235"
    assert render_memo.fmt_usd(98.5, 2) == "$98.50"
    assert render_memo.fmt_ils(8909.52) == "ILS 8,910"
    assert render_memo.fmt_shares(15.0) == "15" and render_memo.fmt_shares(1.25) == "1.25"
    price = common.fact(69.85, "USD/share", "D:PX:KO", "2026-10-02", "price")
    assert render_memo.fmt_fact(price) == "$69.85 (as of 2026-10-02)"
    assert render_memo.fmt_fact(price, asof=False) == "$69.85"
    assert render_memo.fmt_fact(common.fact(3.88, "pct", "D:FRED:DFF", "2026-10-02", "macro")) == \
        "3.88% (as of 2026-10-02)"
    assert render_memo.fmt_fact(common.fact(None, "USD", "D:SEC:KO:CF", "2026-06-27", "fundamental")) == \
        "n/a (missing) (as of 2026-06-27)"
    assert render_memo.fmt_fact(common.fact(3.7123, "ILS/USD", "D:PX:ILS=X", "2026-10-02", "price"), 4) == \
        "3.7123 ILS/USD (as of 2026-10-02)"


def test_text_helpers() -> None:
    assert render_memo.md_cell("a|b\nc") == "a\\|b c"
    assert render_memo.md_row(["x|y", None, 3]) == "| x\\|y |  | 3 |"
    assert render_memo.md_sep("lr", 3) == "| --- | ---: | --- |"
    assert render_memo.md_text("<script>x</script> < 5") == "&lt;script>x&lt;/script> < 5"
    assert render_memo.safe_url("https://www.sec.gov/x?a=1&b=2") == "https://www.sec.gov/x?a=1&b=2"
    assert render_memo.safe_url("javascript:alert(1)") == ""
    assert render_memo.safe_url("ftp://x.org") == ""
    assert render_memo.one_line("First sentence here. Second one.") == "First sentence here."
    assert render_memo.one_line("word " * 40).endswith("…")
    assert render_memo.badge_class("PASS") == "b-mute" and render_memo.badge_class("pass") == "b-pos"


def test_md_text_escapes_leading_block_markers() -> None:
    md_text = render_memo.md_text
    assert md_text("## 5. Holding Verdicts\nfake") == "\\## 5. Holding Verdicts fake"
    assert md_text("> quoted") == "\\> quoted"
    assert md_text("- item") == "\\- item" and md_text("* item") == "\\* item"
    assert md_text("1. first") == "1\\. first" and md_text("2) second") == "2\\) second"
    assert md_text("---") == "\\---" and md_text("```py") == "\\```py"
    # ordinary text (numbers, signed values, #tags) is untouched
    for plain in ("-4.6 pp", "10.0%", "#1 pick", "*bold*", "1.5x", "+2.0 pp"):
        assert md_text(plain) == plain


def test_one_line_does_not_split_on_abbreviations() -> None:
    one_line = render_memo.one_line
    assert one_line("Trades at 18x vs. peers at 22x, so upside is limited; HOLD.") == \
        "Trades at 18x vs. peers at 22x, so upside is limited; HOLD."
    assert one_line("U.S. rate cuts help margins and the balance sheet is strong.") == \
        "U.S. rate cuts help margins and the balance sheet is strong."
    assert one_line("U.S. Treasury yields fell. Second sentence.") == "U.S. Treasury yields fell."
    assert one_line("Coca-Cola Co. Is cheap. More.") == "Coca-Cola Co. Is cheap."
    assert one_line("Margin 18.5% vs. 20%. Then more.") == "Margin 18.5% vs. 20%."
    assert one_line("Strong franchise. Upside small.") == "Strong franchise."


# =====================================================================================
# fixture integrity + deterministic figures
# =====================================================================================


def _full_context(path: Path) -> dict:
    rel = path.relative_to(FIXTURE_RUN).as_posix()
    phase = rel.split("/")[0]
    ctx: dict = {"run_dir": str(FIXTURE_RUN),
                 "verdicts": [str(p) for p in sorted(FIXTURE_RUN.glob("phase[23]/*_verdict.json"))]}
    for key in ("macro", "macro_view", "lookthrough", "cio_allocation", "prices"):
        ctx[key] = str(FIXTURE_RUN / "phase1" / f"{key}.json")
    if phase in ("phase2", "phase3"):
        ctx["aggregate"] = str(FIXTURE_RUN / phase / "aggregate.json")
        ticker = path.name.split(".")[0].rsplit("_", 1)[-1]
        for key in ("technicals", "fundamentals", "valuation"):
            data = FIXTURE_RUN / phase / "data" / f"{key}_{ticker}.json"
            if data.is_file():
                ctx[key] = str(data)
    return ctx


def test_fixture_outputs_pass_full_semantic_validation() -> None:
    """Every resolved agent output and data file in the fixture run validates with the full
    cross-file context (aggregate, verdicts, macro_view, cio_allocation, lookthrough, ...)."""
    from scripts import validate
    manifest = json.loads(_read(FIXTURE_RUN / "manifest.json"))
    resolved = {Path(o["resolved_output"]).name for o in manifest["orders"].values()}
    checked = 0
    for path in sorted(FIXTURE_RUN.rglob("*.json")):
        rel = path.relative_to(FIXTURE_RUN).as_posix()
        if rel == "manifest.json":
            continue
        if "/reports/" in rel and path.name not in resolved:
            continue  # superseded attempt-1 output (fundamental_JPM.json -> .r2.json), invalid on purpose
        errors = validate.validate_file(path, context=_full_context(path))
        assert errors == [], f"{rel}: {errors}"
        checked += 1
    assert checked >= 35


def test_deterministic_figures_come_from_aggregate_not_the_verdict_copy(run_dir: Path) -> None:
    path = run_dir / "phase2" / "consumer_staples_verdict.json"
    doc = json.loads(_read(path))
    ko = next(v for v in doc["verdicts"] if v["ticker"] == "KO")
    ko.update({"rr": 9.99, "agg_conviction": 9.9, "agg_evidence": 1.1, "rule_action": "ADD"})
    common.write_json(path, doc)
    md = _read(render_memo.render(run_dir)[0])
    assert "| KO | Consumer Staples | HOLD | HOLD | 5.6 | 7.2 | n/m (FV below price) |" in _md_section(md, 5)
    s10 = _md_section(md, 10)
    assert "KO rr 9.99 differs from phase2/aggregate.json -0.49; the aggregate value is shown" in s10
    assert "KO rule_action 'ADD' differs from phase2/aggregate.json 'HOLD'" in s10
    assert "9.99" not in md.replace(s10, "")
    # the mis-copied rule action is not reported as a sector-head deviation
    assert "| KO |" not in _md_section(md, 9)


def test_verdict_for_ticker_missing_from_aggregate_is_flagged(run_dir: Path) -> None:
    path = run_dir / "phase3" / "health_care_verdict.json"
    doc = json.loads(_read(path))
    doc["verdicts"].append({**doc["verdicts"][0], "ticker": "ABBV", "action": "PASS", "rule_action": "PASS",
                            "rr": 0.67, "rationale": "Too little upside."})
    common.write_json(path, doc)
    md = _read(render_memo.render(run_dir)[0])
    assert "phase3/health_care_verdict.json: ABBV has a verdict but is not in phase3/aggregate.json" \
        in _md_section(md, 10)


@pytest.mark.parametrize("rel,key,bad", [
    ("phase2/aggregate.json", "tickers", [1, 2]),
    ("phase2/evidence.json", "reports", ["x"]),
    ("phase3/aggregate.json", "tickers", "oops"),
    ("phase3/aggregate_light.json", "tickers", [1]),
    ("phase3/evidence.json", "reports", 7),
    ("phase3/evidence_light.json", "reports", ["x"]),
])
@pytest.mark.parametrize("mode", ["full", "trial"])
def test_wrong_typed_containers_render_with_a_load_warning(run_dir: Path, rel: str, key: str, bad: object,
                                                           mode: str) -> None:
    path = run_dir / rel
    doc = json.loads(_read(path))
    doc[key] = bad
    common.write_json(path, doc)
    md = _read(render_memo.render(run_dir, mode=mode, ticker="KO" if mode == "trial" else None)[0])
    nums = render_memo.TRIAL_SECTIONS if mode == "trial" else tuple(render_memo.SECTION_TITLES)
    _assert_in_order(md, [f"## {render_memo.SECTION_TITLES[n]}" for n in nums])
    assert f"{rel}: '{key}' is not an object ({type(bad).__name__}); ignored" in _md_section(md, 10)


def _subsection_body(section: str, sub: str) -> str:
    return section[section.index(f"### {sub}"):].split("\n\n", 2)[1]


def test_missing_cio_outputs_render_not_available_not_none(run_dir: Path) -> None:
    (run_dir / "phase4" / "cio_final.json").unlink()
    (run_dir / "phase1" / "cio_allocation.json").unlink()
    md = _read(render_memo.render(run_dir)[0])
    s9 = _md_section(md, 9)
    for sub in ("CIO macro-bound overrides", "CIO action overrides"):
        body = _subsection_body(s9, sub)
        assert render_memo.NA_TEXT in body and "None." not in body
    assert "Sector-head rule deviations" in s9 and "| PG |" in s9  # verdict deviations still shown
    s5 = _md_section(md, 5)
    assert "final committee action (CIO)" not in s5
    assert "Action = sector-head verdict" in s5


def test_failed_cio_final_falls_back_to_verdict_actions_with_caption(run_dir: Path) -> None:
    (run_dir / "phase4" / "cio_final.json").unlink()
    common.write_json(run_dir / "phase4" / "cio_final.failed.json",
                      {"failed": True, "order_id": "p4-cio", "errors": ["$.final_actions: synthetic"]})
    md = _read(render_memo.render(run_dir)[0])
    s5 = _md_section(md, 5)
    assert "| JPM | Financials | HOLD | HOLD |" in s5
    assert "Action = sector-head verdict (the CIO's final reconciliation is not available in this run)" in s5
    assert render_memo.NA_TEXT in _subsection_body(_md_section(md, 9), "CIO action overrides")


def test_agent_text_cannot_inject_markdown_headings(run_dir: Path) -> None:
    path = run_dir / "phase1" / "macro_view.json"
    doc = json.loads(_read(path))
    doc["regime_summary"] = "## 5. Holding Verdicts\nfake"
    common.write_json(path, doc)
    md = _read(render_memo.render(run_dir)[0])
    headings = [line for line in md.splitlines() if line.startswith("## ")]
    assert headings == [f"## {t}" for t in TITLES]
    assert "\\## 5. Holding Verdicts fake" in _md_section(md, 3)


# =====================================================================================
# first-live-run review: R/R presentation, cash labels, currency, full exits, flags, config drift
# =====================================================================================


def _mutate(run_dir: Path, rel: str, fn) -> None:
    path = run_dir / rel
    doc = json.loads(_read(path))
    fn(doc)
    common.write_json(path, doc)


def _row(section: str, ticker: str) -> list[str]:
    """Cells of the markdown table row that starts with ``| <ticker> |``."""
    line = next(ln for ln in section.splitlines() if ln.startswith(f"| {ticker} |"))
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _headers(section: str, first: str) -> list[str]:
    line = next(ln for ln in section.splitlines() if ln.startswith(f"| {first} |"))
    return [c.strip() for c in line.strip().strip("|").split("|")]


def test_holdings_table_has_fair_value_and_stop_next_to_rr_and_marks_negative_rr_as_not_meaningful(
        run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    s5 = _md_section(_read(md_path), 5)
    heads = _headers(s5, "Ticker")
    i = heads.index("R/R")
    assert heads[i:i + 3] == ["R/R", "Fair value (base)", "Stop"]
    ko = _row(s5, "KO")
    assert ko[i:i + 3] == ["n/m (FV below price)", "$67.90", "$65.90"]       # fv_base / invalidation of the aggregate
    assert _row(s5, "JPM")[i:i + 3] == ["1.47", "$262.00", "$225.00"]
    # the raw ratios are in the footnote (markdown has no tooltips) ...
    assert "raw R/R: KO -0.49, PG -0.55" in s5 and "negative and not meaningful" in s5
    assert "-0.49 |" not in s5.split("Why", 1)[1].split("_n/m", 1)[0], "no bare negative ratio in the table"
    # ... and in a tooltip in the HTML table
    html = _html_section(_read(html_path), 5)
    assert re.search(r'<td class="num" title="[^"]*-0\.49[^"]*">n/m \(FV below price\)</td>', html)
    assert 'title="stop source: technical_report"' in html


def test_null_rr_shows_its_reason_instead_of_a_bare_n_a(run_dir: Path) -> None:
    def tight(doc):
        pg = doc["tickers"]["PG"]
        pg["rr"].update(rr=None, upside_pct=-3.2, downside_pct=0.4, null_reason="stop_too_tight")

    _mutate(run_dir, "phase2/aggregate.json", tight)
    _mutate(run_dir, "phase3/aggregate_light.json",
            lambda doc: doc["tickers"]["ABBV"]["rr"].update(rr=None, invalidation=190.0,
                                                            null_reason="price_below_invalidation"))
    md = _read(render_memo.render(run_dir)[0])
    s5, s6 = _md_section(md, 5), _md_section(md, 6)
    heads = _headers(s5, "Ticker")
    i = heads.index("R/R")
    assert _row(s5, "PG")[i] == "n/a (stop_too_tight)"
    assert _row(s5, "QQQ")[i] == "n/a (not_applicable)"                    # funds: the aggregate's own reason
    assert _row(s5, "BRK-B")[i] == "n/a"                                   # no rr object at all: nothing to say
    assert "n/a (stop_too_tight): the stop is too close to the price for a meaningful ratio" in s5
    assert "n/a (not_applicable): no R/R test for this asset type (funds)" in s5
    # the candidates table (section 6) uses the same columns
    cheads = _headers(s6, "Ticker")
    assert cheads[cheads.index("R/R"):cheads.index("R/R") + 3] == ["R/R", "Fair value (base)", "Stop"]
    assert _row(s6, "ABBV")[cheads.index("R/R")] == "n/a (price_below_invalidation)"
    assert "n/a (price_below_invalidation): the price is at or below the invalidation (stop) price" in s6
    assert _row(s6, "MRK")[cheads.index("R/R"):cheads.index("R/R") + 3] == ["2.56", "$128.00", "$87.00"]


def test_trial_table_and_detail_line_keep_the_raw_ratio_but_explain_it(run_dir: Path) -> None:
    md = _read(render_memo.render(run_dir, mode="trial", ticker="KO")[0])
    s5 = _md_section(md, 5)
    assert "| KO | Consumer Staples | HOLD | HOLD | 5.6 | 7.2 | n/m (FV below price) | $67.90 | $65.90 |" in s5
    assert "Risk/reward:** -0.49" in s5 and "n/m (FV below price): not a meaningful ratio" in s5


def test_valuation_envelope_caveats_are_surfaced_in_data_gaps(run_dir: Path) -> None:
    low_re = r"_\d+ low-severity data flags not listed\._"
    before = re.findall(low_re, _md_section(_read(render_memo.render(run_dir)[0]), 10))
    for ticker, sector_flag in (("JPM", "dcf_financials_caution"), ("KO", None)):
        flags = [common.flag("multiple_bands_are_assumptions", "low",
                             "P/E, EV/EBIT and P/B cells use configured bands - assumptions, not market data")]
        if sector_flag:
            flags.append(common.flag(sector_flag, "medium", "DCF and net debt are of limited meaning for financials"))
        doc = common.envelope("valuation", "2026-10-02", sources=[], flags=flags, ticker=ticker)
        common.write_json(run_dir / "phase2" / "data" / f"valuation_{ticker}.json", doc)
    md = _read(render_memo.render(run_dir)[0])
    s10 = _md_section(md, 10)
    assert "### Valuation caveats (valuation files)" in s10
    assert "| JPM | dcf_financials_caution | medium | DCF and net debt are of limited meaning for financials |" in s10
    assert "| JPM | multiple_bands_are_assumptions | low |" in s10 and "| KO | multiple_bands_are_assumptions | low |" in s10
    assert re.findall(low_re, s10) == before, "the caveats are not counted as hidden low flags"
    # not duplicated in the generic data-quality table
    assert s10.count("dcf_financials_caution |") == 1
    trial = _md_section(_read(render_memo.render(run_dir, mode="trial", ticker="KO")[0]), 10)
    assert "| KO | multiple_bands_are_assumptions | low |" in trial and "| JPM |" not in trial.split("Missing data")[0]


def test_section_4_separates_the_profile_cash_floor_from_the_cio_cash_target(run_dir: Path) -> None:
    _mutate(run_dir, "phase4/cio_final.json", lambda d: d["final_target_sector_weights"].update(cash=14.4))
    _mutate(run_dir, "phase1/cio_allocation.json", lambda d: d["target_sector_weights"].update(cash=7.5))
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert "- **Cash (current / post-trade):** 9.6% / 11.4%" in s4
    assert "- **Cash floor (profile):** 5.0%" in s4
    assert "- **Cash target (CIO):** 14.4% (Phase 1 allocation 7.5%)" in s4
    assert "Cash (current / target / post-trade)" not in s4
    assert "| Cash | 5.0% | — | 7.5% | 14.4% | 11.4% | no |" in s4        # the table already showed the CIO target
    # only the Phase 1 allocation exists (no CIO reconciliation): say so
    (run_dir / "phase4" / "cio_final.json").unlink()
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert "- **Cash target (CIO):** 7.5% (Phase 1 allocation; no final CIO reconciliation)" in s4


def test_section_4_flags_an_unclassified_bucket_the_targets_leave_out(run_dir: Path) -> None:
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert "has no target" not in s4, "the fixture targets cover the unclassified bucket"

    def drop(doc, key):
        doc[key].pop("unclassified", None)

    _mutate(run_dir, "phase1/cio_allocation.json", lambda d: drop(d, "target_sector_weights"))
    _mutate(run_dir, "phase4/cio_final.json", lambda d: drop(d, "final_target_sector_weights"))
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert "Unclassified exposure (1.0%) has no target" in s4
    # an explicit 0 is a target
    _mutate(run_dir, "phase4/cio_final.json", lambda d: d["final_target_sector_weights"].update(unclassified=0.0))
    assert "has no target" not in _md_section(_read(render_memo.render(run_dir)[0]), 4)


def _currency_run(run_dir: Path) -> None:
    (run_dir / "inputs").mkdir(exist_ok=True)
    (run_dir / "inputs" / "portfolio.yaml").write_text(
        "holdings:\n"
        "  - {ticker: QQQ, currency: USD}\n  - {ticker: KO, currency: USD}\n  - {ticker: PG, currency: USD}\n"
        "  - {ticker: JPM, currency: USD}\n  - {ticker: BRK-B, currency: USD}\n"
        "  - {ticker: '1186063', currency: USD, proxy_ticker: QQQ}\n"
        "  - {ticker: LUMI.TA, currency: ILS}\n", encoding="utf-8")

    def plan(doc):
        base = {"asset_type": "stock", "sector": "financials", "trade": {"side": "NONE", "shares": 0, "value_usd": 0,
                                                                         "value_ils": 0}, "tax": {"applicable": False},
                "notes": []}
        doc["positions"].append(dict(base, ticker="1186063", asset_type="fund", sector="funds", action="HOLD",
                                     shares_cur=7.6, weight_cur_pct=15.0, weight_tgt_pct=15.0, value_usd_cur=5000))
        doc["positions"].append(dict(base, ticker="LUMI.TA", action="SELL", full_exit=True,
                                     note="Full exit: the remaining value is below the minimum trade size.",
                                     shares_cur=45, weight_cur_pct=3.0, weight_tgt_pct=0.0, value_usd_cur=1100,
                                     trade={"side": "SELL", "shares": 45, "value_usd": 1100, "value_ils": 3300}))
        doc["notes"].append("Base currency ILS: USD amounts are converted at USD/ILS 3.0532 (as of 2026-10-02). "
                            "Every holding is a USD asset, so the portfolio carries USD/ILS currency exposure \u2014 "
                            "a weaker dollar lowers the ILS value even when USD prices are unchanged.")

    _mutate(run_dir, "phase4/portfolio_plan.json", plan)


def test_currency_section_derives_exposure_from_the_holdings_not_from_cash(run_dir: Path) -> None:
    _currency_run(run_dir)
    s7 = _md_section(_read(render_memo.render(run_dir)[0]), 7)
    cur = s7.split("### Currency", 1)[1].split("### Plan notes", 1)[0]
    assert "USD-listed holdings:** 90.4% \u2014 QQQ, KO, PG, JPM, BRK-B" in cur
    assert ("TASE funds priced via US proxies:** 15.0% \u2014 1186063 (QQQ): ILS-listed units, economically USD"
            in cur)
    assert "ILS-listed holdings:** 3.0% \u2014 LUMI.TA" in cur
    assert "Cash:** 9.6% \u2014 currency split not recorded" in cur and "cash_usd" in cur
    assert "Economic USD exposure (securities):** 105.4%" in cur
    assert "USD-denominated positions" not in cur
    # the old "100 - cash" claim is gone, and so is portfolio.py's "every holding is a USD asset"
    assert "Every holding is a USD asset" not in s7 and "every holding is a USD asset" not in s7.lower()
    assert "Base currency ILS: USD amounts are converted at USD/ILS 3.0532 (as of 2026-10-02)." in s7


def test_currency_note_is_kept_when_every_holding_really_is_usd(run_dir: Path) -> None:
    def note(doc):
        doc["notes"].append("Every holding is a USD asset, so the portfolio carries USD/ILS currency exposure.")

    _mutate(run_dir, "phase4/portfolio_plan.json", note)
    s7 = _md_section(_read(render_memo.render(run_dir)[0]), 7)
    assert "Every holding is a USD asset" in s7                       # nothing contradicts it: not touched
    assert "ILS-listed holdings:" not in s7 and "TASE funds priced via US proxies:" not in s7
    assert "listing currencies are not known" in s7                  # no portfolio.yaml snapshot in this run


def test_full_exit_is_marked_in_the_trades_and_positions_tables(run_dir: Path) -> None:
    _currency_run(run_dir)
    md_path, html_path = render_memo.render(run_dir)
    md, html = _read(md_path), _read(html_path)
    s7, s4 = _md_section(md, 7), _md_section(md, 4)
    # section 7 shows the exit kind (and the share of the position) in its own columns
    assert "| Action | Exit | Of position |" in s7
    assert "| LUMI.TA | SELL | 45 | $1,100 | ILS 3,300 | SELL | full | 100% | \u2014 |" in s7
    assert "| JPM | SELL | 10 | $2,400 | ILS 8,910 | TRIM | partial | 33% | \u2014 |" in s7, "a partial sale is not a full exit"
    assert "LUMI.TA: Full exit: the remaining value is below the minimum trade size." in s7
    assert "| LUMI.TA | stock | Financials | 3.0% | 0.0% | -3.0 pp | SELL (full exit) |" in s4
    # the badge still colours it
    assert re.search(r'<span class="badge b-neg">SELL \(full exit\)</span>', _html_section(html, 4))
    assert re.search(r'<span class="badge b-neg">SELL</span>', _html_section(html, 7))
    assert render_memo.badge_class("SELL (full exit)") == "b-neg" and render_memo.badge_class("SELL") == "b-neg"
    assert render_memo.badge_class("TRIM -> HOLD (trim < 1 share)") == "b-neutral"


def test_position_flags_appear_in_the_portfolio_and_holding_tables_with_a_legend(run_dir: Path) -> None:
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert _headers(s4, "Ticker")[-1] == "Flags"
    assert _row(s4, "BRK-B")[-1] == "conglomerate" and _row(s4, "KO")[-1] == "\u2014"
    assert "Flags: conglomerate \u2014 diversified holding company" in s4
    s5 = _md_section(_read(render_memo.render(run_dir)[0]), 5)
    heads = _headers(s5, "Ticker")
    assert heads[-2:] == ["Flags", "Why"] and _row(s5, "BRK-B")[heads.index("Flags")] == "conglomerate"
    assert "Flags: conglomerate \u2014" in s5
    # no flagged holding -> no Flags column
    _mutate(run_dir, "phase1/lookthrough.json",
            lambda d: [h.update(flags=[]) for h in d["holdings"]])
    m = json.loads(_read(run_dir / "manifest.json"))
    for h in m["holdings"]:
        h.pop("flags", None)
    common.write_json(run_dir / "manifest.json", m)
    md = _read(render_memo.render(run_dir)[0])
    assert "Flags" not in _md_section(md, 4) and "Flags" not in _md_section(md, 5)


def test_converted_quotes_are_flagged_from_the_prices_native_block(run_dir: Path) -> None:
    def lumi(doc):
        doc["tickers"]["JPM"]["native"] = {"quote_currency": "ILA", "currency": "ILS",
                                           "last_close": common.fact(75.22, "ILS/share", "D:PX:JPM", "2026-10-02",
                                                                     "price"), "usdils": 3.0532}

    _mutate(run_dir, "phase1/prices.json", lumi)
    s4 = _md_section(_read(render_memo.render(run_dir)[0]), 4)
    assert _row(s4, "JPM")[-1] == "converted_to_usd"
    assert "converted_to_usd \u2014 quote converted from its native currency to USD" in s4


def test_config_changed_during_the_run_is_a_data_gap_entry_in_both_modes(run_dir: Path) -> None:
    msg = ("config investor_profile.yaml changed after this run started \u2014 decisions used the snapshot in "
           "inputs/")
    m = json.loads(_read(run_dir / "manifest.json"))
    m["config_drift"] = [{"file": "investor_profile.yaml", "change": "changed", "message": msg}]
    m["warnings"] = [f"p4.finalize: {msg}", "p1.data: KO price is stale"]
    common.write_json(run_dir / "manifest.json", m)
    md_path, html_path = render_memo.render(run_dir)
    s10 = _md_section(_read(md_path), 10)
    assert "### Configuration changed during the run" in s10 and f"- {msg}" in s10
    assert s10.count(msg) == 1, "the same text is not repeated under 'Run warnings'"
    assert "p1.data: KO price is stale" in s10
    assert msg in htmllib.unescape(_html_section(_read(html_path), 10))
    trial = _md_section(_read(render_memo.render(run_dir, mode="trial", ticker="KO")[0]), 10)
    assert f"- {msg}" in trial

    # without a recorded drift nothing is said
    m.pop("config_drift")
    common.write_json(run_dir / "manifest.json", m)
    assert "Configuration changed" not in _md_section(_read(render_memo.render(run_dir)[0]), 10)


def test_summary_digest_dedupes_sources_by_url_and_caps_missing_data(run_dir: Path) -> None:
    d = render_memo.summary_digest(run_dir)
    urls = [s["url"] for s in d["sources"]]
    assert urls and len(urls) == len(set(urls)), "de-duplicated by URL"
    assert {"ids", "url", "title", "kind", "accessed", "cited_by"} <= set(d["sources"][0])
    assert any(s["kind"] == "dataset" for s in d["sources"]) and d["sources"][0]["kind"] == "dataset"
    assert d["missing_data_count"] >= 1 and 1 <= len(d["missing_data"]) <= 20
    assert all(": " in m for m in d["missing_data"])

    # many missing items: the count is the total, the list the first 20
    def lots(doc):
        doc["missing_data"] = [f"item {i}" for i in range(45)]

    _mutate(run_dir, "phase1/prices.json", lots)
    d = render_memo.summary_digest(run_dir)
    assert d["missing_data_count"] >= 45 and len(d["missing_data"]) == 20
    assert d["missing_data"][0].endswith(": " + d["missing_data"][0].split(": ", 1)[1])
    # one ticker only (trial summaries)
    ko = render_memo.summary_digest(run_dir, ticker="KO")
    assert 0 < len(ko["sources"]) < len(render_memo.summary_digest(run_dir)["sources"])
    # an empty run dir gives empty digests, not an error
    empty = run_dir.parent / "empty"
    empty.mkdir()
    assert render_memo.summary_digest(empty) == {"sources": [], "missing_data_count": 0, "missing_data": []}


# --------------------------------------------------------------------------- plain-English "Why" column
def test_scrub_jargon_removes_rule_codes_and_spells_out_da():
    out = render_memo._scrub_jargon("R4.trim.limit_breach fired on 46%; the DA verdict is survives.")
    assert "R4" not in out and "fired" not in out and "Devil's Advocate" in out and " DA " not in out


# --------------------------------------------------------------------------- the Why composer (Call / Why / Watch)


def _why_ctx(**over) -> dict:
    """A hand-built context with just what the Why composer reads (no run directory)."""
    ctx = {"cfg": {"actions": {"holding": {"sell_max": 3.0, "trim_below": 4.5}}},
           "settings": {"score_dp": 1, "rr_dp": 2, "max_sector_pct": 35.0}, "holdings": {}, "plan": {},
           "plan_positions": {}, "verdict_index": {}, "aggregates": {}, "report_index": {}, "data_files": [],
           "phase1": {"macro_view": {"sector_constraints": [{"sector": "information_technology",
                                                              "min_weight": 12, "max_weight": 28}]}}}
    ctx.update(over)
    return ctx


def _agg(**over) -> dict:
    a = {"kind": "holding", "asset_type": "stock", "sector": "information_technology", "rule_action": "TRIM",
         "rule_label": "limit_breach", "agg_conviction": 4.4, "agg_evidence": 6.6,
         "da": {"verdict": "survives"},
         "inputs": {"price": {"value": 198.84}, "position_pct": 6.45, "cap_pct": 10.0, "limit_breach": True,
                    "sector_lookthrough_pct": 46.46, "cio_target_pct": 32.0, "max_sector_pct": 35.0,
                    "sector_breach_tolerance_pp": 1.0, "sector_limit_pct": 33.0,
                    "limit_breach_reasons": ["sector look-through 46.46% > limit 33.00%"]},
         "specialists": [{"role": "fundamental", "status": "ok", "stance": "bearish", "invalidation_price": None},
                         {"role": "technical", "status": "ok", "stance": "neutral", "invalidation_price": 190.77},
                         {"role": "osint", "status": "ok", "stance": "neutral"},
                         {"role": "operator_ceo", "status": "ok", "stance": "neutral"}],
         "rr": {"price": 198.84, "fv_base": 26.75, "invalidation": 190.77, "invalidation_source": "technical_report",
                "upside_pct": -86.55, "downside_pct": 4.06, "rr": -21.31, "null_reason": None}}
    a.update(over)
    return a


_RATIONALE = ("The sector is far above its allowed weight, which drives the trim. The business is strong. "
              "Insider selling by directors is a minor concern. The stock stays in the portfolio; only the "
              "sector excess is reduced.")


def _compose(pos, ctx=None, a=None, v=None, action="TRIM", tk="PLTR"):
    ctx = ctx or _why_ctx()
    a = a or _agg()
    v = v if v is not None else {"rationale": _RATIONALE, "action": action, "_sector": "information_technology"}
    return render_memo.compose_why(ctx, tk, v, a, pos, action)


def _pos(**over) -> dict:
    p = {"ticker": "PLTR", "action": "TRIM", "shares_cur": 12, "weight_cur_pct": 6.45, "weight_tgt_pct": 0.0,
         "sizing_rule": "trim_breach_sector", "price": {"value": 198.84}, "dropped_reason": None, "full_exit": True,
         "trade": {"side": "SELL", "shares": 12, "value_usd": 2386.08, "value_ils": 7308.09}}
    p.update(over)
    return p


def _parts(comp) -> dict:
    return dict(comp["parts"])


def test_why_full_exit_comes_from_the_plan_and_never_says_the_stock_stays():
    comp = _compose(_pos())
    parts = _parts(comp)
    assert parts["Call"].startswith("Sell all 12 shares (about $2,386); the verdict was TRIM")
    assert "full exit" in parts["Call"]
    cell = comp["cell"]
    assert "stays in the portfolio" not in cell and "only the sector excess" not in cell
    # price vs fair value as a percentage, the lens stances, the risk/reward, the ONE binding limit
    assert "Price $198.84 is 643% above the model's fair value $26.75" in parts["Why"]
    assert "conviction 4.4/10 (fundamental bearish; technical, news and management neutral)" in parts["Why"]
    assert "reward-to-risk not meaningful" in parts["Why"]
    assert "46.5% of the portfolio vs the 32% limit" in parts["Why"]
    assert "33.00" not in cell and "35%" not in cell, "the other sector numbers are named once, in the section note"
    assert "Insider selling by directors is a minor concern" in parts["Why"]    # a new fact is kept
    assert "far above its allowed weight" not in cell                           # restates the limit clause
    assert parts["Watch"].startswith("a close below $190.77 (technical stop)")
    assert "far from the market price" in parts["Model caveat"] and "verify, not a fact" in parts["Model caveat"]
    assert comp["sectors"] == {"information_technology": _agg()["inputs"]}


def test_why_partial_trim_states_shares_percentage_and_what_is_kept():
    pos = _pos(shares_cur=8, weight_tgt_pct=8.9, full_exit=False, sizing_rule="trim_breach_position",
               exit_kind="partial", trade_pct_of_position=0.25,
               trade={"side": "SELL", "shares": 2, "value_usd": 1097.12, "value_ils": 3360.26})
    call = _parts(_compose(pos))["Call"]
    assert call == "Sell 2 of 8 shares (25%), about $1,097, to get back under the single-stock cap; keep 6."
    # the old plan (no exit_kind / trade_pct_of_position) gives the same answer
    old = dict(pos)
    old.pop("exit_kind"), old.pop("trade_pct_of_position")
    assert _parts(_compose(old))["Call"] == call


def test_why_dropped_trade_reads_as_a_hold_not_as_a_trim():
    pos = _pos(shares_cur=2, weight_tgt_pct=3.74, full_exit=False, sizing_rule="trim_conviction",
               dropped_reason="rounds_to_zero_shares",
               trade={"side": "NONE", "shares": 0, "value_usd": 0.0, "proposed_shares": 0.659998})
    a = _agg(rule_label="trim_conviction", agg_conviction=4.0, inputs={"price": {"value": 692.13}, "limit_breach": False})
    a["rr"].update(price=692.13, fv_base=358.10, invalidation=645.72)
    comp = _compose(pos, a=a, v={"rationale": "Results due October 29 matter.", "action": "TRIM"})
    parts = _parts(comp)
    assert parts["Call"] == ("TRIM verdict, but no trade: a 0.66-share trim is too small to execute "
                             "(less than 1 share); effectively HOLD.")
    assert "Price $692.13 is 93% above the model's fair value $358.10" in parts["Why"]
    assert "conviction 4.0/10" in parts["Why"] and "weak (below 4.5)" in parts["Why"]
    assert "key date: results on October 29" in parts["Watch"]
    pv = comp["plan"]
    assert pv["effective"] == "HOLD" and render_memo._action_label(pv) == "TRIM -> HOLD (trim < 1 share)"
    # an explicit effective_action / reason from the plan wins
    pos2 = dict(pos, effective_action="HOLD", effective_action_reason="custom reason", dropped_reason=None)
    assert render_memo._plan_view(pos2, "TRIM")["reason_full"] == "custom reason"


def test_why_without_a_plan_never_claims_a_size_or_an_outcome():
    comp = _compose(None)
    call = _parts(comp)["Call"]
    assert call == "TRIM verdict - the portfolio plan will size any trade (no plan in this run)."
    assert "stays in the portfolio" not in comp["cell"] and "exit" not in comp["cell"].lower()
    hold = _parts(_compose(None, action="HOLD", a=_agg(rule_label="hold", rule_action="HOLD",
                                                          inputs={"price": {"value": 198.84}}), tk="PLTR"))["Call"]
    assert hold == "HOLD verdict - no change proposed."


def test_why_hold_keeps_the_position_and_funds_have_no_fair_value():
    pos = _pos(action="HOLD", shares_cur=59, weight_cur_pct=22.4, weight_tgt_pct=22.4, full_exit=False,
               trade={"side": "NONE", "shares": 0, "value_usd": 0.0})
    a = _agg(asset_type="fund", rule_label="hold", rule_action="HOLD", inputs={"price": {"value": 140.55}, "limit_breach": False},
             specialists=[{"role": "etf", "status": "ok", "stance": "neutral"},
                          {"role": "technical", "status": "ok", "stance": "neutral", "invalidation_price": 136.33}],
             rr={"rr": None, "null_reason": "not_applicable", "fv_base": None})
    comp = _compose(pos, a=a, action="HOLD", tk="SPMO", v={"rationale": "Both analysts are neutral.", "action": "HOLD"})
    parts = _parts(comp)
    assert parts["Call"] == "No trade: keep the 59 shares (22.4% of the portfolio)."
    assert parts["Why"].startswith("A fund has no fair value in the model; conviction 4.4/10 (fund review and technical neutral)")
    assert parts["Watch"] == "a close below $136.33 (technical stop)"
    assert "Model caveat" not in parts


def test_why_model_caveat_from_valuation_flags_and_far_fair_value():
    flags = {"ticker": "BRK-B", "rel": "phase2/data/valuation_BRK-B.json",
             "doc": {"flags": [{"code": "possible_captive_finance", "severity": "medium"},
                               {"code": "not_meaningful", "severity": "low"}],
                     "cells": [{"method": "dcf_nopat", "tv_share": 0.95, "flags": ["tv_gt_90"]}]}}
    ctx = _why_ctx(data_files=[flags])
    cav = render_memo._model_caveat(ctx, "BRK-B", 548.0, 507.0)
    assert "finance arm" in cav and "not meaningful" in cav and "terminal value" in cav
    assert "far from the market price" not in cav            # 0.93: close to the price
    assert render_memo._model_caveat(ctx, "BRK-B", 548.0, 200.0).endswith("not a fact.")
    assert render_memo._model_caveat(ctx, "KO", 100.0, 105.0) == "", "no files, fair value near the price: no caveat"
    assert "too low" not in render_memo._model_caveat(_why_ctx(), "KO", 100.0, 170.0) and \
        "far from" in render_memo._model_caveat(_why_ctx(), "KO", 100.0, 170.0)


def test_sector_limit_helper_names_the_binding_limit_and_the_others_once():
    ctx = _why_ctx()
    info = render_memo._sector_limit_info(ctx, "information_technology", _agg()["inputs"])
    assert info["value"] == 32.0 and info["source"] == "target" and info["trigger"] == 33.0
    assert info["profile"] == 35.0 and info["macro_max"] == 28.0
    note = render_memo._sector_limit_note(ctx, {"information_technology": _agg()["inputs"]})
    assert "binding limit is 32%" in note and "your profile cap is 35%" in note and "tops out at 28%" in note
    assert note.count("35%") == 1 and note.count("28%") == 1
    # the profile cap binds when it is lower than the committee's target
    low = dict(_agg()["inputs"], max_sector_pct=30.0)
    assert render_memo._sector_limit_info(ctx, "information_technology", low)["source"] == "profile"


def test_cell_breaks_render_as_br_in_markdown_and_html():
    cell = render_memo.CELL_BREAK.join(["Call: sell | now", "Why: <b>bold</b> & more"])
    assert render_memo.md_cell(cell) == "**Call:** sell \\| now<br>**Why:** &lt;b>bold&lt;/b> & more"
    html = str(render_memo.cell_html(cell))
    assert html == "<strong>Call:</strong> sell | now<br><strong>Why:</strong> &lt;b&gt;bold&lt;/b&gt; &amp; more"
    assert str(render_memo.cell_html("plain <i>")) == "plain &lt;i&gt;"


# --------------------------------------------------------------------------- effective action in every section


def _drop_jpm(doc):
    p = next(x for x in doc["positions"] if x["ticker"] == "JPM")
    p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0, "value_ils": 0.0, "proposed_shares": 0.66}
    p["dropped_reason"] = "rounds_to_zero_shares"


def test_effective_action_is_consistent_in_sections_4_5_and_7(run_dir: Path) -> None:
    _mutate(run_dir, "phase4/portfolio_plan.json", _drop_jpm)
    md_path, html_path = render_memo.render(run_dir)
    md = _read(md_path)
    label = "TRIM -> HOLD (trim < 1 share)"
    assert label in _md_section(md, 4) and label in _md_section(md, 5)
    assert f"| JPM | NONE | 0 | $0 | ILS 0 | {label} | \u2014 | \u2014 | rounds_to_zero_shares |" in _md_section(md, 7)
    jpm = _row(_md_section(md, 5), "JPM")
    assert jpm[2] == label and "**Call:** TRIM verdict, but no trade: a 0.66-share trim is too small" in jpm[-1]
    assert re.search(r'<span class="badge b-neutral">TRIM -&gt; HOLD \(trim &lt; 1 share\)</span>',
                     _html_section(_read(html_path), 5))
    # the plan fields the new portfolio.py adds win over anything derived
    def explicit(doc):
        p = next(x for x in doc["positions"] if x["ticker"] == "JPM")
        p.update(effective_action="HOLD", effective_action_reason="below the minimum", exit_kind="none")

    _mutate(run_dir, "phase4/portfolio_plan.json", explicit)
    assert "TRIM -> HOLD (trim < 1 share)" in _md_section(_read(render_memo.render(run_dir)[0]), 5)


def test_old_plan_without_the_new_fields_renders_every_holding(run_dir: Path) -> None:
    s5 = _md_section(_read(render_memo.render(run_dir)[0]), 5)
    for t in ("QQQ", "KO", "PG", "JPM", "BRK-B"):
        row = _row(s5, t)
        assert row[-1].startswith("**Call:**") and "<br>**Why:**" in row[-1], t
    assert "Sell 10 of 30 shares (33%), about $2,400" in _row(s5, "JPM")[-1]
    assert _row(s5, "BRK-B")[2] == "HOLD" and "No trade: keep the 20 shares" in _row(s5, "BRK-B")[-1]
    # the sector-limit sentence appears once, under the table
    assert s5.count("Sector limits - ") <= 1


# --------------------------------------------------------------------------- residual breaches


def _residual(doc):
    doc["residual_breaches"] = [{
        "sector": "information_technology", "post_pct": 41.0, "limit_pct": 35.0, "excess_pp": 6.0,
        "stock_trim_pp": 5.5, "note": "Funds are not sold by the plan.",
        "fund_contributions": [
            {"ticker": "SPMO", "weight_pct": 22.4, "sector_pct_of_portfolio": 12.3},
            {"ticker": "1186063", "weight_pct": 21.7, "sector_pct_of_portfolio": 13.0},
            {"ticker": "QTUM", "weight_pct": 7.1, "sector_pct_of_portfolio": 6.0}]}]


def test_residual_breaches_are_explained_in_plain_english_in_sections_4_and_7(run_dir: Path) -> None:
    _mutate(run_dir, "phase4/portfolio_plan.json", _residual)
    md_path, html_path = render_memo.render(run_dir)
    md = _read(md_path)
    s7, s4 = _md_section(md, 7), _md_section(md, 4)
    assert "### Residual breaches" in s7
    assert "Information Technology would still be 41.0% vs the 35% limit after the planned trades." in s7
    assert "Stocks cannot fix this (the planned stock trims already remove 5.5 pp)" in s7
    assert "SPMO 22.4% of the portfolio (12.3% of it in Information Technology)" in s7
    assert "1186063 21.7% of the portfolio" in s7 and "QTUM 7.1% of the portfolio" in s7
    assert "Only changing those fund holdings, or adding new money in other sectors" in s7
    assert "Funds are not sold by the plan." not in s7, "with a fund breakdown the plan's own note is not repeated"
    assert "Residual breach after the planned trades: Information Technology ends at 41.0% vs the 35% limit" in s4
    assert "Residual breaches" in _html_section(_read(html_path), 7)
    # no residual breach, no block
    def clean(doc):
        doc["residual_breaches"] = []
        for s in doc["sectors"]:
            s["breach"] = False

    _mutate(run_dir, "phase4/portfolio_plan.json", clean)
    md = _read(render_memo.render(run_dir)[0])
    assert "Residual breach" not in _md_section(md, 7) and "Residual breach" not in _md_section(md, 4)


def test_residual_breach_without_the_plan_field_falls_back_to_the_breach_flag(run_dir: Path) -> None:
    def flagged(doc):
        s = next(x for x in doc["sectors"] if x["sector"] == "information_technology")
        s.update(breach=True, post_lookthrough_pct=26.0)

    _mutate(run_dir, "phase4/portfolio_plan.json", flagged)
    s7 = _md_section(_read(render_memo.render(run_dir)[0]), 7)
    assert "Information Technology would still be 26.0% vs the 24% limit after the planned trades." in s7


# --------------------------------------------------------------------------- chair review + audit

CHAIR = {
    "schema": "chair_review/1", "run_id": "2026-10-03_1015", "round": 2, "reviewed_at": "2026-10-03T12:00:00Z",
    "verdict": "approved_with_notes", "headline": "Plan is coherent; <script>alert(1)</script> two notes remain",
    "executive_summary": ["Technology stays above its limit after the trims.", "JPM trim is executable."],
    "decisions": [{"ticker": "JPM", "action": "TRIM", "chair_view": "Sell 10 of 30 | keep 20", "confidence": 7,
                   "what_would_change_it": "A close below $225"},
                  {"ticker": "KO", "action": "HOLD", "chair_view": "No trade", "confidence": "high",
                   "what_would_change_it": "Volume down two quarters"}],
    "changes_made": [{"finding_id": "A1", "what": "Reworded the summary", "how": "chair_edit"}],
    "open_issues": ["A2: sector still above limit"],
    "user_actions": ["Decide how to redirect new money."],
    "disclaimer_confirmed": True,
}
AUDIT = {"schema": "audit/1", "summary": {"high": 1, "medium": 1, "low": 0, "verdict": "needs_attention"},
         "findings": [
             {"id": "A1", "code": "claim_vs_plan", "severity": "high", "ticker": "JPM",
              "message": "Summary says full exit but the plan sells 33%", "fix": "Reword"},
             {"id": "A2", "code": "residual_breach", "severity": "medium", "ticker": None,
              "message": "Tech 41% vs 35%", "fix": "Name the lever"},
             {"id": "A3", "code": "x", "severity": "low", "ticker": "KO", "message": "Minor", "fix": "Ignore"}]}


def test_chair_review_block_is_prominent_unnumbered_and_escaped(run_dir: Path) -> None:
    common.write_json(run_dir / "phase4" / "chair_review.json", CHAIR)
    md_path, html_path = render_memo.render(run_dir)
    md, html = _read(md_path), _read(html_path)
    # numbering 1-11 is unchanged; the chair block is one extra heading BEFORE section 1
    assert [ln for ln in md.splitlines() if ln.startswith("## ")] == ["## Chair's Final Review"] + [f"## {t}" for t in TITLES]
    assert md.index("## Chair's Final Review") < md.index(f"## {TITLES[0]}")
    top = md[: md.index(f"## {TITLES[0]}")]
    assert "**Verdict: APPROVED WITH NOTES** _(round 2" in top
    assert "Technology stays above its limit after the trims." in top
    assert "| JPM | TRIM | 7/10 | Sell 10 of 30 \\| keep 20 | A close below $225 |" in top
    assert "| KO | HOLD | high | No trade |" in top
    assert "### What the chair fixed" in top and "| A1 | Reworded the summary |" in top
    assert "### Open issues" in top and "A2: sector still above limit" in top
    assert "### What you need to do" in top and "Decide how to redirect new money." in top
    assert "Chair review: not performed" not in md and "Chair review: not performed" not in html
    # the HTML card comes first, carries the verdict badge, and never lets the headline execute
    assert html.index('class="chair chair-b-warn"') < html.index(f"<h2>{TITLES[0]}</h2>")
    assert '<span class="badge b-warn">Approved with notes</span>' in html
    assert "<script" not in html.lower() and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Sell 10 of 30 | keep 20" in html and "prefers-color-scheme: dark" in html
    # trial memos stay single-ticker: no chair block
    trial = _read(render_memo.render(run_dir, mode="trial", ticker="KO")[0])
    assert "Chair's Final Review" not in trial and "Chair review: not performed" not in trial


def test_without_a_chair_review_the_memo_has_only_a_muted_note(run_dir: Path) -> None:
    md_path, html_path = render_memo.render(run_dir)
    md, html = _read(md_path), _read(html_path)
    assert "Chair's Final Review" not in md and "Chair's Final Review" not in html
    assert "_Chair review: not performed_" in md.split(f"## {TITLES[0]}")[0]
    assert '<p class="muted">Chair review: not performed</p>' in html
    assert md.count("\n## ") == 11


def test_chair_verdict_variants(run_dir: Path) -> None:
    for verdict, cls in (("approved", "b-pos"), ("needs_rework", "b-neg")):
        common.write_json(run_dir / "phase4" / "chair_review.json", dict(CHAIR, verdict=verdict, disclaimer_confirmed=False))
        html = _read(render_memo.render(run_dir)[1])
        assert f'class="chair chair-{cls}"' in html and f'<span class="badge {cls}">' in html
        assert "did not confirm that the disclaimer" in html
    # a broken file (not an object) behaves like an absent one
    (run_dir / "phase4" / "chair_review.json").write_text("[1, 2]", encoding="utf-8")
    assert "Chair review: not performed" in _read(render_memo.render(run_dir)[0])


def test_chair_audit_table_in_section_10_with_handling_from_the_chair_review(run_dir: Path) -> None:
    common.write_json(run_dir / "phase4" / "audit.json", AUDIT)
    s10 = _md_section(_read(render_memo.render(run_dir)[0]), 10)
    assert "### Chair audit" in s10 and "High / medium / low findings:** 1 / 1 / 0" in s10
    assert _headers(s10, "ID") == ["ID", "Severity", "Ticker", "What was found", "How it was handled"]
    assert _row(s10, "A1")[-1] == "No chair review in this run"
    # with a chair review: fixed / open issue / not recorded
    common.write_json(run_dir / "phase4" / "chair_review.json", CHAIR)
    md_path, html_path = render_memo.render(run_dir)
    s10 = _md_section(_read(md_path), 10)
    assert _row(s10, "A1")[-1] == "Fixed: Reworded the summary (edited by the chair)"
    assert _row(s10, "A2")[-1] == "Open issue: A2: sector still above limit"
    assert _row(s10, "A3")[-1] == "Not fixed in this run: no fix recorded"
    assert [r[0] for r in [_row(s10, f"A{i}") for i in (1, 2, 3)]] == ["A1", "A2", "A3"]   # severity order
    assert '<span class="badge b-neg">high</span>' in _html_section(_read(html_path), 10)
    # trial mode lists only the findings of its ticker
    trial = _md_section(_read(render_memo.render(run_dir, mode="trial", ticker="KO")[0]), 10)
    assert "| A3 |" in trial and "| A1 |" not in trial
    # no audit file: no section
    (run_dir / "phase4" / "audit.json").unlink()
    assert "Chair audit" not in _md_section(_read(render_memo.render(run_dir)[0]), 10)
