"""portfolio.py (DESIGN §4.11): sizing rules, constraint handling, tax/FX notes, schema validity,
and run() over a small synthetic run directory. Fully offline."""
from __future__ import annotations

import copy
import json
import math
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from conftest import REPO_ROOT
from scripts import common, portfolio

AS_OF = "2026-10-02"

PROFILE: dict[str, Any] = {
    "horizon_years": 10, "risk_tolerance": "moderate", "max_position_pct": 10, "max_fund_position_pct": 40,
    "max_sector_pct": 35, "min_trade_usd": 200, "target_cash_pct": 5, "base_currency": "ILS",
    "tax_jurisdiction": "IL", "capital_gains_tax_pct": 25, "allow_short": False,
    "sec_contact_email": "tester@agent-investors.dev", "allow_fractional_shares": False,
}
PRICES = {"QQQ": 500.0, "SPMO": 100.0, "KO": 60.0, "PG": 125.0, "JPM": 200.0, "BRK-B": 500.0}
SHARES = {"QQQ": 60, "SPMO": 150, "KO": 200, "PG": 80, "JPM": 40, "BRK-B": 20}
COST = {"QQQ": 400.0, "SPMO": None, "KO": 50.0, "PG": 160.0, "JPM": None, "BRK-B": 300.0}
#: total 100,000 USD: QQQ 30%, SPMO 15%, KO 12%, PG 10%, JPM 8%, BRK-B 10%, cash 15%
WEIGHTS = {"QQQ": 30.0, "SPMO": 15.0, "KO": 12.0, "PG": 10.0, "JPM": 8.0, "BRK-B": 10.0}
SPLITS = {
    "QQQ": {"information_technology": 60, "communication_services": 20, "consumer_discretionary": 20},
    "SPMO": {"information_technology": 40, "financials": 30, "industrials": 30},
    "KO": {"consumer_staples": 100}, "PG": {"consumer_staples": 100},
    "JPM": {"financials": 100}, "BRK-B": {"financials": 100},
}
SECTOR_OF = {"QQQ": "funds", "SPMO": "funds", "KO": "consumer_staples", "PG": "consumer_staples",
             "JPM": "financials", "BRK-B": "financials"}
LOOKTHROUGH_WEIGHTS = {"information_technology": 24.0, "communication_services": 6.0,
                       "consumer_discretionary": 6.0, "financials": 22.5, "industrials": 4.5,
                       "consumer_staples": 22.0, "cash": 15.0}
#: targets equal to the current look-through: no sector breach unless a test changes them
NEUTRAL_TARGETS = {"information_technology": 24.0, "communication_services": 6.0,
                   "consumer_discretionary": 6.0, "financials": 22.5, "industrials": 4.5,
                   "consumer_staples": 22.0, "health_care": 5.0, "cash": 10.0}


# =====================================================================================
# builders
# =====================================================================================


def _px(t: str, v: float) -> dict:
    return {"last_close": common.fact(v, "USD/share", f"D:PX:{t}", AS_OF, "price"),
            "last_date": AS_OF, "first_date": "2023-10-02", "rows": 753, "provider": "yfinance",
            "stale": False, "adv_usd_63d": common.fact(1e9, "USD", f"D:PX:{t}", AS_OF, "price"),
            "returns_pct": {k: common.fact(1.0, "pct", f"D:PX:{t}", AS_OF, "price") for k in ("1m", "3m", "6m", "12m")},
            "series_path": f"data/cache/prices/prices_{t}.csv"}


def make_prices(extra: dict[str, float] | None = None, fx: float | None = 3.7) -> dict:
    tickers = {t: _px(t, v) for t, v in {**PRICES, **(extra or {})}.items()}
    srcs = [common.dataset_source(f"D:PX:{t}") for t in tickers] + [common.dataset_source("D:PX:ILS=X")]
    fx_fact = common.fact(fx, "ILS/USD", "D:PX:ILS=X", AS_OF, "price") if fx is not None else None
    return common.envelope("prices", AS_OF, sources=srcs, tickers=tickers, failed={}, fx={"usdils": fx_fact})


def make_lookthrough(weights: dict[str, float] | None = None, sector_weights: dict[str, float] | None = None) -> dict:
    weights = weights or WEIGHTS
    holdings = []
    for t, w in weights.items():
        holdings.append({"ticker": t, "asset_type": "fund" if SECTOR_OF[t] == "funds" else "stock",
                         "weight_pct": common.fact(w, "pct", "D:CFG:portfolio", AS_OF, "other"),
                         "sector": SECTOR_OF[t], "sector_split": SPLITS[t],
                         "classification_source": "test", "flags": []})
    src = common.source("D:CFG:portfolio", "https://github.com/agent-investors/config/portfolio.yaml",
                        "portfolio.yaml", AS_OF)
    return common.envelope(
        "lookthrough", AS_OF, sources=[src],
        total_value_usd=common.fact(100000.0, "USD", "D:CFG:portfolio", AS_OF, "other"),
        holdings=holdings, sector_weights_pct=dict(sector_weights or LOOKTHROUGH_WEIGHTS), funds={},
        overlap={"pairs": [], "direct_overlap": []})


def make_portfolio_cfg() -> dict:
    return {"holdings": [{"ticker": t, "shares": SHARES[t], "cost_basis": COST[t], "cost_basis_currency": "USD",
                          "proxy_ticker": None} for t in SHARES]}


def make_cfg(**sizing: float) -> dict:
    cfg = common.committee_cfg()
    cfg["sizing"] = {"trim_fraction": 0.33, "breach_buffer_pp": 0.5, "add_step_pp": 2.0,
                     "initial_position_pct": 3.0, **sizing}
    cfg.setdefault("actions", {})["sector_breach_tolerance_pp"] = 1.0
    return cfg


def plan_for(actions: dict, *, targets: dict | None = None, profile: dict | None = None,
             prices: dict | None = None, extra_prices: dict | None = None, cfg: dict | None = None,
             lookthrough: dict | None = None, schema_name: str = "portfolio_plan") -> dict:
    return portfolio.build_plan(
        lookthrough=lookthrough or make_lookthrough(), prices=prices or make_prices(), actions=actions,
        target_sector_weights=NEUTRAL_TARGETS if targets is None else targets,
        profile={**PROFILE, **(profile or {})}, cfg=cfg or make_cfg(), portfolio_cfg=make_portfolio_cfg(),
        extra_prices=extra_prices, schema_name=schema_name)


def pos(plan: dict, ticker: str) -> dict:
    for p in plan["positions"]:
        if p["ticker"] == ticker:
            return p
    raise KeyError(ticker)


def sector_row(plan: dict, sector: str) -> dict:
    return next(s for s in plan["sectors"] if s["sector"] == sector)


def check(plan: dict, name: str) -> dict:
    return next(c for c in plan["constraint_checks"] if c["check"] == name)


HOLD_ALL = {t: {"action": "HOLD", "conviction": 5.0} for t in WEIGHTS}


def with_actions(**over: dict) -> dict:
    acts = copy.deepcopy(HOLD_ALL)
    for t, a in over.items():
        acts[t.replace("_", "-")] = a
    return acts


# =====================================================================================
# schema helper
# =====================================================================================


@pytest.fixture(scope="module")
def plan_validator() -> Draft202012Validator:
    resources = []
    for path in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
    registry = Registry().with_resources(resources)
    schema = json.loads((REPO_ROOT / "schemas" / "data" / "portfolio_plan.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def _errors(validator: Draft202012Validator, doc: dict) -> list[str]:
    doc = json.loads(common.dumps_json(doc))  # exactly what lands on disk
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(doc)]


# =====================================================================================
# sizing rules
# =====================================================================================


def test_hold_everything_produces_no_trades():
    plan = plan_for(HOLD_ALL)
    assert all(p["trade"]["side"] == "NONE" for p in plan["positions"])
    assert plan["cash"]["current_pct"] == pytest.approx(15.0)
    assert plan["cash"]["post_pct"] == pytest.approx(15.0)
    assert plan["cash"]["target_pct"] == PROFILE["target_cash_pct"]
    for p in plan["positions"]:
        assert p["weight_tgt_pct"] == pytest.approx(p["weight_cur_pct"])


def test_sell_is_a_full_exit():
    plan = plan_for(with_actions(JPM={"action": "SELL", "conviction": 2.5}))
    jpm = pos(plan, "JPM")
    assert jpm["sizing_rule"] == "sell_full_exit"
    assert jpm["trade"]["side"] == "SELL"
    assert jpm["trade"]["shares"] == SHARES["JPM"]
    assert jpm["trade"]["value_usd"] == pytest.approx(8000.0)
    assert jpm["weight_tgt_pct"] == 0.0
    assert plan["cash"]["post_pct"] == pytest.approx(23.0)
    assert sector_row(plan, "financials")["post_lookthrough_pct"] == pytest.approx(14.5)


def test_conviction_trim_formula_and_whole_shares():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}))
    pg = pos(plan, "PG")
    # no position or sector breach -> conviction trim: min(cap, 10 * (1 - 0.33)) = 6.7%
    assert pg["sizing_rule"] == "trim_conviction"
    # 3.3 pp of 100k = 3300 USD / 125 = 26.4 -> 26 whole shares
    assert pg["trade"] == {**pg["trade"], "side": "SELL", "shares": 26}
    assert isinstance(pg["trade"]["shares"], int)
    assert pg["trade"]["value_usd"] == pytest.approx(26 * 125.0)
    assert pg["weight_tgt_pct"] == pytest.approx(10.0 - 3.25)


def test_conviction_trim_respects_custom_trim_fraction():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), cfg=make_cfg(trim_fraction=0.5))
    assert pos(plan, "PG")["trade"]["shares"] == 40  # 5 pp = 5000 / 125


def test_breach_trim_position_over_cap():
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 4.2}))
    ko = pos(plan, "KO")
    # 12% > 10% cap -> min(cap - 0.5, cur) = 9.5%: 2.5 pp = 2500 / 60 = 41.67 -> a breach trim
    # rounds UP (42 shares) so the sized 9.5% target is actually reached
    assert ko["sizing_rule"] == "trim_breach_position"
    assert ko["trade"]["shares"] == 42
    assert ko["weight_tgt_pct"] == pytest.approx(12.0 - 42 * 60 / 1000)
    assert ko["weight_tgt_pct"] <= PROFILE["max_position_pct"] - 0.5
    assert check(plan, "max_position_pct")["status"] == "pass"


