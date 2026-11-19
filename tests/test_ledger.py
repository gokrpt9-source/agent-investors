"""ledger.py (DESIGN §4.12): append + dedupe, CSV round trip, scoring vs SPY / sector ETF, hit
logic, min-age exclusion, role x action summary, rows_from_run on a synthetic run dir. Offline."""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from conftest import REPO_ROOT
from scripts import common, ledger

AS_OF = "2026-10-02"
RUN_ID = "2026-10-02_1000"


def _row(**kw: Any) -> dict:
    base = {c: None for c in ledger.LEDGER_COLUMNS}
    base.update({"run_id": "2026-08-01_0900", "rec_date": "2026-08-01", "price_date": "2026-07-31",
                 "asset_type": "stock", "kind": "holding", "run_mode": "full"})
    base.update(kw)
    return base


# =====================================================================================
# append / CSV
# =====================================================================================


def test_columns_match_committed_header():
    header = (REPO_ROOT / "ledger" / "recommendations.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(ledger.LEDGER_COLUMNS)


def test_append_creates_header_and_dedupes(tmp_path: Path):
    path = tmp_path / "ledger" / "recommendations.csv"
    rows = [_row(ticker="KO", role="committee", action="HOLD"),
            _row(ticker="KO", role="fundamental", stance="neutral", conviction=6),
            _row(ticker="KO", role="committee", action="TRIM")]          # duplicate key in the batch
    assert ledger.append(rows, path) == 2
    assert ledger.append(rows, path) == 0                                 # already present
    assert ledger.append([_row(ticker="KO", role="committee", run_id="2026-09-01_0900")], path) == 1
    loaded = ledger.load_rows(path)
    assert len(loaded) == 3
    assert [r["action"] for r in loaded if r["role"] == "committee"][0] == "HOLD"  # first one kept
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(ledger.LEDGER_COLUMNS)
    assert text.count(",".join(ledger.LEDGER_COLUMNS)) == 1


def test_append_to_existing_header_only_file(tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    common.write_text(path, ",".join(ledger.LEDGER_COLUMNS) + "\n")
    assert ledger.append([_row(ticker="PG", role="technical", stance="bearish")], path) == 1
    assert len(ledger.load_rows(path)) == 1


def test_append_defaults_to_env_ledger():
    assert ledger.append([_row(ticker="JPM", role="osint", stance="neutral")]) == 1
    assert common.ledger_path().is_file()
    assert ledger.load_rows()[0]["ticker"] == "JPM"


def test_append_rejects_foreign_header(tmp_path: Path):
    path = tmp_path / "bad.csv"
    common.write_text(path, "a,b,c\n1,2,3\n")
    with pytest.raises(common.ConfigError):
        ledger.append([_row(ticker="KO", role="committee")], path)


def test_csv_round_trip_with_commas_quotes_and_newlines(tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    thesis = 'Pricing power, "moat" intact;\nsecond line, with commas\r\nand a ‘unicode’ quote — ₪'
    assert ledger.append([_row(ticker="KO", role="fundamental", thesis=thesis, conviction=6,
                               fair_value_base=61.5, invalidation_price=None)], path) == 1
    assert ledger.append([_row(ticker="KO", role="technical", thesis="plain")], path) == 1
    rows = ledger.load_rows(path)
    assert len(rows) == 2
    assert rows[0]["thesis"] == thesis
    assert rows[0]["conviction"] == "6"
    assert rows[0]["fair_value_base"] == "61.5"
    assert rows[0]["invalidation_price"] == ""
    assert rows[1]["thesis"] == "plain"
    # the raw file is valid CSV for the stdlib reader too
    with open(path, encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 2


# =====================================================================================
# score
# =====================================================================================

PRICES_THEN_NOW = {
    # ticker: (at 2026-07-31, now 2026-10-02)
    "SPY": (500.0, 525.0),   # +5%
    "XLV": (40.0, 41.2),     # +3%
    "XLP": (80.0, 80.0),     # 0%
    "MRK": (100.0, 112.0),   # +12%
    "KO": (60.0, 66.0),      # +10%
    "PG": (150.0, 141.0),    # -6%
}


def price_fn(ticker: str, date_s: str) -> float | None:
    then, now = PRICES_THEN_NOW.get(ticker, (None, None))
    return now if date_s >= "2026-10-01" else then


def _score_rows() -> list[dict]:
    return [
        _row(ticker="MRK", role="committee", action="BUY", kind="candidate", sector="health_care",
             price_at_rec=100.0, spy_at_rec=500.0, sector_etf="XLV", sector_etf_at_rec=40.0),
        _row(ticker="KO", role="committee", action="SELL", sector="consumer_staples",
             price_at_rec=60.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0),
        _row(ticker="KO", role="fundamental", stance="bearish", sector="consumer_staples",
             price_at_rec=60.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0),
        _row(ticker="PG", role="technical", stance="bearish", sector="consumer_staples",
             price_at_rec=150.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0),
        _row(ticker="MRK", role="technical", stance="bullish", kind="candidate", sector="health_care",
             price_at_rec=None, spy_at_rec=None, sector_etf="XLV", sector_etf_at_rec=None),  # looked up
        _row(ticker="PG", role="committee", action="HOLD", sector="consumer_staples",
             price_at_rec=150.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0),
        _row(ticker="PG", role="osint", stance="neutral", sector="consumer_staples",
             price_at_rec=150.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0),
        _row(ticker="KO", role="sector_head", action="TRIM", sector="consumer_staples", run_id="2026-09-25_0900",
             rec_date="2026-09-25", price_date="2026-09-25", price_at_rec=60.0, spy_at_rec=500.0,
             sector_etf="XLP", sector_etf_at_rec=80.0),                                   # too recent
    ]


@pytest.fixture
def scored_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "recommendations.csv"
    ledger.append(_score_rows(), path)
    return path


def _find(result: dict, ticker: str, role: str) -> dict:
    return next(r for r in result["rows"] if r["ticker"] == ticker and r["role"] == role)


def test_score_returns_vs_spy_and_sector(scored_ledger: Path):
    res = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF)
    mrk = _find(res, "MRK", "committee")
    assert mrk["ret_pct"] == pytest.approx(12.0)
    assert mrk["spy_ret_pct"] == pytest.approx(5.0)
    assert mrk["sector_ret_pct"] == pytest.approx(3.0)
    assert mrk["excess_spy_pp"] == pytest.approx(7.0)
    assert mrk["excess_sector_pp"] == pytest.approx(9.0)
    assert mrk["days"] == (pd.Timestamp(AS_OF) - pd.Timestamp("2026-08-01")).days
    assert mrk["hit"] is True
    # blank at-rec prices are looked up with price_fn at the price date
    tech = _find(res, "MRK", "technical")
    assert tech["ret_pct"] == pytest.approx(12.0) and tech["spy_ret_pct"] == pytest.approx(5.0)
    assert tech["hit"] is True


def test_hit_logic_per_action_and_stance(scored_ledger: Path):
    res = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF)
    assert _find(res, "KO", "committee")["hit"] is False      # SELL but KO beat SPY (+5 pp)
    assert _find(res, "KO", "fundamental")["hit"] is False    # bearish, outperformed
    assert _find(res, "PG", "technical")["hit"] is True       # bearish, PG -6% vs SPY +5%
    assert _find(res, "PG", "technical")["excess_spy_pp"] == pytest.approx(-11.0)
    assert _find(res, "PG", "committee")["hit"] is None       # HOLD excluded from hit rates
    assert _find(res, "PG", "osint")["hit"] is None           # neutral excluded
    assert _find(res, "PG", "committee")["status"] == "scored"


def test_min_age_days_exclusion(scored_ledger: Path):
    res = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF)
    young = _find(res, "KO", "sector_head")
    assert young["status"] == "too_recent" and young["ret_pct"] is None and young["days"] == 7
    assert res["min_age_days"] == common.committee_cfg()["ledger"]["min_age_days"]
    assert res["counts"]["too_recent"] == 1 and res["counts"]["scored"] == 7
    res2 = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF, min_age_days=5)
    assert _find(res2, "KO", "sector_head")["status"] == "scored"
    assert _find(res2, "KO", "sector_head")["hit"] is False   # TRIM, but KO beat SPY by 5 pp
    assert res2["counts"]["too_recent"] == 0


