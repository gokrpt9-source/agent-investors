"""risk_reward.py (DESIGN 4.9, SPEC 10): normal, null inputs, price below invalidation, fv below price."""
from __future__ import annotations

import json

import pytest

from scripts import risk_reward as rrm


def test_normal_case() -> None:
    res = rrm.risk_reward(100.0, 130.0, 90.0)
    assert res == {"upside_pct": 30.0, "downside_pct": 10.0, "rr": 3.0, "null_reason": None}


def test_rr_rounds_half_up_with_decimal() -> None:
    # 20.05 / 10 = 2.005 exactly -> 2.01 half-up (binary-float round() would give 2.0)
    res = rrm.risk_reward(100, 120.05, 90)
    assert res["rr"] == 2.01
    assert res["upside_pct"] == 20.05
    # 1.994 -> 1.99 ; 1.995 -> 2.00
    assert rrm.risk_reward(100, 119.94, 90)["rr"] == 1.99
    assert rrm.risk_reward(100, 119.95, 90)["rr"] == 2.0


def test_round_dp_comes_from_config_and_can_be_overridden() -> None:
    assert rrm._round_dp({"risk_reward": {"round_dp": 3}}) == 3
    assert rrm._round_dp(None) == 2  # committee.yaml risk_reward.round_dp
    res = rrm.risk_reward(100, 120.05, 90, round_dp=1)
    assert res["rr"] == 2.0  # 2.005 -> 2.0 at 1 dp
    assert rrm.risk_reward(100, 133.333, 90, cfg={"risk_reward": {"round_dp": 3}})["rr"] == 3.333


@pytest.mark.parametrize("price,fv,inv", [
    (None, 130, 90), (100, None, 90), (100, 130, None), (None, None, None),
    (0, 130, 90), (-5, 130, 90), (100, 0, 90), (100, -1, 90), (100, 130, 0), (100, 130, -2),
    (float("nan"), 130, 90), ("abc", 130, 90),
])
def test_missing_inputs_give_null_rr(price, fv, inv) -> None:
    res = rrm.risk_reward(price, fv, inv)
    assert res["rr"] is None
    assert res["null_reason"] == "missing_input"


def test_partial_inputs_still_report_available_legs() -> None:
    res = rrm.risk_reward(100, 125, None)
    assert res["upside_pct"] == 25.0 and res["downside_pct"] is None and res["rr"] is None
    res = rrm.risk_reward(100, None, 95)
    assert res["upside_pct"] is None and res["downside_pct"] == 5.0
    res = rrm.risk_reward(None, 125, 95)
    assert res == {"upside_pct": None, "downside_pct": None, "rr": None, "null_reason": "missing_input"}


@pytest.mark.parametrize("inv", [105.0, 100.0])
def test_price_below_or_at_invalidation(inv) -> None:
    res = rrm.risk_reward(100.0, 130.0, inv)
    assert res["rr"] is None
    assert res["null_reason"] == "price_below_invalidation"
    assert res["upside_pct"] == 30.0


def test_fair_value_below_price_gives_negative_rr() -> None:
    res = rrm.risk_reward(100.0, 80.0, 90.0)
    assert res["upside_pct"] == -20.0
    assert res["downside_pct"] == 10.0
    assert res["rr"] == -2.0
    assert res["null_reason"] is None


def test_round_half_up_helpers() -> None:
    assert rrm.round_half_up(6.95, 1) == 7.0
    assert rrm.round_half_up(6.94, 1) == 6.9
    assert rrm.round_half_up(4.45, 1) == 4.5
    assert rrm.round_half_up(2.675, 2) == 2.68  # float repr '2.675' rounds up
    assert rrm.round_half_up(-0.04, 1) == 0.0
    assert rrm.round_half_up(None, 1) is None
    assert rrm.to_decimal(True) is None
    assert rrm.to_decimal(float("inf")) is None