@pytest.mark.parametrize("ko_conv, pg_conv, cut", [(4.0, 3.8, "PG"), (3.5, 4.4, "KO")])
def test_sector_breach_trims_lowest_conviction_first(ko_conv: float, pg_conv: float, cut: str):
    targets = {**NEUTRAL_TARGETS, "consumer_staples": 18.0}  # 22% > 18 + 1 tolerance -> breach
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": ko_conv},
                                 PG={"action": "TRIM", "conviction": pg_conv}), targets=targets)
    ko, pg = pos(plan, "KO"), pos(plan, "PG")
    # base breach trims: KO 12 -> 9.5 (position), PG 10 -> 9.5 (min(cap - 0.5, cur)); staples 19 > 18
    # -> the remaining 1 pp comes from the LOWER-conviction TRIM only
    lower, higher = (pg, ko) if cut == "PG" else (ko, pg)
    assert lower["weight_tgt_pct"] < 9.0
    assert higher["weight_tgt_pct"] >= 9.45
    assert lower["sizing_rule"] == "trim_breach_sector"
    staples = sector_row(plan, "consumer_staples")
    assert staples["post_lookthrough_pct"] == pytest.approx(18.0, abs=0.1)  # whole-share rounding slack
    assert staples["breach"] is False


def test_add_respects_max_position_and_sector_headroom():
    targets = {**NEUTRAL_TARGETS, "financials": 30.0}  # 7.5 pp of financials headroom
    plan = plan_for(with_actions(JPM={"action": "ADD", "conviction": 7.5},
                                 BRK_B={"action": "ADD", "conviction": 7.2}), targets=targets)
    jpm, brk = pos(plan, "JPM"), pos(plan, "BRK-B")
    assert jpm["trade"]["side"] == "BUY" and jpm["trade"]["shares"] == 10       # +2 pp = 2000 / 200
    assert jpm["weight_tgt_pct"] == pytest.approx(10.0)
    assert brk["trade"]["side"] == "NONE" and brk["dropped_reason"] == "at_position_cap"  # already at 10%
    assert all(p["weight_tgt_pct"] <= PROFILE["max_position_pct"] + 1e-9
               for p in plan["positions"] if p["asset_type"] == "stock" and p["action"] == "ADD")


def test_add_blocked_without_sector_headroom():
    plan = plan_for(with_actions(JPM={"action": "ADD", "conviction": 7.5}))  # financials at target 22.5
    jpm = pos(plan, "JPM")
    assert jpm["trade"]["side"] == "NONE"
    assert jpm["dropped_reason"] == "no_sector_headroom"


def test_buys_respect_max_sector_and_targets():
    targets = {**NEUTRAL_TARGETS, "health_care": 4.0}
    acts = with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"},
                        LLY={"action": "BUY", "conviction": 7.1, "sector": "health_care"})
    plan = plan_for(acts, targets=targets, extra_prices={"MRK": 100.0, "LLY": 100.0})
    mrk, lly = pos(plan, "MRK"), pos(plan, "LLY")
    # higher conviction first: MRK gets the initial 3%, LLY only the remaining 1 pp of headroom
    assert mrk["weight_tgt_pct"] == pytest.approx(3.0) and mrk["trade"]["shares"] == 30
    assert lly["weight_tgt_pct"] == pytest.approx(1.0) and lly["trade"]["shares"] == 10
    assert sector_row(plan, "health_care")["post_lookthrough_pct"] <= 4.0 + 1e-9
    assert mrk["kind"] == "candidate" and mrk["weight_cur_pct"] == 0.0


def test_max_sector_pct_caps_buys_even_without_cio_target():
    targets = {k: v for k, v in NEUTRAL_TARGETS.items() if k != "consumer_staples"}
    lt = make_lookthrough(sector_weights={**LOOKTHROUGH_WEIGHTS, "consumer_staples": 34.0, "cash": 3.0})
    acts = with_actions(KO={"action": "HOLD", "conviction": 6.0},
                        CL={"action": "BUY", "conviction": 8.0, "sector": "consumer_staples"})
    plan = plan_for(acts, targets=targets, lookthrough=lt, extra_prices={"CL": 50.0},
                    profile={"target_cash_pct": 0})
    cl = pos(plan, "CL")
    assert cl["weight_tgt_pct"] == pytest.approx(1.0)  # 35 max - 34 current
    assert sector_row(plan, "consumer_staples")["post_lookthrough_pct"] <= 35.0 + 1e-9


def test_min_trade_drops_small_trades():
    targets = {**NEUTRAL_TARGETS, "health_care": 0.15}  # 150 USD of headroom < 200 minimum
    acts = with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"})
    plan = plan_for(acts, targets=targets, extra_prices={"MRK": 10.0})
    mrk = pos(plan, "MRK")
    assert mrk["trade"]["side"] == "NONE" and mrk["trade"]["value_usd"] == 0.0
    assert mrk["dropped_reason"] == "below_min_trade_usd"
    assert mrk["trade"]["proposed_value_usd"] == pytest.approx(150.0)
    assert mrk["weight_tgt_pct"] == 0.0
    assert "MRK" in check(plan, "min_trade_usd")["detail"]
    assert all(p["trade"]["side"] == "NONE" or abs(p["trade"]["value_usd"]) >= PROFILE["min_trade_usd"]
               for p in plan["positions"])


def test_min_trade_also_applies_to_sells():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), profile={"min_trade_usd": 5000})
    pg = pos(plan, "PG")
    assert pg["trade"]["side"] == "NONE" and pg["dropped_reason"] == "below_min_trade_usd"
    assert pg["weight_tgt_pct"] == pytest.approx(10.0)


def test_rounds_to_zero_shares_is_dropped():
    targets = {**NEUTRAL_TARGETS, "health_care": 3.0}
    acts = with_actions(BIG={"action": "BUY", "conviction": 8.0, "sector": "health_care"})
    plan = plan_for(acts, targets=targets, extra_prices={"BIG": 5000.0})  # 3000 USD < one share
    big = pos(plan, "BIG")
    assert big["trade"]["side"] == "NONE" and big["dropped_reason"] == "rounds_to_zero_shares"


def test_buys_scaled_pro_rata_to_keep_target_cash():
    targets = {**NEUTRAL_TARGETS, "health_care": 10.0}
    acts = with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"},
                        LLY={"action": "BUY", "conviction": 7.5, "sector": "health_care"})
    # cash 15%, target 10% -> 5 pp available for 6 pp of buys -> factor 5/6 -> 2.5 pp each
    plan = plan_for(acts, targets=targets, profile={"target_cash_pct": 10},
                    extra_prices={"MRK": 100.0, "LLY": 100.0})
    mrk, lly = pos(plan, "MRK"), pos(plan, "LLY")
    assert mrk["trade"]["shares"] == 25 and lly["trade"]["shares"] == 25
    assert mrk["weight_tgt_pct"] == pytest.approx(2.5) and lly["weight_tgt_pct"] == pytest.approx(2.5)
    assert plan["cash"]["post_pct"] >= 10.0 - 1e-9
    assert plan["sizing"]["buy_scale"] == pytest.approx(5 / 6, rel=1e-5)
    assert check(plan, "target_cash_pct")["status"] == "pass"
    assert any(f["code"] == "buys_scaled" for f in plan["flags"])


def test_sell_proceeds_fund_buys_without_scaling():
    targets = {**NEUTRAL_TARGETS, "health_care": 10.0}
    acts = with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"},
                        LLY={"action": "BUY", "conviction": 7.5, "sector": "health_care"},
                        JPM={"action": "SELL", "conviction": 2.0})
    plan = plan_for(acts, targets=targets, profile={"target_cash_pct": 10},
                    extra_prices={"MRK": 100.0, "LLY": 100.0})
    assert pos(plan, "MRK")["trade"]["shares"] == 30 and pos(plan, "LLY")["trade"]["shares"] == 30
    assert plan["sizing"]["buy_scale"] == 1.0
    assert plan["cash"]["post_pct"] == pytest.approx(15.0 + 8.0 - 6.0)


def test_funds_never_sold_to_fund_stock_buys():
    targets = {**NEUTRAL_TARGETS, "health_care": 10.0}
    acts = with_actions(MRK={"action": "BUY", "conviction": 9.0, "sector": "health_care"})
    plan = plan_for(acts, targets=targets, profile={"target_cash_pct": 15},  # zero spare cash
                    extra_prices={"MRK": 100.0})
    mrk = pos(plan, "MRK")
    assert mrk["trade"]["side"] == "NONE" and mrk["dropped_reason"] == "insufficient_cash"
    for fund in ("QQQ", "SPMO"):
        assert pos(plan, fund)["trade"]["side"] == "NONE"
        assert pos(plan, fund)["weight_tgt_pct"] == pytest.approx(WEIGHTS[fund])
    assert check(plan, "funds_not_sold_for_buys")["status"] == "pass"
    assert any("never sold" in n for n in plan["notes"])


def test_fund_add_limited_by_lookthrough_headroom_and_fund_cap():
    targets = {**NEUTRAL_TARGETS, "information_technology": 30.0, "communication_services": 10.0,
               "consumer_discretionary": 10.0}
    plan = plan_for(with_actions(QQQ={"action": "ADD", "conviction": 7.8}), targets=targets)
    qqq = pos(plan, "QQQ")
    assert qqq["trade"]["side"] == "BUY"
    assert qqq["trade"]["shares"] == 4  # +2 pp = 2000 USD / 500
    assert qqq["weight_tgt_pct"] == pytest.approx(32.0)
    assert qqq["cap_pct"] == PROFILE["max_fund_position_pct"]
    # QQQ is 60% IT: +2 pp of QQQ adds 1.2 pp to IT look-through
    assert sector_row(plan, "information_technology")["post_lookthrough_pct"] == pytest.approx(25.2)


# =====================================================================================
# tax, FX
# =====================================================================================


def test_tax_flag_on_gains_only():
    acts = with_actions(KO={"action": "SELL", "conviction": 2.0},   # cost 50 < 60 -> gain
                        PG={"action": "SELL", "conviction": 2.0},   # cost 160 > 125 -> loss
                        JPM={"action": "SELL", "conviction": 2.0})  # no cost basis
    plan = plan_for(acts)
    ko, pg, jpm = pos(plan, "KO"), pos(plan, "PG"), pos(plan, "JPM")
    assert ko["tax"]["applicable"] is True
    assert ko["tax"]["est_gain_usd"] == pytest.approx((60 - 50) * 200)
    assert ko["tax"]["est_tax_usd"] == pytest.approx(0.25 * 2000)
    assert "real gain" in ko["tax"]["note"].lower() and "cpi" in ko["tax"]["note"].lower()
    assert "tax advisor" in ko["tax"]["note"].lower()
    assert pg["tax"]["applicable"] is False and pg["tax"]["est_tax_usd"] is None
    assert pg["tax"]["est_gain_usd"] == pytest.approx((125 - 160) * 80)
    assert jpm["tax"]["applicable"] is False and jpm["tax"]["est_tax_usd"] is None
    assert "cost basis" in jpm["tax"]["note"].lower()
    # buys / holds never carry a tax estimate
    assert pos(plan, "QQQ")["tax"]["applicable"] is False
    assert any("real gain" in n.lower() for n in plan["notes"])


