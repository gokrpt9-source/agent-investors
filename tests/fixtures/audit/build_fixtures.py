"""Regenerates the audit fixtures next to this file (run: ``python tests/fixtures/audit/build_fixtures.py``).

* ``clean/``   - a tiny, internally consistent committee run (AAA stock, BBB stock, FND fund, candidate CCC):
                 the audit finds nothing on it.
* ``flawed/``  - the same run with a known set of defects (one per check) so the audit has something to find.
* ``live_portfolio_same.yaml`` / ``live_portfolio_changed.yaml`` - the "live" config/portfolio.yaml to compare
                 with (identical to the run's snapshot / AAA sold down to 6 shares).

The fixtures are plain JSON / YAML / Markdown so tests can also edit a copy in a temp dir.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_ID = "2026-10-02_0900"
AS_OF = "2026-10-02"
PX_DATE = "2026-10-01"


def put(base: Path, rel: str, data) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8", newline="\n")
    else:
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def fact(v, unit="USD/share", kind="price", as_of=PX_DATE):
    return {"value": v, "unit": unit, "source_id": "D:X", "as_of": as_of, "kind": kind, "na_reason": None}


PORTFOLIO_YAML = """as_of: "2026-10-01"
total_value_usd: null
cash_usd: 400
cash_pct: null
holdings:
  - ticker: "AAA"
    asset_type: stock
    shares: 10
    cost_basis: 80
    currency: USD
  - ticker: "BBB"
    asset_type: stock
    shares: 3
    cost_basis: 190
    currency: USD
  - ticker: "FND"
    asset_type: fund
    shares: 80
    cost_basis: 90
    currency: USD
"""
PROFILE_YAML = """max_position_pct: 10
max_fund_position_pct: 90
max_sector_pct: 35
min_trade_usd: 200
target_cash_pct: 5
"""
COMMITTEE_YAML = """version: 1
actions:
  min_evidence: 5
