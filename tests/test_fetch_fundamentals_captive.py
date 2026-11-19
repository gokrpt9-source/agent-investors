"""fetch_fundamentals.py captive-finance handling: ``captive_finance`` flag + annotated consolidated
leverage + ``net_debt_ebitda_ex_captive`` for listed tickers, ``possible_captive_finance`` safeguard for
unlisted industrials / consumer_discretionary, schema validity."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scripts import common, fetch_fundamentals as FF
from test_fetch_fundamentals import _base_facts, _flow3, _fx, _mini_cf

REPO = Path(__file__).resolve().parents[1]
#: price 100 x 10 shares = market cap 1000; debt 700 - cash 100 = net debt 600; EBITDA = 140 + 20 = 160.
ASOF = date(2026, 10, 2)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    resources = []
    for p in sorted((REPO / "schemas").glob("*.json")) + sorted((REPO / "schemas" / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    reg = Registry().with_resources(resources)
    return Draft202012Validator(reg.contents("urn:agent-investors:data/fundamentals"), registry=reg,
                                format_checker=FormatChecker())


def _facts(net_debt_heavy: bool = True) -> dict:
    f = _base_facts()
    f["OperatingIncomeLoss"] = _flow3(100, 40, 80)                                   # TTM 140
    f["DepreciationDepletionAndAmortization"] = _flow3(15, 6, 11)                    # TTM 20
    f["CashAndCashEquivalentsAtCarryingValue"] = [_fx(100, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    debt = 700 if net_debt_heavy else 150
    f["LongTermDebtNoncurrent"] = [_fx(debt - 100, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    f["LongTermDebtCurrent"] = [_fx(100, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    return f


def _build(ticker: str, facts: dict, *, sic: int | None = 3531, vcfg: dict | None = None) -> dict:
    sub = {"sic": str(sic), "sicDescription": "x"} if sic is not None else None
    return FF.build_payload(ticker, cik=1, cf=_mini_cf(facts), sub=sub, price=100.0, price_as_of="2026-09-30",
                            as_of=ASOF, sector_class="default", yf_cap=lambda t: (None, "offline"),
                            valuation_cfg=vcfg)


def _codes(built: dict) -> dict[str, dict]:
    return {f["code"]: f for f in built["flags"]}


def _doc(built: dict) -> dict:
    return common.envelope(FF.SCHEMA, "2026-10-02", sources=built["sources"], missing_data=built["missing_data"],
                           flags=built["flags"], **built["payload"])


def test_fixture_arithmetic() -> None:
    pay = _build("ZZMM", _facts())["payload"]
    assert pay["balance"]["net_debt"]["value"] == 600.0 and pay["market_cap"]["value"] == 1000.0
    assert pay["ttm"]["ebitda"]["value"] == 160.0
    assert pay["valuation"]["net_debt_ebitda"]["value"] == pytest.approx(3.75)


def test_listed_ticker_gets_flag_notes_and_null_ex_captive(validator) -> None:
    built = _build("CAT", _facts())
    pay, flags = built["payload"], _codes(built)
    flag = flags["captive_finance"]
    assert flag["severity"] == "medium"
    assert "captive finance subsidiary" in flag["detail"] and "overstated" in flag["detail"]
    assert "possible_captive_finance" not in flags                                     # not both
    # the consolidated number is KEPT (schema stable, consumers unchanged) but annotated
    nde = pay["valuation"]["net_debt_ebitda"]
    assert nde["value"] == pytest.approx(3.75) and "includes captive finance debt" in nde["note"]
    assert "includes captive finance debt" in pay["balance"]["net_debt"]["note"]
    ex = pay["valuation"]["net_debt_ebitda_ex_captive"]
    assert ex["value"] is None and ex["na_reason"] == "not_applicable"
    assert "cannot be separated from XBRL companyfacts" in ex["note"]
    assert any("overstated" in n for n in pay["balance_notes"])
    assert any("net_debt_ebitda_ex_captive is null" in n for n in pay["balance_notes"])
    assert common.is_fact(ex)
    errs = [e.message for e in validator.iter_errors(_doc(built))]
    assert errs == []


def test_override_fraction_fills_ex_captive(validator) -> None:
    built = _build("CAT", _facts(), vcfg={"captive_finance_net_debt_pct_override": {"CAT": 0.1}})
    ex = built["payload"]["valuation"]["net_debt_ebitda_ex_captive"]
    assert ex["value"] == pytest.approx(600 * 0.1 / 160) and ex["na_reason"] is None
    assert "assumption" in ex["note"]
    assert [e.message for e in validator.iter_errors(_doc(built))] == []


def test_config_driven_list(validator) -> None:
    assert "captive_finance" not in _codes(_build("CAT", _facts(), vcfg={"captive_finance_tickers": []}))
    assert "captive_finance" in _codes(_build("ZZMM", _facts(), vcfg={"captive_finance_tickers": ["zzmm"]}))


def test_financials_are_exempt() -> None:
    f = _facts()
    built = FF.build_payload("F", cik=1, cf=_mini_cf(f), sub={"sic": "6199"}, price=100.0, price_as_of="2026-09-30",
                             as_of=ASOF, sector_class="financials", yf_cap=lambda t: (None, "offline"))
    assert "captive_finance" not in _codes(built) and "balance_notes" not in built["payload"]
    assert "net_debt_ebitda_ex_captive" not in built["payload"]["valuation"]


def test_generic_safeguard_flags_leveraged_industrial(validator) -> None:
    built = _build("ZZMM", _facts(), sic=3531)                                         # industrials, 600/1000 = 60 %
    flag = _codes(built)["possible_captive_finance"]
    assert flag["severity"] == "low" and "60%" in flag["detail"] and "industrials" in flag["detail"]
    assert "captive_finance" not in _codes(built)
    assert "net_debt_ebitda_ex_captive" not in built["payload"]["valuation"]           # only listed tickers get it
    assert "balance_notes" not in built["payload"]
    assert [e.message for e in validator.iter_errors(_doc(built))] == []


def test_generic_safeguard_consumer_discretionary_and_threshold() -> None:
    assert "possible_captive_finance" in _codes(_build("ZZMM", _facts(), sic=3711))     # SIC 3711 -> consumer_discretionary
    assert "possible_captive_finance" not in _codes(_build("ZZMM", _facts(net_debt_heavy=False)))   # 50/1000 < 25 %
    assert "possible_captive_finance" not in _codes(_build("ZZMM", _facts(), sic=2080))             # consumer_staples
    assert "possible_captive_finance" not in _codes(_build("ZZMM", _facts(), sic=None))             # sector unknown
    cfg = {"possible_captive_net_debt_to_mcap": 0.7}
    assert "possible_captive_finance" not in _codes(_build("ZZMM", _facts(), vcfg=cfg))


def test_safeguard_silent_without_net_debt_or_market_cap() -> None:
    f = _facts()
    del f["LongTermDebtNoncurrent"], f["LongTermDebtCurrent"]
    assert "possible_captive_finance" not in _codes(_build("ZZMM", f))
    built = FF.build_payload("ZZMM", cik=1, cf=_mini_cf(_facts()), sub={"sic": "3531"}, price=None, price_as_of=None,
                             as_of=ASOF, sector_class="default", yf_cap=lambda t: (None, "offline"))
    assert "possible_captive_finance" not in _codes(built)


def test_ordinary_company_payload_is_unchanged() -> None:
    built = _build("ZZMM", _facts(net_debt_heavy=False), sic=2080)
    pay = built["payload"]
    assert "balance_notes" not in pay and "net_debt_ebitda_ex_captive" not in pay["valuation"]
    assert not any(f["code"] in ("captive_finance", "possible_captive_finance") for f in built["flags"])
    assert "note" not in pay["valuation"]["net_debt_ebitda"] or pay["valuation"]["net_debt_ebitda"]["note"] is None
