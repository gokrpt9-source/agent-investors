"""fetch_macro.py (DESIGN §4.5): FRED CSV parsing, date-lookup changes, monthly period-end as_of,
YoY, diff vs pct changes, history tail, caching and partial failure. Fully offline: the FRED
endpoint is served from tests/fixtures/fred (SYNTHETIC data in the real CSV format)."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import common, fetch_macro

REPO_ROOT = Path(__file__).resolve().parents[1]
FRED_FIX = REPO_ROOT / "tests" / "fixtures" / "fred"
AS_OF = "2026-10-02"


# --------------------------------------------------------------------------- helpers
def _schema_errors(doc: dict, name: str) -> list[str]:
    resources = []
    for p in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        resources.append((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)))
    validator = Draft202012Validator(common.load_schema(name), registry=Registry().with_resources(resources),
                                     format_checker=FormatChecker())
    plain = json.loads(common.dumps_json(doc))
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(plain)]


def _csv_values(name: str) -> dict[str, str]:
    """Raw cells of a fixture CSV (independent of the parser under test)."""
    text = (FRED_FIX / "http" / name).read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    return {r[0]: r[1] for r in rows[1:] if r}


@pytest.fixture
def fred(use_fixtures):
    return use_fixtures(FRED_FIX)


# --------------------------------------------------------------------------- parsing
def test_parse_new_header_blank_and_dot_missing():
    text = "observation_date,DGS10\n2026-07-01,4.28\n2026-07-02,\n2026-07-03,.\n2026-07-06,4.30\n"
    obs = fetch_macro.parse_fred_csv(text, "DGS10")
    assert obs == [(date(2026, 7, 1), 4.28), (date(2026, 7, 2), None), (date(2026, 7, 3), None),
                   (date(2026, 7, 6), 4.30)]


def test_parse_old_date_header_bom_crlf_and_unsorted():
    text = "﻿DATE,CPIAUCSL\r\n2026-02-01,324.0\r\n2026-01-01,323.0\r\nnot-a-date,1\r\n2026-01-01,323.5\r\n"
    obs = fetch_macro.parse_fred_csv(text, "CPIAUCSL")
    # sorted, junk row skipped, duplicate date keeps the last row
    assert obs == [(date(2026, 1, 1), 323.5), (date(2026, 2, 1), 324.0)]


def test_parse_picks_named_column():
    text = "observation_date,OTHER,DGS2\n2026-01-02,9.9,3.5\n"
    assert fetch_macro.parse_fred_csv(text, "DGS2") == [(date(2026, 1, 2), 3.5)]


@pytest.mark.parametrize("text", ["<!DOCTYPE html><html><body>blocked</body></html>", "", "foo,bar\n1,2\n",
                                  "observation_date,DGS10\n"])
def test_parse_rejects_html_empty_and_bad_header(text):
    with pytest.raises(common.DataUnavailable):
        fetch_macro.parse_fred_csv(text, "DGS10")


# --------------------------------------------------------------------------- date helpers
def test_shift_months_and_month_end():
    assert fetch_macro.shift_months(date(2026, 3, 1), 3) == date(2025, 12, 1)
    assert fetch_macro.shift_months(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert fetch_macro.shift_months(date(2026, 8, 1), 12) == date(2025, 8, 1)
    assert fetch_macro.month_end(date(2024, 2, 1)) == date(2024, 2, 29)
    assert fetch_macro.days_to_months(91) == 3 and fetch_macro.days_to_months(365) == 12


def test_monthly_3m_lookup_is_month_based_not_91_days():
    # 2026-03-01 minus 91 days is 2025-11-30 -> a naive day lookup would pick November.
    obs = [(date(2025, m, 1), 100.0 + m) for m in range(1, 13)] + [(date(2026, m, 1), 120.0 + m) for m in (1, 2, 3)]
    meta = {"units": "index", "frequency": "m", "change": "pct", "yoy": True}
    e = fetch_macro.compute_series("XX", obs, meta, as_of=date(2026, 10, 2))
    assert e["chg_3m"]["note"].startswith("prior 2025-12-01")
    assert e["chg_3m"]["value"] == pytest.approx(round((123.0 / 112.0 - 1) * 100, 4))
    assert e["yoy_pct"]["value"] == pytest.approx(round((123.0 / 103.0 - 1) * 100, 4))
    assert e["latest"]["as_of"] == "2026-03-31" and e["latest"]["observation_date"] == "2026-03-01"


# --------------------------------------------------------------------------- full run (fixtures)
def test_run_daily_diff_series_by_date_lookup(fred):
    payload = fetch_macro.run(["DGS10"], as_of=AS_OF)
    e = payload["series"]["DGS10"]
    raw = _csv_values("DGS10.csv")
    # observations after as_of (2026-10-05, 2026-10-06) are ignored
    assert e["observation_date"] == "2026-10-02"
    assert e["latest"]["value"] == pytest.approx(float(raw["2026-10-02"]))
    assert e["latest"]["as_of"] == "2026-10-02"  # daily -> as_of is the observation date
    assert e["latest"]["unit"] == "pct" and e["latest"]["source_id"] == "D:FRED:DGS10"
    assert e["latest"]["kind"] == "macro"
    # 3m: 2026-10-02 - 91 d = 2026-07-03, a '.' cell -> last valid obs on/before = 2026-07-02
    assert e["chg_3m"]["value"] == pytest.approx(round(float(raw["2026-10-02"]) - float(raw["2026-07-02"]), 4))
    assert e["chg_3m"]["unit"] == "pp"
    assert "2026-07-02" in e["chg_3m"]["derivation"]
    # 12m: 2026-10-02 - 365 d = 2025-10-02 (a Thursday with data)
    assert e["chg_12m"]["value"] == pytest.approx(round(float(raw["2026-10-02"]) - float(raw["2025-10-02"]), 4))
    assert e["yoy_pct"] is None  # yoy: false in committee.yaml
    assert e["frequency"] == "d" and e["provider"] == "fred" and e["stale"] is False
    tail = e["history_tail"]
    assert len(tail) == 13 and tail[-1][0] == "2026-10-02"
    assert all(v is not None for _, v in tail)  # missing cells are skipped
    assert not _schema_errors(payload, "macro")


def test_run_monthly_cpi_yoy_period_end_and_tail(fred):
    payload = fetch_macro.run(["CPIAUCSL"], as_of=AS_OF)
    e = payload["series"]["CPIAUCSL"]
    assert e["observation_date"] == "2026-08-01"
    latest = e["latest"]
    assert latest["value"] == pytest.approx(329.6)
    assert latest["as_of"] == "2026-08-31"  # monthly -> period end
    assert latest["observation_date"] == "2026-08-01" and latest["period_end"] == "2026-08-31"
    assert latest["unit"] == "index"
    # pct change: 3m vs 2026-05-01 (327.0), 12m vs 2025-08-01 (320.0)
    assert e["chg_3m"]["value"] == pytest.approx(0.7951)
    assert e["chg_3m"]["unit"] == "pct"
    assert e["chg_12m"]["value"] == pytest.approx(3.0)
    # YoY = 12 observations back on the monthly series
    assert e["yoy_pct"]["value"] == pytest.approx(3.0)
    assert e["yoy_pct"]["unit"] == "pct" and e["yoy_pct"]["as_of"] == "2026-08-31"
    # tail: 13 non-missing observations; 2025-10-01 ('.') is skipped
    dates = [d for d, _ in e["history_tail"]]
    assert len(dates) == 13 and dates[0] == "2025-07-01" and dates[-1] == "2026-08-01"
    assert "2025-10-01" not in dates
    assert not _schema_errors(payload, "macro")


def test_run_monthly_diff_series_unrate(fred):
    e = fetch_macro.run(["UNRATE"], as_of=AS_OF)["series"]["UNRATE"]
    assert e["latest"]["value"] == pytest.approx(4.3)
    assert e["chg_3m"]["value"] == pytest.approx(0.2) and e["chg_3m"]["unit"] == "pp"
    assert e["chg_12m"]["value"] == pytest.approx(0.3)
    assert e["yoy_pct"] is None


def test_run_pct_change_daily_with_bom(fred):
    e = fetch_macro.run(["DCOILWTICO"], as_of=AS_OF)["series"]["DCOILWTICO"]
    raw = _csv_values("DCOILWTICO.csv")
    latest = float(raw["2026-10-02"])
    prior = float(raw["2026-07-03"])  # 2026-10-02 - 91 d = 2026-07-03 (has a value in this file)
    assert e["latest"]["unit"] == "USD/bbl"
    assert e["chg_3m"]["unit"] == "pct"
    assert e["chg_3m"]["value"] == pytest.approx(round((latest / prior - 1) * 100, 4))


def test_insufficient_history_is_null_with_reason(fred):
    payload = fetch_macro.run(["CPILFESL"], as_of=AS_OF)
    e = payload["series"]["CPILFESL"]
    assert e["latest"]["value"] == pytest.approx(335.0)
    assert e["chg_3m"]["value"] == pytest.approx(round((335.0 / 332.0 - 1) * 100, 4))
    assert e["chg_12m"]["value"] is None and e["chg_12m"]["na_reason"] == "insufficient_history"
    assert e["yoy_pct"]["value"] is None and e["yoy_pct"]["na_reason"] == "insufficient_history"
    assert any("CPILFESL" in m and "chg_12m" in m for m in payload["missing_data"])
    assert not _schema_errors(payload, "macro")


def test_partial_failure_gives_null_entries_and_missing_data(fred, tmp_path):
    out = tmp_path / "phase1" / "macro.json"
    payload = fetch_macro.run(["DGS10", "VIXCLS", "T10Y2Y", "DFF"], out=out, as_of=AS_OF)
    series = payload["series"]
    assert list(series) == ["DGS10", "VIXCLS", "T10Y2Y", "DFF"]
    assert series["DGS10"]["latest"]["value"] is not None
    for sid in ("VIXCLS", "T10Y2Y", "DFF"):  # 404 / HTML page / no fixture
        e = series[sid]
        assert e["provider"] == "failed"
        assert e["latest"]["value"] is None and e["latest"]["na_reason"] == "missing"
        assert e["chg_3m"]["value"] is None and e["chg_12m"]["value"] is None
        assert e["observation_date"] is None and e["history_tail"] == []
        assert any(m.startswith(f"FRED {sid}") for m in payload["missing_data"])
    assert {f["code"] for f in payload["flags"]} >= {"series_failed"}
    # only series that returned data are cited as sources
    assert [s["id"] for s in payload["sources"]] == ["D:FRED:DGS10"]
    assert payload["sources"][0]["url"] == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    written = common.read_json(out)
    assert written["schema"] == "macro/v1" and written["series"]["DGS10"] == json.loads(
        common.dumps_json(series["DGS10"]))
    assert not _schema_errors(written, "macro")


def test_default_series_list_comes_from_config(fred):
    payload = fetch_macro.run(as_of=AS_OF)
    cfg_ids = [s["id"] for s in common.committee_cfg()["macro"]["series"]]
    assert list(payload["series"]) == cfg_ids
    assert payload["series"]["CPIAUCSL"]["yoy_pct"]["value"] == pytest.approx(3.0)
    assert payload["series"]["CPILFESL"]["yoy_pct"] is not None  # yoy: true -> Fact (null value)
    assert payload["series"]["DFF"]["yoy_pct"] is None  # yoy: false, failed -> null
    assert not _schema_errors(payload, "macro")


def test_all_failed_flag_offline():
    # no fixtures: AGINV_OFFLINE=1 -> every series fails, nothing raised
    payload = fetch_macro.run(["DGS10", "UNRATE"], as_of=AS_OF)
    assert all(e["provider"] == "failed" for e in payload["series"].values())
    assert "all_series_failed" in {f["code"] for f in payload["flags"]}
    assert len(payload["missing_data"]) == 2
    assert not _schema_errors(payload, "macro")


# --------------------------------------------------------------------------- caching
def test_cache_hit_then_served_without_network(fred, monkeypatch, tmp_path):
    first = fetch_macro.run(["UNRATE"], as_of=AS_OF)
    assert first["series"]["UNRATE"]["provider"] == "fred"
    assert common.DiskCache().path_for("macro", "UNRATE").is_file()
    # fixtures off + offline: a fresh cache entry is used, no fetch attempted
    monkeypatch.delenv(common.ENV_FIXTURES_DIR)
    monkeypatch.setenv(common.ENV_OFFLINE, "1")
    second = fetch_macro.run(["UNRATE"], as_of=AS_OF)
    assert second["series"]["UNRATE"]["provider"] == "cache"
    assert second["series"]["UNRATE"]["latest"] == first["series"]["UNRATE"]["latest"]


def test_stale_cache_fallback_is_flagged(fred):
    cache = common.DiskCache()
    cache.put("macro", "VIXCLS", {"series_id": "VIXCLS",
                                  "observations": [["2026-09-29", 18.0], ["2026-09-30", None], ["2026-10-01", 19.8]]},
              common.fred_csv_url("VIXCLS"))
    entry_path = cache.path_for("macro", "VIXCLS")
    entry = common.read_json(entry_path)
    entry["fetched_at"] = "2026-01-01T00:00:00+00:00"  # older than the 24 h TTL
    common.write_json(entry_path, entry)
    payload = fetch_macro.run(["VIXCLS"], as_of=AS_OF)  # fixture says 404 -> stale cache
    e = payload["series"]["VIXCLS"]
    assert e["provider"] == "cache_stale" and e["stale"] is True
    assert e["latest"]["value"] == pytest.approx(19.8)
    assert any(f["code"] == "stale_cache" for f in payload["flags"])
    assert not _schema_errors(payload, "macro")


def test_errors_are_not_cached(fred):
    fetch_macro.run(["VIXCLS"], as_of=AS_OF)
    assert not common.DiskCache().path_for("macro", "VIXCLS").exists()


# --------------------------------------------------------------------------- CLI
def test_cli_writes_file(fred, tmp_path, capsys):
    out = tmp_path / "macro.json"
    rc = fetch_macro.main(["--series", "UNRATE,CPIAUCSL", "--out", str(out), "--as-of", AS_OF])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["series"] == 2 and summary["failed"] == []
    doc = common.read_json(out)
    assert doc["as_of"] == AS_OF and set(doc["series"]) == {"UNRATE", "CPIAUCSL"}


def _stderr_lines(capsys) -> list[str]:
    cap = capsys.readouterr()
    assert "Traceback" not in cap.err and "Traceback" not in cap.out
    return [ln for ln in cap.err.splitlines() if ln.strip()]


@pytest.mark.parametrize("bad", ["garbage", "2026-13-45", "10/02/2026"])
def test_cli_bad_as_of_flag_is_rc2_one_line(fred, tmp_path, capsys, bad):
    out = tmp_path / "macro.json"
    rc = fetch_macro.main(["--series", "UNRATE", "--out", str(out), "--as-of", bad])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("error: ") and "--as-of" in lines[0]
    assert not out.exists()


def test_cli_bad_as_of_env_is_rc2_one_line(fred, monkeypatch, capsys):
    monkeypatch.setenv(common.ENV_AS_OF, "not-a-date")
    rc = fetch_macro.main(["--series", "UNRATE"])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("error: ") and common.ENV_AS_OF in lines[0]


def test_cli_missing_config_dir_without_series_is_rc2(fred, tmp_path, capsys):
    rc = fetch_macro.main(["--config-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "macro.json")])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("error: ") and "committee.yaml" in lines[0]
    assert not (tmp_path / "macro.json").exists()


def test_cli_unparseable_config_is_one_line(fred, tmp_env, tmp_path, capsys):
    (tmp_env.config_dir / "committee.yaml").write_text("macro: [unclosed\n  - a: {b\n", encoding="utf-8")
    common.clear_caches()
    rc = fetch_macro.main([])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("error: ")


def test_cli_empty_macro_series_config_is_rc2(fred, tmp_env, capsys):
    tmp_env.update_config("committee", lambda c: c["macro"].update(series=[]))
    rc = fetch_macro.main([])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "macro.series" in lines[0]


def test_cli_explicit_series_works_without_config(fred, tmp_path, capsys):
    """--series needs no committee.yaml (run() falls back to defaults), so a missing config is fine."""
    rc = fetch_macro.main(["--series", "UNRATE", "--config-dir", str(tmp_path / "nope"),
                           "--out", str(tmp_path / "m.json"), "--as-of", AS_OF])
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["series"] == 1


def test_cli_unwritable_out_is_rc2_not_a_traceback(fred, tmp_path, capsys):
    # --out names an existing directory: the atomic write cannot replace it
    rc = fetch_macro.main(["--series", "UNRATE", "--out", str(tmp_path), "--as-of", AS_OF])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("error: ")


def test_cli_fixture_mode_with_real_cache_dir_is_refused(fred, tmp_path, monkeypatch, capsys):
    """Fixture data must never land in <repo>/data/cache: without AGINV_CACHE_DIR the CLI stops with rc 2."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", root)
    monkeypatch.delenv(common.ENV_CACHE_DIR)
    rc = fetch_macro.main(["--series", "UNRATE", "--as-of", AS_OF])
    assert rc == 2
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "real cache dir" in lines[0] and common.ENV_CACHE_DIR in lines[0]
    assert not (root / "data").exists()
    with pytest.raises(common.ConfigError):
        fetch_macro.run(["UNRATE"], as_of=AS_OF)


def test_cli_fixture_mode_with_explicit_cache_dir_flag(fred, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(common.ENV_CACHE_DIR)
    cache = tmp_path / "scratch_cache"
    rc = fetch_macro.main(["--series", "UNRATE", "--as-of", AS_OF, "--cache-dir", str(cache)])
    assert rc == 0 and (cache / "macro" / "UNRATE.json").is_file()
    # second run is served from that cache even though the fixture seam is also active
    fetch_macro.main(["--series", "UNRATE", "--as-of", AS_OF, "--cache-dir", str(cache)])
    out2 = fetch_macro.run(["UNRATE"], as_of=AS_OF)
    assert out2["series"]["UNRATE"]["provider"] == "cache"