def test_tax_rate_comes_from_profile():
    plan = plan_for(with_actions(KO={"action": "SELL", "conviction": 2.0}), profile={"capital_gains_tax_pct": 30})
    assert pos(plan, "KO")["tax"]["est_tax_usd"] == pytest.approx(0.30 * 2000)


def test_ils_values_use_fx_fact():
    plan = plan_for(with_actions(JPM={"action": "SELL", "conviction": 2.0}), prices=make_prices(fx=3.65))
    jpm = pos(plan, "JPM")
    assert plan["fx_usdils"]["value"] == pytest.approx(3.65)
    assert plan["fx_usdils"]["source_id"] == "D:PX:ILS=X"
    assert jpm["trade"]["value_ils"] == pytest.approx(8000.0 * 3.65)
    assert jpm["value_ils_cur"] == pytest.approx(8000.0 * 3.65)
    assert plan["base_currency"] == "ILS"
    assert any("USD/ILS" in n for n in plan["notes"])


def test_missing_fx_leaves_ils_null():
    plan = plan_for(with_actions(JPM={"action": "SELL", "conviction": 2.0}), prices=make_prices(fx=None))
    assert plan["fx_usdils"] is None
    assert pos(plan, "JPM")["trade"]["value_ils"] is None
    assert any("USD/ILS" in m for m in plan["missing_data"])


def test_unheld_trim_is_ignored_and_missing_verdict_holds():
    acts = with_actions(XYZ={"action": "TRIM", "conviction": 3.0})
    del acts["PG"]
    plan = plan_for(acts)
    assert "XYZ" not in {p["ticker"] for p in plan["positions"]}
    pg = pos(plan, "PG")
    assert pg["action"] == "HOLD" and pg["action_source"] == "no_verdict"
    assert any("PG" in m for m in plan["missing_data"])


# =====================================================================================
# schema
# =====================================================================================