def test_cli_prints_json(capsys) -> None:
    assert rrm.main(["--price", "100", "--fv", "130", "--inv", "90"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rr"] == 3.0
    assert rrm.main(["--price", "100", "--fv", "null", "--inv", "90"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rr"] is None and out["null_reason"] == "missing_input"


# --------------------------------------------------------------------------- stop_too_tight
def test_absurd_ratio_from_a_microscopic_stop_is_null() -> None:
    # regression: risk_reward(100, 130, 99.999999) used to be 30,000,000
    res = rrm.risk_reward(100, 130, 99.999999)
    assert res["rr"] is None and res["null_reason"] == "stop_too_tight"
    assert res["upside_pct"] == 30.0 and res["downside_pct"] == 0.0     # both legs still reported


def test_live_pltr_like_tight_stop_below_the_floor() -> None:
    # stop 0.9% away (an ordinary 1.86% stop is above the 1.0% floor and keeps its rr)
    res = rrm.risk_reward(100.0, 130.0, 99.1)
    assert res["rr"] is None and res["null_reason"] == "stop_too_tight" and res["downside_pct"] == 0.9
    assert rrm.risk_reward(100.0, 130.0, 98.14)["rr"] == 16.13


def test_min_downside_boundary_is_inclusive() -> None:
    # exactly 1.0% below the price -> rr is computed; the exact (unrounded) downside is compared
    at = rrm.risk_reward(100, 130, 99)
    assert at == {"upside_pct": 30.0, "downside_pct": 1.0, "rr": 30.0, "null_reason": None}
    just_below = rrm.risk_reward(100, 130, 99.01)                      # 0.99% -> null
    assert just_below["rr"] is None and just_below["null_reason"] == "stop_too_tight"
    assert just_below["downside_pct"] == 0.99
    assert rrm.risk_reward(100, 130, 98.99)["rr"] == pytest.approx(29.7)  # 1.01% -> rr
    # rounding must not decide: 0.9996% displays as 1.0 (2 dp) but is below the floor
    rounded_up = rrm.risk_reward(100, 130, 99.0004)
    assert rounded_up["downside_pct"] == 1.0 and rounded_up["rr"] is None
    assert rounded_up["null_reason"] == "stop_too_tight"


def test_min_downside_is_scale_free_and_decimal_exact() -> None:
    # 1% of an awkward price: Decimal arithmetic, not float noise, decides the boundary
    assert rrm.risk_reward(69.85, 90, 69.1515)["null_reason"] is None        # exactly 1% below
    assert rrm.risk_reward(69.85, 90, 69.15151)["null_reason"] == "stop_too_tight"
    assert rrm.risk_reward(3, 4, 2.97)["null_reason"] is None                # exactly 1%
    assert rrm.risk_reward(3, 4, 2.971)["null_reason"] == "stop_too_tight"


def test_min_downside_comes_from_config_and_can_be_overridden() -> None:
    cfg = {"risk_reward": {"round_dp": 2, "min_downside_pct": 5.0}}
    assert rrm.resolve_min_downside_pct(cfg) == 5
    assert rrm.risk_reward(100, 130, 96, cfg=cfg)["null_reason"] == "stop_too_tight"       # 4% < 5%
    assert rrm.risk_reward(100, 130, 95, cfg=cfg)["rr"] == 6.0                             # exactly 5%
    # the risk_reward section alone is accepted too
    assert rrm.resolve_min_downside_pct(cfg["risk_reward"]) == 5
    # explicit argument wins over the config; 0 disables the guard
    assert rrm.risk_reward(100, 130, 96, cfg=cfg, min_downside_pct=3)["rr"] == 7.5
    assert rrm.risk_reward(100, 130, 99.999999, min_downside_pct=0)["rr"] == 30_000_000.0
    assert rrm.resolve_min_downside_pct({"risk_reward": {"min_downside_pct": 0}}) == 0


def test_min_downside_default_when_the_key_is_absent_or_invalid() -> None:
    assert rrm.resolve_min_downside_pct({}) == 1
    assert rrm.resolve_min_downside_pct({"risk_reward": {"round_dp": 2}}) == 1
    assert rrm.resolve_min_downside_pct({"risk_reward": {"min_downside_pct": -3}}) == 1
    assert rrm.resolve_min_downside_pct({"risk_reward": {"min_downside_pct": "x"}}) == 1
    assert rrm.risk_reward(100, 130, 99.5, cfg={"risk_reward": {"round_dp": 2}})["null_reason"] == "stop_too_tight"
    assert rrm.resolve_min_downside_pct(None) == 1          # committee.yaml ships 1.0


def test_committee_yaml_declares_min_downside_pct() -> None:
    from scripts import common
    cfg = common.committee_cfg()
    assert cfg["risk_reward"]["min_downside_pct"] == 1.0
    assert common.validate_config("committee", cfg) == []


def test_other_null_reasons_take_precedence_over_stop_too_tight() -> None:
    assert rrm.risk_reward(100, 130, 100.0)["null_reason"] == "price_below_invalidation"
    assert rrm.risk_reward(100, 130, 100.5)["null_reason"] == "price_below_invalidation"
    assert rrm.risk_reward(100, None, 99.999)["null_reason"] == "missing_input"
    assert rrm.risk_reward(100, 130, None)["null_reason"] == "missing_input"


def test_negative_rr_with_a_reasonable_stop_is_unchanged() -> None:
    res = rrm.risk_reward(100.0, 80.0, 98.0)          # 2% stop, fair value below price
    assert res["rr"] == -10.0 and res["null_reason"] is None


def test_cli_min_downside_flag(capsys) -> None:
    assert rrm.main(["--price", "100", "--fv", "130", "--inv", "99.5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rr"] is None and out["null_reason"] == "stop_too_tight" and out["downside_pct"] == 0.5
    assert rrm.main(["--price", "100", "--fv", "130", "--inv", "99.5", "--min-downside-pct", "0.5"]) == 0
    assert json.loads(capsys.readouterr().out)["rr"] == 60.0