def test_summary_by_role_and_action(scored_ledger: Path):
    res = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF)
    s = res["summary"]
    assert s["committee"]["BUY"] == {"n": 1, "n_hit_eligible": 1, "hits": 1, "hit_rate": 1.0,
                                     "mean_excess_spy_pp": 7.0, "mean_excess_sector_pp": 9.0}
    assert s["committee"]["SELL"]["hit_rate"] == 0.0
    assert s["committee"]["HOLD"]["hit_rate"] is None and s["committee"]["HOLD"]["n"] == 1
    assert s["committee"]["HOLD"]["mean_excess_spy_pp"] == pytest.approx(-11.0)
    assert s["technical"]["bearish"]["hit_rate"] == 1.0
    assert s["technical"]["bullish"]["hit_rate"] == 1.0
    assert s["fundamental"]["bearish"]["hit_rate"] == 0.0
    assert s["osint"]["neutral"]["hit_rate"] is None
    assert "sector_head" not in s                                # too recent
    assert res["by_role"]["technical"]["hit_rate"] == 1.0
    assert res["by_role"]["committee"]["n_hit_eligible"] == 2
    assert res["by_action"]["BUY"]["n"] == 1
    md = ledger.render_scorecard_md(res)
    assert common.DISCLAIMER in md and "## By role and action" in md and "committee · BUY" in md


def test_score_missing_prices_marks_no_price(tmp_path: Path):
    path = tmp_path / "l.csv"
    ledger.append([_row(ticker="ZZZ", role="committee", action="BUY", price_at_rec=10.0, spy_at_rec=500.0)], path)
    res = ledger.score(path, price_fn=lambda t, d: None, as_of=AS_OF)
    assert res["rows"][0]["status"] == "no_price" and res["rows"][0]["hit"] is None
    assert res["summary"] == {}


def test_default_price_fn_reads_cached_series_at_or_before(monkeypatch: pytest.MonkeyPatch):
    df = pd.DataFrame({"Close": [10.0, 11.0, 12.0]},
                      index=pd.to_datetime(["2026-09-30", "2026-10-02", "2026-10-05"]))
    fake = types.ModuleType("scripts.fetch_prices")
    calls: list[str] = []

    def load_series(t: str) -> pd.DataFrame | None:
        calls.append(t)
        return df if t == "KO" else None
    fake.load_series = load_series  # type: ignore[attr-defined]
    import scripts
    monkeypatch.setitem(sys.modules, "scripts.fetch_prices", fake)
    monkeypatch.setattr(scripts, "fetch_prices", fake, raising=False)
    fn = ledger.default_price_fn()
    assert fn("KO", "2026-10-04") == 11.0       # Sunday -> last close at or before
    assert fn("KO", "2026-10-02") == 11.0
    assert fn("KO", "2026-09-01") is None       # before the series
    assert fn("XYZ", "2026-10-02") is None
    assert calls.count("KO") == 1               # series loaded once per ticker


# =====================================================================================
# rows_from_run
# =====================================================================================


def _px(t: str, v: float) -> dict:
    return {"last_close": common.fact(v, "USD/share", f"D:PX:{t}", AS_OF, "price"), "last_date": AS_OF}