def test_plan_validates_against_schema(plan_validator: Draft202012Validator):
    targets = {**NEUTRAL_TARGETS, "consumer_staples": 18.0, "health_care": 10.0}
    acts = with_actions(KO={"action": "TRIM", "conviction": 4.0}, PG={"action": "TRIM", "conviction": 3.8},
                        JPM={"action": "SELL", "conviction": 2.5},
                        MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"})
    plan = plan_for(acts, targets=targets, extra_prices={"MRK": 100.0})
    assert plan["schema"] == "portfolio_plan/v1"
    assert _errors(plan_validator, plan) == []
    preview = plan_for(acts, targets=targets, extra_prices={"MRK": 100.0}, schema_name="portfolio_preview")
    assert preview["schema"] == "portfolio_preview/v1"
    assert _errors(plan_validator, preview) == []
    # every sector row from the 11 GICS keys + cash + unclassified
    assert [s["sector"] for s in plan["sectors"]] == list(common.SECTOR_KEYS) + ["cash", "unclassified"]
    assert {s["id"] for s in plan["sources"]} >= {"D:PX:KO", "D:PX:ILS=X"}


def test_schema_rejects_broken_plan(plan_validator: Draft202012Validator):
    plan = plan_for(HOLD_ALL)
    bad = copy.deepcopy(plan)
    bad["positions"][0]["trade"]["side"] = "SHORT"
    assert _errors(plan_validator, bad)


# =====================================================================================
# run(run_dir)
# =====================================================================================


def _verdict(sector: str, phase: int, verdicts: list[dict]) -> dict:
    return {"schema_version": "1.0", "run_id": "2026-10-02_1000", "order_id": f"p{phase}-sh-{sector}",
            "agent_role": "sector_head", "phase": phase, "sector": sector, "as_of": AS_OF,
            "summary": "test", "verdicts": verdicts, "sources": []}


def _v(t: str, action: str, conv: float, kind: str = "holding", **extra: Any) -> dict:
    return {"ticker": t, "kind": kind, "action": action, "rule_action": action, "agg_conviction": conv,
            "agg_evidence": 7.0, "rr": 2.0, "rationale": "test", "dissents": [], "unresolved_red_flags": [],
            "rule_deviation": False, "deviation_justification": None, **extra}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "runs" / "2026-10-02_1000"
    inputs = rd / "inputs"
    inputs.mkdir(parents=True)
    for f in (REPO_ROOT / "config").glob("*.yaml"):
        shutil.copy2(f, inputs / f.name)
    import yaml
    common.write_text(inputs / "investor_profile.yaml", yaml.safe_dump(PROFILE, sort_keys=False))
    common.write_text(inputs / "portfolio.yaml", yaml.safe_dump(make_portfolio_cfg(), sort_keys=False))
    common.write_json(rd / "manifest.json", {"run_id": "2026-10-02_1000", "as_of": AS_OF,
                                             "mode": {"dry_run": True, "quick": False}, "orders": {}})
    common.write_json(rd / "phase1" / "lookthrough.json", make_lookthrough())
    common.write_json(rd / "phase1" / "prices.json", make_prices())
    common.write_json(rd / "phase1" / "cio_allocation.json",
                      {"target_sector_weights": {**NEUTRAL_TARGETS, "consumer_staples": 18.0, "health_care": 8.0}})
    common.write_json(rd / "phase2" / "consumer_staples_verdict.json",
                      _verdict("consumer_staples", 2, [_v("KO", "TRIM", 4.0), _v("PG", "HOLD", 5.5)]))
    common.write_json(rd / "phase2" / "financials_verdict.json",
                      _verdict("financials", 2, [_v("JPM", "SELL", 2.5), _v("BRK-B", "HOLD", 6.0)]))
    common.write_json(rd / "phase2" / "funds_verdict.json",
                      _verdict("funds", 2, [_v("QQQ", "HOLD", 6.0), _v("SPMO", "HOLD", 6.0)]))
    common.write_json(rd / "phase3" / "health_care_verdict.json", _verdict("health_care", 3, [
        _v("MRK", "BUY", 7.6, "candidate", thesis="t", fair_value_low=90, fair_value_base=130,
           fair_value_high=150, invalidation_price=88),
        _v("LLY", "PASS", 5.0, "candidate")]))
    common.write_json(rd / "phase3" / "data" / "technicals_MRK.json",
                      {"ticker": "MRK", "close": common.fact(100.0, "USD/share", "D:PX:MRK", AS_OF, "price")})
    return rd


def test_run_preview_reads_verdicts_and_candidate_prices(run_dir: Path, plan_validator: Draft202012Validator):
    plan = portfolio.run(run_dir, final=False)
    out = run_dir / "phase4" / "portfolio_preview.json"
    assert out.is_file()
    # DESIGN §4.11: both files are portfolio_plan/v1 (validate can infer the schema)
    assert common.read_json(out)["schema"] == "portfolio_plan/v1"
    assert _errors(plan_validator, common.read_json(out)) == []
    tickers = {p["ticker"]: p for p in plan["positions"]}
    assert "LLY" not in tickers                       # PASS candidates are not in the plan
    assert tickers["MRK"]["trade"]["side"] == "BUY"
    assert tickers["MRK"]["price"]["value"] == pytest.approx(100.0)
    assert tickers["MRK"]["trade"]["shares"] == 30
    assert tickers["JPM"]["trade"]["side"] == "SELL" and tickers["JPM"]["trade"]["shares"] == 40
    assert tickers["KO"]["action_source"] == "sector_verdict_p2"
    assert tickers["KO"]["trade"]["side"] == "SELL"
    assert sector_row(plan, "consumer_staples")["target_pct"] == 18.0


def test_run_final_uses_cio_final(run_dir: Path, plan_validator: Draft202012Validator):
    common.write_json(run_dir / "phase4" / "cio_final.json", {
        "agent_role": "cio", "mode": "reconciliation",
        "final_target_sector_weights": {**NEUTRAL_TARGETS, "health_care": 8.0},
        "final_actions": [
            {"ticker": "KO", "action": "HOLD", "source_verdict_action": "TRIM", "override": True,
             "override_justification": "x"},
            {"ticker": "PG", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "JPM", "action": "SELL", "source_verdict_action": "SELL", "override": False},
            {"ticker": "BRK-B", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "QQQ", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "SPMO", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "MRK", "action": "BUY", "source_verdict_action": "BUY", "override": False}]})
    plan = portfolio.run(run_dir, final=True)
    out = run_dir / "phase4" / "portfolio_plan.json"
    assert out.is_file() and plan["schema"] == "portfolio_plan/v1"
    assert _errors(plan_validator, common.read_json(out)) == []
    ko = pos(plan, "KO")
    assert ko["action"] == "HOLD" and ko["action_source"] == "cio_override" and ko["trade"]["side"] == "NONE"
    assert pos(plan, "MRK")["action_source"] == "cio_final"
    assert pos(plan, "MRK")["conviction"] == pytest.approx(7.6)
    assert not any(f["code"] == "cio_final_missing" for f in plan["flags"])


def test_run_final_without_cio_final_falls_back_and_flags(run_dir: Path):
    plan = portfolio.run(run_dir, final=True)
    assert any(f["code"] == "cio_final_missing" and f["severity"] == "high" for f in plan["flags"])
    assert (run_dir / "phase4" / "portfolio_plan.json").is_file()


def test_run_candidate_price_falls_back_to_cached_series(run_dir: Path, monkeypatch: pytest.MonkeyPatch):
    (run_dir / "phase3" / "data" / "technicals_MRK.json").unlink()
    df = pd.DataFrame({"Close": [95.0, 101.0, 999.0]},
                      index=pd.to_datetime(["2026-09-30", "2026-10-01", "2026-10-05"]))
    fake = types.ModuleType("scripts.fetch_prices")
    fake.load_series = lambda t: df if t == "MRK" else None  # type: ignore[attr-defined]
    import scripts
    monkeypatch.setitem(sys.modules, "scripts.fetch_prices", fake)
    monkeypatch.setattr(scripts, "fetch_prices", fake, raising=False)
    plan = portfolio.run(run_dir, final=False)
    mrk = pos(plan, "MRK")
    assert mrk["price"]["value"] == pytest.approx(101.0)  # at or before as_of, never a later bar
    assert mrk["price"]["as_of"] == "2026-10-01"


def test_cli_writes_preview(run_dir: Path, capsys: pytest.CaptureFixture[str]):
    assert portfolio.main(["--run-dir", str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["out"].endswith("phase4/portfolio_preview.json")
    assert set(out["checks"]) >= {"max_position_pct", "max_sector_pct", "target_cash_pct"}


def test_as_built_lookthrough_shape_proxy_and_cash_block():
    """etf_lookthrough also emits price_ticker/proxy_ticker/price/value_usd per holding and a
    top-level cash block; portfolio cash must come from that block (not 100 - holdings, which
    would swallow an unallocated residual), and proxy-priced sells get no tax estimate."""
    lt = make_lookthrough(sector_weights={**LOOKTHROUGH_WEIGHTS, "cash": 10.0, "unclassified": 5.0})
    for h in lt["holdings"]:
        if h["ticker"] == "QQQ":
            h.update({"ticker": "TASE-NDX", "proxy_ticker": "QQQ", "price_ticker": "QQQ", "shares": 60,
                      "price": common.fact(500.0, "USD/share", "D:PX:QQQ", AS_OF, "price")})
    lt["cash"] = {"value_usd": common.fact(10000.0, "USD", None, AS_OF, "other"),
                  "weight_pct": common.fact(10.0, "pct", None, AS_OF, "other"), "input": "cash_usd"}
    pcfg = make_portfolio_cfg()
    pcfg["holdings"].append({"ticker": "TASE-NDX", "shares": 60, "cost_basis": 100.0, "proxy_ticker": "QQQ"})
    acts = {**HOLD_ALL, "TASE-NDX": {"action": "TRIM", "conviction": 4.0}}
    del acts["QQQ"]
    plan = portfolio.build_plan(lookthrough=lt, prices=make_prices(), actions=acts,
                                target_sector_weights=NEUTRAL_TARGETS, profile=PROFILE, cfg=make_cfg(),
                                portfolio_cfg=pcfg)
    assert plan["cash"]["current_pct"] == pytest.approx(10.0)
    fund = pos(plan, "TASE-NDX")
    assert fund["price_ticker"] == "QQQ" and fund["price"]["value"] == pytest.approx(500.0)
    # conviction trim of a fund: 30% -> 20.1% = 9.9 pp = 9900 USD / 500 = 19.8 -> 19 units
    assert fund["trade"]["side"] == "SELL" and fund["trade"]["shares"] == 19
    assert fund["tax"]["applicable"] is False and "proxy" in fund["tax"]["note"].lower()
    assert any("proxy" in n.lower() for n in plan["notes"])


def test_constraint_checks_warn_on_residual_fund_driven_breach():
    lt = make_lookthrough(sector_weights={**LOOKTHROUGH_WEIGHTS, "information_technology": 37.0, "cash": 2.0})
    plan = plan_for(HOLD_ALL, lookthrough=lt)
    c = check(plan, "max_sector_pct")
    assert c["status"] == "warn" and "information_technology" in c["detail"]
    assert sector_row(plan, "information_technology")["breach"] is True
    assert not math.isnan(sector_row(plan, "information_technology")["post_lookthrough_pct"])


# =====================================================================================
# integration: the render unit's synthetic run (tests/fixtures/memo_run), copied to tmp
# =====================================================================================


@pytest.fixture
def memo_run_copy(tmp_path: Path) -> Path:
    src = REPO_ROOT / "tests" / "fixtures" / "memo_run"
    if not (src / "phase1" / "lookthrough.json").is_file():
        pytest.skip("tests/fixtures/memo_run not available")
    dst = tmp_path / "memo_run"
    shutil.copytree(src, dst)
    for name in ("portfolio_preview.json", "portfolio_plan.json"):
        (dst / "phase4" / name).unlink(missing_ok=True)
    return dst


@pytest.mark.parametrize("final", [False, True])
def test_run_on_shared_synthetic_run(memo_run_copy: Path, plan_validator: Draft202012Validator, final: bool):
    plan = portfolio.run(memo_run_copy, final=final)
    out = memo_run_copy / "phase4" / ("portfolio_plan.json" if final else "portfolio_preview.json")
    doc = common.read_json(out)
    assert _errors(plan_validator, doc) == []
    held = {h["ticker"] for h in common.read_json(memo_run_copy / "phase1" / "lookthrough.json")["holdings"]}
    assert held <= {p["ticker"] for p in plan["positions"]}
    assert plan["cash"]["post_pct"] >= min(plan["cash"]["current_pct"], plan["cash"]["target_pct"]) - 1e-6
    for p in plan["positions"]:
        assert p["trade"]["side"] in ("BUY", "SELL", "NONE")
        if p["trade"]["side"] != "NONE":
            assert abs(p["trade"]["value_usd"]) >= PROFILE["min_trade_usd"] or p["action"] == "SELL"


def test_preview_schema_is_inferable_by_validate(run_dir: Path):
    portfolio.run(run_dir, final=False)
    doc = common.read_json(run_dir / "phase4" / "portfolio_preview.json")
    try:
        from scripts import validate
    except ImportError:  # sibling unit not present
        pytest.skip("scripts/validate.py not available")
    assert validate.infer_schema(doc) == "portfolio_plan"


def test_unpriced_partial_sell_is_dropped_not_unsized():
    no_prices = {"tickers": {}, "fx": {"usdils": None}}
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), prices=no_prices)
    pg = pos(plan, "PG")
    assert pg["trade"]["side"] == "NONE" and pg["trade"]["shares"] == 0
    assert pg["dropped_reason"] == "no_price"
    assert pg["trade"]["proposed_value_usd"] == pytest.approx(3300.0)
    assert pg["weight_tgt_pct"] == pytest.approx(10.0)
    assert not any(p["trade"]["side"] != "NONE" and p["trade"]["shares"] is None
                   for p in plan["positions"] if p["action"] != "SELL")
    assert any("PG" in m for m in plan["missing_data"])


def test_unpriced_full_exit_still_sells_every_share():
    plan = plan_for(with_actions(JPM={"action": "SELL", "conviction": 2.0}),
                    prices={"tickers": {}, "fx": {"usdils": None}})
    jpm = pos(plan, "JPM")
    assert jpm["trade"]["side"] == "SELL" and jpm["weight_tgt_pct"] == 0.0


# ---- manifest-driven resolution (failed sector heads, CIO retries) ----------------------


def _set_order(rd: Path, oid: str, **fields: Any) -> None:
    m = common.read_json(rd / "manifest.json")
    m["orders"][oid].update(fields)
    common.write_json(rd / "manifest.json", m)


def _fail_order(rd: Path, oid: str, output_rel: str) -> None:
    stub_rel = output_rel[: -len(".json")] + ".failed.json"
    common.write_json(rd / stub_rel, {"order_id": oid, "status": "failed", "attempts": 2, "errors": ["x"]})
    _set_order(rd, oid, status="failed", attempt=2, resolved_output=stub_rel)


def test_all_failed_sector_heads_of_a_phase_yield_no_verdicts(memo_run_copy: Path):
    rd = memo_run_copy
    assert portfolio.verdict_files(rd, 3)                                   # sanity: valid run
    _fail_order(rd, "p3-sh-health_care", "phase3/health_care_verdict.json")
    assert portfolio.verdict_files(rd, 3) == []                             # attempt-1 file NOT globbed
    assert {p.name for p in portfolio.verdict_files(rd, 2)} == {
        "consumer_staples_verdict.json", "financials_verdict.json", "funds_verdict.json"}
    plan = portfolio.run(rd, final=False)
    assert "MRK" not in {p["ticker"] for p in plan["positions"]}            # no BUY without a verdict


def test_failed_order_without_resolved_output_is_not_used(memo_run_copy: Path):
    rd = memo_run_copy
    _set_order(rd, "p3-sh-health_care", status="failed", resolved_output=None)
    assert portfolio.verdict_files(rd, 3) == []


def test_sector_head_retry_is_preferred(memo_run_copy: Path):
    rd = memo_run_copy
    doc = common.read_json(rd / "phase3" / "health_care_verdict.json")
    for v in doc["verdicts"]:
        if v["ticker"] == "MRK":
            v["action"] = "PASS"
    common.write_json(rd / "phase3" / "health_care_verdict.r2.json", doc)
    _set_order(rd, "p3-sh-health_care", attempt=2, resolved_output="phase3/health_care_verdict.r2.json")
    assert [p.name for p in portfolio.verdict_files(rd, 3)] == ["health_care_verdict.r2.json"]
    assert "MRK" not in {p["ticker"] for p in portfolio.run(rd, final=False)["positions"]}


def test_run_final_uses_cio_retry_from_manifest(memo_run_copy: Path):
    rd = memo_run_copy
    doc = common.read_json(rd / "phase4" / "cio_final.json")
    for fa in doc["final_actions"]:
        if fa["ticker"] == "JPM":
            fa.update({"action": "HOLD", "override": False})
            fa.pop("override_justification", None)
    common.write_json(rd / "phase4" / "cio_final.r2.json", doc)
    _set_order(rd, "p4-cio", attempt=2, resolved_output="phase4/cio_final.r2.json")
    resolved, path = portfolio.resolve_cio(rd, "reconciliation")
    assert path is not None and path.name == "cio_final.r2.json"
    plan = portfolio.run(rd, final=True)
    jpm = pos(plan, "JPM")
    assert (jpm["action"], jpm["action_source"], jpm["trade"]["side"]) == ("HOLD", "cio_final", "NONE")
    assert not any(f["code"] == "cio_final_missing" for f in plan["flags"])


def test_run_final_failed_cio_order_falls_back_and_flags(memo_run_copy: Path):
    rd = memo_run_copy
    _fail_order(rd, "p4-cio", "phase4/cio_final.json")                      # attempt-1 file still on disk
    assert portfolio.resolve_cio(rd, "reconciliation") == (None, None)
    plan = portfolio.run(rd, final=True)
    assert any(f["code"] == "cio_final_missing" for f in plan["flags"])
    assert pos(plan, "JPM")["action_source"].startswith("sector_verdict")  # not the rejected CIO TRIM


def test_preview_uses_cio_allocation_retry_and_ignores_failed(memo_run_copy: Path):
    rd = memo_run_copy
    alloc = common.read_json(rd / "phase1" / "cio_allocation.json")
    alloc["target_sector_weights"] = {**alloc["target_sector_weights"], "health_care": 6.5}
    common.write_json(rd / "phase1" / "cio_allocation.r2.json", alloc)
    _set_order(rd, "p1-cio", attempt=2, resolved_output="phase1/cio_allocation.r2.json")
    plan = portfolio.run(rd, final=False)
    assert sector_row(plan, "health_care")["target_pct"] == 6.5
    _fail_order(rd, "p1-cio", "phase1/cio_allocation.json")
    plan = portfolio.run(rd, final=False)
    assert sector_row(plan, "health_care")["target_pct"] is None
    assert any(f["code"] == "cio_targets_missing" for f in plan["flags"])


def test_no_manifest_orders_falls_back_to_r2_then_attempt1(run_dir: Path):
    common.write_json(run_dir / "phase1" / "cio_allocation.r2.json",
                      {"target_sector_weights": {**NEUTRAL_TARGETS, "health_care": 7.0}})
    assert portfolio.resolve_cio(run_dir, "allocation")[1].name == "cio_allocation.r2.json"
    plan = portfolio.run(run_dir, final=False)
    assert sector_row(plan, "health_care")["target_pct"] == 7.0


@pytest.mark.parametrize("label, rule, shares", [
    ("trim_conviction", "trim_conviction", 26),     # DESIGN §4.11: a third of 10% -> 3.3 pp / 125 = 26
    ("limit_breach", "trim_breach_sector", 12),     # 10 -> 9.5 (cap - buffer) -> 8.5 for the 20.5% limit
])
def test_run_trim_sizing_follows_the_aggregate_rule_label(run_dir: Path, label: str, rule: str, shares: int):
    """Integration (dry run): a conviction TRIM must be sized as a conviction trim even when the
    sector sits above its CIO target; the reason comes from the aggregate rule label."""
    common.write_json(run_dir / "phase1" / "cio_allocation.json",
                      {"target_sector_weights": {**NEUTRAL_TARGETS, "consumer_staples": 20.5}})
    common.write_json(run_dir / "phase2" / "consumer_staples_verdict.json",
                      _verdict("consumer_staples", 2, [_v("KO", "HOLD", 5.0), _v("PG", "TRIM", 4.0)]))
    common.write_json(run_dir / "phase2" / "aggregate.json", {"tickers": {"PG": {
        "kind": "holding", "asset_type": "stock", "sector": "consumer_staples", "agg_conviction": 4.0,
        "rule_action": "TRIM", "rule_label": label}}})
    pg = pos(portfolio.run(run_dir, final=False), "PG")
    assert pg["sizing_rule"] == rule
    assert pg["trade"]["side"] == "SELL" and pg["trade"]["shares"] == shares


def test_run_trim_from_a_deviation_keeps_the_breach_fallback(run_dir: Path):
    """A TRIM the rules did not produce (sector-head deviation) has no rule label to follow."""
    common.write_json(run_dir / "phase1" / "cio_allocation.json",
                      {"target_sector_weights": {**NEUTRAL_TARGETS, "consumer_staples": 20.5}})
    common.write_json(run_dir / "phase2" / "consumer_staples_verdict.json",
                      _verdict("consumer_staples", 2, [_v("KO", "HOLD", 5.0),
                                                       _v("PG", "TRIM", 5.0, rule_action="HOLD")]))
    common.write_json(run_dir / "phase2" / "aggregate.json", {"tickers": {"PG": {
        "kind": "holding", "asset_type": "stock", "sector": "consumer_staples", "agg_conviction": 5.0,
        "rule_action": "HOLD", "rule_label": "hold"}}})
    assert pos(portfolio.run(run_dir, final=False), "PG")["sizing_rule"] == "trim_breach_sector"


# --------------------------------------------------------------------------- ILS cost basis (Tel Aviv holdings)
def _ils_case(*, cost_ils: float | None = 60.0, native_ils: float | None = 75.0, cb_currency: str = "ILS"):
    prices = make_prices()
    if native_ils is not None:
        prices["tickers"]["KO"]["native"] = {
            "quote_currency": "ILA", "currency": "ILS",
            "last_close": common.fact(native_ils, "ILS/share", "D:PX:KO", AS_OF, "price"),
            "usdils": 3.7, "usdils_as_of": AS_OF, "fx_usd_per_native": 0.01 / 3.7}
    pcfg = make_portfolio_cfg()
    for h in pcfg["holdings"]:
        if h["ticker"] == "KO":
            h["cost_basis"], h["cost_basis_currency"] = cost_ils, cb_currency
    return portfolio.build_plan(
        lookthrough=make_lookthrough(), prices=prices, actions=with_actions(KO={"action": "SELL", "conviction": 2.0}),
        target_sector_weights=NEUTRAL_TARGETS, profile=PROFILE, cfg=make_cfg(), portfolio_cfg=pcfg)


def test_tax_estimate_in_ils_for_a_tel_aviv_holding_with_ils_cost_basis():
    ko = pos(_ils_case(), "KO")
    shares = ko["trade"]["shares"]
    assert shares == 200
    tax = ko["tax"]
    assert tax["applicable"] is True
    assert tax["est_gain_ils"] == pytest.approx((75.0 - 60.0) * shares)
    assert tax["est_tax_ils"] == pytest.approx(0.25 * (75.0 - 60.0) * shares)
    assert tax["est_gain_usd"] == pytest.approx((75.0 - 60.0) * shares / 3.7, rel=1e-4)
    assert tax["est_tax_usd"] == pytest.approx(0.25 * (75.0 - 60.0) * shares / 3.7, rel=1e-4)
    assert "real gain" in tax["note"].lower() and "tax advisor" in tax["note"].lower()


def test_ils_cost_basis_loss_has_no_tax():
    tax = pos(_ils_case(cost_ils=90.0), "KO")["tax"]
    assert tax["applicable"] is False and tax["est_tax_ils"] is None
    assert tax["est_gain_ils"] == pytest.approx((75.0 - 90.0) * 200)


def test_ils_cost_basis_without_native_price_is_not_guessed():
    tax = pos(_ils_case(native_ils=None), "KO")["tax"]
    assert tax["applicable"] is False and tax["est_tax_usd"] is None
    assert "ILS" in tax["note"]


def test_other_cost_basis_currency_not_estimated():
    tax = pos(_ils_case(cb_currency="EUR"), "KO")["tax"]
    assert tax["applicable"] is False and "EUR" in tax["note"]


# =====================================================================================
# breach trims round UP; full exits are visible; weight_intent_pct; honest cap-check wording
# =====================================================================================


def plan_custom(actions: dict, *, weights: dict | None = None, prices_over: dict | None = None,
                shares_over: dict | None = None, profile: dict | None = None, cfg: dict | None = None,
                targets: dict | None = None, prices: dict | None = None) -> dict:
    """``plan_for`` with consistent weights / prices / share counts overridden per ticker."""
    lt = make_lookthrough(weights={**WEIGHTS, **(weights or {})})
    pcfg = make_portfolio_cfg()
    for h in pcfg["holdings"]:
        if h["ticker"] in (shares_over or {}):
            h["shares"] = shares_over[h["ticker"]]
    return portfolio.build_plan(
        lookthrough=lt, prices=prices or make_prices(extra=prices_over), actions=actions,
        target_sector_weights=NEUTRAL_TARGETS if targets is None else targets,
        profile={**PROFILE, **(profile or {})}, cfg=cfg or make_cfg(), portfolio_cfg=pcfg)


#: BRK-B as 7 shares x $1500 = 10.5% of 100k: the 1 pp breach is 0.67 of a share (live case: 0.81)
BRK_BREACH = dict(weights={"BRK-B": 10.5}, prices_over={"BRK-B": 1500.0}, shares_over={"BRK-B": 7})


def test_breach_trim_under_one_share_rounds_up_instead_of_leaving_the_breach():
    plan = plan_custom(with_actions(BRK_B={"action": "TRIM", "conviction": 4.0}), **BRK_BREACH)
    brk = pos(plan, "BRK-B")
    assert brk["sizing_rule"] == "trim_breach_position"
    assert brk["trade"]["side"] == "SELL" and brk["trade"]["shares"] == 1       # floor would give 0
    assert brk["dropped_reason"] is None
    assert brk["weight_tgt_pct"] == pytest.approx(9.0) and brk["weight_tgt_pct"] <= PROFILE["max_position_pct"]
    assert brk["full_exit"] is False
    # KO (12%) is only HOLD in this plan: the cap check still warns, and says why for KO only
    c = check(plan, "max_position_pct")
    assert c["status"] == "warn" and "KO 12.00%" in c["detail"] and "BRK-B" not in c["detail"]
    assert "no reducing verdict" in c["detail"]


def test_breach_trim_that_passes_the_check_when_nothing_else_is_over_cap():
    plan = plan_custom(with_actions(KO={"action": "TRIM", "conviction": 4.0},
                                    BRK_B={"action": "TRIM", "conviction": 4.0}), **BRK_BREACH)
    assert check(plan, "max_position_pct")["status"] == "pass"


def test_conviction_trims_and_buys_still_round_down():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}))    # 3.3 pp / 125 = 26.4
    assert pos(plan, "PG")["trade"]["shares"] == 26
    plan = plan_for(with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"}),
                    targets={**NEUTRAL_TARGETS, "health_care": 10.0}, extra_prices={"MRK": 70.0})
    assert pos(plan, "MRK")["trade"]["shares"] == 42                            # 3000 / 70 = 42.86 -> 42


