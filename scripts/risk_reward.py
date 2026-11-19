"""Risk/reward arithmetic (DESIGN §4.9, SPEC §3).

    upside_pct   = (fair_value_base - price) / price * 100
    downside_pct = (price - invalidation_price) / price * 100
    rr           = upside / downside, rounded (Decimal ROUND_HALF_UP) to ``risk_reward.round_dp``

Any None / non-positive input -> ``rr`` null with ``null_reason: "missing_input"``;
invalidation >= price -> ``rr`` null with ``null_reason: "price_below_invalidation"``;
a stop closer to the price than ``risk_reward.min_downside_pct`` (default 1.0, percent) ->
``rr`` null with ``null_reason: "stop_too_tight"`` (``upside_pct`` / ``downside_pct`` are still
reported): dividing by a near-zero downside yields absurd ratios (``risk_reward(100, 130,
99.999999)`` would be 30,000,000). The test is ``downside_pct < min_downside_pct`` on the exact
(unrounded) downside, so a stop exactly ``min_downside_pct`` away still produces an ``rr``.
``rr`` may be negative when the fair value is below the price.

All arithmetic is done in ``Decimal`` built from the shortest decimal representation of each
input, so ``2.005`` rounds to ``2.01`` (not the binary-float ``2.0``).

Also home of the shared rounding helpers (``to_decimal``, ``round_half_up``) used by
``evidence_score.py`` and ``aggregate.py``.

CLI::

    python scripts/risk_reward.py --price 100 --fv 130 --inv 90
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation  # noqa: E402
from typing import Any, Mapping  # noqa: E402

#: Fallback used only when committee.yaml has no ``risk_reward.round_dp``.
DEFAULT_ROUND_DP = 2
#: Fallback used only when committee.yaml has no ``risk_reward.min_downside_pct`` (percent).
DEFAULT_MIN_DOWNSIDE_PCT = 1.0

NULL_MISSING_INPUT = "missing_input"
NULL_PRICE_BELOW_INVALIDATION = "price_below_invalidation"
NULL_STOP_TOO_TIGHT = "stop_too_tight"


# =====================================================================================
# Decimal helpers (shared)
# =====================================================================================


def to_decimal(x: Any) -> Decimal | None:
    """Exact-as-written Decimal of ``x`` (floats via their shortest repr), None for
    None / bool / NaN / inf / non-numeric."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, Decimal):
        return x if x.is_finite() else None
    if isinstance(x, int):
        return Decimal(x)
    f = common.safe_float(x)
    if f is None:
        return None
    try:
        d = Decimal(repr(f))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def quantize_half_up(x: Any, dp: int) -> Decimal | None:
    """``x`` rounded half-up to ``dp`` decimal places, as a Decimal (None stays None)."""
    d = to_decimal(x)
    if d is None:
        return None
    return d.quantize(Decimal(1).scaleb(-int(dp)), rounding=ROUND_HALF_UP)


def round_half_up(x: Any, dp: int) -> float | None:
    """``x`` rounded half-up to ``dp`` decimal places, as a float (None stays None)."""
    q = quantize_half_up(x, dp)
    if q is None:
        return None
    f = float(q)
    return 0.0 if f == 0 else f  # no "-0.0" in JSON


def _round_dp(cfg: Mapping[str, Any] | None) -> int:
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    section = cfg.get("risk_reward") if isinstance(cfg, Mapping) else None
    if isinstance(section, Mapping) and section.get("round_dp") is not None:
        return int(section["round_dp"])
    if isinstance(cfg, Mapping) and cfg.get("round_dp") is not None:  # the section itself
        return int(cfg["round_dp"])
    return DEFAULT_ROUND_DP