"""


def agg_entry(kind, asset_type, sector, sector_class, price, fv, specialists, da, conv, ev, rule_action, rr=None,
              unresolved=()):
    return {
        "kind": kind, "asset_type": asset_type, "sector": sector, "sector_class": sector_class,
        "inputs": {"price": fact(price), "position_pct": 10.0, "cap_pct": 10.0},
        "specialists": specialists, "conviction_mean": conv, "da": da,
        "agg_conviction": conv, "agg_evidence": ev,
        "rr": {"price": price, "fv_base": fv, "invalidation": price * 0.9, "rr": rr, "null_reason": None if fv else "not_applicable"},
        "flags": {"all": list(unresolved), "unresolved": list(unresolved)},
        "rule_action": rule_action, "rule_label": "limit_breach", "rule_trace": [], "notes": [],
    }


def spec(role, stance, ev=8.0, status="ok"):
    return {"role": role, "path": f"phase2/reports/{role}.json", "status": status, "stance": stance,
            "conviction": 5.0, "effective_conviction": 5.0, "evidence": ev, "in_mean": True, "invalidation_price": None}


DA_OK = {"path": "x", "status": "ok", "verdict": "survives", "adj": 0.0, "evidence": 7.0}
DA_NONE = {"path": None, "status": "not_run", "verdict": None, "adj": 0, "evidence": None}


def fundamental_report(ticker, fv, price):
    return {
        "schema_version": "1.0", "agent_role": "fundamental", "ticker": ticker, "as_of": AS_OF,
        "thesis": f"{ticker} is a steady business trading close to the fair value from the grid.",
        "stance": "neutral", "risks": [{"point": "Growth could slow if demand weakens.", "severity": "medium"}],
        "key_figures": [{"name": "Price", "value": price, "unit": "USD/share", "source_id": "D:X", "as_of": PX_DATE}],
        "missing_data": [],
        "extensions": {"valuation": {"fair_value_low": fv * 0.8, "fair_value_base": fv, "fair_value_high": fv * 1.2}},
    }


def base_run() -> dict[str, object]:
    """relative path -> JSON-able / text content of the clean run."""
    f: dict[str, object] = {}
    f["inputs/portfolio.yaml"] = PORTFOLIO_YAML
    f["inputs/investor_profile.yaml"] = PROFILE_YAML
    f["inputs/committee.yaml"] = COMMITTEE_YAML
    sha = hashlib.sha256(PORTFOLIO_YAML.encode("utf-8")).hexdigest()
    f["manifest.json"] = {
        "run_id": RUN_ID, "created_at": "2026-10-02T08:00:00+00:00", "as_of": AS_OF, "status": "done",
        "config_sha256": {"portfolio.yaml": sha},
        "holdings": [{"ticker": t, "asset_type": a, "sector": s, "position_pct": p}
                     for t, a, s, p in (("AAA", "stock", "information_technology", 10.0),
                                         ("BBB", "stock", "health_care", 6.0), ("FND", "fund", "funds", 80.0))],
    }
    f["phase1/lookthrough.json"] = {
        "schema": "lookthrough/v1", "as_of": AS_OF,
        "holdings": [
            {"ticker": "AAA", "asset_type": "stock", "contribution_pct": {"information_technology": 10.0}},
            {"ticker": "BBB", "asset_type": "stock", "contribution_pct": {"health_care": 6.0}},
            {"ticker": "FND", "asset_type": "fund", "contribution_pct": {"information_technology": 24.0,
                                                                          "health_care": 8.0}},
        ],
    }
    # ---- phase 2 aggregate, verdicts, reports, data
    f["phase2/aggregate.json"] = {"schema": "aggregate/v1", "as_of": AS_OF, "tickers": {
        "AAA": agg_entry("holding", "stock", "information_technology", "default", 100.0, 90.0,
                         [spec("fundamental", "bearish"), spec("technical", "neutral"), spec("osint", "neutral"),
                          spec("operator_ceo", "neutral")], DA_OK, 4.4, 8.0, "TRIM", rr=-1.0),
        "BBB": agg_entry("holding", "stock", "health_care", "default", 200.0, 210.0,
                         [spec("fundamental", "neutral"), spec("technical", "neutral"), spec("osint", "neutral"),
                          spec("operator_ceo", "neutral")], DA_OK, 5.2, 8.0, "HOLD", rr=1.5),
        "FND": agg_entry("holding", "fund", "funds", "fund", 100.0, None,
                         [spec("etf", "neutral"), spec("technical", "neutral")], DA_NONE, 5.0, 8.0, "HOLD"),
    }}
    f["phase2/information_technology_verdict.json"] = {
        "schema_version": "1.0", "agent_role": "sector_head", "phase": 2, "sector": "information_technology",
        "summary": "Technology is above the 32% limit, so AAA is trimmed while the fund is left alone.",
        "verdicts": [{
            "ticker": "AAA", "kind": "holding", "action": "TRIM", "rule_action": "TRIM",
            "rationale": ("The sector is above its 32% limit, which drives the trim. The business is sound and the "
                          "price is slightly above fair value, so about a third of the position is sold and the "
                          "rest stays in the portfolio."),
        }],
    }
    f["phase2/health_care_verdict.json"] = {
        "schema_version": "1.0", "agent_role": "sector_head", "phase": 2, "sector": "health_care",
        "summary": "BBB is fairly valued and no limit is breached, so it is held.",
        "verdicts": [{"ticker": "BBB", "kind": "holding", "action": "HOLD", "rule_action": "HOLD",
                      "rationale": "Fairly valued, no limit breached, nothing in the news changes the picture."}],
    }
    f["phase2/funds_verdict.json"] = {
        "schema_version": "1.0", "agent_role": "sector_head", "phase": 2, "sector": "funds",
        "summary": "The fund is held.",
        "verdicts": [{"ticker": "FND", "kind": "holding", "action": "HOLD", "rule_action": "HOLD",
                      "rationale": "Neutral lenses, no cap breached, cheap and diversified."}],
    }
    for t, fv, px in (("AAA", 90.0, 100.0), ("BBB", 210.0, 200.0)):
        f[f"phase2/reports/fundamental_{t}.json"] = fundamental_report(t, fv, px)
        f[f"phase2/data/valuation_{t}.json"] = {
            "schema": "valuation/v1", "ticker": t, "as_of": AS_OF, "price": fact(px), "flags": [],
            "methods_available": ["dcf_nopat", "pe"], "cells": [{"id": "PE-mid", "fv_per_share": fv, "na_reason": None}],
            "reverse_dcf": {"implied_growth_pct": fact(8.0, "pct", "other")},
        }
        f[f"phase2/data/fundamentals_{t}.json"] = {"schema": "fundamentals/v1", "ticker": t, "flags": []}
    # ---- phase 3 candidate
    f["phase3/aggregate.json"] = {"schema": "aggregate/v1", "as_of": AS_OF, "tickers": {
        "CCC": agg_entry("candidate", "stock", "health_care", "default", 50.0, 55.0,
                         [spec("fundamental", "neutral"), spec("technical", "neutral")], DA_OK, 5.0, 8.0, "PASS", rr=0.5),
    }}
    f["phase3/health_care_verdict.json"] = {
        "schema_version": "1.0", "agent_role": "sector_head", "phase": 3, "sector": "health_care",
        "summary": "CCC does not clear the buy bar.",
        "verdicts": [{"ticker": "CCC", "kind": "candidate", "action": "PASS", "rule_action": "PASS",
                      "rationale": "Reward to risk is too low for a new position; revisit if the price falls."}],
    }
    f["phase3/data/valuation_CCC.json"] = {"schema": "valuation/v1", "ticker": "CCC", "flags": [],
                                           "methods_available": ["pe"], "cells": [{"id": "PE-mid", "na_reason": None}]}
    f["phase3/reports/fundamental_CCC.json"] = fundamental_report("CCC", 55.0, 50.0)
    # ---- phase 4
    f["phase4/cio_final.json"] = {
        "schema_version": "1.0", "run_id": RUN_ID, "agent_role": "cio", "mode": "reconciliation", "as_of": AS_OF,
        "executive_summary": [
            "Technology look-through is 34% against the 35% profile cap; trimming AAA brings it to 31%.",
            "No candidate earned a BUY, so the proceeds stay in cash for now.",
        ],
        "reconciliation": [{
            "topic": "AAA trim", "phase1_position": "Phase 1 wanted technology lower.",
            "findings": "AAA TRIM sells 3 shares of the 10 held; the rest of the position stays.",
            "resolution": "Accept the trim as sized; no override.",
        }],
        "final_target_sector_weights": {"information_technology": 31.0},
        "final_actions": [
            {"ticker": "AAA", "action": "TRIM", "source_verdict_action": "TRIM", "override": False},
            {"ticker": "BBB", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "FND", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "CCC", "action": "PASS", "source_verdict_action": "PASS", "override": False},
        ],
        "constraint_overrides": [], "top_risks": ["Fund overlap keeps technology above half of the portfolio."],
        "dissent_summary": "No dissent worth recording.", "conviction": 6, "conviction_reason": "Verdicts agree.",
    }
    pos = lambda t, a, sec, act, px, cur, sell, w0, w1, **kw: {  # noqa: E731
        "ticker": t, "asset_type": a, "sector": sec, "kind": "holding", "action": act, "action_source": "cio_final",
        "price": fact(px), "shares_cur": cur, "weight_cur_pct": w0, "weight_tgt_pct": w1, "weight_intent_pct": w1,
        "trade": {"side": "SELL" if sell else "NONE", "shares": sell, "value_usd": sell * px},
        "full_exit": bool(sell and sell >= cur), "dropped_reason": None, "notes": [], **kw}
    f["phase4/portfolio_plan.json"] = {
        "schema": "portfolio_plan/v1", "as_of": AS_OF,
        "total_value_usd": fact(10000.0, "USD", "price"),
        "cash": {"current_pct": 4.0, "target_pct": 5.0, "post_pct": 7.0, "post_usd": 700.0},
        "positions": [
            pos("AAA", "stock", "information_technology", "TRIM", 100.0, 10, 3, 10.0, 7.0,
                notes=["reduced 3.0 pp for the information_technology sector limit 32%"]),
            pos("BBB", "stock", "health_care", "HOLD", 200.0, 3, 0, 6.0, 6.0),
            pos("FND", "fund", "funds", "HOLD", 100.0, 80, 0, 80.0, 80.0),
        ],
        "sectors": [
            {"sector": "information_technology", "cur_lookthrough_pct": 34.0, "post_lookthrough_pct": 31.0,
             "target_pct": 31.0, "max_pct": 35.0, "gap_pp": 0.0, "breach": False},
            {"sector": "health_care", "cur_lookthrough_pct": 14.0, "post_lookthrough_pct": 14.0,
             "target_pct": 14.0, "max_pct": 35.0, "gap_pp": 0.0, "breach": False},
            {"sector": "cash", "cur_lookthrough_pct": 4.0, "post_lookthrough_pct": 7.0, "target_pct": 7.0,
             "max_pct": None, "gap_pp": 0.0, "breach": False},
        ],
        "constraint_checks": [{"check": "max_sector_pct", "status": "pass", "detail": "all sectors <= 35%"}],
        "notes": ["Information Technology ends at 31%, inside its 32% limit."],
        "sizing": {"trim_fraction": 0.33},
    }
    f["phase4/memo.md"] = MEMO_MD
    return f


MEMO_MD = """# Investment Committee Memo