def test_conviction_trim_of_a_position_above_its_cap_still_ends_under_the_cap():
    """A TRIM whose conviction sizing is clamped to the cap (no buffer) used to round down to 10.02%."""
    cfg = make_cfg(trim_fraction=0.15)                       # 12 * 0.85 = 10.2 -> clamped to the 10% cap
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 4.0, "trim_reason": "conviction"}), cfg=cfg)
    ko = pos(plan, "KO")
    assert ko["sizing_rule"] == "trim_conviction"
    assert ko["trade"]["shares"] == 34                       # 2 pp = 2000 / 60 = 33.3 -> 34 (33 would leave 10.02%)
    assert ko["weight_tgt_pct"] <= PROFILE["max_position_pct"]
    assert check(plan, "max_position_pct")["status"] == "pass"


def test_breach_trim_below_min_trade_is_dropped_with_reason_note_and_the_warning_stays():
    plan = plan_custom(with_actions(BRK_B={"action": "TRIM", "conviction": 4.0}), profile={"min_trade_usd": 2000},
                       **BRK_BREACH)
    brk = pos(plan, "BRK-B")
    assert brk["trade"]["side"] == "NONE" and brk["dropped_reason"] == "below_min_trade_usd"
    assert brk["trade"]["proposed_shares"] == 1 and brk["trade"]["proposed_value_usd"] == pytest.approx(1500.0)
    assert brk["weight_tgt_pct"] == pytest.approx(10.5)
    assert any("below the $2,000 minimum" in n and "breach remains" in n for n in brk["notes"])
    c = check(plan, "max_position_pct")
    assert c["status"] == "warn"
    # not "no reducing verdict": there IS a TRIM verdict, its trade was dropped, and the detail says why
    assert "BRK-B 10.50% (TRIM verdict, but the trade was dropped: below_min_trade_usd)" in c["detail"]
    assert "KO 12.00% (no reducing verdict)" in c["detail"]