def resolve_min_downside_pct(cfg: Mapping[str, Any] | None = None) -> Decimal:
    """``risk_reward.min_downside_pct`` (percent) from the whole committee config, the
    ``risk_reward`` section itself, or None (loads committee.yaml); falls back to
    ``DEFAULT_MIN_DOWNSIDE_PCT`` when absent / not a non-negative number. 0 disables the guard."""
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    section = cfg.get("risk_reward") if isinstance(cfg, Mapping) else None
    raw = None
    if isinstance(section, Mapping):
        raw = section.get("min_downside_pct")
    elif isinstance(cfg, Mapping):  # the section itself
        raw = cfg.get("min_downside_pct")
    d = to_decimal(raw)
    if d is None or d < 0:
        return Decimal(repr(DEFAULT_MIN_DOWNSIDE_PCT))
    return d


# =====================================================================================
# risk_reward
# =====================================================================================


def _positive(x: Any) -> Decimal | None:
    d = to_decimal(x)
    if d is None or d <= 0:
        return None
    return d


def risk_reward(price: Any, fair_value_base: Any, invalidation_price: Any, *,
                round_dp: int | None = None, cfg: Mapping[str, Any] | None = None,
                min_downside_pct: Any = None) -> dict:
    """Return ``{"upside_pct","downside_pct","rr","null_reason"}``.

    ``upside_pct`` / ``downside_pct`` are reported whenever their own inputs are usable (even
    when ``rr`` is null); all three numbers are rounded half-up to ``round_dp`` (default:
    committee.yaml ``risk_reward.round_dp``). ``rr`` is null with ``stop_too_tight`` when the
    exact downside is below ``min_downside_pct`` (default: committee.yaml
    ``risk_reward.min_downside_pct``, 1.0)."""
    dp = int(round_dp) if round_dp is not None else _round_dp(cfg)
    floor_pct = to_decimal(min_downside_pct)
    if floor_pct is None or floor_pct < 0:
        floor_pct = resolve_min_downside_pct(cfg)
    p = _positive(price)
    fv = _positive(fair_value_base)
    inv = _positive(invalidation_price)
    out: dict[str, Any] = {"upside_pct": None, "downside_pct": None, "rr": None, "null_reason": None}
    if p is None:
        out["null_reason"] = NULL_MISSING_INPUT
        return out
    hundred = Decimal(100)
    if fv is not None:
        out["upside_pct"] = round_half_up((fv - p) / p * hundred, dp)
    downside_exact: Decimal | None = None
    if inv is not None:
        downside_exact = (p - inv) * hundred / p
        out["downside_pct"] = round_half_up(downside_exact, dp)
    if fv is None or inv is None:
        out["null_reason"] = NULL_MISSING_INPUT
        return out
    if inv >= p:
        out["null_reason"] = NULL_PRICE_BELOW_INVALIDATION
        return out
    if downside_exact < floor_pct:
        out["null_reason"] = NULL_STOP_TOO_TIGHT
        return out
    out["rr"] = round_half_up((fv - p) / (p - inv), dp)
    return out


# =====================================================================================
# CLI
# =====================================================================================


def _cli_number(text: str) -> float | None:
    if text is None or str(text).strip().lower() in ("", "none", "null"):
        return None
    f = float(text)
    return f if math.isfinite(f) else None


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Risk/reward from price, base fair value and invalidation price.")
    parser.add_argument("--price", required=True, type=_cli_number, help="current price (USD/share)")
    parser.add_argument("--fv", required=True, type=_cli_number, help="base fair value (USD/share)")
    parser.add_argument("--inv", required=True, type=_cli_number, help="invalidation (stop) price (USD/share)")
    parser.add_argument("--round-dp", type=int, default=None, help="override risk_reward.round_dp")
    parser.add_argument("--min-downside-pct", type=float, default=None,
                        help="override risk_reward.min_downside_pct (stops closer than this give rr null)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    common.apply_common_args(args)
    result = risk_reward(args.price, args.fv, args.inv, round_dp=args.round_dp,
                         min_downside_pct=args.min_downside_pct)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
