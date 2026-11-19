"""Structure checks for the knowledge playbooks (knowledge/README.md describes the format).

Covers the 11 GICS sector playbooks (``knowledge/sectors/<sector>.md``) and the fund playbook
(``knowledge/etf_playbook.md``):

* every playbook exists (hard assertion — no skip);
* the YAML front-matter parses and carries every required key with the right types;
* ``sector`` matches the file, ``name`` the display name, ``benchmark_etf`` the ETF configured in
  ``config/committee.yaml`` (``sector_etfs``; ``benchmark`` for funds) and ``sector_class`` the
  class ``common.sector_class`` assigns;
* ``macro_sensitivities`` reference FRED ids configured in ``committee.yaml: macro.series``;
* the body has the title and all 8 section headings, each exactly once, in order.

The per-file checks skip with an explanatory message while a playbook written by another builder
does not exist yet; ``test_all_playbooks_exist`` is the test that fails in that case.
Purely offline: reads repo files only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import common

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
SECTORS_DIR = KNOWLEDGE_DIR / "sectors"
ETF_PLAYBOOK = KNOWLEDGE_DIR / "etf_playbook.md"
README = KNOWLEDGE_DIR / "README.md"
CONFIG_DIR = REPO_ROOT / "config"

FUNDS_KEY = "funds"
FUNDS_NAME = "Funds (ETFs)"

REQUIRED_KEYS: tuple[str, ...] = (
    "sector", "name", "benchmark_etf", "sector_class", "key_kpis", "valuation_methods",
    "macro_sensitivities", "failure_modes", "edgar_tags_of_interest",
)
NON_EMPTY_STR_LISTS: tuple[str, ...] = ("key_kpis", "valuation_methods", "failure_modes")
DIRECTIONS = frozenset({"positive", "negative", "mixed"})
# us-gaap element names (an optional namespace prefix such as "us-gaap:" is tolerated)
TAG_RE = re.compile(r"^(?:[a-z][a-z0-9-]*:)?[A-Z][A-Za-z0-9]*$")

SECTOR_HEADINGS: tuple[str, ...] = (
    "## 1. Scope & benchmark",
    "## 2. KPIs that matter",
    "## 3. Valuation methods",
    "## 4. Macro sensitivities",
    "## 5. Typical failure modes",
    "## 6. Red flags for the Devil's Advocate",
    "## 7. Lens checklist",
    "## 8. Data notes",
)
ETF_HEADINGS: tuple[str, ...] = (
    "## 1. Role in portfolio",
    "## 2. Expense ratio & costs",
    "## 3. Concentration (top-10)",
    "## 4. Overlap math & look-through sector caps",
    "## 5. Factor-ETF specifics",
    "## 6. How the deterministic fund rules work",
    "## 7. Israeli-investor considerations",
    "## 8. Data notes",
)

ALL_KEYS: tuple[str, ...] = tuple(common.SECTOR_KEYS) + (FUNDS_KEY,)


# =====================================================================================
# helpers
# =====================================================================================


def playbook_path(key: str) -> Path:
    return ETF_PLAYBOOK if key == FUNDS_KEY else SECTORS_DIR / f"{key}.md"


def committee() -> dict[str, Any]:
    return common.load_config("committee", CONFIG_DIR)


def split_front_matter(text: str, where: str) -> tuple[dict[str, Any], str]:
    """Return (front-matter dict, body) of a ``---``-delimited Markdown file."""
    lines = text.lstrip("﻿").splitlines()
    assert lines and lines[0].strip() == "---", f"{where}: file must start with a '---' front-matter line"
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError as exc:  # pragma: no cover - message path
                raise AssertionError(f"{where}: front-matter is not valid YAML: {exc}") from exc
            assert isinstance(data, dict), f"{where}: front-matter must be a YAML mapping"
            return data, "\n".join(lines[i + 1:])
    raise AssertionError(f"{where}: front-matter has no closing '---' line")


def headings(body: str) -> list[str]:
    """Markdown ATX headings outside fenced code blocks, stripped, typographic apostrophes
    normalized to ASCII (they render identically)."""
    out: list[str] = []
    in_fence = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{1,6} ", line):
            out.append(line.replace("’", "'"))
    return out


def load_playbook(key: str) -> tuple[dict[str, Any], str]:
    path = playbook_path(key)
    if not path.is_file():
        pytest.skip(f"{path.relative_to(REPO_ROOT).as_posix()} not written yet (another builder writes it "
                    "in parallel); test_all_playbooks_exist enforces its presence")
    text = path.read_text(encoding="utf-8")  # strict UTF-8: a decode error fails the test
    return split_front_matter(text, path.relative_to(REPO_ROOT).as_posix())


def expected_name(key: str) -> str:
    return FUNDS_NAME if key == FUNDS_KEY else common.sector_display(key)


def expected_title(key: str) -> str:
    if key == FUNDS_KEY:
        return f"# {FUNDS_NAME} — ETF Playbook"
    return f"# {common.sector_display(key)} — Sector Playbook"


def expected_benchmark(key: str, cfg: dict[str, Any]) -> str:
    return cfg["benchmark"] if key == FUNDS_KEY else cfg["sector_etfs"][key]


# =====================================================================================
# tests
# =====================================================================================


def test_all_playbooks_exist() -> None:
    expected = [playbook_path(k) for k in ALL_KEYS]
    missing = [p.relative_to(REPO_ROOT).as_posix() for p in expected if not p.is_file()]
    assert not missing, f"missing playbooks: {missing}"
    assert len(common.SECTOR_KEYS) == 11


def test_sector_list_matches_config() -> None:
    cfg = committee()
    assert set(cfg["sector_etfs"]) == set(common.SECTOR_KEYS)


def test_readme_documents_format() -> None:
    assert README.is_file(), "knowledge/README.md is missing"
    text = README.read_text(encoding="utf-8")
    for key in REQUIRED_KEYS:
        assert f"`{key}`" in text, f"README does not document front-matter key {key!r}"
    for heading in SECTOR_HEADINGS + ETF_HEADINGS:
        assert heading in text, f"README does not list heading {heading!r}"
    assert "etf_playbook.md" in text


@pytest.mark.parametrize("key", ALL_KEYS)
def test_front_matter_keys_and_types(key: str) -> None:
    fm, _ = load_playbook(key)
    missing = [k for k in REQUIRED_KEYS if k not in fm]
    assert not missing, f"{key}: front-matter lacks {missing}"
    assert fm["sector"] == key, f"{key}: front-matter sector is {fm['sector']!r}"
    assert fm["name"] == expected_name(key), f"{key}: name {fm['name']!r} != {expected_name(key)!r}"
    for field in NON_EMPTY_STR_LISTS:
        value = fm[field]
        assert isinstance(value, list) and value, f"{key}: {field} must be a non-empty list"
        bad = [v for v in value if not isinstance(v, str) or not v.strip()]
        assert not bad, f"{key}: {field} entries must be non-empty strings, got {bad!r}"
    tags = fm["edgar_tags_of_interest"]
    assert isinstance(tags, list), f"{key}: edgar_tags_of_interest must be a list (use [] when empty)"
    bad_tags = [t for t in tags if not isinstance(t, str) or not TAG_RE.match(t)]
    assert not bad_tags, f"{key}: edgar_tags_of_interest entries must be us-gaap tag names, got {bad_tags!r}"
    assert len(set(tags)) == len(tags), f"{key}: duplicate edgar tags"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_benchmark_etf_matches_config(key: str) -> None:
    fm, _ = load_playbook(key)
    want = expected_benchmark(key, committee())
    assert fm.get("benchmark_etf") == want, (
        f"{key}: benchmark_etf {fm.get('benchmark_etf')!r} != committee.yaml value {want!r}")


@pytest.mark.parametrize("key", ALL_KEYS)
def test_sector_class(key: str) -> None:
    fm, _ = load_playbook(key)
    want = common.sector_class(key)
    assert want == ("fund" if key == FUNDS_KEY else "financials" if key == "financials" else "default")
    assert fm.get("sector_class") == want, f"{key}: sector_class {fm.get('sector_class')!r} != {want!r}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_macro_sensitivities_use_configured_series(key: str) -> None:
    fm, _ = load_playbook(key)
    known = {s["id"] for s in committee()["macro"]["series"]}
    items = fm.get("macro_sensitivities")
    assert isinstance(items, list) and items, f"{key}: macro_sensitivities must be a non-empty list"
    for i, item in enumerate(items):
        where = f"{key}: macro_sensitivities[{i}]"
        assert isinstance(item, dict), f"{where} must be a mapping"
        assert set(item) >= {"series", "direction", "mechanism"}, f"{where} needs series/direction/mechanism"
        assert item["series"] in known, f"{where}: {item['series']!r} is not a FRED id in committee.yaml"
        assert item["direction"] in DIRECTIONS, f"{where}: direction {item['direction']!r} not in {sorted(DIRECTIONS)}"
        assert isinstance(item["mechanism"], str) and item["mechanism"].strip(), f"{where}: empty mechanism"
    series = [item["series"] for item in items]
    assert len(set(series)) == len(series), f"{key}: a FRED series is listed twice"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_headings_present_in_order(key: str) -> None:
    _, body = load_playbook(key)
    found = headings(body)
    h1 = [h for h in found if h.startswith("# ")]
    assert h1 == [expected_title(key)], f"{key}: expected one title {expected_title(key)!r}, found {h1!r}"
    wanted = ETF_HEADINGS if key == FUNDS_KEY else SECTOR_HEADINGS
    positions = []
    for heading in wanted:
        count = found.count(heading)
        assert count == 1, f"{key}: heading {heading!r} appears {count} times (expected exactly once)"
        positions.append(found.index(heading))
    assert positions == sorted(positions), f"{key}: section headings out of order: {found!r}"
    assert found.index(expected_title(key)) < positions[0], f"{key}: title must precede the sections"


# =====================================================================================
# etf playbook content checks (contract facts the fund analyst and sector head rely on)
# =====================================================================================


def etf_section(number: int) -> str:
    """Body text of ETF section ``## <number>. ...`` up to the next ``## `` heading."""
    if not ETF_PLAYBOOK.is_file():
        pytest.skip("knowledge/etf_playbook.md not written yet")
    _, body = split_front_matter(ETF_PLAYBOOK.read_text(encoding="utf-8"), "knowledge/etf_playbook.md")
    match = re.search(rf"^## {number}\. .*?$(.*?)(?=^## |\Z)", body, flags=re.M | re.S)
    assert match, f"etf playbook: section {number} not found"
    return " ".join(match.group(1).split())  # collapse line wraps for phrase matching