def test_breach_trim_without_a_price_keeps_the_warning_with_the_drop_reason():
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 4.0}),
                    prices={"tickers": {}, "fx": {"usdils": None}})
    ko = pos(plan, "KO")
    assert ko["trade"]["side"] == "NONE" and ko["dropped_reason"] == "no_price"
    c = check(plan, "max_position_pct")
    assert c["status"] == "warn" and "KO 12.00% (TRIM verdict, but the trade was dropped: no_price)" in c["detail"]


def test_fractional_breach_trim_rounds_the_fraction_up():
    # no buffer: 10.5% -> the 10% cap is 0.5 pp = 500 USD = 0.333333.. shares of a 1500 stock;
    # plain rounding gives 0.333333 (499.9995 USD, leaving 10.0000005%); the breach trim rounds up
    plan = plan_custom(with_actions(BRK_B={"action": "TRIM", "conviction": 4.0}), cfg=make_cfg(breach_buffer_pp=0.0),
                       profile={"allow_fractional_shares": True}, **BRK_BREACH)
    brk = pos(plan, "BRK-B")
    assert brk["trade"]["shares"] == pytest.approx(0.333334, abs=1e-9)
    assert brk["trade"]["shares"] * 1500.0 >= 500.0
    # conviction trims keep nearest-6dp rounding in fractional mode
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), profile={"allow_fractional_shares": True})
    assert pos(plan, "PG")["trade"]["shares"] == pytest.approx(26.4, abs=1e-6)


def test_sector_breach_cuts_round_up_so_the_sector_actually_reaches_its_limit():
    targets = {**NEUTRAL_TARGETS, "consumer_staples": 18.0}
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 4.0},
                                 PG={"action": "TRIM", "conviction": 3.8}), targets=targets)
    ko, pg = pos(plan, "KO"), pos(plan, "PG")
    # KO 2.5 pp / 60 = 41.67 -> 42 shares; PG 0.5 + 1.0 pp = 1500 / 125 = 12 shares exactly
    assert ko["trade"]["shares"] == 42 and pg["trade"]["shares"] == 12
    staples = sector_row(plan, "consumer_staples")["post_lookthrough_pct"]
    assert staples <= 18.0 + 1e-9, staples                           # floor rounding gave 18.04


def test_trim_that_sells_every_share_is_visible_as_a_full_exit():
    """QCOM live case: a sector-limit cut sized the lowest-conviction TRIM to zero. Since
    ``sizing.sector_trim_max_fraction`` (default 0.5) that only happens when the config explicitly allows
    a whole-position cut (fraction 1.0): the full-exit marking and the TRIM-is-not-SELL note are unchanged."""
    targets = {**NEUTRAL_TARGETS, "consumer_staples": 8.0}
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 3.5}, PG={"action": "TRIM", "conviction": 4.4},
                                 JPM={"action": "SELL", "conviction": 2.0}), targets=targets,
                    cfg=make_cfg(sector_trim_max_fraction=1.0))
    ko, pg, jpm = pos(plan, "KO"), pos(plan, "PG"), pos(plan, "JPM")
    assert ko["action"] == "TRIM"                                    # the verdict is not rewritten
    assert ko["sizing_rule"] == "trim_breach_sector"
    assert ko["trade"]["side"] == "SELL" and ko["trade"]["shares"] == SHARES["KO"]
    assert ko["weight_tgt_pct"] == 0.0 and ko["dropped_reason"] is None
    assert ko["full_exit"] is True
    note = [n for n in ko["notes"] if n.startswith("TRIM sizing exits the whole position")]
    assert len(note) == 1 and "Consumer Staples sector limit/target requires it" in note[0]
    assert "verdict is TRIM, not SELL" in note[0]
    # a partial TRIM is not a full exit; a SELL is a full exit but needs no TRIM warning
    assert pg["trade"]["side"] == "SELL" and pg["full_exit"] is False
    assert not any("exits the whole position" in n for n in pg["notes"])
    assert jpm["full_exit"] is True and not any("TRIM sizing" in n for n in jpm["notes"])
    assert all(isinstance(p["full_exit"], bool) for p in plan["positions"])
    assert [p["ticker"] for p in plan["positions"] if p["full_exit"]] == ["KO", "JPM"]


def test_breach_round_up_to_the_only_share_is_a_full_exit_with_a_rounding_note():
    plan = plan_custom(with_actions(BRK_B={"action": "TRIM", "conviction": 4.0}),
                       weights={"BRK-B": 10.5}, prices_over={"BRK-B": 10500.0}, shares_over={"BRK-B": 1})
    brk = pos(plan, "BRK-B")
    assert brk["trade"]["side"] == "SELL" and brk["trade"]["shares"] == 1       # never more than held
    assert brk["full_exit"] is True and brk["weight_tgt_pct"] == 0.0
    assert any("whole-share rounding" in n and "exits the whole position" in n for n in brk["notes"])


def test_full_exit_plan_still_validates_against_the_schema(plan_validator: Draft202012Validator):
    targets = {**NEUTRAL_TARGETS, "consumer_staples": 8.0}
    plan = plan_for(with_actions(KO={"action": "TRIM", "conviction": 3.5}, PG={"action": "TRIM", "conviction": 4.4}),
                    targets=targets, cfg=make_cfg(sector_trim_max_fraction=1.0))
    assert pos(plan, "KO")["full_exit"] is True
    assert pos(plan, "KO")["exit_kind"] == "full" and pos(plan, "KO")["trade_pct_of_position"] == 1.0
    assert _errors(plan_validator, plan) == []


def test_weight_intent_is_the_sizing_target_even_when_the_trade_is_dropped():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), profile={"min_trade_usd": 5000})
    pg = pos(plan, "PG")
    assert pg["dropped_reason"] == "below_min_trade_usd"
    assert pg["weight_tgt_pct"] == pytest.approx(10.0)               # nothing traded: weight unchanged
    assert pg["weight_intent_pct"] == pytest.approx(6.7)             # what the conviction sizing asked for
    hold = pos(plan, "QQQ")
    assert hold["weight_intent_pct"] == pytest.approx(hold["weight_cur_pct"])
    sell = plan_for(with_actions(JPM={"action": "SELL", "conviction": 2.0}))
    assert pos(sell, "JPM")["weight_intent_pct"] == 0.0
    buy = plan_for(with_actions(MRK={"action": "BUY", "conviction": 8.0, "sector": "health_care"}),
                   targets={**NEUTRAL_TARGETS, "health_care": 10.0}, extra_prices={"MRK": 100.0})
    assert pos(buy, "MRK")["weight_intent_pct"] == pytest.approx(3.0)


def test_sector_residual_names_the_dropped_reducing_trade():
    lt = make_lookthrough(sector_weights={**LOOKTHROUGH_WEIGHTS, "financials": 37.0, "cash": 2.0})
    plan = portfolio.build_plan(
        lookthrough=lt, prices=make_prices(), actions=with_actions(JPM={"action": "TRIM", "conviction": 4.0}),
        target_sector_weights=NEUTRAL_TARGETS, profile={**PROFILE, "min_trade_usd": 50000}, cfg=make_cfg(),
        portfolio_cfg=make_portfolio_cfg())
    c = check(plan, "max_sector_pct")
    assert c["status"] == "warn" and "financials" in c["detail"]
    assert "reducing trade dropped: JPM TRIM: below_min_trade_usd" in c["detail"]


# ---- randomized plans -----------------------------------------------------------------------


def _fuzz_case(rng: Any) -> dict:
    total = 100_000.0
    sectors = ["consumer_staples", "financials", "industrials", "health_care", "energy"]
    holdings, prices, pcfg_holdings, actions = [], {}, [], {}
    used = 0.0
    for i in range(rng.randint(3, 8)):
        is_fund = i == 0 and rng.random() < 0.4
        t = "FUND" if is_fund else f"S{i}"
        price = round(rng.choice([8, 25, 60, 150, 400, 900, 2500]) * rng.uniform(0.8, 1.2), 2)
        want = rng.uniform(1, 45 if is_fund else 20)
        shares = max(1, int(want / 100 * total / price))
        weight = shares * price / total * 100
        if used + weight > 95:
            continue
        used += weight
        sector = "funds" if is_fund else rng.choice(sectors)
        split = {"information_technology": 60, "financials": 40} if is_fund else {sector: 100}
        holdings.append({"ticker": t, "asset_type": "fund" if is_fund else "stock", "sector": sector,
                         "sector_split": split,
                         "weight_pct": common.fact(weight, "pct", "D:CFG:portfolio", AS_OF, "other")})
        prices[t] = price
        pcfg_holdings.append({"ticker": t, "shares": shares, "cost_basis": None, "cost_basis_currency": "USD"})
        actions[t] = {"action": rng.choice(["HOLD", "TRIM", "TRIM", "TRIM", "SELL", "ADD"]),
                      "conviction": round(rng.uniform(1, 9), 1)}
        if actions[t]["action"] == "TRIM":
            actions[t]["trim_reason"] = rng.choice([None, "breach", "conviction"])
    lt = common.envelope("lookthrough", AS_OF, sources=[], holdings=holdings, sector_weights_pct={},
                         total_value_usd=common.fact(total, "USD", "D:CFG:portfolio", AS_OF, "other"))
    targets = {s: rng.uniform(5, 30) for s in sectors if rng.random() < 0.6}
    cfg = make_cfg(trim_fraction=rng.uniform(0.1, 0.5), breach_buffer_pp=rng.choice([0.0, 0.5, 1.0]))
    profile = {"min_trade_usd": rng.choice([0, 200, 1500]), "allow_fractional_shares": rng.random() < 0.3}
    return dict(lookthrough=lt, actions=actions, targets=targets, cfg=cfg, profile=profile,
                pcfg={"holdings": pcfg_holdings}, price_map=prices)


