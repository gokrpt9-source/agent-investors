"""scripts/_edgar.py — pure EDGAR parsing (DESIGN §4.3 steps 3-13).

Synthetic companyfacts under tests/fixtures/edgar/synthetic/ carry hand-designed numbers (expected
values below are computed by hand in the comments); trimmed REAL payloads (KO, JPM, BRK-B, fetched
once from data.sec.gov) under tests/fixtures/edgar/http/sec/ provide structural checks. Offline."""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from scripts import _edgar as E

FX = Path(__file__).resolve().parent / "fixtures" / "edgar"
REAL = FX / "http" / "sec"
SYN = FX / "synthetic" / "http" / "sec"
AS_OF = date(2026, 10, 2)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg() -> dict:
    return E.settings()


@pytest.fixture(scope="module")
def zzttm() -> dict:
    return _load(SYN / "companyfacts_CIK0009900001.json")


@pytest.fixture(scope="module")
def zzq4() -> dict:
    return _load(SYN / "companyfacts_CIK0009900002.json")


@pytest.fixture(scope="module")
def zzneg() -> dict:
    return _load(SYN / "companyfacts_CIK0009900003.json")


@pytest.fixture(scope="module")
def ko() -> dict:
    return _load(REAL / "companyfacts_CIK0000021344.json")


@pytest.fixture(scope="module")
def ko_sub() -> dict:
    return _load(REAL / "submissions_CIK0000021344.json")


def _mini(facts: dict) -> dict:
    """companyfacts skeleton: {"tag": [fact, ...]} (us-gaap, USD) or {"dei:Tag": [...]} (shares)."""
    out: dict = {"cik": 1, "entityName": "MINI", "facts": {}}
    for tag, arr in facts.items():
        ns, name = E.split_tag(tag)
        unit = "shares" if ns == "dei" or "Shares" in name else "USD"
        out["facts"].setdefault(ns, {})[name] = {"units": {unit: arr}}
    return out


