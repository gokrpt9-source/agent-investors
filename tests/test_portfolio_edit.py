"""portfolio_edit.py: comment-preserving, validated edits of portfolio.yaml. Offline."""
from __future__ import annotations

import shutil

import pytest
import yaml

from scripts import common, portfolio_edit as pe

REPO = common.REPO_ROOT


@pytest.fixture
def pf(tmp_path):
    p = tmp_path / "config" / "portfolio.yaml"
    p.parent.mkdir()
    shutil.copy2(REPO / "tests" / "fixtures" / "portfolio_edit" / "portfolio.yaml", p)
    return p


def _data(p):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _h(d, t):
    return next(h for h in d["holdings"] if h["ticker"] == t)


def test_edit_shares_cost_cash_keeps_comments_and_notes(pf, tmp_env):
    before = pf.read_text(encoding="utf-8")
    res = pe.save_edits({"cash_usd": 999.5, "holdings": {"PLTR": {"shares": 20, "cost_basis": 150.25}}},
                        pf, backup_dir=pf.parent / "bk")
    after = pf.read_text(encoding="utf-8")
    d = _data(pf)
    assert _h(d, "PLTR")["shares"] == 20 and _h(d, "PLTR")["cost_basis"] == 150.25 and d["cash_usd"] == 999.5
    assert after.count("# Field reference:") == before.count("# Field reference:") == 1     # header kept
    assert "Spot bitcoin ETF" in after                                                       # notes kept
    assert res["changed"] and (pf.parent / "bk").is_dir() and len(list((pf.parent / "bk").iterdir())) == 1
    assert _h(_data(pf), "SPMO")["shares"] == 69                                             # others untouched


def test_proxy_fund_units_rescale_equivalent_shares(pf, tmp_env):
    d0 = _data(pf)
    ratio = _h(d0, "1186063")["shares"] / _h(d0, "1186063")["units"]
    pe.save_edits({"holdings": {"1186063": {"units": 100, "unit_cost_ils": 270.0}}}, pf, backup_dir=pf.parent / "bk")
    h = _h(_data(pf), "1186063")
    assert h["units"] == 100 and h["unit_cost_ils"] == 270.0
    assert h["shares"] == pytest.approx(ratio * 100, rel=1e-5)


def test_add_and_remove_holdings(pf, tmp_env):
    pe.save_edits({"add": [{"ticker": "ko", "shares": 5, "cost_basis": 60, "currency": "USD"}],
                   "remove": ["MCK"]}, pf, backup_dir=pf.parent / "bk")
    d = _data(pf)
    tickers = [h["ticker"] for h in d["holdings"]]
    assert "KO" in tickers and "MCK" not in tickers
    ko = _h(d, "KO")
    assert ko["shares"] == 5 and ko["cost_basis"] == 60 and ko["cost_basis_currency"] == "USD"
    pe.save_edits({"add": [{"ticker": "LUMI2.TA", "shares": 3, "cost_basis": 50, "currency": "ILS"}]}, pf,
                  backup_dir=pf.parent / "bk")
    assert _h(_data(pf), "LUMI2.TA")["cost_basis_currency"] == "ILS"


@pytest.mark.parametrize("edits,msg", [
    ({"holdings": {"NOPE": {"shares": 1}}}, "not in portfolio"),
    ({"holdings": {"PLTR": {"shares": -1}}}, "greater than"),
    ({"holdings": {"PLTR": {"shares": "abc"}}}, "not a number"),
    ({"holdings": {"PLTR": {"note": "x"}}}, "not editable"),
    ({"holdings": {"PLTR": {"shares": 0}}}, "greater than"),
    ({"cash_usd": -5}, "at least 0"),
    ({"remove": ["ZZZ"]}, "cannot remove"),
    ({"add": [{"ticker": "PLTR", "shares": 1}]}, "already in the portfolio"),
    ({"add": [{"ticker": "bad ticker!", "shares": 1}]}, "valid symbol"),
    ({"add": [{"ticker": "NEW", "shares": 1, "currency": "EUR"}]}, "USD or ILS"),
])
def test_invalid_edits_are_rejected_and_file_untouched(pf, tmp_env, edits, msg):
    before = pf.read_text(encoding="utf-8")
    with pytest.raises(pe.EditError, match=msg):
        pe.save_edits(edits, pf, backup_dir=pf.parent / "bk")
    assert pf.read_text(encoding="utf-8") == before and not (pf.parent / "bk").exists()


def test_removing_everything_is_refused(pf, tmp_env):
    all_t = [h["ticker"] for h in _data(pf)["holdings"]]
    with pytest.raises(pe.EditError, match="empty"):
        pe.save_edits({"remove": all_t}, pf, backup_dir=pf.parent / "bk")


def test_noop_edit_writes_nothing(pf, tmp_env):
    res = pe.save_edits({"holdings": {"PLTR": {"shares": 12}}}, pf, backup_dir=pf.parent / "bk")
    assert res["changed"] is False and not (pf.parent / "bk").exists()


def test_crlf_files_stay_crlf(pf, tmp_env):
    pf.write_bytes(pf.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    pe.save_edits({"holdings": {"PLTR": {"shares": 13}}}, pf, backup_dir=pf.parent / "bk")
    raw = pf.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