@pytest.fixture
def synthetic_run(tmp_path: Path) -> Path:
    rd = tmp_path / "runs" / RUN_ID
    (rd / "inputs").mkdir(parents=True)
    for f in (REPO_ROOT / "config").glob("*.yaml"):
        shutil.copy2(f, rd / "inputs" / f.name)
    rep2, rep3 = rd / "phase2" / "reports", rd / "phase3" / "reports"

    def report(path: Path, doc: dict) -> str:
        common.write_json(path, doc)
        return path.as_posix()

    ko_fund = report(rep2 / "fundamental_KO.json", {
        "agent_role": "fundamental", "ticker": "KO", "stance": "neutral", "conviction": 6,
        "thesis": 'Durable franchise, "fully" valued,\nlimited upside.', "invalidation_price": None,
        "extensions": {"valuation": {"fair_value_base": 64.5}}})
    report(rep2 / "technical_KO.json", {"agent_role": "technical", "ticker": "KO", "stance": "bearish",
                                        "conviction": 4, "thesis": "first attempt", "invalidation_price": 66})
    ko_tech_r2 = report(rep2 / "technical_KO.r2.json", {
        "agent_role": "technical", "ticker": "KO", "stance": "bearish", "conviction": 4,
        "thesis": "Below a falling 200-day average.", "invalidation_price": 66.0})
    ko_da = report(rep2 / "devils_advocate_KO.json", {
        "agent_role": "devils_advocate", "ticker": "KO", "verdict_on_thesis": "damaged",
        "verdict_reason": "Dividend coverage is thin.", "bear_case_price": 55.0})
    ko_osint_failed = report(rep2 / "osint_KO.failed.json", {
        "order_id": "p2-a-osint-KO", "agent_role": "osint", "ticker": "KO", "status": "failed",
        "attempts": 2, "errors": ["x"]})
    qqq_etf = report(rep2 / "etf_QQQ.json", {"agent_role": "etf", "ticker": "QQQ", "stance": "neutral",
                                             "conviction": 6, "action": "HOLD", "thesis": "Core growth fund."})
    mrk_fund = report(rep3 / "fundamental_MRK.json", {
        "agent_role": "fundamental", "ticker": "MRK", "stance": "bullish", "conviction": 8,
        "thesis": "Pipeline depth.", "invalidation_price": 88.0,
        "extensions": {"valuation": {"fair_value_base": 130.0}}})
    sh_staples = report(rd / "phase2" / "consumer_staples_verdict.json", {
        "agent_role": "sector_head", "phase": 2, "sector": "consumer_staples", "verdicts": [
            {"ticker": "KO", "kind": "holding", "action": "TRIM", "rule_action": "TRIM", "agg_conviction": 4.2,
             "agg_evidence": 7.0, "rr": 0.8, "rationale": "Valuation full, momentum weak."}]})
    sh_funds = report(rd / "phase2" / "funds_verdict.json", {
        "agent_role": "sector_head", "phase": 2, "sector": "funds", "verdicts": [
            {"ticker": "QQQ", "kind": "holding", "action": "HOLD", "rule_action": "HOLD", "agg_conviction": 6.0,
             "agg_evidence": 7.5, "rr": None, "rationale": "Core holding."}]})
    sh_hc = report(rd / "phase3" / "health_care_verdict.json", {
        "agent_role": "sector_head", "phase": 3, "sector": "health_care", "verdicts": [
            {"ticker": "MRK", "kind": "candidate", "action": "BUY", "rule_action": "BUY", "agg_conviction": 7.6,
             "agg_evidence": 7.2, "rr": 2.7, "rationale": "Passes every rule.", "thesis": "Undervalued pipeline.",
             "fair_value_base": 130.0, "invalidation_price": 88.0}]})
    cio = report(rd / "phase4" / "cio_final.json", {
        "agent_role": "cio", "mode": "reconciliation", "final_target_sector_weights": {"cash": 5},
        "final_actions": [
            {"ticker": "KO", "action": "HOLD", "source_verdict_action": "TRIM", "override": True,
             "override_justification": "Tax drag outweighs the small valuation gap, so the trim waits."},
            {"ticker": "QQQ", "action": "HOLD", "source_verdict_action": "HOLD", "override": False},
            {"ticker": "MRK", "action": "BUY", "source_verdict_action": "BUY", "override": False}]})
    common.write_json(rd / "phase2" / "evidence.json", {"schema": "evidence/v1", "reports": {
        ko_fund: {"agent_role": "fundamental", "ticker": "KO", "status": "ok", "score": 8},
        ko_tech_r2: {"agent_role": "technical", "ticker": "KO", "status": "ok", "score": 9},
        ko_da: {"agent_role": "devils_advocate", "ticker": "KO", "status": "ok", "score": 6},
        # relative key form is matched by its phaseN/... tail
        "phase2/reports/etf_QQQ.json": {"agent_role": "etf", "ticker": "QQQ", "status": "ok", "score": 7}}})
    common.write_json(rd / "phase3" / "evidence.json", {"schema": "evidence/v1", "reports": {
        mrk_fund: {"agent_role": "fundamental", "ticker": "MRK", "status": "ok", "score": 8}}})
    common.write_json(rd / "phase2" / "aggregate.json", {"schema": "aggregate/v1", "tickers": {
        "KO": {"kind": "holding", "asset_type": "stock", "sector": "consumer_staples", "agg_conviction": 4.2,
               "agg_evidence": 7.0, "rule_action": "TRIM",
               "rr": {"price": 66.0, "fv_base": 64.5, "invalidation": 61.0, "rr": 0.8}},
        "QQQ": {"kind": "holding", "asset_type": "fund", "sector": "funds", "agg_conviction": 6.0,
                "agg_evidence": 7.5, "rule_action": "HOLD", "rr": None}}})
    common.write_json(rd / "phase3" / "aggregate.json", {"schema": "aggregate/v1", "tickers": {
        "MRK": {"kind": "candidate", "asset_type": "stock", "sector": "health_care", "agg_conviction": 7.6,
                "agg_evidence": 7.2, "rule_action": "BUY",
                "rr": {"price": 100.0, "fv_base": 130.0, "invalidation": 88.0, "rr": 2.5}}}})
    common.write_json(rd / "phase1" / "prices.json", {"schema": "prices/v1", "tickers": {
        "KO": _px("KO", 66.0), "QQQ": _px("QQQ", 500.0), "SPY": _px("SPY", 525.0), "XLP": _px("XLP", 80.0),
        "XLV": _px("XLV", 41.0)}, "fx": {"usdils": None}})
    common.write_json(rd / "phase3" / "data" / "technicals_MRK.json",
                      {"close": common.fact(100.0, "USD/share", "D:PX:MRK", "2026-10-01", "price")})
    common.write_json(rd / "phase4" / "portfolio_plan.json", {"positions": [
        {"ticker": "KO", "weight_tgt_pct": 12.0}, {"ticker": "QQQ", "weight_tgt_pct": 30.0},
        {"ticker": "MRK", "weight_tgt_pct": 3.0}]})

    def order(role: str, ticker: str | None, phase: int, output: str, resolved: str | None,
              sector: str, attempt: int = 1, mode: str | None = None, status: str = "done") -> dict:
        return {"step": f"p{phase}", "agent_type": common.AGENT_TYPES[role], "agent_role": role, "mode": mode,
                "ticker": ticker, "sector": sector, "sector_class": "default", "phase": phase,
                "attempt": attempt, "status": status, "order_path": "x.md", "output": output,
                "resolved_output": resolved, "errors": []}

    orders = {
        "p2-a-fundamental-KO": order("fundamental", "KO", 2, ko_fund, ko_fund, "consumer_staples"),
        "p2-a-technical-KO": order("technical", "KO", 2, (rep2 / "technical_KO.json").as_posix(), ko_tech_r2,
                                   "consumer_staples", attempt=2),
        "p2-a-osint-KO": order("osint", "KO", 2, (rep2 / "osint_KO.json").as_posix(), ko_osint_failed,
                               "consumer_staples", attempt=2, status="failed"),
        "p2-b-devils_advocate-KO": order("devils_advocate", "KO", 2, ko_da, ko_da, "consumer_staples"),
        "p2-a-etf-QQQ": order("etf", "QQQ", 2, qqq_etf, qqq_etf, "funds"),
        "p2-sh-consumer_staples": order("sector_head", None, 2, sh_staples, sh_staples, "consumer_staples",
                                        mode="phase2"),
        "p2-sh-funds": order("sector_head", None, 2, sh_funds, sh_funds, "funds", mode="phase2"),
        "p3-light-fundamental-MRK": order("fundamental", "MRK", 3, mrk_fund, mrk_fund, "health_care"),
        "p3-sh-health_care": order("sector_head", None, 3, sh_hc, sh_hc, "health_care", mode="phase3"),
        "p4-cio": order("cio", None, 4, cio, cio, "", mode="reconciliation"),
    }
    common.write_json(rd / "manifest.json", {
        "run_id": RUN_ID, "created_at": "2026-10-02T10:00:00+00:00", "as_of": AS_OF,
        "mode": {"dry_run": True, "quick": False, "sectors": None, "holding": None},
        "holdings": [
            {"ticker": "KO", "asset_type": "stock", "sector": "consumer_staples", "sector_class": "default",
             "classification_source": "yfinance", "position_pct": 12.0},
            {"ticker": "QQQ", "asset_type": "fund", "sector": "funds", "sector_class": "fund",
             "classification_source": "portfolio", "position_pct": 30.0}],
        "candidates": {"health_care": ["MRK"]}, "finalists": {"health_care": ["MRK"]},
        "orders": orders, "steps": [], "warnings": []})
    return rd