def _f(val, end, filed, start=None, form="10-K", accn=None):
    d = {"end": end, "val": val, "accn": accn or f"acc-{filed}", "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


# --------------------------------------------------------------------------------------------- config
def test_settings_merges_config_over_defaults():
    s = E.settings({"debt_formula_max_gap_days": 300})
    assert s["debt_formula_max_gap_days"] == 300
    assert s["concept_max_gap_days"] == 300          # follows the debt gap unless set explicitly
    assert s["duration_days"]["Q"] == [80, 100]
    assert s["fy_history_years"] == E.DEFAULT_FY_HISTORY_YEARS


# --------------------------------------------------------------------------------------------- step 5
@pytest.mark.parametrize("days,bucket", [(None, "I"), (79, None), (80, "Q"), (100, "Q"), (101, None),
                                         (170, "H"), (200, "H"), (260, "9M"), (290, "9M"),
                                         (350, "FY"), (364, "FY"), (380, "FY"), (381, None)])
def test_bucket_boundaries(cfg, days, bucket):
    assert E.bucket_for(days, cfg) == bucket


def test_normalize_filters_forms_and_dedupes_latest_filed(cfg):
    cf = _mini({"Revenues": [
        _f(100, "2024-12-31", "2025-02-01", "2024-01-01"),
        _f(105, "2024-12-31", "2026-02-01", "2024-01-01"),                 # restated later -> wins
        _f(999, "2024-12-31", "2025-03-01", "2024-01-01", form="8-K"),     # form not accepted
        _f(50, "2025-03-31", "2025-05-01", "2025-01-01", form="20-F"),     # 20-F only for FY/instants
        _f(400, "2023-12-31", "2024-03-01", "2023-01-01", form="20-F"),    # 20-F FY accepted
        _f(7, "2025-02-15", "2025-05-01", "2025-01-01", form="10-Q"),      # 45 days: no bucket
    ]})
    obs = E.normalize(cf, "Revenues", "USD", cfg)
    assert [(o.end.isoformat(), o.val, o.bucket) for o in obs] == [("2023-12-31", 400.0, "FY"),
                                                                    ("2024-12-31", 105.0, "FY")]
    assert obs[1].filed == date(2026, 2, 1)


def test_normalize_as_of_excludes_later_filings(cfg):
    cf = _mini({"Revenues": [_f(100, "2024-12-31", "2025-02-01", "2024-01-01"),
                             _f(105, "2024-12-31", "2026-02-01", "2024-01-01")]})
    obs = E.normalize(cf, "Revenues", "USD", cfg, as_of=date(2025, 6, 30))
    assert [o.val for o in obs] == [100.0]


def test_restated_duplicate_latest_filed_wins(zzttm, cfg):
    # OperatingIncomeLoss FY2024: 150 in the FY2024 10-K, restated to 140 in the FY2025 10-K
    fy24 = [o for o in E.normalize(zzttm, "OperatingIncomeLoss", "USD", cfg)
            if o.bucket == "FY" and o.end == date(2024, 12, 31)]
    assert len(fy24) == 1
    assert fy24[0].val == 140.0 and fy24[0].filed == date(2026, 2, 13)
    assert E.flow(zzttm, "operating_income", cfg).fy[2024].val == 140.0


def test_fiscal_year_labels():
    assert E.fiscal_year(date(2025, 12, 31)) == 2025
    assert E.fiscal_year(date(2022, 1, 1)) == 2021       # 52/53-week year ending on Jan 1
    assert E.fiscal_year(date(2026, 1, 31)) == 2026      # retail FY ending in late January


# --------------------------------------------------------------------------------------------- step 7
def test_ttm_fy_plus_ytd_minus_prior_ytd(zzttm, cfg):
    r = E.flow(zzttm, "revenue", cfg, as_of=AS_OF)
    # FY2025 1200 + 6M2026 640 - 6M2025 560 = 1280
    assert r.ttm.value == pytest.approx(1280.0)
    assert r.ttm.derivation == "FY2025+6M2026-6M2025"
    assert r.ttm.end == date(2026, 6, 30) and r.ttm.filed == date(2026, 8, 3)
    assert r.tag == "Revenues"
    # prior-year TTM at 2025-06-30: FY2024 1100 + 6M2025 560 - 6M2024 520 = 1140
    assert r.prior_ttm is not None and r.prior_ttm.value == pytest.approx(1140.0)


def test_ttm_latest_fy_is_the_fy(zzneg, cfg):
    r = E.flow(zzneg, "net_income", cfg)
    assert r.ttm.value == -50.0 and r.ttm.derivation == "FY2025" and r.ttm.end == date(2025, 12, 31)


def test_ttm_four_quarter_fallback_with_derived_q4(zzq4, cfg):
    r = E.flow(zzq4, "revenue", cfg)
    # no 3M2025 -> YTD path fails; quarters 240 + 250 + (1000-720=280) + 300 = 1070
    assert r.ttm.value == pytest.approx(1070.0)
    assert r.ttm.derivation == "4Q[2025-06-30+2025-09-30+2025-12-31(FY2025-9M2025)+2026-03-31]"
    assert r.ttm.end == date(2026, 3, 31)
    q = E.quarters(E.normalize(zzq4, "Revenues", "USD", cfg), cfg)
    assert q[date(2025, 12, 31)].val == 280.0 and q[date(2025, 12, 31)].derived == "FY2025-9M2025"


def test_ttm_insufficient_history(zzq4, cfg):
    r = E.flow(zzq4, "net_income", cfg)           # a single Q1 value
    assert r.ttm.value is None and r.ttm.na_reason == "insufficient_history"


def test_ttm_missing_concept(zzq4, cfg):
    r = E.flow(zzq4, "ocf", cfg)
    assert r.tag is None and r.ttm.na_reason == "missing"


def test_ttm_quarter_gap_breaks_chain(cfg):
    obs = E.normalize(_mini({"Revenues": [
        _f(10, "2025-03-31", "2025-05-01", "2025-01-01", "10-Q"),
        _f(10, "2025-06-30", "2025-08-01", "2025-04-01", "10-Q"),
        # 2025-09-30 quarter missing -> 2025-06-30 -> 2025-12-31 gap of 184 days
        _f(10, "2025-12-31", "2026-02-01", "2025-10-01", "10-Q"),
        _f(10, "2026-03-31", "2026-05-01", "2026-01-01", "10-Q"),
    ]}), "Revenues", "USD", cfg)
    assert E.ttm(obs, cfg).na_reason == "insufficient_history"


# --------------------------------------------------------------------------------------------- step 6
def test_tag_choice_latest_end_wins_and_ties_use_priority(cfg):
    cf = _mini({
        "Revenues": [_f(100, "2025-12-31", "2026-02-01", "2025-01-01")],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [_f(90, "2025-12-31", "2026-02-01", "2025-01-01")],
        "SalesRevenueNet": [_f(80, "2024-12-31", "2025-02-01", "2024-01-01")],
    })
    assert E.flow(cf, "revenue", cfg).tag == "Revenues"            # tie on end -> priority
    cf2 = copy.deepcopy(cf)
    cf2["facts"]["us-gaap"]["SalesRevenueNet"]["units"]["USD"].append(
        _f(120, "2026-12-31", "2027-02-01", "2026-01-01"))
    r = E.flow(cf2, "revenue", cfg)
    assert r.tag == "SalesRevenueNet" and r.ttm.value == 120.0        # latest end beats priority


def test_history_stitching_flags_tag_mix(cfg):
    cf = _mini({
        "SalesRevenueNet": [_f(v, f"{y}-12-31", f"{y + 1}-02-01", f"{y}-01-01") for y, v in
                            ((2021, 80), (2022, 90), (2023, 95))],
        "Revenues": [_f(v, f"{y}-12-31", f"{y + 1}-02-01", f"{y}-01-01") for y, v in ((2024, 100), (2025, 110))],
    })
    r = E.flow(cf, "revenue", cfg)
    assert r.tag == "Revenues" and r.stitched is True
    assert r.fy_tags == {2025: "Revenues", 2024: "Revenues", 2023: "SalesRevenueNet",
                         2022: "SalesRevenueNet", 2021: "SalesRevenueNet"}
    codes = [c for c, _s, _d in E.parse(cf, cfg).flags]
    assert "tag_mix" in codes


def test_history_prefers_current_tag_over_priority(cfg):
    cf = _mini({
        "Revenues": [_f(500, f"{y}-12-31", f"{y + 1}-02-01", f"{y}-01-01") for y in (2023, 2024)],
        "RevenuesNetOfInterestExpense": [_f(v, f"{y}-12-31", f"{y + 1}-02-01", f"{y}-01-01")
                                         for y, v in ((2023, 300), (2024, 320), (2025, 350))],
    })
    r = E.flow(cf, "revenue", cfg)
    assert r.tag == "RevenuesNetOfInterestExpense"
    assert {y: o.val for y, o in r.fy.items()} == {2023: 300.0, 2024: 320.0, 2025: 350.0}
    assert r.stitched is False


def test_stale_concept_is_ignored(cfg):
    cf = _mini({
        "Revenues": [_f(100, "2025-12-31", "2026-02-01", "2025-01-01")],
        "OperatingIncomeLoss": [_f(10, "2013-12-31", "2014-02-01", "2013-01-01")],
    })
    p = E.parse(cf, cfg)
    assert p.ttm["revenue"].value == 100.0
    assert p.ttm["operating_income"].value is None and p.ttm["operating_income"].na_reason == "missing"
    assert any(c == "stale_tag" and "operating_income" in d for c, _s, d in p.flags)


# --------------------------------------------------------------------------------------------- step 8
def test_mixed_instants_flag(zzneg, cfg):
    p = E.parse(zzneg, cfg)
    flags = {c: d for c, _s, d in p.flags}
    assert "mixed_instants" in flags and "total_debt 2025-09-30" in flags["mixed_instants"]
    assert p.balance["cash"].end == date(2025, 12, 31)


def test_gross_profit_falls_back_to_revenue_minus_cogs(zzttm, cfg):
    p = E.parse(zzttm, cfg)
    # revenue 1280 - cogs (700 + 370 - 330 = 740) = 540
    assert p.ttm["gross_profit"].value == pytest.approx(540.0)
    assert p.ttm["gross_profit"].derivation.startswith("revenue-cogs")


# --------------------------------------------------------------------------------------------- step 9
def test_cagr():
    v, r = E.cagr_pct(900, 1200, 3)
    assert r is None and v == pytest.approx(((1200 / 900) ** (1 / 3) - 1) * 100)
    assert E.cagr_pct(-50, 100, 3) == (None, "negative_denominator")
    assert E.cagr_pct(50, -1, 3) == (None, "negative_denominator")
    assert E.cagr_pct(None, 100, 3) == (None, "insufficient_history")


def test_negative_cagr_base_in_history(zzttm, cfg):
    fy = E.flow(zzttm, "net_income", cfg).fy
    assert fy[2022].val == -50.0 and fy[2025].val == 100.0
    assert E.cagr_pct(fy[2022].val, fy[2025].val, 3) == (None, "negative_denominator")


# --------------------------------------------------------------------------------------------- step 4
def test_debt_formula_falls_through_stale_tags_ko(ko, cfg):
    p = E.parse(ko, cfg, as_of=AS_OF)
    d = p.debt
    assert d.formula in ("c", "d")
    assert d.value.end == date(2026, 4, 3)
    # (c) LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities 43,558M + CommercialPaper 250M
    assert d.formula == "c" and d.value.value == pytest.approx(43_808_000_000)
    assert d.stb_tag == "CommercialPaper" and not d.stb_assumed_zero
    assert any(r.startswith("(a) LongTermDebt:") and "2024-03-29" in r for r in d.rejected)
    # the stale LongTermDebt tag exists in the fixture (the drift is real)
    assert "LongTermDebt" in ko["facts"]["us-gaap"]


def test_debt_formula_b_latest_common_instant(zzneg, cfg):
    d = E.total_debt(zzneg, cfg, newest_bs=date(2025, 12, 31))
    # (a) LongTermDebt 2023-12-31 is 731 d old -> rejected; (b) common instant 2025-09-30:
    # 150 + 30 + ShortTermBorrowings 20 = 200 (LongTermDebtNoncurrent at 12-31 has no current part)
    assert d.formula == "b" and d.value.value == 200.0 and d.value.end == date(2025, 9, 30)
    assert d.value.derivation == "(b) LongTermDebtNoncurrent + LongTermDebtCurrent + ShortTermBorrowings"
    assert d.rejected and "731 d" in d.rejected[0]


def test_debt_stb_assumed_zero(zzttm, cfg):
    d = E.total_debt(zzttm, cfg, newest_bs=date(2026, 6, 30))
    assert d.formula == "a" and d.value.value == 300.0 and d.stb_assumed_zero
    assert "stb_assumed_zero" in [c for c, _s, _d in E.parse(zzttm, cfg).flags]


def _flag(p, code):
    return next((f for f in p.flags if f[0] == code), None)


def _pltr_like(extra: dict | None = None) -> dict:
    """Debt-free issuer: cash, short-term investments, equity; a treasury portfolio of debt SECURITIES
    (investment-side tags that match /Debt/) and a long-retired 2021 term loan; no live debt tags."""
    facts = {
        "CashAndCashEquivalentsAtCarryingValue": [_f(2_000, "2026-06-30", "2026-08-04", form="10-Q")],
        "MarketableSecuritiesCurrent": [_f(7_000, "2026-06-30", "2026-08-04", form="10-Q")],
        "StockholdersEquity": [_f(9_000, "2026-06-30", "2026-08-04", form="10-Q")],
        "AvailableForSaleSecuritiesDebtSecurities": [_f(5_000, "2026-06-30", "2026-08-04", form="10-Q")],
        "DebtSecuritiesAvailableForSaleUnrealizedLossPosition": [_f(3, "2026-06-30", "2026-08-04", form="10-Q")],
        "LongTermDebtNoncurrent": [_f(100, "2021-12-31", "2022-02-20")],           # retired: 4.5 years old
        "RepaymentsOfDebt": [_f(100, "2023-12-31", "2024-02-20", "2023-01-01")],   # a flow, never a balance
    }
    facts.update(extra or {})
    return _mini(facts)


# --- "no debt-related XBRL tags reported -> 0" (live PLTR: formulas (a)-(d) never qualify)
def test_debt_not_reported_assumed_zero_pltr_like(cfg):
    p = E.parse(_pltr_like(), cfg, as_of=AS_OF)
    d = p.debt
    assert d.value.ok and d.value.value == 0.0 and d.assumed_zero and d.formula is None
    assert d.value.derivation == "no debt-related XBRL tags reported -> 0"
    assert d.value.end == date(2026, 6, 30) and d.value.filed == date(2026, 8, 4)
    code, sev, detail = _flag(p, "debt_not_reported_assumed_zero")
    assert sev == "medium" and "total debt taken as 0" in detail
    assert _flag(p, "debt_unavailable") is None and not any(m.startswith("total_debt") for m in p.missing)
    assert _flag(p, "mixed_instants") is None                   # the 0 sits on the newest balance sheet


def test_debt_assumed_zero_ignores_flows_shares_and_investment_side_tags(cfg):
    # a share count and a rate that merely have "Debt" in the name, and a current debt-SECURITY balance
    extra = {"DebtInstrumentInterestRateStatedPercentage": [_f(0.05, "2026-06-30", "2026-08-04")],
             "ConvertibleDebtSecurities": []}
    cf = _pltr_like(extra)
    cf["facts"]["us-gaap"]["DebtInstrumentInterestRateStatedPercentage"]["units"] = {
        "pure": [_f(0.05, "2026-06-30", "2026-08-04")]}
    assert E.debt_tags_near(cf, date(2026, 6, 30), cfg) == {}
    assert E.parse(cf, cfg, as_of=AS_OF).debt.assumed_zero


@pytest.mark.parametrize("tag", ["ConvertibleNotesPayable", "LoansPayableCurrent", "NotesPayable",
                                 "OtherBorrowings", "LineOfCredit", "DebtInstrumentCarryingAmount",
                                 "CommercialPaper", "LongTermDebtFairValue", "ConvertibleDebtSecurities"])
def test_current_debt_like_tag_not_covered_by_formulas_keeps_null(cfg, tag):
    p = E.parse(_pltr_like({tag: [_f(40, "2026-06-30", "2026-08-04", form="10-Q")]}), cfg, as_of=AS_OF)
    assert p.debt.value.value is None and not p.debt.assumed_zero
    _c, sev, detail = _flag(p, "debt_unavailable")
    assert sev == "medium" and tag in detail and "2026-06-30" in detail
    assert _flag(p, "debt_not_reported_assumed_zero") is None


def test_debt_tag_inside_gap_window_blocks_outside_does_not(cfg):
    # newest balance sheet 2026-06-30; max gap 400 d -> 2025-06-01 is 394 d old (blocks), 2025-05-01 is 425 d (not)
    inside = _pltr_like({"ConvertibleNotesPayable": [_f(5, "2025-06-01", "2025-07-30", form="10-Q")]})
    outside = _pltr_like({"ConvertibleNotesPayable": [_f(5, "2025-05-01", "2025-07-30", form="10-Q")]})
    assert E.parse(inside, cfg, as_of=AS_OF).debt.value.value is None
    assert E.parse(outside, cfg, as_of=AS_OF).debt.value.value == 0.0
    tight = E.settings({"debt_formula_max_gap_days": 100})
    assert E.parse(inside, tight, as_of=AS_OF).debt.assumed_zero         # same tag, tighter window


def test_debt_flow_observation_of_a_debt_tag_does_not_block(cfg):
    cf = _pltr_like({"InterestExpenseDebt": [_f(9, "2026-06-30", "2026-08-04", "2026-01-01", form="10-Q")]})
    assert E.parse(cf, cfg, as_of=AS_OF).debt.assumed_zero


def test_debt_assumed_zero_needs_a_balance_sheet(cfg):
    cf = _mini({"RepaymentsOfDebt": [_f(1, "2025-12-31", "2026-02-01", "2025-01-01")]})
    p = E.parse(cf, cfg)
    assert p.debt.value.value is None and _flag(p, "debt_unavailable") and not p.debt.assumed_zero


def test_formula_debt_still_wins_over_the_zero_assumption(zzttm, cfg):
    p = E.parse(zzttm, cfg)
    assert p.debt.formula == "a" and not p.debt.assumed_zero
    assert _flag(p, "debt_not_reported_assumed_zero") is None


def test_debt_point_in_time(cfg):
    # a debt tag filed AFTER as_of is invisible, so on the earlier date the company looks debt-free
    cf = _pltr_like({"CashAndCashEquivalentsAtCarryingValue": [_f(1_800, "2026-03-31", "2026-05-04", form="10-Q"),
                                                              _f(2_000, "2026-06-30", "2026-08-04", form="10-Q")],
                     "ConvertibleNotesPayable": [_f(5, "2026-06-30", "2026-08-04", form="10-Q")]})
    early = E.parse(cf, cfg, as_of=date(2026, 7, 1))                 # the Q2 10-Q is not filed yet
    assert early.newest_bs == date(2026, 3, 31) and early.debt.assumed_zero is True
    assert E.parse(cf, cfg, as_of=AS_OF).debt.assumed_zero is False   # Q2 10-Q filed: NotesPayable visible


def test_trim_keeps_debt_tags_and_marks_the_scan(cfg):
    cf = _pltr_like({"ConvertibleNotesPayable": [_f(5, "2026-06-30", "2026-08-04", form="10-Q"),
                                      _f(7, "2026-03-31", "2026-05-04", "2026-01-01", form="10-Q")],
                     "Revenues": [_f(1, "2025-12-31", "2026-02-01", "2025-01-01")]})
    cf["facts"]["us-gaap"]["OtherUnrelatedTag"] = {"units": {"USD": [_f(1, "2026-06-30", "2026-08-04")]}}
    t = E.trim_companyfacts(cf)
    names = set(t["facts"]["us-gaap"])
    assert t["debt_scan"] is True and "ConvertibleNotesPayable" in names and "RepaymentsOfDebt" not in names   # flows dropped
    assert "AvailableForSaleSecuritiesDebtSecurities" not in names and "OtherUnrelatedTag" not in names
    assert [o["val"] for o in t["facts"]["us-gaap"]["ConvertibleNotesPayable"]["units"]["USD"]] == [5]         # instants only
    assert E.parse(t, cfg, as_of=AS_OF).debt.value.value is None                    # still blocked after trimming
    clean = E.trim_companyfacts(_pltr_like())
    assert E.parse(clean, cfg, as_of=AS_OF).debt.assumed_zero


def test_legacy_trimmed_cache_never_assumes_zero(cfg):
    legacy = E.trim_companyfacts(_pltr_like())
    legacy.pop("debt_scan")                                 # written before the debt-tag scan existed
    assert not E.debt_scan_available(legacy) and E.debt_scan_available(_pltr_like())
    p = E.parse(legacy, cfg, as_of=AS_OF)
    assert p.debt.value.value is None and not p.debt.assumed_zero
    assert "predates the debt-tag scan" in _flag(p, "debt_unavailable")[2]


def test_debt_unavailable_when_debt_tag_exists_but_no_formula(cfg):
    cf = _mini({"CashAndCashEquivalentsAtCarryingValue": [_f(1, "2025-12-31", "2026-02-01")],
                "ConvertibleNotesPayable": [_f(10, "2025-12-31", "2026-02-01")]})
    p = E.parse(cf, cfg)
    assert p.debt.value.value is None
    assert "debt_unavailable" in [c for c, _s, _d in p.flags]
    assert any(m.startswith("total_debt") for m in p.missing)


# --------------------------------------------------------------------------------------------- step 10
def test_multi_class_shares_are_summed(zzq4, cfg):
    s = E.share_count(zzq4, cfg)
    assert s.value.value == 350.0 and s.classes_summed == 2        # 300 (class A) + 50 (class B)
    assert s.value.end == date(2026, 4, 24) and s.tag == "dei:EntityCommonStockSharesOutstanding"
    assert "sum of 2 class counts" in s.value.derivation


def test_share_class_factor_and_tag_override(zzq4, cfg):
    s = E.share_count(zzq4, cfg, class_factor=2.0)
    assert s.value.value == 700.0 and s.raw_value == 350.0 and "class_factor" in s.value.derivation
    w = E.share_count(zzq4, cfg, shares_tags=["WeightedAverageNumberOfDilutedSharesOutstanding"])
    assert w.value.value == 345.0 and w.tag == "WeightedAverageNumberOfDilutedSharesOutstanding"


def test_brk_shares_are_stale_class_a_counts(cfg):
    brk = _load(REAL / "companyfacts_CIK0001067983.json")
    p = E.parse(brk, cfg, as_of=AS_OF)
    assert p.shares.value.end == date(2011, 4, 29)     # only old Class-A cover-page counts exist
    assert p.shares_stale is True


# --------------------------------------------------------------------------------------------- step 13
def test_lag_detection_ko(ko, ko_sub):
    lag = E.detect_lag(ko, ko_sub, 21344, as_of=AS_OF)
    assert lag.lag is True
    assert lag.latest["form"] == "10-Q" and lag.latest["filed"] == "2026-07-29"
    assert lag.message.startswith("latest 10-Q for 2026-07-03 (filed 2026-07-29) not yet in companyfacts")
    assert lag.url == "https://www.sec.gov/Archives/edgar/data/21344/000162828026050503/ko-20260703.htm"
    # before that 10-Q was filed there is no lag
    assert E.detect_lag(ko, ko_sub, 21344, as_of=date(2026, 7, 1)).lag is False


def test_lag_detection_absent_for_current_filers():
    for cik in (19617, 1067983):
        cf = _load(REAL / f"companyfacts_CIK{cik:010d}.json")
        sub = _load(REAL / f"submissions_CIK{cik:010d}.json")
        assert E.detect_lag(cf, sub, cik, as_of=AS_OF).lag is False


def test_companyfacts_latest(ko):
    assert E.companyfacts_latest(ko, as_of=AS_OF) == {"form": "10-Q", "filed": "2026-04-30",
                                                       "period_end": "2026-04-03",
                                                       "accn": "0001628280-26-028802"}


# --------------------------------------------------------------------------------------------- 11/12
def test_effective_tax_rate():
    assert E.effective_tax_rate(30, 140)[0] == pytest.approx(30 / 140)
    rate, flag, _ = E.effective_tax_rate(70, 140)
    assert rate == 0.35 and flag == "tax_rate_clamped"
    rate, flag, _ = E.effective_tax_rate(5, -10)
    assert rate == 0.21 and flag == "default_tax_rate"
    assert E.effective_tax_rate(None, 10)[1] == "default_tax_rate"


@pytest.mark.parametrize("sic,cls", [(6021, "financials"), (6331, "financials"), (6798, "default"),
                                     (6000, "financials"), (6799, "financials"), (5999, "default"),
                                     (2080, "default"), ("6211", "financials"), (None, "default")])
def test_sector_class_from_sic(sic, cls):
    assert E.sector_class_from_sic(sic) == cls


# --------------------------------------------------------------------------------------------- trimming
def test_trim_companyfacts_keeps_needed_tags_and_all_accessions(ko):
    raw = copy.deepcopy(ko)
    raw["facts"]["us-gaap"]["SomeUnusedTag"] = {"units": {"USD": [_f(1, "2026-06-30", "2026-07-30",
                                                                      accn="only-in-unused-tag")]}}
    t = E.trim_companyfacts(raw)
    assert "SomeUnusedTag" not in t["facts"]["us-gaap"] and "Revenues" in t["facts"]["us-gaap"]
    assert "only-in-unused-tag" in t["accns_all"]
    assert "only-in-unused-tag" in E.companyfacts_accns(t)


def test_trim_submissions_keeps_latest_periodic_rows(ko_sub):
    t = E.trim_submissions(ko_sub, keep=5, periodic=2)
    forms = t["filings"]["recent"]["form"]
    assert forms[:5] == ko_sub["filings"]["recent"]["form"][:5]
    assert sum(f in E.PERIODIC_FORMS for f in forms) == 2
    assert E.latest_periodic(t)["accn"] == E.latest_periodic(ko_sub)["accn"]


# --------------------------------------------------------------------------------------------- review fixes
def test_equal_per_class_share_counts_are_all_summed(cfg):
    cf = _mini({"dei:EntityCommonStockSharesOutstanding": [
        _f(100, "2026-07-20", "2026-08-01", form="10-Q", accn="X"),
        _f(100, "2026-07-20", "2026-08-01", form="10-Q", accn="X"),
        _f(50, "2026-07-20", "2026-08-01", form="10-Q", accn="X"),
        _f(999, "2026-07-20", "2026-07-25", form="10-Q", accn="OLD"),     # older filing: not summed
    ]})
    s = E.share_count(cf, cfg)
    assert s.value.value == 250.0 and s.classes_summed == 3
    assert "sum of 3 class counts" in s.value.derivation


def test_lag_ignores_newer_amendment():
    cf = _mini({"Revenues": [_f(10, "2026-06-30", "2026-08-01", "2026-04-01", "10-Q", accn="Q2")]})
    sub = {"filings": {"recent": {
        "accessionNumber": ["KA", "Q2"], "filingDate": ["2026-08-20", "2026-08-01"],
        "reportDate": ["2025-12-31", "2026-06-30"], "form": ["10-K/A", "10-Q"],
        "primaryDocument": ["a.htm", "q.htm"]}}}
    lag = E.detect_lag(cf, sub, 1, as_of=AS_OF)
    assert lag.lag is False and lag.latest["accn"] == "Q2" and lag.latest["form"] == "10-Q"
    # a newer ORIGINAL 10-Q absent from companyfacts still lags
    sub2 = copy.deepcopy(sub)
    rec = sub2["filings"]["recent"]
    for k, v in (("accessionNumber", "Q3"), ("filingDate", "2026-09-30"), ("reportDate", "2026-09-26"),
                 ("form", "10-Q"), ("primaryDocument", "q3.htm")):
        rec[k].insert(0, v)
    assert E.detect_lag(cf, sub2, 1, as_of=AS_OF).lag is True


def _flow3(fy, h25, h26):
    return [_f(fy, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25"),
            _f(h25, "2025-06-30", "2025-08-01", "2025-01-01", "10-Q", accn="Q25"),
            _f(h26, "2026-06-30", "2026-08-01", "2026-01-01", "10-Q", accn="Q26")]


def test_misaligned_gross_profit_uses_aligned_revenue_minus_cogs(cfg):
    gp_10k_only = [_f(600, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25")]
    cf = _mini({"Revenues": _flow3(1000, 400, 800), "CostOfRevenue": _flow3(400, 170, 330),
                "GrossProfit": gp_10k_only})
    gp = E.parse(cf, cfg).ttm["gross_profit"]
    assert gp.value == pytest.approx(1400 - 560) and gp.end == date(2026, 6, 30)
    assert gp.derivation.startswith("revenue-cogs") and "GrossProfit TTM ends 2025-12-31" in gp.derivation
    # without an aligned cogs the misaligned GrossProfit TTM is kept (fetch_fundamentals nulls the margin)
    cf2 = _mini({"Revenues": _flow3(1000, 400, 800), "GrossProfit": gp_10k_only})
    gp2 = E.parse(cf2, cfg).ttm["gross_profit"]
    assert gp2.value == 600.0 and gp2.end == date(2025, 12, 31)


def test_intangibles_from_parts_requires_both_at_common_instant(cfg):
    both = _mini({"IndefiniteLivedIntangibleAssetsExcludingGoodwill": [_f(120, "2026-06-30", "2026-08-01")],
                  "FiniteLivedIntangibleAssetsNet": [_f(80, "2026-06-30", "2026-08-01")]})
    r = E.intangibles_from_parts(both, cfg)
    assert r.value.value == 200.0 and r.value.end == date(2026, 6, 30)
    assert E.parse(both, cfg).balance["intangibles"].value == 200.0
    one = _mini({"FiniteLivedIntangibleAssetsNet": [_f(80, "2026-06-30", "2026-08-01")]})
    assert E.intangibles_from_parts(one, cfg).value.value is None
    assert "FiniteLivedIntangibleAssetsNet" in E.all_tags()


# --------------------------------------------------------------------------------------------- step 4: formula choice
def _bs(extra: dict, *, cash_end: str = "2026-03-31", cash_filed: str = "2026-04-30") -> dict:
    """Mini company with a cash balance at ``cash_end`` (sets the newest balance sheet) + ``extra`` tags."""
    facts = {"CashAndCashEquivalentsAtCarryingValue": [_f(1_000, cash_end, cash_filed, form="10-Q")]}
    facts.update(extra)
    return _mini(facts)


def test_debt_newest_instant_formula_beats_older_long_term_debt_abt_like(cfg):
    # ABT: LongTermDebt is only reported at the 10-K date, the 10-Q has noncurrent + current legs
    cf = _bs({"LongTermDebt": [_f(12_900, "2025-12-31", "2026-02-20")],
              "LongTermDebtNoncurrent": [_f(30_000, "2025-12-31", "2026-02-20"), _f(30_100, "2026-03-31", "2026-04-30", form="10-Q")],
              "LongTermDebtCurrent": [_f(3_900, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "b" and d.value.value == 34_000.0 and d.value.end == date(2026, 3, 31)
    assert d.value.derivation == "(b) LongTermDebtNoncurrent + LongTermDebtCurrent + STB(0, not reported)"
    assert d.stb_assumed_zero and not d.current_portion_missing


def test_debt_googl_like_noncurrent_plus_current_over_stale_long_term_debt(cfg):
    cf = _bs({"LongTermDebt": [_f(49_100, "2025-03-31", "2025-04-30", form="10-Q")],         # 12 months old
              "LongTermDebtNoncurrent": [_f(98_200, "2026-03-31", "2026-04-30", form="10-Q")],
              "LongTermDebtCurrent": [_f(2_000, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "b" and d.value.value == pytest.approx(100_200.0)


def test_debt_older_formula_loses_even_when_it_has_higher_priority(cfg):
    cf = _bs({"LongTermDebt": [_f(10, "2025-12-31", "2026-02-20")],
              "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": [_f(55, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "c" and d.value.value == 55.0


def test_debt_same_instant_ties_resolve_in_priority_order(cfg):
    cf = _bs({"LongTermDebt": [_f(100, "2026-03-31", "2026-04-30", form="10-Q")],
              "LongTermDebtNoncurrent": [_f(90, "2026-03-31", "2026-04-30", form="10-Q")],
              "LongTermDebtCurrent": [_f(10, "2026-03-31", "2026-04-30", form="10-Q")],
              "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": [_f(120, "2026-03-31", "2026-04-30", form="10-Q")]})
    assert E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31)).formula == "a"
    del cf["facts"]["us-gaap"]["LongTermDebt"]
    assert E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31)).formula == "b"
    del cf["facts"]["us-gaap"]["LongTermDebtNoncurrent"]
    assert E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31)).formula == "c"


def test_debt_400_day_qualification_still_applies_to_the_latest_instant(cfg):
    cf = _bs({"LongTermDebtNoncurrent": [_f(30, "2024-12-31", "2025-02-20")],
              "LongTermDebtCurrent": [_f(4, "2024-12-31", "2025-02-20")]})
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.value.value is None and any("(b) LongTermDebtNoncurrent+LongTermDebtCurrent|DebtCurrent" in r and "455 d" in r
                                         for r in d.rejected)


def test_debt_current_leg_may_be_debt_current_and_stb_is_not_added_on_top(cfg):
    # UNH / LLY: DebtCurrent already contains short-term borrowings
    cf = _bs({"LongTermDebtNoncurrent": [_f(60_000, "2026-03-31", "2026-04-30", form="10-Q")],
              "DebtCurrent": [_f(5_000, "2026-03-31", "2026-04-30", form="10-Q")],
              "ShortTermBorrowings": [_f(2_000, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "b" and d.value.value == 65_000.0 and not d.stb_assumed_zero and d.stb_tag is None
    assert d.value.derivation == "(b) LongTermDebtNoncurrent + DebtCurrent"
    # an explicit LongTermDebtCurrent at the same instant is preferred (and STB is added as before)
    cf["facts"]["us-gaap"]["LongTermDebtCurrent"] = {"units": {"USD": [_f(4_000, "2026-03-31", "2026-04-30", form="10-Q")]}}
    d = E.total_debt(cf, cfg, newest_bs=date(2026, 3, 31))
    assert d.value.value == 66_000.0 and d.stb_tag == "ShortTermBorrowings"


def test_debt_oracle_and_realty_income_like_formulas(cfg):
    orcl = _bs({"LongTermNotesAndLoans": [_f(80_000, "2026-03-31", "2026-04-30", form="10-Q")],
                "NotesPayableCurrent": [_f(7_000, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(orcl, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "e" and d.value.value == 87_000.0
    o = _bs({"NotesPayable": [_f(20_000, "2026-03-31", "2026-04-30", form="10-Q")],
             "LoansPayable": [_f(1_500, "2026-03-31", "2026-04-30", form="10-Q")]})
    d = E.total_debt(o, cfg, newest_bs=date(2026, 3, 31))
    assert d.formula == "f" and d.value.value == 21_500.0 and d.stb_assumed_zero
    assert E.parse(o, cfg, as_of=AS_OF).debt.formula == "f"            # also the parse() path


def test_noncurrent_leg_alone_is_used_with_a_flag_only_when_no_complete_formula_qualifies(cfg):
    cf = _bs({"LongTermDebtNoncurrent": [_f(30_000, "2026-03-31", "2026-04-30", form="10-Q")]})
    p = E.parse(cf, cfg, as_of=AS_OF)
    d = p.debt
    assert d.formula == "b" and d.value.value == 30_000.0 and d.current_portion_missing
    assert "noncurrent leg only" in d.value.derivation
    code, sev, detail = _flag(p, "debt_current_portion_missing")
    assert sev == "medium" and "LongTermDebtNoncurrent" in detail
    assert _flag(p, "debt_unavailable") is None and _flag(p, "debt_not_reported_assumed_zero") is None
    # a complete formula, even an older one, wins over the partial fallback (zzneg-style data)
    cf2 = _bs({"LongTermDebtNoncurrent": [_f(30_000, "2026-03-31", "2026-04-30", form="10-Q"),
                                          _f(29_000, "2025-12-31", "2026-02-20")],
               "LongTermDebtCurrent": [_f(2_000, "2025-12-31", "2026-02-20")]})
    d2 = E.total_debt(cf2, cfg, newest_bs=date(2026, 3, 31))
    assert d2.value.value == 31_000.0 and not d2.current_portion_missing and d2.value.end == date(2025, 12, 31)
    # NotesPayable alone is NOT a noncurrent leg: it never triggers the partial fallback
    cf3 = _bs({"NotesPayable": [_f(20_000, "2026-03-31", "2026-04-30", form="10-Q")]})
    assert E.total_debt(cf3, cfg, newest_bs=date(2026, 3, 31)).value.value is None


# --------------------------------------------------------------------------------------------- 52/53-week calendars
def _retail_cf(extra: dict | None = None) -> dict:
    """52-week retail calendar (COST, PEP style): FY2025 = 2025-02-02..2026-01-31 (363 d), 12/12/12/16-week
    quarters. Latest filing = the 36-week (9M, 251 d) 10-Q ending 2026-10-09.
    revenue: FY2025 1000, 9M FY2025 700, 9M FY2026 760 -> TTM 1060; H: 24-week 167 d."""
    return _mini({"Revenues": [
        _f(1000, "2026-01-31", "2026-03-20", "2025-02-02"),                       # FY2025: 363 d
        _f(700, "2025-10-11", "2025-11-20", "2025-02-02", "10-Q"),                # 9M FY2025: 251 d
        _f(760, "2026-10-09", "2026-11-20", "2026-02-01", "10-Q"),                # 9M FY2026: 251 d
        _f(450, "2025-07-19", "2025-08-20", "2025-02-02", "10-Q"),                # H FY2025: 167 d
        _f(500, "2026-07-18", "2026-08-20", "2026-02-01", "10-Q"),                # H FY2026: 167 d
        _f(900, "2025-02-01", "2025-03-20", "2024-02-04"),                        # FY2024
    ], **(extra or {})})


def test_52_week_calendar_buckets_and_ttm(cfg):
    assert E.bucket_for(167, cfg) == "H" and E.bucket_for(251, cfg) == "9M" and E.bucket_for(160, cfg) == "H"
    assert E.bucket_for(159, cfg) is None and E.bucket_for(244, cfg) is None
    r = E.flow(_retail_cf(), "revenue", cfg)
    assert r.ttm.ok and r.ttm.value == pytest.approx(1000 + 760 - 700) and r.ttm.end == date(2026, 10, 9)
    assert r.ttm.derivation == "FY2026+9M2026-9M2025"


def test_52_week_calendar_half_year_ttm(cfg):
    cf = _mini({"Revenues": [
        _f(1000, "2026-01-31", "2026-03-20", "2025-02-02"),
        _f(450, "2025-07-19", "2025-08-20", "2025-02-02", "10-Q"),
        _f(500, "2026-07-18", "2026-08-20", "2026-02-01", "10-Q")]})
    r = E.flow(cf, "revenue", cfg)
    assert r.ttm.value == pytest.approx(1050.0) and r.ttm.derivation == "FY2026+6M2026-6M2025"


def test_52_week_calendar_derived_q4_and_four_quarter_chain(cfg):
    cf = _mini({"Revenues": [
        _f(1000, "2026-01-31", "2026-03-20", "2025-02-02"),                       # FY (363 d)
        _f(250, "2025-04-26", "2025-05-20", "2025-02-02", "10-Q"),                # Q1 12 weeks (83 d)
        _f(250, "2025-07-19", "2025-08-20", "2025-04-27", "10-Q"),                # Q2 12 weeks
        _f(150, "2025-10-11", "2025-11-20", "2025-07-20", "10-Q"),                # Q3 12 weeks
        _f(700, "2025-10-11", "2025-11-20", "2025-02-02", "10-Q"),                # 9M (251 d)
    ]})
    obs = [o for o in E.normalize(cf, "Revenues", "USD", cfg) if o.bucket != "I"]
    q = E.quarters(obs, cfg)
    assert date(2026, 1, 31) in q and q[date(2026, 1, 31)].val == 300.0 and q[date(2026, 1, 31)].derived == "FY2026-9M2025"
    chain = E._quarter_chain(q, date(2026, 1, 31), cfg)               # Q4 ends 112 d after Q3: needs gap <= 120
    assert chain is not None and [o.end for o in chain] == [date(2025, 4, 26), date(2025, 7, 19),
                                                            date(2025, 10, 11), date(2026, 1, 31)]


def test_narrow_pre_fix_bounds_lose_the_52_week_ttm():
    old = E.settings({"duration_days": {"Q": [80, 100], "H": [170, 200], "9M": [260, 290], "FY": [350, 380]},
                      "quarter_gap_days": [84, 98]})
    r = E.flow(_retail_cf(), "revenue", old)
    # the 24/36-week periods are dropped, so the TTM silently falls back to the stale fiscal year (ended 8 months
    # earlier) - this is why committee.yaml must carry the wider bounds
    assert r.ttm.end == date(2026, 1, 31) and r.ttm.derivation == "FY2026"
    assert E.flow(_retail_cf(), "revenue", E.settings()).ttm.end == date(2026, 10, 9)
    assert E.settings()["duration_days"]["H"] == [160, 200] and E.settings()["quarter_gap_days"] == [84, 120]


# --------------------------------------------------------------------------------------------- step 3: tag coverage
def _ann(tag_values: dict) -> dict:
    """FY-only mini company: {tag: value} each as FY2025 (2025-12-31) + the 10-K filing."""
    return _mini({t: [_f(v, "2025-12-31", "2026-02-20", "2025-01-01")] for t, v in tag_values.items()})


def test_da_falls_back_to_depreciation_plus_amortization_of_intangibles(cfg):
    p = E.parse(_ann({"Depreciation": 300, "AmortizationOfIntangibleAssets": 40, "Revenues": 1000}), cfg, as_of=AS_OF)
    da = p.ttm["da"]
    assert da.ok and da.value == 340.0 and da.end == date(2025, 12, 31)
    assert da.tag == "Depreciation+AmortizationOfIntangibleAssets" and da.derivation.startswith("depreciation + amortization")
    assert not any(c == "stale_tag" and "da:" in d for c, _s, d in p.flags)
    # the named D&A tags still win when present, and a lone Depreciation tag is not an answer
    p = E.parse(_ann({"DepreciationDepletionAndAmortization": 500, "Depreciation": 300,
                      "AmortizationOfIntangibleAssets": 40}), cfg, as_of=AS_OF)
    assert p.ttm["da"].value == 500.0 and p.ttm["da"].tag == "DepreciationDepletionAndAmortization"
    assert not E.parse(_ann({"Depreciation": 300}), cfg, as_of=AS_OF).ttm["da"].ok


def test_da_parts_must_share_the_period_and_be_current(cfg):
    cf = _mini({"Depreciation": [_f(300, "2025-12-31", "2026-02-20", "2025-01-01")],
                "AmortizationOfIntangibleAssets": [_f(40, "2024-12-31", "2025-02-20", "2024-01-01")],
                "Revenues": [_f(1000, "2025-12-31", "2026-02-20", "2025-01-01")]})
    assert not E.parse(cf, cfg, as_of=AS_OF).ttm["da"].ok
    # stale first-tag D&A (older than the gap) is replaced by current parts
    cf2 = _mini({"DepreciationAndAmortization": [_f(900, "2023-12-31", "2024-02-20", "2023-01-01")],
                 "Depreciation": [_f(300, "2025-12-31", "2026-02-20", "2025-01-01")],
                 "AmortizationOfIntangibleAssets": [_f(40, "2025-12-31", "2026-02-20", "2025-01-01")],
                 "Revenues": [_f(1000, "2025-12-31", "2026-02-20", "2025-01-01")]})
    assert E.parse(cf2, cfg, as_of=AS_OF).ttm["da"].value == 340.0


@pytest.mark.parametrize("tag", ["PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsForCapitalImprovements"])
def test_capex_alternative_tags(cfg, tag):
    p = E.parse(_ann({tag: 123, "Revenues": 1000}), cfg, as_of=AS_OF)
    assert p.ttm["capex"].value == 123.0 and p.ttm["capex"].tag == tag
    # the primary tag keeps priority at the same period
    p = E.parse(_ann({tag: 123, "PaymentsToAcquirePropertyPlantAndEquipment": 77, "Revenues": 1000}), cfg, as_of=AS_OF)
    assert p.ttm["capex"].value == 77.0