def _check_fuzz_plan(seed: int, case: dict) -> None:
    prices = make_prices()
    prices["tickers"] = {t: _px(t, v) for t, v in case["price_map"].items()}
    plan = portfolio.build_plan(
        lookthrough=case["lookthrough"], prices=prices, actions=case["actions"],
        target_sector_weights=case["targets"], profile={**PROFILE, **case["profile"]}, cfg=case["cfg"],
        portfolio_cfg=case["pcfg"])
    for p in plan["positions"]:
        cap = PROFILE["max_fund_position_pct"] if p["asset_type"] == "fund" else PROFILE["max_position_pct"]
        tr = p["trade"]
        if tr["side"] == "SELL":
            assert tr["shares"] <= p["shares_cur"] + 1e-9, (seed, p)         # never sells more than held
        assert p["weight_tgt_pct"] >= -1e-9, (seed, p)
        assert p["action"] in ("TRIM", "SELL") or tr["side"] != "SELL", (seed, p)   # only reducing verdicts sell
        assert isinstance(p["full_exit"], bool)
        assert p["exit_kind"] == ("full" if p["full_exit"] else "partial" if tr["side"] == "SELL" else "none"), (seed, p)
        pct = p["trade_pct_of_position"]
        assert pct is None or 0.0 <= pct <= 1.0, (seed, p)
        if tr["side"] == "SELL" and p["shares_cur"]:
            assert pct == pytest.approx(tr["shares"] / p["shares_cur"], abs=1e-6), (seed, p)
        reducing_dropped = p["action"] in ("TRIM", "SELL") and tr["side"] != "SELL"
        assert p["effective_action"] == ("HOLD" if reducing_dropped else p["action"]), (seed, p)
        assert (p["effective_action_reason"] is not None) == reducing_dropped, (seed, p)
        if p["asset_type"] == "stock" and p["action"] == "TRIM" and p["weight_cur_pct"] <= cap + 1e-3:
            assert not p["full_exit"], (seed, p)              # a TRIM verdict is no full exit through the sector rule
            if pct is not None:
                assert pct <= 0.5 + 1e-6, (seed, p)
        if p["full_exit"]:
            assert tr["side"] == "SELL" and p["weight_tgt_pct"] == 0.0 and tr["shares"] == p["shares_cur"], (seed, p)
        if p["action"] == "TRIM" and p["weight_cur_pct"] > cap + 1e-3:        # a hard-cap breach with a TRIM verdict
            if tr["side"] == "SELL":
                assert p["weight_tgt_pct"] <= cap + 1e-3, (seed, p["ticker"], p["weight_tgt_pct"])
            else:
                assert p["dropped_reason"] == "below_min_trade_usd", (seed, p["ticker"], p["dropped_reason"])
    for c in plan["constraint_checks"]:
        if c["check"] in ("no_short", "whole_shares", "funds_not_sold_for_buys", "min_trade_usd",
                          "max_position_pct", "max_fund_position_pct"):
            assert c["status"] != "fail", (seed, c)
        if c["check"] in ("max_position_pct", "max_fund_position_pct") and c["status"] == "warn":
            for p in plan["positions"]:
                if p["action"] in ("TRIM", "SELL") and p["dropped_reason"] and f"{p['ticker']} " in c["detail"]:
                    assert (f"{p['ticker']} {p['weight_tgt_pct']:.2f}% "
                            f"({p['action']} verdict, but the trade was dropped") in c["detail"], (seed, c)


def test_fuzz_breach_trims_end_under_the_cap_unless_min_trade_blocked_them():
    """400 random portfolios: a position over its cap with a TRIM verdict ends at or under the cap, or its
    trade was dropped for min_trade_usd (and the check says so); nothing sells more than held or goes short."""
    import random
    for seed in range(400):
        _check_fuzz_plan(seed, _fuzz_case(random.Random(seed)))


# =====================================================================================
# sector-limit cure by stocks is capped (sizing.sector_trim_max_fraction), residual breaches,
# effective_action, trade_pct_of_position / exit_kind
# =====================================================================================

#: a small IT-heavy world (total 100k USD): QQQ 20% + SPMO 15% hold 18 pp of IT through their splits,
#: PLTR 8% and QCOM 4% are the only IT stocks. IT look-through 30%.
IT_WORLD_EXTRA = {
    "PLTR": dict(price=200.0, shares=40, sector="information_technology", split={"information_technology": 100}),
    "QCOM": dict(price=100.0, shares=40, sector="information_technology", split={"information_technology": 100}),
    "CAT": dict(price=500.0, shares=2, sector="industrials", split={"industrials": 100}),
}