def test_etf_rules_section_lists_da_broken_sell() -> None:
    """DESIGN §4.10 rule (3): SELL if conv <= sell_max OR DA broken; for funds only when
    roles.da_for_funds is on, and only a justified deviation rebuts it."""
    cfg = committee()
    assert "da_for_funds" in cfg["roles"], "committee.yaml roles.da_for_funds is gone; update the playbook"
    assert "broken" in cfg["aggregation"]["da_adjust"]
    text = etf_section(6)
    precedence = text[text.index("Other precedence"):]
    sell_clause = precedence[:precedence.index("TRIM if between")]
    assert "sell_max" in sell_clause
    assert "broken" in sell_clause, "§6 SELL trigger must include a broken DA verdict"
    assert "da_for_funds" in sell_clause, "§6 DA-broken SELL must be tied to roles.da_for_funds"
    assert "deviation" in sell_clause, "§6 must say a DA-broken SELL is rebutted only via a deviation"


def _etf_order_inputs_source() -> str | None:
    src = (REPO_ROOT / "scripts" / "_steps.py").read_text(encoding="utf-8")
    match = re.search(r'if role == "etf":(.*?)return \[', src, flags=re.S)
    return match.group(1) if match else None


def test_etf_playbook_does_not_assign_analyst_unavailable_inputs() -> None:
    """DESIGN §7: agents read only their work order's inputs. While the etf order carries no CIO
    allocation / macro view, the playbook must keep those comparisons away from the etf analyst."""
    block = _etf_order_inputs_source()
    if block is None:
        pytest.skip("etf work-order input block not found in scripts/_steps.py")
    if "cio_allocation" in block or "macro_view" in block:
        pytest.skip("etf work orders now carry the CIO allocation / macro view")
    role = etf_section(1)
    assert "not the CIO allocation or the macro view" in role
    assert "never from memory" in role
    caps = etf_section(4)
    assert "the etf analyst tests only `max_sector_pct`" in caps
    rules = etf_section(6)
    assert "etf analyst names any look-through sector above `max_sector_pct`" in rules
    assert "The etf analyst must name the breach" not in rules


def test_etf_playbook_warns_on_proxy_priced_shares() -> None:
    """A TASE fund with proxy_ticker given as shares is valued at the proxy's USD close
    (etf_lookthrough flag ``proxy_priced``); the analyst must flag the weight as unreliable."""
    portfolio_doc = (CONFIG_DIR / "portfolio.yaml").read_text(encoding="utf-8")
    assert "proxy_ticker" in portfolio_doc
    lookthrough_src = (REPO_ROOT / "scripts" / "etf_lookthrough.py").read_text(encoding="utf-8")
    assert '"proxy_priced"' in lookthrough_src, "etf_lookthrough no longer emits proxy_priced; update §7"
    text = etf_section(7)
    for phrase in ("proxy_priced", "`shares`", "`weight_pct`", "unreliable", "`missing_data`",
                   "max_fund_position_pct"):
        assert phrase in text, f"§7 must mention {phrase} for proxy-priced TASE funds"
