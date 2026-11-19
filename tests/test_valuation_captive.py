"""Captive-finance net-debt treatment (valuation.py): listed vs unlisted ticker, override fraction,
flag text, reverse DCF, schema validity, config sanity."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from scripts import common, valuation as val
from test_valuation import cells_by_id, load, schema_errors

CONSOLIDATED_ND = 29_497_000_000.0
EBIT = 13_092_000_000.0
NO_CAPTIVES: dict[str, Any] = {"captive_finance_tickers": []}


@pytest.fixture()
def cat() -> dict:
    return load("CAT")


def grid(f: dict, **vcfg: Any) -> dict:
    """build_grid with the repo valuation config overlaid by ``vcfg``."""
    base = copy.deepcopy(common.committee_cfg()["valuation"])
    base.update(vcfg)
    return val.build_grid(f, None, "industrials", {"valuation": base})


def test_default_config_lists_the_captive_tickers() -> None:
    cfg = val.resolve_cfg()
    assert {"CAT", "DE", "F", "GM", "PCAR", "HOG"} <= val.captive_tickers(cfg)
    assert "AGCO" not in val.captive_tickers(cfg)           # AGCO's finance arm is an unconsolidated JV
    assert cfg["captive_finance_net_debt_pct_override"] == {}
    assert val.DEFAULTS["captive_finance_tickers"], "code-level default must not be empty"


def test_listed_ticker_deducts_no_net_debt(cat: dict) -> None:
    doc = grid(cat)
    assert doc["net_debt_basis"] == "captive_excluded"
    assert doc["net_debt"]["value"] == 0.0 and doc["net_debt"]["na_reason"] is None
    assert doc["net_debt_consolidated"]["value"] == pytest.approx(CONSOLIDATED_ND)
    cell = cells_by_id(doc)["DCF-NOPAT-r10-g06"]
    assert cell["params"]["net_debt"] == 0.0
    assert cell["params"]["equity_value"] == cell["params"]["ev"]                 # equity = EV
    assert cell["fv_per_share"] == pytest.approx(cell["params"]["ev"] / cat["shares"]["value"], abs=0.005)
    assert doc["reverse_dcf"]["assumptions"]["net_debt"] == 0.0
    assert doc["reverse_dcf"]["assumptions"]["net_debt_basis"] == "captive_excluded"
    ev_ebit = cells_by_id(doc)["EVEBIT-mid"]                                      # EV/EBIT shares the treatment
    assert ev_ebit["params"]["net_debt"] == 0.0
    assert ev_ebit["params"]["equity_value"] == pytest.approx(ev_ebit["params"]["multiple"] * EBIT, abs=0.01)
    assert not any("net debt missing" in m for m in doc["missing_data"])


def test_unlisted_ticker_keeps_consolidated_net_debt(cat: dict) -> None:
    listed, unlisted = grid(cat), grid(cat, **NO_CAPTIVES)
    assert unlisted["net_debt_basis"] == "consolidated"
    assert unlisted["net_debt"]["value"] == pytest.approx(CONSOLIDATED_ND)
    assert "net_debt_consolidated" in unlisted
    assert "captive_finance_debt_excluded" not in [f["code"] for f in unlisted["flags"]]
    a, b = cells_by_id(listed)["DCF-NOPAT-r10-g06"], cells_by_id(unlisted)["DCF-NOPAT-r10-g06"]
    shares = cat["shares"]["value"]
    assert a["fv_per_share"] - b["fv_per_share"] == pytest.approx(CONSOLIDATED_ND / shares, abs=0.01)
    assert 63.0 < CONSOLIDATED_ND / shares < 65.0                                  # the ~$64/share at stake
    # P/E cells ignore net debt entirely
    assert cells_by_id(listed)["PE-mid"]["fv_per_share"] == cells_by_id(unlisted)["PE-mid"]["fv_per_share"]
    # removing the captive debt lowers the price-implied growth
    assert (listed["reverse_dcf"]["implied_growth_pct"]["value"]
            < unlisted["reverse_dcf"]["implied_growth_pct"]["value"])


def test_unlisted_ticker_with_the_same_numbers_is_consolidated(cat: dict) -> None:
    other = copy.deepcopy(cat)
    other["ticker"] = "ZZZ"
    assert val.build_grid(other, None, "industrials")["net_debt_basis"] == "consolidated"


def test_flag_text_and_memo_fields(cat: dict) -> None:
    doc = grid(cat)
    flag = next(f for f in doc["flags"] if f["code"] == "captive_finance_debt_excluded")
    assert flag["severity"] == "medium"
    d = flag["detail"]
    assert "captive finance" in d and "NOT deducted" in d and "$29.5B" in d
    assert "industrial net debt and cash are ignored" in d.lower() and "slightly high" in d
    assert any("captive finance" in m and "net debt taken as 0" in m for m in doc["missing_data"])
    assert "net_debt_basis=captive_excluded" in doc["net_debt"]["note"]


def test_override_fraction_deducts_that_share(cat: dict) -> None:
    doc = grid(cat, captive_finance_net_debt_pct_override={"CAT": 0.10})
    assert doc["net_debt_basis"] == "override"
    assert doc["net_debt"]["value"] == pytest.approx(0.10 * CONSOLIDATED_ND)
    cell = cells_by_id(doc)["DCF-NOPAT-r10-g06"]
    assert cell["params"]["net_debt"] == pytest.approx(0.10 * CONSOLIDATED_ND, abs=0.01)
    assert cell["params"]["equity_value"] == pytest.approx(cell["params"]["ev"] - 0.10 * CONSOLIDATED_ND, abs=0.02)
    flag = next(f for f in doc["flags"] if f["code"] == "captive_finance_debt_excluded")
    assert "10%" in flag["detail"] and "assumption" in flag["detail"]
    zero = grid(cat, captive_finance_net_debt_pct_override={"CAT": 0.0})
    assert zero["net_debt_basis"] == "override" and zero["net_debt"]["value"] == 0.0
    full = grid(cat, captive_finance_net_debt_pct_override={"CAT": 1.0})
    assert full["net_debt"]["value"] == pytest.approx(CONSOLIDATED_ND)


def test_override_works_for_ticker_not_on_the_list(cat: dict) -> None:
    doc = grid(cat, captive_finance_tickers=[], captive_finance_net_debt_pct_override={"cat": 0.25})
    assert doc["net_debt_basis"] == "override" and doc["net_debt"]["value"] == pytest.approx(0.25 * CONSOLIDATED_ND)


@pytest.mark.parametrize("bad", [1.5, -0.1, "abc"])
def test_invalid_override_is_ignored_and_flagged(cat: dict, bad: Any) -> None:
    doc = grid(cat, captive_finance_net_debt_pct_override={"CAT": bad})
    assert doc["net_debt_basis"] == "captive_excluded"           # falls back to the list treatment
    assert "captive_finance_override_invalid" in [f["code"] for f in doc["flags"]]


def test_listed_ticker_with_missing_consolidated_net_debt_still_values(cat: dict) -> None:
    f = copy.deepcopy(cat)
    for k in ("net_debt", "total_debt"):
        f["balance"][k].update(value=None, na_reason="missing")
    doc = grid(f)
    assert doc["net_debt"]["value"] == 0.0 and doc["net_debt_consolidated"]["value"] is None
    assert cells_by_id(doc)["DCF-NOPAT-r10-g06"]["fv_per_share"] is not None
    ov = grid(f, captive_finance_net_debt_pct_override={"CAT": 0.1})        # fraction of an unknown -> unknown
    assert ov["net_debt"]["value"] is None and cells_by_id(ov)["DCF-NOPAT-r10-g06"]["na_reason"] == "missing"
    assert any("net debt missing" in m for m in ov["missing_data"])


def test_before_after_numbers(cat: dict) -> None:
    """Hand check on the CAT-like fixture: only the net-debt deduction differs."""
    before, after = grid(cat, **NO_CAPTIVES), grid(cat)
    shares = cat["shares"]["value"]
    b, a = cells_by_id(before)["DCF-NOPAT-r10-g06"], cells_by_id(after)["DCF-NOPAT-r10-g06"]
    ev = a["params"]["ev"]
    assert b["fv_per_share"] == pytest.approx((ev - CONSOLIDATED_ND) / shares, abs=0.01)
    assert a["fv_per_share"] == pytest.approx(ev / shares, abs=0.01)
    assert after["reverse_dcf"]["implied_growth_pct"]["value"] < before["reverse_dcf"]["implied_growth_pct"]["value"]


def test_outputs_validate_against_schema(cat: dict) -> None:
    for doc in (grid(cat), grid(cat, **NO_CAPTIVES),
                grid(cat, captive_finance_net_debt_pct_override={"CAT": 0.2})):
        assert schema_errors(doc, "urn:agent-investors:data/valuation") == []
        json.dumps(doc, allow_nan=False)
    bad = grid(cat)
    bad["net_debt_basis"] = "bogus"
    assert schema_errors(bad, "urn:agent-investors:data/valuation")


def test_possible_captive_flag_is_propagated(cat: dict) -> None:
    f = copy.deepcopy(cat)
    f["flags"] = [{"code": "possible_captive_finance", "severity": "low", "detail": "net debt is 40% of market cap"}]
    doc = grid(f, **NO_CAPTIVES)
    assert any(x["code"] == "possible_captive_finance" and x["detail"].startswith("from fundamentals:")
               for x in doc["flags"])


def test_repo_config_has_captive_keys() -> None:
    cfg = common.committee_cfg()["valuation"]
    assert isinstance(cfg["captive_finance_tickers"], list) and cfg["captive_finance_net_debt_pct_override"] == {}
    assert 0 < cfg["possible_captive_net_debt_to_mcap"] < 1