def world_plan(weights: dict[str, float], actions: dict, *, targets: dict | None = None,
               shares: dict | None = None, prices: dict | None = None, cfg: dict | None = None,
               profile: dict | None = None, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A plan over exactly ``weights`` (percent of a 100k USD portfolio). Share counts and prices come from
    the module tables plus IT_WORLD_EXTRA; ``shares`` / ``prices`` override per ticker."""
    for t, e in IT_WORLD_EXTRA.items():
        monkeypatch.setitem(PRICES, t, e["price"])
        monkeypatch.setitem(SHARES, t, e["shares"])
        monkeypatch.setitem(COST, t, None)
        monkeypatch.setitem(SECTOR_OF, t, e["sector"])
        monkeypatch.setitem(SPLITS, t, e["split"])
    monkeypatch.setitem(SHARES, "QQQ", 40)       # 40 x 500 = 20% of the 100k portfolio
    for t, v in (shares or {}).items():
        monkeypatch.setitem(SHARES, t, v)
    for t, v in (prices or {}).items():
        monkeypatch.setitem(PRICES, t, v)
    sw: dict[str, float] = {"cash": 100.0 - sum(weights.values())}
    for t, w in weights.items():
        for sec, f in SPLITS[t].items():
            sw[sec] = sw.get(sec, 0.0) + w * f / 100.0
    lt = make_lookthrough(weights=weights, sector_weights=sw)
    return portfolio.build_plan(
        lookthrough=lt, prices=make_prices(), actions=actions,
        target_sector_weights=targets or {}, profile={**PROFILE, **(profile or {})},
        cfg=cfg or make_cfg(), portfolio_cfg=make_portfolio_cfg())


IT_WEIGHTS = {"QQQ": 20.0, "SPMO": 15.0, "PLTR": 8.0, "QCOM": 4.0}
IT_TARGETS = {"information_technology": 20.0}     # limit 20: IT is 30 -> 10 pp over (tolerance 1)


def it_actions(**over: dict) -> dict:
    acts = {"QQQ": {"action": "HOLD", "conviction": 5.0}, "SPMO": {"action": "HOLD", "conviction": 5.0},
            "PLTR": {"action": "TRIM", "conviction": 5.0}, "QCOM": {"action": "TRIM", "conviction": 4.4}}
    acts.update(over)
    return acts


def test_sector_cure_never_exceeds_the_max_fraction_of_a_trim_stock(monkeypatch: pytest.MonkeyPatch):
    """PLTR / QCOM live case: both are TRIM verdicts and stay in the portfolio. The IT limit needs far more
    than the stocks can give, so each loses exactly half (boundary: fraction 0.5 reached, not exceeded)."""
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, monkeypatch=monkeypatch)
    qcom, pltr = pos(plan, "QCOM"), pos(plan, "PLTR")
    for p, sold in ((qcom, 20), (pltr, 20)):
        assert p["action"] == "TRIM" and p["sizing_rule"] == "trim_breach_sector"
        assert p["trade"]["side"] == "SELL" and p["trade"]["shares"] == sold
        assert p["full_exit"] is False and p["exit_kind"] == "partial"
        assert p["trade_pct_of_position"] == pytest.approx(0.5)
        assert p["effective_action"] == "TRIM" and p["effective_action_reason"] is None
        assert any("capped at 50%" in n for n in p["notes"])
        assert not any("exits the whole position" in n for n in p["notes"])
    assert qcom["weight_tgt_pct"] == pytest.approx(2.0) and pltr["weight_tgt_pct"] == pytest.approx(4.0)
    assert sector_row(plan, "information_technology")["post_lookthrough_pct"] == pytest.approx(24.0)


def test_lowest_conviction_is_still_cut_first_under_the_fraction_cap(monkeypatch: pytest.MonkeyPatch):
    # a small excess only reaches the lowest-conviction stock; the higher one is untouched
    targets = {"information_technology": 27.0}       # 30 -> needs 3 pp; QCOM (4.4) can give up to 2, PLTR the rest
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=targets, monkeypatch=monkeypatch)
    qcom, pltr = pos(plan, "QCOM"), pos(plan, "PLTR")
    assert qcom["weight_tgt_pct"] == pytest.approx(2.0)          # capped at half of 4%
    assert pltr["weight_tgt_pct"] == pytest.approx(7.0)          # only the 1 pp QCOM could not give
    assert plan["residual_breaches"] == []
    swapped = world_plan(IT_WEIGHTS, it_actions(PLTR={"action": "TRIM", "conviction": 3.0}), targets=targets,
                         monkeypatch=monkeypatch)
    assert pos(swapped, "PLTR")["weight_tgt_pct"] == pytest.approx(5.0)   # now PLTR is lowest and gives all 3 pp
    assert pos(swapped, "QCOM")["weight_tgt_pct"] == pytest.approx(4.0)


def test_sector_trim_max_fraction_is_configurable_and_validated(monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, cfg=make_cfg(sector_trim_max_fraction=0.25),
                      monkeypatch=monkeypatch)
    assert pos(plan, "QCOM")["trade"]["shares"] == 10 and pos(plan, "PLTR")["trade"]["shares"] == 10
    assert plan["sizing"]["sector_trim_max_fraction"] == 0.25
    assert world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS,
                      monkeypatch=monkeypatch)["sizing"]["sector_trim_max_fraction"] == 0.5     # default
    for bad in (0, -0.1, 1.5, "half"):
        with pytest.raises(common.ConfigError, match="sector_trim_max_fraction"):
            world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, cfg=make_cfg(sector_trim_max_fraction=bad),
                       monkeypatch=monkeypatch)


def test_fraction_one_allows_the_old_whole_position_cut(monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, cfg=make_cfg(sector_trim_max_fraction=1.0),
                      monkeypatch=monkeypatch)
    qcom, pltr = pos(plan, "QCOM"), pos(plan, "PLTR")
    assert qcom["full_exit"] is True and qcom["exit_kind"] == "full" and qcom["weight_tgt_pct"] == 0.0
    assert any("exits the whole position" in n for n in qcom["notes"])
    assert pltr["weight_tgt_pct"] == pytest.approx(2.0)              # the limit is reached with 6 pp of PLTR
    assert plan["residual_breaches"] == []


def test_whole_share_rounding_up_cannot_push_a_sector_cut_past_the_fraction(monkeypatch: pytest.MonkeyPatch):
    # QCOM: 3 shares x 1333.33 = 4%. The sector rule wants 2 pp = 1.5 shares -> ceil would sell 2 (67%);
    # the cap allows floor(0.5 x 3) = 1 share
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, shares={"QCOM": 3},
                      prices={"QCOM": 4000.0 / 3}, monkeypatch=monkeypatch)
    qcom = pos(plan, "QCOM")
    assert qcom["trade"]["side"] == "SELL" and qcom["trade"]["shares"] == 1
    assert qcom["trade_pct_of_position"] == pytest.approx(1 / 3, abs=1e-6) and qcom["exit_kind"] == "partial"
    # a single share cannot be cut at all by the sector rule (floor(0.5) = 0): the TRIM is not a full exit
    one = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, shares={"QCOM": 1}, prices={"QCOM": 4000.0},
                     monkeypatch=monkeypatch)
    q1 = pos(one, "QCOM")
    assert q1["trade"]["side"] == "NONE" and q1["dropped_reason"] == "rounds_to_zero_shares"
    assert q1["full_exit"] is False and q1["exit_kind"] == "none"
    assert q1["action"] == "TRIM" and q1["effective_action"] == "HOLD"


def test_sell_verdict_is_still_a_full_exit(monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(QCOM={"action": "SELL", "conviction": 2.0}), targets=IT_TARGETS,
                      monkeypatch=monkeypatch)
    qcom = pos(plan, "QCOM")
    assert qcom["sizing_rule"] == "sell_full_exit" and qcom["trade"]["shares"] == 40
    assert qcom["full_exit"] is True and qcom["exit_kind"] == "full" and qcom["trade_pct_of_position"] == 1.0
    assert qcom["effective_action"] == "SELL" and qcom["effective_action_reason"] is None
    # the sold 4 pp counts toward the sector; the TRIM stock then only gives what is still missing, up to half
    assert pos(plan, "PLTR")["trade"]["shares"] == 20


def test_position_cap_breach_is_still_cured_fully_even_beyond_the_fraction(monkeypatch: pytest.MonkeyPatch):
    # PLTR 24% (cap 10): the cure to 9.5% is a 60% cut > 50% and rounds UP; the fraction only limits the sector rule
    plan = world_plan({"PLTR": 24.0, "QCOM": 4.0}, it_actions(), targets={}, shares={"PLTR": 120},
                      monkeypatch=monkeypatch)
    pltr = pos(plan, "PLTR")
    assert pltr["sizing_rule"] == "trim_breach_position"
    assert pltr["trade"]["shares"] == 73                                    # 14.5 pp = 14500 / 200 = 72.5 -> 73
    assert pltr["weight_tgt_pct"] == pytest.approx(24 - 73 * 200 / 1000) and pltr["weight_tgt_pct"] <= 10
    assert pltr["trade_pct_of_position"] == pytest.approx(73 / 120, abs=1e-6) and pltr["exit_kind"] == "partial"
    assert check(plan, "max_position_pct")["status"] == "pass"


def test_brk_b_style_position_cap_and_sector_breach_together(monkeypatch: pytest.MonkeyPatch):
    # BRK-B 12% (cap 10) and JPM 8% are the whole financials sector (20%), limit 14%: BRK-B is cured to 9.5%
    # by the position rule, then the sector rule may take BRK-B down to 6% in total (half of 12%), not further
    w = {"BRK-B": 12.0, "JPM": 8.0}
    acts = {"BRK-B": {"action": "TRIM", "conviction": 4.0}, "JPM": {"action": "TRIM", "conviction": 5.0}}
    plan = world_plan(w, acts, targets={"financials": 14.0}, shares={"BRK-B": 24, "JPM": 40}, monkeypatch=monkeypatch)
    brk, jpm = pos(plan, "BRK-B"), pos(plan, "JPM")
    assert brk["trade"]["shares"] == 12 and brk["weight_tgt_pct"] == pytest.approx(6.0)
    assert brk["trade_pct_of_position"] == pytest.approx(0.5) and brk["exit_kind"] == "partial"
    assert jpm["trade"]["side"] == "NONE" and jpm["weight_tgt_pct"] == pytest.approx(8.0)   # 14% reached already
    assert sector_row(plan, "financials")["post_lookthrough_pct"] == pytest.approx(14.0)
    assert plan["residual_breaches"] == []
    # a tighter limit (8%): BRK-B is capped at 6%, JPM also at half (4%), 2 pp stay over -> residual breach
    tight = world_plan(w, acts, targets={"financials": 8.0}, shares={"BRK-B": 24, "JPM": 40}, monkeypatch=monkeypatch)
    assert pos(tight, "BRK-B")["weight_tgt_pct"] == pytest.approx(6.0)
    assert pos(tight, "JPM")["weight_tgt_pct"] == pytest.approx(4.0)
    rb = tight["residual_breaches"]
    assert [r["sector"] for r in rb] == ["financials"]
    assert rb[0]["post_pct"] == pytest.approx(10.0) and rb[0]["limit_pct"] == pytest.approx(8.0)
    assert rb[0]["excess_pp"] == pytest.approx(2.0) and rb[0]["stock_trim_pp"] == pytest.approx(10.0)
    assert rb[0]["fund_contributions"] == []                                  # no fund holds financials here


def test_residual_breaches_report_the_fund_contributions(monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, monkeypatch=monkeypatch)
    (rb,) = plan["residual_breaches"]
    assert rb["sector"] == "information_technology"
    assert rb["post_pct"] == pytest.approx(24.0) and rb["limit_pct"] == pytest.approx(20.0)
    assert rb["excess_pp"] == pytest.approx(4.0)
    assert rb["stock_trim_pp"] == pytest.approx(6.0)                          # QCOM 2 pp + PLTR 4 pp
    assert rb["fund_contributions"] == [
        {"ticker": "QQQ", "weight_pct": 20.0, "sector_pct_of_portfolio": 12.0},
        {"ticker": "SPMO", "weight_pct": 15.0, "sector_pct_of_portfolio": 6.0}]
    note = rb["note"]
    assert "fund overlap" in note and "new contributions" in note and "QQQ 12.0 pp" in note
    assert "funds are not sold" not in note
    assert note in plan["notes"]                                              # the human note is kept
    it = sector_row(plan, "information_technology")
    assert it["breach"] is False                                              # 24% < the 35% hard cap
    c = check(plan, "max_sector_pct")
    assert c["status"] == "pass"
    # hard-cap breach: the check warns and points to residual_breaches
    hard = world_plan(IT_WEIGHTS, it_actions(), targets={}, profile={"max_sector_pct": 22}, monkeypatch=monkeypatch)
    assert hard["residual_breaches"] and check(hard, "max_sector_pct")["status"] == "warn"
    assert "residual_breaches" in check(hard, "max_sector_pct")["detail"]


def test_no_residual_breaches_when_the_stock_trims_cure_the_sector(monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(), targets={"information_technology": 27.0}, monkeypatch=monkeypatch)
    assert plan["residual_breaches"] == []
    held = world_plan(IT_WEIGHTS, {t: {"action": "HOLD", "conviction": 5.0} for t in IT_WEIGHTS},
                      targets=IT_TARGETS, monkeypatch=monkeypatch)
    assert [r["sector"] for r in held["residual_breaches"]] == ["information_technology"]    # nothing to trim
    assert held["residual_breaches"][0]["stock_trim_pp"] == 0.0


def test_new_plan_fields_validate_against_the_schema(plan_validator: Draft202012Validator,
                                                      monkeypatch: pytest.MonkeyPatch):
    plan = world_plan(IT_WEIGHTS, it_actions(), targets=IT_TARGETS, monkeypatch=monkeypatch)
    assert _errors(plan_validator, plan) == []
    assert plan["residual_breaches"]
    bad = copy.deepcopy(plan)
    bad["positions"][0]["exit_kind"] = "most"
    bad["residual_breaches"][0]["fund_contributions"][0].pop("ticker")
    bad["positions"][1]["effective_action"] = "MAYBE"
    bad["positions"][2]["trade_pct_of_position"] = 1.5
    errs = "\n".join(_errors(plan_validator, bad))
    assert "exit_kind" in errs and "ticker" in errs and "effective_action" in errs and "trade_pct_of_position" in errs


def test_cat_like_sub_share_trim_is_effectively_a_hold(monkeypatch: pytest.MonkeyPatch):
    # CAT live case: 2 shares x $500 = 1% of the portfolio; the conviction trim (33%) is 0.66 of a share
    plan = world_plan({"CAT": 1.0}, {"CAT": {"action": "TRIM", "conviction": 4.0}}, monkeypatch=monkeypatch)
    cat = pos(plan, "CAT")
    assert cat["action"] == "TRIM"                                            # the verdict itself is untouched
    assert cat["trade"]["side"] == "NONE" and cat["dropped_reason"] == "rounds_to_zero_shares"
    assert cat["effective_action"] == "HOLD"
    assert cat["effective_action_reason"] == "trim of 0.66 share rounds to zero shares - no trade"
    assert cat["exit_kind"] == "none" and cat["trade_pct_of_position"] == 0.0 and cat["full_exit"] is False


def test_effective_action_for_each_dropped_trim_reason(monkeypatch: pytest.MonkeyPatch):
    below = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}), profile={"min_trade_usd": 5000})
    pg = pos(below, "PG")
    assert pg["dropped_reason"] == "below_min_trade_usd" and pg["action"] == "TRIM"
    assert pg["effective_action"] == "HOLD" and "below the minimum trade size - no trade" in pg["effective_action_reason"]
    assert "26 share(s)" in pg["effective_action_reason"]
    nop = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}),
                   prices={"tickers": {}, "fx": {"usdils": None}})
    assert pos(nop, "PG")["dropped_reason"] == "no_price" and pos(nop, "PG")["effective_action"] == "HOLD"
    assert "no price" in pos(nop, "PG")["effective_action_reason"]
    sell_nop = plan_for(with_actions(PG={"action": "SELL", "conviction": 2.0}),
                        prices={"tickers": {}, "fx": {"usdils": None}})
    assert pos(sell_nop, "PG")["full_exit"] is True and pos(sell_nop, "PG")["effective_action"] == "SELL"


def test_executable_positions_keep_their_action_as_effective_action():
    plan = plan_for(with_actions(PG={"action": "TRIM", "conviction": 4.0}, JPM={"action": "SELL", "conviction": 2.0},
                                 BRK_B={"action": "ADD", "conviction": 7.0}))
    for p in plan["positions"]:
        assert p["effective_action"] == p["action"], p["ticker"]
        assert p["effective_action_reason"] is None
    pg, jpm, ko = pos(plan, "PG"), pos(plan, "JPM"), pos(plan, "KO")
    assert (pg["exit_kind"], jpm["exit_kind"], ko["exit_kind"]) == ("partial", "full", "none")
    assert pg["trade_pct_of_position"] == pytest.approx(26 / 80, abs=1e-6)
    assert jpm["trade_pct_of_position"] == 1.0 and ko["trade_pct_of_position"] == 0.0


def test_trade_pct_of_position_is_null_without_a_share_count():
    lt = make_lookthrough()
    pcfg = {"holdings": [h for h in make_portfolio_cfg()["holdings"] if h["ticker"] != "PG"]}
    plan = portfolio.build_plan(lookthrough=lt, prices=make_prices(), actions=with_actions(PG={"action": "TRIM", "conviction": 4.0}),
                                target_sector_weights=NEUTRAL_TARGETS, profile=PROFILE, cfg=make_cfg(),
                                portfolio_cfg=pcfg)
    pg = pos(plan, "PG")
    assert pg["shares_cur"] is None and pg["trade_pct_of_position"] is None
