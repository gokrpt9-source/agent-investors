"""valuation.py (DESIGN 4.4): DCF vs a hand calculation, tv_share flag, reverse DCF, financials
P/B cells, negative base CF -> na cells, cell id format, schema-valid output, run() + CLI."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import common, valuation as val

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "valuation"


# --------------------------------------------------------------------------- helpers
def schema_errors(doc: Any, schema_id: str) -> list[str]:
    resources = []
    for p in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    reg = Registry().with_resources(resources)
    validator = Draft202012Validator(reg.contents(schema_id), registry=reg, format_checker=FormatChecker())
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(doc)]


def load(name: str) -> dict:
    return json.loads((FIX / f"fundamentals_{name}.json").read_text(encoding="utf-8"))


def cells_by_id(doc: dict) -> dict[str, dict]:
    return {c["id"]: c for c in doc["cells"]}


@pytest.fixture()
def tst() -> dict:
    return load("TST")


@pytest.fixture()
def bnk() -> dict:
    return load("BNK")


# --------------------------------------------------------------------------- DCF arithmetic
def test_dcf_matches_hand_calculation(tst: dict) -> None:
    """TST: EBITDA 1250, capex 250, tax 20 % -> NOPAT 800; net debt 1000; 100 shares.
    r = 10 %, g = 6 % -> exit multiple 15x (g <= 7 %), 5 years."""
    doc = val.build_grid(tst, None, "consumer_staples")
    assert doc["base_cf"]["nopat"]["value"] == pytest.approx(800.0)
    cell = cells_by_id(doc)["DCF-NOPAT-r10-g06"]
    # hand calculation, year by year
    pv = [800 * 1.06 / 1.10,                 # 770.909
          800 * 1.06 ** 2 / 1.10 ** 2,       # 742.876
          800 * 1.06 ** 3 / 1.10 ** 3,       # 715.863
          800 * 1.06 ** 4 / 1.10 ** 4,       # 689.832
          800 * 1.06 ** 5 / 1.10 ** 5]       # 664.744
    pv_tv = 800 * 1.06 ** 5 * 15 / 1.10 ** 5  # 9971.19
    ev = sum(pv) + pv_tv
    assert sum(pv) == pytest.approx(3584.2247, abs=1e-3)
    assert pv_tv == pytest.approx(9971.1936, abs=1e-3)
    assert cell["fv_per_share"] == pytest.approx((ev - 1000) / 100, abs=0.005)
    assert cell["fv_per_share"] == 125.55
    assert cell["tv_share"] == pytest.approx(pv_tv / ev, abs=1e-4)
    assert cell["params"]["exit_multiple"] == 15
    assert cell["params"]["equity_value"] == pytest.approx(ev - 1000, abs=0.01)
    assert cell["flags"] == [] and cell["na_reason"] is None
    # zero growth, r 10 %: exit multiple 10x -> sum of an annuity + 10x / 1.1^5
    zero = cells_by_id(doc)["DCF-NOPAT-r10-g00"]
    annuity = sum(800 / 1.1 ** t for t in range(1, 6))
    assert zero["fv_per_share"] == pytest.approx((annuity + 8000 / 1.1 ** 5 - 1000) / 100, abs=0.005)


def test_exit_multiple_piecewise() -> None:
    table = common.committee_cfg()["valuation"]["exit_multiple_by_growth"]
    assert [val.exit_multiple(g, table) for g in (-0.05, 0.0, 0.03, 0.04, 0.06, 0.07, 0.10, 0.11, 0.15)] == \
        [10, 10, 12, 12, 15, 15, 18, 18, 22]


def test_fcf_cells_use_fcf_and_tax_rate_from_fundamentals(tst: dict) -> None:
    doc = val.build_grid(tst, None, "consumer_staples")
    assert doc["base_cf"]["fcf"]["value"] == 700
    assert doc["tax_rate"]["value"] == pytest.approx(0.20)   # 20 pct -> 0.20 fraction
    c = cells_by_id(doc)["DCF-FCF-r10-g00"]
    annuity = sum(700 / 1.1 ** t for t in range(1, 6))
    assert c["fv_per_share"] == pytest.approx((annuity + 7000 / 1.1 ** 5 - 1000) / 100, abs=0.005)
    # missing tax rate -> configured default 21 % and a flag
    t2 = copy.deepcopy(tst)
    t2["valuation"]["tax_rate_used"] = common.na_fact("pct", "D:SEC:TST:CF", "2026-06-30", "fundamental")
    d2 = val.build_grid(t2, None, "consumer_staples")
    assert d2["base_cf"]["nopat"]["value"] == pytest.approx(1000 * 0.79)
    assert any(f["code"] == "default_tax_rate" for f in d2["flags"])


def test_tv_share_flag(tst: dict) -> None:
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["valuation"]["exit_multiple_by_growth"] = [[1.0, 60]]
    doc = val.build_grid(tst, None, "consumer_staples", cfg)
    cell = cells_by_id(doc)["DCF-NOPAT-r10-g06"]
    assert cell["tv_share"] > 0.9
    assert "tv_gt_90" in cell["flags"]
    assert any(f["code"] == "tv_gt_90" for f in doc["flags"])
    normal = val.build_grid(tst, None, "consumer_staples")
    assert all("tv_gt_90" not in c["flags"] for c in normal["cells"])
    assert not any(f["code"] == "tv_gt_90" for f in normal["flags"])


# --------------------------------------------------------------------------- reverse DCF
def test_reverse_dcf_recovers_known_growth(tst: dict) -> None:
    # price implied by NOPAT 800, r 10 %, exit 15x, g = 7 %
    price = val.fair_value_per_share(800.0, 0.10, 0.07, 5, 15.0, 1000.0, 100.0)
    doc = val.build_grid(tst, price, "consumer_staples")
    implied = doc["reverse_dcf"]["implied_growth_pct"]
    assert implied["value"] == pytest.approx(7.0, abs=0.01)
    assert implied["unit"] == "pct" and implied["source_id"] == "D:VAL:TST"
    assert doc["reverse_dcf"]["assumptions"]["exit_multiple"] == 15
    assert doc["reverse_dcf"]["assumptions"]["discount_rate"] == 0.10
    assert doc["price"]["value"] == pytest.approx(price)
    # negative implied growth is found too
    low = val.fair_value_per_share(800.0, 0.10, -0.12, 5, 15.0, 1000.0, 100.0)
    g, why = val.reverse_dcf_growth(800.0, low, 1000.0, 100.0, r=0.10, multiple=15.0, years=5, bounds=[-0.3, 0.6])
    assert why is None and g == pytest.approx(-0.12, abs=1e-6)


def test_reverse_dcf_null_without_sign_change(tst: dict) -> None:
    doc = val.build_grid(tst, 100000.0, "consumer_staples")
    implied = doc["reverse_dcf"]["implied_growth_pct"]
    assert implied["value"] is None
    assert implied["na_reason"] == "not_meaningful"
    assert doc["reverse_dcf"]["assumptions"]["null_reason"] == "no_sign_change"


# --------------------------------------------------------------------------- financials / negative CF
def test_financials_use_pe_and_pb_cells(bnk: dict) -> None:
    doc = val.build_grid(bnk, None, "financials")
    ids = cells_by_id(doc)
    assert {"PB-low", "PB-mid", "PB-high", "PE-low", "PE-mid", "PE-high"} <= set(ids)
    assert not any(i.startswith("EVEBIT") for i in ids)
    assert ids["PB-mid"]["fv_per_share"] == pytest.approx(1.5 * 4000 / 100)
    assert ids["PE-low"]["fv_per_share"] == pytest.approx(9 * 500 / 100)
    assert ids["PB-mid"]["params"]["assumption"] is True
    assert ids["PB-mid"]["params"]["source_id"] == "D:CFG:valuation.multiple_bands"
    assert doc["sector_class"] == "financials"
    assert "p_b" in doc["methods_available"] and "ev_ebit" not in doc["methods_available"]
    # no EBITDA for the bank -> NOPAT cells are na ("missing"), FCF cells computed but flagged
    assert ids["DCF-NOPAT-r10-g03"]["na_reason"] == "missing"
    assert ids["DCF-FCF-r10-g03"]["fv_per_share"] is not None
    assert "financials_low_reliability" in ids["DCF-FCF-r10-g03"]["flags"]
    assert any(f["code"] == "dcf_financials_caution" for f in doc["flags"])
    assert doc["reverse_dcf"]["implied_growth_pct"]["value"] is None
    assert schema_errors(doc, "urn:agent-investors:data/valuation") == []


def test_default_sector_uses_pe_and_ev_ebit(tst: dict) -> None:
    doc = val.build_grid(tst, None, "consumer_staples")
    ids = cells_by_id(doc)
    assert ids["PE-mid"]["fv_per_share"] == pytest.approx(21 * 6.0)          # EPS 600/100
    assert ids["EVEBIT-mid"]["fv_per_share"] == pytest.approx((17 * 1000 - 1000) / 100)
    assert not any(i.startswith("PB-") for i in ids)
    assert doc["methods_available"] == ["dcf_nopat", "dcf_fcf", "pe", "ev_ebit"]


def test_negative_base_cf_gives_na_cells(tst: dict) -> None:
    t = copy.deepcopy(tst)
    t["ttm"]["ebitda"]["value"] = 200      # EBITDA 200 - capex 250 < 0 -> NOPAT negative
    t["ttm"]["fcf"]["value"] = -50
    t["ttm"]["net_income"]["value"] = -10
    doc = val.build_grid(t, None, "consumer_staples")
    dcf = [c for c in doc["cells"] if c["method"] in ("dcf_nopat", "dcf_fcf")]
    assert len(dcf) == 20
    assert all(c["fv_per_share"] is None and c["na_reason"] == "base_cf_not_positive" for c in dcf)
    ids = cells_by_id(doc)
    assert ids["PE-mid"]["fv_per_share"] is None and ids["PE-mid"]["na_reason"] == "negative_earnings"
    assert ids["EVEBIT-mid"]["fv_per_share"] is not None   # EBIT still positive
    assert doc["methods_available"] == ["ev_ebit"]
    implied = doc["reverse_dcf"]["implied_growth_pct"]
    assert implied["value"] is None and implied["na_reason"] == "not_meaningful"
    assert schema_errors(doc, "urn:agent-investors:data/valuation") == []


def test_non_sec_fundamentals_give_all_na(tst: dict) -> None:
    t = copy.deepcopy(tst)
    for section in ("ttm", "balance"):
        for k, f in t[section].items():
            t[section][k] = common.na_fact(f["unit"], None, None, "fundamental")
    t["shares"] = common.na_fact("shares", None, None, "fundamental")
    t["cik"] = None
    t["coverage"] = "non_sec"
    doc = val.build_grid(t, 50.0, "consumer_staples")
    assert all(c["fv_per_share"] is None for c in doc["cells"])
    assert doc["methods_available"] == []
    assert doc["missing_data"]
    assert schema_errors(doc, "urn:agent-investors:data/valuation") == []


# --------------------------------------------------------------------------- ids + schema
def test_cell_id_format(tst: dict) -> None:
    doc = val.build_grid(tst, None, "consumer_staples")
    dcf_ids = [c["id"] for c in doc["cells"] if c["method"].startswith("dcf_")]
    assert len(dcf_ids) == 2 * 2 * 5
    assert all(re.fullmatch(r"DCF-(NOPAT|FCF)-r(09|10)-g(00|03|06|10|15)", i) for i in dcf_ids)
    assert "DCF-NOPAT-r10-g06" in dcf_ids and "DCF-FCF-r09-g15" in dcf_ids
    assert len(set(c["id"] for c in doc["cells"])) == len(doc["cells"])
    assert val.dcf_cell_id("nopat", 0.10, 0.06) == "DCF-NOPAT-r10-g06"
    assert val.dcf_cell_id("fcf", 0.095, 0.0) == "DCF-FCF-r09p5-g00"
    assert val.multiple_cell_id("ev_ebit", "low") == "EVEBIT-low"


def test_output_validates_and_carries_provenance(tst: dict) -> None:
    doc = val.build_grid(tst, None, "consumer_staples")
    assert schema_errors(doc, "urn:agent-investors:data/valuation") == []
    ids = [s["id"] for s in doc["sources"]]
    assert ids[0] == "D:VAL:TST" and "D:SEC:TST:CF" in ids and "D:PX:TST" in ids
    val_src = doc["sources"][0]
    assert val_src["url"] == "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json"
    assert doc["shares"]["value"] == 100 and doc["net_debt"]["value"] == 1000
    assert doc["base_cf"]["nopat"]["filed"] == "2026-07-30"
    assert any(f["code"] == "multiple_bands_are_assumptions" for f in doc["flags"])


def test_run_and_cli_write_file(tmp_path: Path, capsys) -> None:
    out = tmp_path / "valuation_TST.json"
    doc = val.run("tst", FIX / "fundamentals_TST.json", out, sector="consumer_staples")
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["cells"] == doc["cells"]
    assert on_disk["sources"][0]["path"] == out.as_posix()
    with pytest.raises(ValueError):
        val.run("XYZ", FIX / "fundamentals_TST.json", tmp_path / "x.json", sector="consumer_staples")
    out2 = tmp_path / "cli" / "valuation_BNK.json"
    assert val.main(["--ticker", "BNK", "--fundamentals", str(FIX / "fundamentals_BNK.json"),
                     "--sector", "financials", "--out", str(out2)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ticker"] == "BNK" and "p_b" in summary["methods_available"]
    assert schema_errors(json.loads(out2.read_text(encoding="utf-8")), "urn:agent-investors:data/valuation") == []