## 2. Executive Summary

- Technology look-through is 34% against the 35% profile cap; trimming AAA brings it to 31%.

## 5. Holding Verdicts

| Ticker | Sector | Action | Rule | Conviction | Evidence | R/R | Fair value (base) | Stop | Why |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| AAA | Information Technology | TRIM | TRIM | 4.4 | 8.0 | n/m (FV below price) | $90.00 | $90.00 | Trim, not sell: the sector is 34.00% of the portfolio once the funds' holdings are counted, above the 32.00% sector limit. Conviction 4.4/10 is above the sell level (3.0), so about a third of the position is sold and the rest stays in the portfolio. |
| BBB | Health Care | HOLD | HOLD | 5.2 | 8.0 | 1.5 | $210.00 | $180.00 | Hold: conviction 5.2/10 is neither strong enough to add nor weak enough to trim, and no limit is breached. |
| FND | Funds | HOLD | HOLD | 5.0 | 8.0 | n/a | n/a | n/a | Hold: conviction 5.0/10 is neither strong enough to add nor weak enough to trim, and no limit is breached. |

## 6. New Proposals

No new position qualified.

## 7. Proposed Rebalancing Plan

| Ticker | Side | Shares | Action |
| --- | --- | ---: | --- |
| AAA | SELL | 3 | TRIM |
"""


def write_run(base: Path, files: dict) -> None:
    if base.exists():
        shutil.rmtree(base)
    for rel, content in files.items():
        put(base, rel, content)


def flawed_files() -> dict:
    f = copy.deepcopy(base_run())
    plan = f["phase4/portfolio_plan.json"]
    # trim_became_full_exit: AAA TRIM sized as a full exit
    aaa = plan["positions"][0]
    aaa["trade"].update({"shares": 10, "value_usd": 1000.0})
    aaa["full_exit"] = True
    aaa["weight_tgt_pct"] = 0.0
    # action_vs_trade: BBB TRIM with no executable trade
    f["phase4/cio_final.json"]["final_actions"][1]["action"] = "TRIM"
    plan["positions"][1]["action"] = "TRIM"
    plan["positions"][1]["dropped_reason"] = "rounds_to_zero_shares"
    plan["positions"][1]["trade"]["proposed_shares"] = 0.4
    # text_vs_plan_contradiction: verdict says "stays in the portfolio"; CIO says 3 shares (plan now sells 10)
    # (verdict rationale already says it; the CIO line says "sells 3 shares")
    # valuation_outlier: AAA fair value 20 (ratio 0.2), BBB 100 (0.5)
    f["phase2/aggregate.json"]["tickers"]["AAA"]["rr"]["fv_base"] = 20.0
    f["phase2/aggregate.json"]["tickers"]["BBB"]["rr"]["fv_base"] = 100.0
    # captive flag on BBB valuation
    f["phase2/data/valuation_BBB.json"]["flags"] = [
        {"code": "possible_captive_finance", "severity": "low", "detail": "net debt / market cap 0.4"}]
    # residual breach: IT stays above the 35% cap
    plan["sectors"][0].update({"post_lookthrough_pct": 36.5, "breach": True})
    # limit inconsistency: the memo's Why says 33.00% while the plan says 32%
    f["phase4/memo.md"] = MEMO_MD.replace("32.00% sector limit", "33.00% sector limit").replace(
        "the rest stays in the portfolio. |", "R4.trim fired so the rest stays in the portfolio. |")
    # weak why: BBB
    f["phase4/memo.md"] = f["phase4/memo.md"].replace(
        "Hold: conviction 5.2/10 is neither strong enough to add nor weak enough to trim, and no limit is breached.",
        "Hold, rule fired.")
    # unresolved flag + failed report + low evidence on BBB
    agg_b = f["phase2/aggregate.json"]["tickers"]["BBB"]
    agg_b["flags"]["unresolved"] = [{"flag_id": "osint:F1", "role": "osint", "id": "F1", "severity": "high",
                                     "flag": "Regulator opened an investigation.", "must_address": True}]
    agg_b["specialists"][3]["status"] = "failed"
    agg_b["specialists"][2]["evidence"] = 3.0
    # thin review: AAA loses its Devil's Advocate; BBB SELL while lenses bullish is covered by a mutation in tests
    f["phase2/aggregate.json"]["tickers"]["AAA"]["da"] = dict(DA_NONE)
    # pending event on BBB
    f["phase2/reports/fundamental_BBB.json"]["risks"].append(
        {"point": "Third-quarter results are due October 3 and could move the stock.", "severity": "medium"})
    # cash drag
    plan["cash"]["post_pct"] = 18.0
    plan["cash"]["post_usd"] = 1800.0
    return f


def main() -> None:
    write_run(HERE / "clean", base_run())
    write_run(HERE / "flawed", flawed_files())
    put(HERE, "live_portfolio_same.yaml", PORTFOLIO_YAML)
    put(HERE, "live_portfolio_changed.yaml", PORTFOLIO_YAML.replace("shares: 10", "shares: 6", 1))
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