def _by(rows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["ticker"], r["role"]): r for r in rows}


def test_rows_from_run_builds_one_row_per_ticker_role(synthetic_run: Path):
    rows = ledger.rows_from_run(synthetic_run)
    keyed = _by(rows)
    assert len(keyed) == len(rows)                          # unique (ticker, role)
    assert set(keyed) == {("KO", "fundamental"), ("KO", "technical"), ("KO", "devils_advocate"),
                          ("KO", "sector_head"), ("KO", "committee"), ("QQQ", "etf"),
                          ("QQQ", "sector_head"), ("QQQ", "committee"), ("MRK", "fundamental"),
                          ("MRK", "sector_head"), ("MRK", "committee")}
    assert all(list(r) == list(ledger.LEDGER_COLUMNS) for r in rows)
    assert all(r["run_id"] == RUN_ID and r["rec_date"] == AS_OF and r["run_mode"] == "dry_run" for r in rows)

    fund = keyed[("KO", "fundamental")]
    assert fund["evidence"] == 8 and fund["conviction"] == 6 and fund["stance"] == "neutral"
    assert fund["fair_value_base"] == 64.5 and fund["action"] is None
    assert fund["price_at_rec"] == 66.0 and fund["price_date"] == AS_OF
    assert fund["spy_at_rec"] == 525.0 and fund["sector_etf"] == "XLP" and fund["sector_etf_at_rec"] == 80.0
    assert fund["kind"] == "holding" and fund["asset_type"] == "stock"

    tech = keyed[("KO", "technical")]                       # retry output wins
    assert tech["thesis"] == "Below a falling 200-day average." and tech["evidence"] == 9
    assert tech["report_path"].endswith("technical_KO.r2.json")

    da = keyed[("KO", "devils_advocate")]
    assert da["stance"] == "bearish" and da["evidence"] == 6 and "damaged" in da["thesis"]

    sh = keyed[("KO", "sector_head")]
    assert (sh["action"], sh["rule_action"], sh["conviction"], sh["evidence"], sh["rr"]) == ("TRIM", "TRIM", 4.2, 7.0, 0.8)

    com = keyed[("KO", "committee")]
    assert com["action"] == "HOLD" and com["rule_action"] == "TRIM"      # CIO override carried
    assert com["thesis"].startswith("Tax drag") and com["target_weight_pct"] == 12.0
    assert com["fair_value_base"] == 64.5 and com["invalidation_price"] == 61.0 and com["rr"] == 0.8
    assert com["report_path"].endswith("phase4/cio_final.json")

    etf = keyed[("QQQ", "etf")]
    assert etf["action"] == "HOLD" and etf["evidence"] == 7 and etf["asset_type"] == "fund"
    assert etf["sector"] == "funds" and etf["sector_etf"] == ""

    mrk = keyed[("MRK", "committee")]
    assert mrk["kind"] == "candidate" and mrk["action"] == "BUY" and mrk["sector"] == "health_care"
    assert mrk["price_at_rec"] == 100.0 and mrk["price_date"] == "2026-10-01"   # technicals close
    assert mrk["sector_etf"] == "XLV" and mrk["sector_etf_at_rec"] == 41.0
    assert mrk["target_weight_pct"] == 3.0 and mrk["rr"] == 2.5
    assert keyed[("MRK", "fundamental")]["evidence"] == 8
    assert keyed[("MRK", "sector_head")]["thesis"] == "Undervalued pipeline."


def test_rows_from_run_without_orders_scans_reports(synthetic_run: Path):
    manifest = common.read_json(synthetic_run / "manifest.json")
    manifest["orders"] = {}
    common.write_json(synthetic_run / "manifest.json", manifest)
    keyed = _by(ledger.rows_from_run(synthetic_run))
    assert ("KO", "osint") not in keyed                     # failed stub skipped
    assert keyed[("KO", "technical")]["report_path"].endswith("technical_KO.r2.json")
    assert ("MRK", "committee") in keyed and ("QQQ", "etf") in keyed


def test_rows_from_run_then_append_round_trip(synthetic_run: Path, tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    rows = ledger.rows_from_run(synthetic_run)
    assert ledger.append(rows, path) == len(rows)
    assert ledger.append(ledger.rows_from_run(synthetic_run), path) == 0   # rerun is idempotent
    loaded = {(r["ticker"], r["role"]): r for r in ledger.load_rows(path)}
    assert loaded[("KO", "fundamental")]["thesis"] == 'Durable franchise, "fully" valued,\nlimited upside.'
    assert loaded[("KO", "committee")]["action"] == "HOLD"


def test_cli_append_dry_run_writes_preview_ledger(synthetic_run: Path, capsys: pytest.CaptureFixture[str]):
    assert ledger.main(["append", "--run-dir", str(synthetic_run)]) == 0
    out = json.loads(capsys.readouterr().out)
    preview = synthetic_run / "phase4" / "ledger_preview.csv"
    assert out["ledger"] == preview.as_posix() and preview.is_file()
    assert out["appended"] == out["rows"] == 11
    assert not common.ledger_path().exists()                # the real ledger is untouched


def test_cli_score_writes_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]):
    led = tmp_path / "recommendations.csv"
    ledger.append(_score_rows(), led)
    monkeypatch.setattr(ledger, "default_price_fn", lambda: price_fn)
    out_md = tmp_path / "scorecard.md"
    assert ledger.main(["score", "--ledger", str(led), "--out", str(out_md), "--as-of", AS_OF]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"]["scored"] == 7
    text = out_md.read_text(encoding="utf-8")
    assert "committee · BUY" in text and "| technical |" in text
    assert io.StringIO(text).readline().startswith("# Committee scorecard")


def test_score_tolerates_malformed_dates(tmp_path: Path):
    path = tmp_path / "l.csv"
    ledger.append([_row(ticker="KO", role="committee", action="BUY", rec_date="not-a-date", price_date="",
                        price_at_rec=60.0, spy_at_rec=500.0)], path)
    res = ledger.score(path, price_fn=price_fn, as_of=AS_OF)
    assert res["rows"][0]["status"] == "no_date" and res["counts"]["no_date"] == 1


def test_rows_from_shared_synthetic_run(tmp_path: Path):
    src = REPO_ROOT / "tests" / "fixtures" / "memo_run"
    if not (src / "manifest.json").is_file():
        pytest.skip("tests/fixtures/memo_run not available")
    rd = tmp_path / "memo_run"
    shutil.copytree(src, rd)
    rows = ledger.rows_from_run(rd)
    assert rows
    keys = [(r["ticker"], r["role"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert {r["role"] for r in rows} <= set(ledger.LEDGER_ROLES)
    assert all(not (r["report_path"] or "").endswith(".failed.json") for r in rows)
    path = tmp_path / "ledger.csv"
    assert ledger.append(rows, path) == len(rows)
    assert ledger.append(rows, path) == 0


# =====================================================================================
# CSV formula injection
# =====================================================================================


@pytest.mark.parametrize("thesis", ['=1+1, "q"', "+SUM(A1:A2)", "-2+3 cmd", "@SUM(1)", "\tTAB=1",
                                    "'quoted already", "''=x", "plain - text", "-"])
def test_formula_like_text_is_neutralized_and_round_trips(tmp_path: Path, thesis: str):
    path = tmp_path / "recommendations.csv"
    assert ledger.append([_row(ticker="KO", role="fundamental", thesis=thesis, rr=-1.5)], path) == 1
    with open(path, encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))[0]
    if thesis[:1] in ("=", "+", "-", "@", "\t", "'"):
        assert raw["thesis"] == "'" + thesis          # never a live formula cell in the file
    else:
        assert raw["thesis"] == thesis
    assert raw["rr"] == "-1.5"                         # numbers are never prefixed
    loaded = ledger.load_rows(path)[0]
    assert loaded["thesis"] == thesis and loaded["rr"] == "-1.5"
    # re-appending loaded rows dedupes on the unescaped key
    assert ledger.append(ledger.load_rows(path), path) == 0


# =====================================================================================
# manifest-driven resolution shared with portfolio.py, proxy pricing
# =====================================================================================


def _memo_copy(tmp_path: Path) -> Path:
    src = REPO_ROOT / "tests" / "fixtures" / "memo_run"
    if not (src / "manifest.json").is_file():
        pytest.skip("tests/fixtures/memo_run not available")
    rd = tmp_path / "memo_run"
    shutil.copytree(src, rd)
    return rd


def _set_order(rd: Path, oid: str, **fields: Any) -> None:
    m = common.read_json(rd / "manifest.json")
    m["orders"][oid].update(fields)
    common.write_json(rd / "manifest.json", m)


def test_failed_sector_heads_produce_no_sector_head_rows(tmp_path: Path):
    rd = _memo_copy(tmp_path)
    common.write_json(rd / "phase3" / "health_care_verdict.failed.json",
                      {"order_id": "p3-sh-health_care", "status": "failed", "attempts": 2, "errors": ["x"]})
    _set_order(rd, "p3-sh-health_care", status="failed", attempt=2,
               resolved_output="phase3/health_care_verdict.failed.json")
    keyed = _by(ledger.rows_from_run(rd))
    assert ("MRK", "sector_head") not in keyed
    assert ("KO", "sector_head") in keyed                       # phase-2 verdicts unaffected


def test_cio_retry_resolution_matches_portfolio_plan(tmp_path: Path):
    from scripts import portfolio
    rd = _memo_copy(tmp_path)
    doc = common.read_json(rd / "phase4" / "cio_final.json")
    for fa in doc["final_actions"]:
        if fa["ticker"] == "JPM":
            fa.update({"action": "HOLD", "override": False})
            fa.pop("override_justification", None)
    common.write_json(rd / "phase4" / "cio_final.r2.json", doc)
    _set_order(rd, "p4-cio", attempt=2, resolved_output="phase4/cio_final.r2.json")
    plan = portfolio.run(rd, final=True)
    com = _by(ledger.rows_from_run(rd))[("JPM", "committee")]
    plan_jpm = next(p for p in plan["positions"] if p["ticker"] == "JPM")
    assert com["action"] == plan_jpm["action"] == "HOLD"
    assert com["report_path"].endswith("cio_final.r2.json")


def test_failed_cio_order_ignores_rejected_attempt(tmp_path: Path):
    rd = _memo_copy(tmp_path)
    common.write_json(rd / "phase4" / "cio_final.failed.json",
                      {"order_id": "p4-cio", "status": "failed", "attempts": 2, "errors": ["x"]})
    _set_order(rd, "p4-cio", status="failed", attempt=2, resolved_output="phase4/cio_final.failed.json")
    com = _by(ledger.rows_from_run(rd))[("JPM", "committee")]
    assert com["action"] != "TRIM"                               # the rejected CIO override is not recorded
    assert not com["report_path"].endswith("cio_final.json")


def test_proxy_priced_holding_records_and_scores_proxy_price(synthetic_run: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch):
    rd = synthetic_run
    sh = common.read_json(rd / "phase2" / "funds_verdict.json")
    sh["verdicts"][0]["ticker"] = "TASEQ"
    common.write_json(rd / "phase2" / "funds_verdict.json", sh)
    common.write_json(rd / "phase1" / "lookthrough.json", {"schema": "lookthrough/v1", "holdings": [
        {"ticker": "KO", "asset_type": "stock", "sector": "consumer_staples"},
        {"ticker": "TASEQ", "asset_type": "fund", "sector": "funds", "price_ticker": "QQQ",
         "proxy_ticker": "QQQ"}]})
    keyed = _by(ledger.rows_from_run(rd))
    for role in ("sector_head", "committee"):
        row = keyed[("TASEQ", role)]
        assert row["price_at_rec"] == 500.0 and row["price_date"] == AS_OF   # QQQ proxy close
    assert keyed[("KO", "committee")]["price_at_rec"] == 66.0

    path = tmp_path / "proxy.csv"
    ledger.append(ledger.rows_from_run(rd), path)
    later = {"QQQ": 550.0, "SPY": 551.25, "KO": 66.0, "XLP": 80.0, "XLV": 41.0, "MRK": 100.0}
    fn = lambda t, d: later.get(t)  # noqa: E731 — TASEQ itself has no series

    # mapping from the row's own run (runs/<run_id>/phase1/lookthrough.json)
    monkeypatch.setenv(common.ENV_RUNS_DIR, str(rd.parent))
    res = ledger.score(path, price_fn=fn, as_of="2026-12-31", min_age_days=0)
    row = next(r for r in res["rows"] if r["ticker"] == "TASEQ" and r["role"] == "committee")
    assert row["status"] == "scored" and row["price_ticker"] == "QQQ"
    assert row["ret_pct"] == pytest.approx(10.0)

    # explicit map wins; without any map the raw ticker is unpriced
    monkeypatch.setenv(common.ENV_RUNS_DIR, str(tmp_path / "nowhere"))
    res = ledger.score(path, price_fn=fn, as_of="2026-12-31", min_age_days=0)
    assert next(r for r in res["rows"] if r["ticker"] == "TASEQ" and r["role"] == "committee")["status"] == "no_price"
    res = ledger.score(path, price_fn=fn, as_of="2026-12-31", min_age_days=0, price_tickers={"TASEQ": "QQQ"})
    assert next(r for r in res["rows"] if r["ticker"] == "TASEQ" and r["role"] == "committee")["status"] == "scored"


# =====================================================================================
# append dedupe is on the NORMALISED (run_id, ticker, role)
# =====================================================================================


def test_append_dedupes_on_normalised_ticker_and_role(tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    rows = [_row(ticker="t0", role="committee", action="HOLD"),
            _row(ticker="T0", role="committee", action="TRIM"),               # same key as "t0"
            _row(ticker="brk.b", role="Committee ", action="HOLD"),
            _row(ticker=" BRK-B ", role="committee", action="SELL"),          # same key as "brk.b"
            _row(ticker="BRK-B", role="fundamental", stance="neutral")]
    assert ledger.append(rows, path) == 3
    loaded = ledger.load_rows(path)
    assert [(r["ticker"], r["role"], r["action"]) for r in loaded] == [
        ("T0", "committee", "HOLD"), ("BRK-B", "committee", "HOLD"), ("BRK-B", "fundamental", "")]
    # direct re-appends in any spelling are idempotent
    assert ledger.append([_row(ticker="t0", role="COMMITTEE"), _row(ticker="Brk.B", role="committee")], path) == 0
    assert len(ledger.load_rows(path)) == 3


def test_append_dedupes_against_hand_edited_unnormalised_rows(tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    ledger.append([_row(ticker="KO", role="committee")], path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(",KO,", ",ko ,", 1), encoding="utf-8")       # a spreadsheet user's edit
    assert ledger.load_rows(path)[0]["ticker"] == "ko "
    assert ledger.append([_row(ticker="KO", role="committee")], path) == 0
    assert ledger.append([_row(ticker="KO", role="committee", run_id="2026-09-01_0900")], path) == 1


def test_append_skips_rows_with_a_blank_or_invalid_key(tmp_path: Path):
    path = tmp_path / "recommendations.csv"
    assert ledger.append([_row(ticker="", role="committee"), _row(ticker="KO", role=""),
                          _row(ticker="KO", role="committee", run_id=""), _row(ticker="KO", role="committee")], path) == 1


# =====================================================================================
# stale cached prices are never scored silently
# =====================================================================================


def _install_series(monkeypatch: pytest.MonkeyPatch, data: dict[str, dict[str, float]]) -> None:
    frames = {t: pd.DataFrame({"Close": list(v.values())}, index=pd.to_datetime(list(v.keys())))
              for t, v in data.items()}
    fake = types.ModuleType("scripts.fetch_prices")
    fake.load_series = lambda t: frames.get(t)  # type: ignore[attr-defined]
    import scripts
    monkeypatch.setitem(sys.modules, "scripts.fetch_prices", fake)
    monkeypatch.setattr(scripts, "fetch_prices", fake, raising=False)


def _ko_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "recommendations.csv"
    ledger.append([_row(ticker="KO", role="committee", action="ADD", sector="consumer_staples",
                        price_at_rec=60.0, spy_at_rec=500.0, sector_etf="XLP", sector_etf_at_rec=80.0)], path)
    return path


#: all three series end Fri 2026-10-02 (as_of Sunday 2026-10-04 -> 0 trading days of lag)
FRESH = {"KO": {"2026-08-03": 61.0, "2026-10-02": 66.0},
         "SPY": {"2026-08-03": 505.0, "2026-10-02": 525.0},
         "XLP": {"2026-08-03": 80.5, "2026-10-02": 82.0}}


def test_fresh_series_are_scored_to_their_common_last_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_series(monkeypatch, FRESH)
    res = ledger.score(_ko_ledger(tmp_path), as_of="2026-10-04")
    row = res["rows"][0]
    assert row["status"] == "scored" and row["status_reason"] is None
    assert row["price_end_date"] == "2026-10-02"
    assert row["ret_pct"] == pytest.approx(10.0) and row["spy_ret_pct"] == pytest.approx(5.0)
    assert row["excess_spy_pp"] == pytest.approx(5.0) and row["hit"] is True
    assert res["counts"]["scored"] == 1 and res["counts"]["stale_price"] == 0


def test_live_case_ticker_series_ending_two_months_early_is_not_scored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cached KO ended 2026-08-03 while SPY ran to 2026-10-02: used to be a scored ADD with a nonsense excess."""
    _install_series(monkeypatch, {**FRESH, "KO": {"2026-07-31": 60.0, "2026-08-03": 61.0}})
    res = ledger.score(_ko_ledger(tmp_path), as_of="2026-10-04")
    row = res["rows"][0]
    assert row["status"] == "stale_price"
    assert "KO last bar 2026-08-03" in row["status_reason"] and "trading days before 2026-10-04" in row["status_reason"]
    assert "(max 3)" in row["status_reason"] and "SPY" not in row["status_reason"]
    assert row["ret_pct"] is None and row["excess_spy_pp"] is None and row["hit"] is None
    assert res["counts"]["stale_price"] == 1 and res["counts"]["scored"] == 0
    assert res["summary"] == {} and res["by_role"] == {}
    md = ledger.render_scorecard_md(res)
    assert "stale price 1" in md and "NOT scored" in md


@pytest.mark.parametrize("stale_ticker", ["KO", "SPY", "XLP"])
def test_any_stale_series_blocks_the_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_ticker: str):
    series = {**FRESH, stale_ticker: {"2026-08-03": FRESH[stale_ticker]["2026-08-03"], "2026-09-25": 62.0}}
    _install_series(monkeypatch, series)                       # last bar Fri 09-25: 5 trading days before 10-04
    row = ledger.score(_ko_ledger(tmp_path), as_of="2026-10-04")["rows"][0]
    assert row["status"] == "stale_price" and f"{stale_ticker} last bar 2026-09-25 is 5 trading days" in row["status_reason"]
    assert row["ret_pct"] is None


def test_series_ending_on_different_fresh_dates_are_measured_to_the_earliest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    series = {"KO": {"2026-08-03": 61.0, "2026-09-30": 63.0},                  # 2 trading days stale: allowed
              "SPY": {"2026-08-03": 505.0, "2026-09-30": 515.0, "2026-10-02": 999.0},
              "XLP": {"2026-08-03": 80.5, "2026-09-30": 81.0, "2026-10-02": 999.0}}
    _install_series(monkeypatch, series)
    row = ledger.score(_ko_ledger(tmp_path), as_of="2026-10-04")["rows"][0]
    assert row["status"] == "scored" and row["price_end_date"] == "2026-09-30"
    assert row["ret_pct"] == pytest.approx(5.0)                                # 63 / 60
    assert row["spy_ret_pct"] == pytest.approx(3.0)                            # 515 / 500, NOT the 999 close
    assert row["sector_ret_pct"] == pytest.approx(1.25)                        # 81 / 80, NOT the 999 close


def test_the_staleness_limit_comes_from_committee_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    series = {t: {"2026-08-03": v["2026-08-03"], "2026-09-25": v["2026-10-02"]} for t, v in FRESH.items()}
    _install_series(monkeypatch, series)                                        # 5 trading days stale
    path = _ko_ledger(tmp_path)
    assert ledger.score(path, as_of="2026-10-04")["rows"][0]["status"] == "stale_price"   # default limit 3
    import copy
    cfg = copy.deepcopy(common.committee_cfg())
    cfg["evidence"] = {**cfg["evidence"], "price_max_trading_days": 5}
    monkeypatch.setattr(common, "committee_cfg", lambda config_dir=None: cfg)
    row = ledger.score(path, as_of="2026-10-04")["rows"][0]
    assert row["status"] == "scored" and row["price_end_date"] == "2026-09-25"


def test_missing_series_is_no_price_not_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_series(monkeypatch, {"SPY": FRESH["SPY"], "XLP": FRESH["XLP"]})
    row = ledger.score(_ko_ledger(tmp_path), as_of="2026-10-04")["rows"][0]
    assert row["status"] == "no_price" and row["hit"] is None and "KO" in row["status_reason"]


def test_default_price_fn_exposes_the_last_bar(monkeypatch: pytest.MonkeyPatch):
    _install_series(monkeypatch, {"KO": {"2026-09-30": 10.0, "2026-10-02": 11.0, "2026-10-05": 12.0}})
    fn = ledger.default_price_fn()
    assert fn.last_bar("KO", "2026-10-04") == "2026-10-02"        # Sunday -> the Friday bar, never the later 10-05
    assert fn.last_bar("ko", "2026-10-02") == "2026-10-02"
    assert fn.last_bar("KO", "2026-09-01") is None
    assert fn.last_bar("XYZ", "2026-10-02") is None


def test_injected_price_fn_without_last_bar_is_trusted_as_before(scored_ledger: Path):
    res = ledger.score(scored_ledger, price_fn=price_fn, as_of=AS_OF)
    assert res["counts"]["stale_price"] == 0 and res["counts"]["scored"] == 7
    assert _find(res, "MRK", "committee")["price_end_date"] == AS_OF


# =====================================================================================
# target_weight_pct = the verdict-intended target of the final plan
# =====================================================================================


def _plan_positions(run_dir: Path, name: str, positions: list[dict]) -> None:
    common.write_json(run_dir / "phase4" / name, {"positions": positions})


def test_target_weight_is_the_intended_weight_not_the_unchanged_current_one(synthetic_run: Path):
    _plan_positions(synthetic_run, "portfolio_plan.json", [
        # TRIM whose trade was dropped (rounds to zero / below the minimum): weight stays 12.0, intent 8.04
        {"ticker": "KO", "weight_tgt_pct": 12.0, "weight_intent_pct": 8.04, "dropped_reason": "rounds_to_zero_shares"},
        {"ticker": "QQQ", "weight_tgt_pct": 30.0},                               # older plan: no intent field
        {"ticker": "MRK", "weight_tgt_pct": 2.5, "weight_intent_pct": 3.0}])     # buy scaled by cash: intent 3.0
    keyed = _by(ledger.rows_from_run(synthetic_run))
    assert keyed[("KO", "committee")]["target_weight_pct"] == 8.04
    assert keyed[("QQQ", "committee")]["target_weight_pct"] == 30.0
    assert keyed[("MRK", "committee")]["target_weight_pct"] == 3.0


def test_target_weight_reads_the_final_plan_and_falls_back_to_the_preview(synthetic_run: Path):
    (synthetic_run / "phase4" / "portfolio_plan.json").unlink()                # the fixture ships a final plan
    _plan_positions(synthetic_run, "portfolio_preview.json", [{"ticker": "KO", "weight_intent_pct": 9.9}])
    keyed = _by(ledger.rows_from_run(synthetic_run))
    assert keyed[("KO", "committee")]["target_weight_pct"] == 9.9              # only the preview exists
    _plan_positions(synthetic_run, "portfolio_plan.json", [{"ticker": "KO", "weight_intent_pct": 8.04}])
    keyed = _by(ledger.rows_from_run(synthetic_run))
    assert keyed[("KO", "committee")]["target_weight_pct"] == 8.04             # the final plan wins


def test_target_weight_of_a_dropped_portfolio_trade_end_to_end(synthetic_run: Path):
    from scripts import portfolio
    prof = {"horizon_years": 10, "risk_tolerance": "moderate", "max_position_pct": 10, "max_fund_position_pct": 40,
            "max_sector_pct": 35, "min_trade_usd": 5000, "target_cash_pct": 5, "base_currency": "ILS",
            "tax_jurisdiction": "IL", "capital_gains_tax_pct": 25, "allow_short": False,
            "sec_contact_email": "tester@agent-investors.dev", "allow_fractional_shares": False}
    lt = common.envelope(
        "lookthrough", AS_OF, sources=[], sector_weights_pct={"consumer_staples": 12.0, "cash": 88.0},
        total_value_usd=common.fact(100000.0, "USD", "D:CFG:portfolio", AS_OF, "other"),
        holdings=[{"ticker": "KO", "asset_type": "stock", "sector": "consumer_staples", "shares": 200,
                   "sector_split": {"consumer_staples": 100},
                   "weight_pct": common.fact(12.0, "pct", "D:CFG:portfolio", AS_OF, "other")}])
    prices = {"tickers": {"KO": {"last_close": common.fact(60.0, "USD/share", "D:PX:KO", AS_OF, "price")}}}
    plan = portfolio.build_plan(lookthrough=lt, prices=prices, actions={"KO": {"action": "TRIM", "conviction": 4.0}},
                                target_sector_weights={}, profile=prof, cfg=common.committee_cfg(),
                                portfolio_cfg={"holdings": []})
    ko = plan["positions"][0]
    assert ko["dropped_reason"] == "below_min_trade_usd" and ko["weight_tgt_pct"] == 12.0   # nothing traded
    common.write_json(synthetic_run / "phase4" / "portfolio_plan.json", plan)
    assert _by(ledger.rows_from_run(synthetic_run))[("KO", "committee")]["target_weight_pct"] == ko["weight_intent_pct"]
    assert ko["weight_intent_pct"] < 12.0
