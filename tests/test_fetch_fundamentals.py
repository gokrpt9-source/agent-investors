"""scripts/fetch_fundamentals.py — payload assembly (DESIGN §4.3), fully offline.

* ``tests/fixtures/edgar`` (fixture root): trimmed REAL SEC payloads for KO, JPM, BRK-B + Yahoo info.
* ``tests/fixtures/edgar/synthetic`` (fixture root): hand-designed companies ZZTTM, ZZQ4, ZZNEG
  whose expected ratios are computed by hand below.
Network paths (cache, stale fallback) monkeypatch ``common.http_get_json``."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import scripts
from scripts import common
from scripts import _edgar
from scripts import fetch_fundamentals as FF

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "tests" / "fixtures" / "edgar"
REAL_PRICES = {"KO": 85.65, "JPM": 332.38, "BRK-B": 502.65}      # Yahoo currentPrice in yf/info fixtures


# ------------------------------------------------------------------------------------------- helpers
@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    resources = []
    for p in sorted((REPO / "schemas").glob("*.json")) + sorted((REPO / "schemas" / "data").glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        resources.append((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)))
    schema = json.loads((REPO / "schemas" / "data" / "fundamentals.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=Registry().with_resources(resources),
                                format_checker=FormatChecker())


def _assert_valid(validator: Draft202012Validator, doc: dict) -> None:
    errors = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(doc)]
    assert not errors, errors[:10]


def _v(doc: dict, pointer: str):
    return common.json_pointer_get(doc, pointer)["value"]


def _reason(doc: dict, pointer: str):
    return common.json_pointer_get(doc, pointer)["na_reason"]


def _codes(doc: dict) -> list[str]:
    return [f["code"] for f in doc["flags"]]


@pytest.fixture
def real(use_fixtures):
    use_fixtures("edgar")


@pytest.fixture
def synthetic(use_fixtures):
    use_fixtures("edgar/synthetic")


def _set_share_class(tmp_env, ticker: str, factor: float, tags=None) -> None:
    def mutate(d: dict) -> None:
        sec = d.setdefault("securities", {}).setdefault(ticker, {})
        sec["share_class"] = {"class_factor": factor, "shares_tags": list(tags or [])}
        sec["source_url"] = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    tmp_env.update_config("etf_overrides", mutate)
    assert not common.validate_config("etf_overrides", tmp_env.read_config("etf_overrides"))


# =========================================================================== synthetic, hand-computed
def test_synthetic_full_payload_hand_computed(synthetic, validator, tmp_path):
    out = tmp_path / "fundamentals_ZZTTM.json"
    doc = FF.run("ZZTTM", out, price=20.0)
    _assert_valid(validator, doc)
    assert out.is_file() and json.loads(out.read_text(encoding="utf-8"))["ticker"] == "ZZTTM"
    assert doc["schema"] == "fundamentals/v1" and doc["as_of"] == "2026-10-02"
    assert doc["coverage"] == "sec" and doc["cik"] == 9900001 and doc["lag_detected"] is False
    assert doc["entity"]["sector_class"] == "default" and doc["entity"]["sic"] == 2080

    # TTM = FY2025 + 6M2026 - 6M2025
    rev = doc["ttm"]["revenue"]
    assert rev["value"] == 1280.0 and rev["derivation"] == "FY2025+6M2026-6M2025"
    assert rev["as_of"] == "2026-06-30" and rev["filed"] == "2026-08-03" and rev["kind"] == "fundamental"
    assert rev["tag"] == "Revenues" and rev["source_id"] == "D:SEC:ZZTTM:CF"
    expected_ttm = {"gross_profit": 540, "operating_income": 170, "net_income": 110, "ocf": 270, "capex": 60,
                    "fcf": 210, "da": 44, "ebitda": 214, "pretax_income": 140, "income_tax": 30}
    for k, v in expected_ttm.items():
        assert _v(doc, f"/ttm/{k}") == pytest.approx(v), k

    # balance at 2026-06-30; no ShortTermBorrowings/CommercialPaper -> stb_assumed_zero
    assert {k: _v(doc, f"/balance/{k}") for k in ("cash", "total_debt", "net_debt", "equity", "goodwill",
                                                  "intangibles")} == {
        "cash": 200, "total_debt": 300, "net_debt": 100, "equity": 1000, "goodwill": 50, "intangibles": 30}
    assert _reason(doc, "/balance/st_investments") == "missing"
    assert doc["balance"]["total_debt"]["formula"] == "a"
    assert "stb_assumed_zero" in _codes(doc)

    # price, shares, market cap (Yahoo marketCap 2050 vs 100 x 20 = 2000 -> 2.4% < 10%: no flag)
    assert doc["price"]["value"] == 20.0 and doc["price"]["kind"] == "price"
    assert _v(doc, "/shares") == 100.0 and doc["shares"]["as_of"] == "2026-07-25"
    assert _v(doc, "/market_cap") == 2000.0
    assert "share_count_ambiguous" not in _codes(doc)

    m = doc["margins_pct"]
    assert m["gross"]["value"] == pytest.approx(42.1875)          # 540 / 1280
    assert m["operating"]["value"] == pytest.approx(13.28125)     # 170 / 1280
    assert m["net"]["value"] == pytest.approx(8.59375)            # 110 / 1280
    assert m["fcf"]["value"] == pytest.approx(16.40625)           # 210 / 1280

    g = doc["growth_pct"]
    assert g["revenue_cagr_3y"]["value"] == pytest.approx(((1200 / 900) ** (1 / 3) - 1) * 100)   # 10.0642
    assert g["ni_cagr_3y"]["value"] is None and g["ni_cagr_3y"]["na_reason"] == "negative_denominator"
    assert g["fcf_cagr_3y"]["value"] == pytest.approx(((200 / 160) ** (1 / 3) - 1) * 100)        # 7.7217
    assert g["revenue_yoy_ttm"]["value"] == pytest.approx((1280 / 1140 - 1) * 100)              # 12.2807

    val = doc["valuation"]
    tax = 30 / 140
    expected = {"ev": 2100.0,                                  # 2000 + 300 - 200 - 0
                "pe": 2000 / 110, "ev_ebit": 2100 / 170, "ev_sales": 2100 / 1280,
                "fcf_yield_pct": 10.5, "net_debt_ebitda": 100 / 214, "p_b": 2.0,
                "p_tbv": 2000 / (1000 - 50 - 30), "roe_pct": 11.0,
                "roic_pct": 170 * (1 - tax) / (1000 + 100) * 100, "tax_rate_used": tax}
    for k, v in expected.items():
        assert val[k]["value"] == pytest.approx(v), k
    assert val["tax_rate_used"]["unit"] == "fraction" and val["pe"]["unit"] == "x"

    # FY history (latest first) with the restated FY2024 operating income
    assert [r["fy"] for r in doc["fy"]] == [2025, 2024, 2023, 2022, 2021]
    fy24 = doc["fy"][1]
    assert fy24["operating_income"]["value"] == 140.0 and fy24["operating_income"]["filed"] == "2026-02-13"
    assert doc["fy"][0]["fcf"]["value"] == 200.0 and doc["fy"][0]["period_end"] == "2025-12-31"
    assert doc["tag_map"]["revenue"] == {"tag": "Revenues", "stitched": False}
    assert doc["tag_map"]["gross_profit"]["derived"] == "revenue-cogs"
    ids = {s["id"] for s in doc["sources"]}
    assert {"D:SEC:MAP", "D:SEC:ZZTTM:CF", "D:SEC:ZZTTM:SUB", "D:PX:ZZTTM", "D:YF:ZZTTM:INFO"} <= ids
    assert all(s["url"].startswith("https://") for s in doc["sources"])


def test_negative_net_income_and_mixed_instants(synthetic, validator):
    doc = FF.run("ZZNEG", price=5.0)
    _assert_valid(validator, doc)
    assert _v(doc, "/ttm/net_income") == -50.0
    assert _v(doc, "/valuation/pe") is None and _reason(doc, "/valuation/pe") == "negative_denominator"
    assert _reason(doc, "/valuation/ev_ebit") == "negative_denominator"     # operating income -20
    assert _v(doc, "/valuation/ev") == pytest.approx(200 + 200 - 100)       # mcap 40x5 + debt - cash
    assert doc["balance"]["total_debt"]["formula"] == "b" and _v(doc, "/balance/total_debt") == 200.0
    assert "mixed_instants" in _codes(doc)
    assert "mcap_crosscheck_unavailable" in _codes(doc)                     # no Yahoo fixture for ZZNEG
    assert _v(doc, "/growth_pct/revenue_yoy_ttm") == pytest.approx((500 / 450 - 1) * 100)
    assert _reason(doc, "/growth_pct/revenue_cagr_3y") == "insufficient_history"


def test_quarter_fallback_lag_and_share_divergence(synthetic, validator):
    doc = FF.run("ZZQ4", price=10.0)
    _assert_valid(validator, doc)
    assert _v(doc, "/ttm/revenue") == 1070.0
    assert doc["ttm"]["revenue"]["derivation"].startswith("4Q[")
    assert _reason(doc, "/ttm/net_income") == "insufficient_history"
    assert _reason(doc, "/valuation/pe") == "insufficient_history"
    # synthetic lag: the 10-Q filed 2026-08-05 is absent from companyfacts
    assert doc["lag_detected"] is True and "companyfacts_lag" in _codes(doc)
    assert any(m.startswith("latest 10-Q for 2026-06-30 (filed 2026-08-05) not yet in companyfacts — read it "
                            "directly: https://www.sec.gov/Archives/edgar/data/9900002/") for m in doc["missing_data"])
    assert doc["submissions_latest"]["accn"] == "0009900002-26-000003"
    # 300 + 50 class counts summed = 350 x 10 = 3,500 vs Yahoo 7,000 -> 50% divergence
    assert "multi_class_shares_summed" in _codes(doc) and "share_count_ambiguous" in _codes(doc)
    flag = next(f for f in doc["flags"] if f["code"] == "share_count_ambiguous")
    assert flag["severity"] == "high" and "50.0%" in flag["detail"]
    assert _v(doc, "/market_cap") == 7000.0 and doc["market_cap"]["derivation"] == "yfinance_marketCap"
    assert doc["market_cap"]["source_id"] == "D:YF:ZZQ4:INFO"
    assert _v(doc, "/shares") == pytest.approx(700.0) and _v(doc, "/shares_edgar") == 350.0
    assert doc["coverage"] == "partial"


def test_class_factor_resolves_share_ambiguity(synthetic, tmp_env, validator):
    _set_share_class(tmp_env, "ZZQ4", 2.0)
    doc = FF.run("ZZQ4", price=10.0)
    _assert_valid(validator, doc)
    assert "share_count_ambiguous" not in _codes(doc) and "share_class_factor_applied" in _codes(doc)
    assert _v(doc, "/shares") == 700.0 and "class_factor" in doc["shares"]["derivation"]
    assert _v(doc, "/market_cap") == 7000.0 and doc["market_cap"]["source_id"] == "D:SEC:ZZQ4:CF"
    assert any(s["id"].startswith("D:CFG:etf_overrides") for s in doc["sources"])


def test_non_sec_ticker(real, validator):
    doc = FF.run("TEVA.TA", price=10.0)
    _assert_valid(validator, doc)
    assert doc["coverage"] == "non_sec" and doc["cik"] is None
    assert "not an SEC filer" in doc["missing_data"]
    for block in ("ttm", "balance", "margins_pct", "growth_pct", "valuation"):
        for k, f in doc[block].items():
            assert f["value"] is None and f["na_reason"] == "missing", (block, k)
    assert doc["shares"]["value"] is None and doc["market_cap"]["value"] is None
    assert doc["fy"] == []


# =========================================================================== real trimmed SEC payloads
def test_ko_debt_formula_and_lag(real, validator, tmp_path):
    doc = FF.run("KO", tmp_path / "fundamentals_KO.json", price=REAL_PRICES["KO"])
    _assert_valid(validator, doc)
    assert doc["coverage"] == "sec" and doc["cik"] == 21344 and doc["entity"]["sector_class"] == "default"
    debt = doc["balance"]["total_debt"]
    # LongTermDebt stops in 2024 (tag drift) -> must fall through to (c)/(d), not the stale (a)
    assert debt["formula"] in ("c", "d") and not debt["derivation"].startswith("(a)")
    assert debt["as_of"] == "2026-04-03" and doc["tag_map"]["total_debt"]["formula"] == debt["formula"]
    assert debt["value"] > 40e9

    # lag_detected must equal "latest periodic accession in submissions is absent from companyfacts"
    sub = json.loads((FX / "http" / "sec" / "submissions_CIK0000021344.json").read_text(encoding="utf-8"))
    cf = json.loads((FX / "http" / "sec" / "companyfacts_CIK0000021344.json").read_text(encoding="utf-8"))
    rec = sub["filings"]["recent"]
    rows = [(rec["filingDate"][i], rec["accessionNumber"][i]) for i in range(len(rec["form"]))
            if rec["form"][i] in ("10-K", "10-Q", "10-K/A", "10-Q/A")]
    latest_accn = max(rows)[1]
    accns = {f["accn"] for ns in cf["facts"].values() for node in ns.values()
             for arr in node["units"].values() for f in arr}
    expected = latest_accn not in accns
    assert expected is True                         # KO's 2026-07-29 10-Q is not in companyfacts yet
    assert doc["lag_detected"] is expected and "companyfacts_lag" in _codes(doc)
    assert doc["submissions_latest"]["accn"] == latest_accn
    assert doc["companyfacts_latest"]["period_end"] == "2026-04-03"

    rev = doc["ttm"]["revenue"]
    assert rev["derivation"] == "FY2025+3M2026-3M2025" and rev["as_of"] == "2026-04-03"
    assert doc["valuation"]["ev"]["value"] is not None and doc["valuation"]["pe"]["value"] > 0
    assert "share_count_ambiguous" not in _codes(doc)       # EDGAR shares x price ~ Yahoo marketCap
    assert len(doc["fy"]) == 5 and doc["fy"][0]["fy"] == 2025


def test_jpm_financials(real, validator):
    doc = FF.run("JPM", price=REAL_PRICES["JPM"])
    _assert_valid(validator, doc)
    assert doc["entity"]["sector_class"] == "financials" and doc["entity"]["sic"] == 6021
    for k in ("ev", "ev_ebit", "ev_sales", "net_debt_ebitda"):
        assert doc["valuation"][k]["value"] is None and doc["valuation"][k]["na_reason"] == "not_meaningful", k
    assert _reason(doc, "/margins_pct/gross") == "not_meaningful"
    mcap, eq = _v(doc, "/market_cap"), _v(doc, "/balance/equity")
    assert _v(doc, "/valuation/p_b") == pytest.approx(mcap / eq)
    # no intangibles tag in JPM's companyfacts -> P/TBV is null "missing", never "intangibles = 0"
    assert _v(doc, "/balance/goodwill") is not None and _v(doc, "/balance/intangibles") is None
    assert _v(doc, "/valuation/p_tbv") is None and _reason(doc, "/valuation/p_tbv") == "missing"
    assert "intangibles not reported" in doc["valuation"]["p_tbv"]["derivation"]
    assert any(m.startswith("valuation.p_tbv:") for m in doc["missing_data"])
    assert _v(doc, "/valuation/roe_pct") == pytest.approx(_v(doc, "/ttm/net_income") / eq * 100)
    assert doc["tag_map"]["revenue"]["tag"] == "RevenuesNetOfInterestExpense"


def test_brk_share_count_ambiguity_flagged(real, validator):
    doc = FF.run("BRK-B", price=REAL_PRICES["BRK-B"])
    _assert_valid(validator, doc)
    assert "share_count_ambiguous" in _codes(doc)
    assert doc["market_cap"]["derivation"] == "yfinance_marketCap"
    yf = json.loads((FX / "yf" / "info" / "BRK-B.json").read_text(encoding="utf-8"))
    assert _v(doc, "/market_cap") == yf["marketCap"]
    assert _v(doc, "/shares") == pytest.approx(yf["marketCap"] / REAL_PRICES["BRK-B"])
    assert doc["entity"]["sector_class"] == "financials"


def test_brk_class_factor_applied_from_temp_overrides(real, tmp_env, validator):
    _set_share_class(tmp_env, "BRK-B", 1500.0)
    doc = FF.run("brk.b", price=REAL_PRICES["BRK-B"])
    _assert_valid(validator, doc)
    assert doc["ticker"] == "BRK-B"
    se = doc["shares_edgar"]
    assert se["value"] == pytest.approx(941_481 * 1500) and "class_factor" in se["derivation"]
    assert se["class_factor"] == 1500.0 and doc["tag_map"]["shares"]["class_factor"] == 1500.0
    assert "share_class_factor_applied" in _codes(doc)


def test_sector_class_argument_overrides_sic(real):
    doc = FF.run("KO", price=REAL_PRICES["KO"], sector_class="financials")
    assert doc["entity"]["sector_class"] == "financials"
    assert _reason(doc, "/valuation/ev") == "not_meaningful"


# =========================================================================== price resolution
def _fake_prices(monkeypatch, frame):
    mod = types.ModuleType("scripts.fetch_prices")
    mod.load_series = lambda ticker: frame
    monkeypatch.setitem(sys.modules, "scripts.fetch_prices", mod)
    monkeypatch.setattr(scripts, "fetch_prices", mod, raising=False)


def test_price_from_cached_series(real, monkeypatch):
    df = pd.DataFrame({"Close": [80.0, 84.0, 90.0]},
                      index=pd.to_datetime(["2026-09-30", "2026-10-01", "2026-10-05"]))
    _fake_prices(monkeypatch, df)
    doc = FF.run("KO")
    assert doc["price"]["value"] == 84.0 and doc["price"]["as_of"] == "2026-10-01"   # <= as_of 2026-10-02
    assert doc["market_cap"]["value"] is not None


def test_price_unavailable(real, monkeypatch, validator):
    _fake_prices(monkeypatch, None)
    doc = FF.run("KO")
    _assert_valid(validator, doc)
    assert doc["price"]["value"] is None and doc["market_cap"]["value"] is None
    assert any("price for KO unavailable" in m for m in doc["missing_data"])
    assert _reason(doc, "/valuation/pe") == "missing"
    assert doc["ttm"]["revenue"]["value"] is not None      # fundamentals still delivered


def test_price_import_error(real, monkeypatch):
    monkeypatch.setitem(sys.modules, "scripts.fetch_prices", None)
    monkeypatch.delattr(scripts, "fetch_prices", raising=False)
    doc = FF.run("KO")
    assert doc["price"]["value"] is None


def test_price_fact_argument(real):
    doc = FF.run("KO", price=common.fact(85.0, "USD/share", "D:PX:KO", "2026-10-01", "price"))
    assert doc["price"]["value"] == 85.0 and doc["price"]["as_of"] == "2026-10-01"


# =========================================================================== network path + cache
def _fixture_server(calls: list):
    index = json.loads((FX / "http" / "index.json").read_text(encoding="utf-8"))

    def fake(url, **kw):
        assert kw.get("sec") is True
        calls.append(url)
        return json.loads((FX / "http" / index[url]).read_text(encoding="utf-8"))
    return fake


def test_network_path_uses_cache(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(common, "http_get_json", _fixture_server(calls))
    monkeypatch.setattr(common, "yf_info", lambda t: {"marketCap": 368513318912})
    cache = common.DiskCache(tmp_path / "cache")
    doc1 = FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert len(calls) == 3 and doc1["coverage"] == "sec"
    for kind in (FF.KIND_MAP, FF.KIND_CF, FF.KIND_SUB):
        assert any((tmp_path / "cache" / kind).glob("*.json")), kind
    doc2 = FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert len(calls) == 3                                     # served from cache
    assert doc2["ttm"] == doc1["ttm"] and doc2["lag_detected"] is True


def test_stale_cache_fallback(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(common, "http_get_json", _fixture_server(calls))
    monkeypatch.setattr(common, "yf_info", lambda t: {"marketCap": 368513318912})
    cache = common.DiskCache(tmp_path / "cache")
    FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    for p in (tmp_path / "cache").rglob("*.json"):
        entry = json.loads(p.read_text(encoding="utf-8"))
        entry["fetched_at"] = old
        p.write_text(json.dumps(entry), encoding="utf-8")

    def down(url, **kw):
        raise common.DataUnavailable("HTTP 503", status_code=503, url=url)
    monkeypatch.setattr(common, "http_get_json", down)
    doc = FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert doc["coverage"] == "sec" and "stale_cache" in _codes(doc)
    assert next(f for f in doc["flags"] if f["code"] == "stale_cache")["severity"] == "high"


def test_offline_without_cache_degrades(validator):
    doc = FF.run("KO", price=REAL_PRICES["KO"])          # AGINV_OFFLINE=1, empty cache
    _assert_valid(validator, doc)
    assert doc["coverage"] == "partial" and "sec_unavailable" in _codes(doc)
    assert doc["ttm"]["revenue"]["value"] is None


def test_yahoo_crosscheck_unavailable_is_flagged(real, monkeypatch):
    def unavailable(t):
        raise common.OfflineError("offline")
    monkeypatch.setattr(common, "yf_info", unavailable)
    doc = FF.run("KO", price=REAL_PRICES["KO"])
    assert "mcap_crosscheck_unavailable" in _codes(doc)
    assert doc["market_cap"]["derivation"].startswith("shares(") and doc["market_cap"]["value"] > 0


def test_ticker_map_normalization():
    raw = {"0": {"cik_str": 1067983, "ticker": "BRK.B", "title": "Berkshire"},
           "1": {"cik_str": 21344, "ticker": "KO", "title": "Coca-Cola"}}
    m = FF.normalize_ticker_map(raw)
    assert m["BRK-B"]["cik"] == 1067983 and FF.normalize_ticker_map(m) == m
    assert FF.lookup_cik("brk.b", m) == (1067983, "Berkshire")
    assert FF.lookup_cik("NOPE", m) == (None, None)


# =========================================================================== CLI
def test_cli_writes_valid_file(real, validator, tmp_path, capsys):
    out = tmp_path / "fundamentals_JPM.json"
    rc = FF.main(["--ticker", "JPM", "--out", str(out), "--price", str(REAL_PRICES["JPM"]),
                  "--as-of", "2026-10-02"])
    assert rc == 0 and out.is_file()
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["ticker"] == "JPM" and summary["coverage"] == "sec"
    _assert_valid(validator, json.loads(out.read_text(encoding="utf-8")))


# =========================================================================== review fixes (pure build_payload)
def _mini_cf(facts: dict) -> dict:
    out: dict = {"cik": 1, "entityName": "MINI", "facts": {}}
    for tag, arr in facts.items():
        ns, name = (tag.split(":", 1) if ":" in tag else ("us-gaap", tag))
        unit = "shares" if ns == "dei" else "USD"
        out["facts"].setdefault(ns, {})[name] = {"units": {unit: arr}}
    return out


def _fx(val, end, filed, start=None, form="10-K", accn=None):
    d = {"end": end, "val": val, "accn": accn or f"acc-{filed}", "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def _flow3(fy, h25, h26):
    """FY2025 (10-K) + 6M2025 / 6M2026 (10-Q) of a flow tag."""
    return [_fx(fy, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25"),
            _fx(h25, "2025-06-30", "2025-08-01", "2025-01-01", "10-Q", accn="Q25"),
            _fx(h26, "2026-06-30", "2026-08-01", "2026-01-01", "10-Q", accn="Q26")]


PRETAX_TAG = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"


def _base_facts() -> dict:
    return {
        "Revenues": _flow3(1000, 400, 800),                                # TTM 1400 @ 2026-06-30
        "NetIncomeLoss": _flow3(100, 40, 80),                              # TTM 140
        "StockholdersEquity": [_fx(500, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
        "Goodwill": [_fx(50, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
        "dei:EntityCommonStockSharesOutstanding": [_fx(10, "2026-07-20", "2026-08-01", form="10-Q", accn="Q26")],
    }


def _build(facts: dict) -> dict:
    return FF.build_payload("ZZMM", cik=1, cf=_mini_cf(facts), sub=None, price=100.0, price_as_of="2026-09-30",
                            as_of=datetime(2026, 10, 2).date(), sector_class="default",
                            yf_cap=lambda t: (None, "offline"))


def test_margin_with_period_mismatched_inputs_is_null_and_flagged():
    facts = _base_facts()
    facts["GrossProfit"] = [_fx(600, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25")]   # 10-K only
    facts[PRETAX_TAG] = _flow3(200, 80, 160)                                                   # @ 2026-06-30
    facts["IncomeTaxExpenseBenefit"] = [_fx(30, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25")]
    built = _build(facts)
    pay = built["payload"]
    assert pay["ttm"]["revenue"]["value"] == 1400.0 and pay["ttm"]["gross_profit"]["value"] == 600.0
    g = pay["margins_pct"]["gross"]
    assert g["value"] is None and g["na_reason"] == "missing" and "periods differ" in g["derivation"]
    assert pay["margins_pct"]["net"]["value"] == pytest.approx(10.0)            # aligned: 140 / 1400
    codes = {f["code"]: f for f in built["flags"]}
    assert "period_mismatch" in codes and codes["period_mismatch"]["severity"] == "medium"
    assert "margins_pct.gross" in codes["period_mismatch"]["detail"]
    assert "valuation.tax_rate_used" in codes["period_mismatch"]["detail"]
    assert pay["valuation"]["tax_rate_used"]["value"] == pytest.approx(0.21)     # default, not 30/160
    assert "default_tax_rate" in codes
    assert any(m.startswith("margins_pct.gross:") for m in built["missing_data"])


def test_misaligned_gross_profit_replaced_by_aligned_revenue_minus_cogs():
    facts = _base_facts()
    facts["GrossProfit"] = [_fx(600, "2025-12-31", "2026-02-10", "2025-01-01", accn="K25")]
    facts["CostOfRevenue"] = _flow3(400, 170, 330)                          # TTM 560 @ 2026-06-30
    built = _build(facts)
    pay = built["payload"]
    gp = pay["ttm"]["gross_profit"]
    assert gp["value"] == pytest.approx(840.0) and gp["as_of"] == "2026-06-30"
    assert gp["derivation"].startswith("revenue-cogs")
    assert pay["margins_pct"]["gross"]["value"] == pytest.approx(60.0)
    assert pay["tag_map"]["gross_profit"]["derived"] == "revenue-cogs"
    assert pay["tag_map"]["gross_profit"]["ignored_tag"] == "GrossProfit"
    assert "period_mismatch" not in [f["code"] for f in built["flags"]]


def test_p_tbv_null_when_intangibles_missing_and_computed_from_parts():
    built = _build(_base_facts())                                          # no intangibles tag at all
    v = built["payload"]["valuation"]
    assert v["p_b"]["value"] == pytest.approx(1000.0 / 500.0)
    assert v["p_tbv"]["value"] is None and v["p_tbv"]["na_reason"] == "missing"
    assert any(m.startswith("valuation.p_tbv:") for m in built["missing_data"])

    facts = _base_facts()
    facts["IndefiniteLivedIntangibleAssetsExcludingGoodwill"] = [
        _fx(120, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    facts["FiniteLivedIntangibleAssetsNet"] = [_fx(80, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    built = _build(facts)
    pay = built["payload"]
    assert pay["balance"]["intangibles"]["value"] == 200.0
    assert pay["valuation"]["p_tbv"]["value"] == pytest.approx(1000.0 / (500 - 50 - 200))


# =========================================================================== debt-free issuers (PLTR gap)
def _walk_facts(node, path=""):
    if isinstance(node, dict):
        if common.is_fact(node):
            yield path, node
        for k, v in node.items():
            yield from _walk_facts(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_facts(v, f"{path}/{i}")


def _assert_no_dangling_ids(doc: dict) -> None:
    """A null Fact never cites a source id that is absent from sources[]."""
    known = {s["id"] for s in doc["sources"]}
    bad = [(p, f["source_id"]) for p, f in _walk_facts({k: v for k, v in doc.items() if k != "sources"})
           if f["source_id"] is not None and f["source_id"] not in known and f["value"] is None]
    assert not bad, bad[:5]


def _built_doc(built: dict) -> dict:
    return common.envelope(FF.SCHEMA, "2026-10-02", sources=built["sources"], missing_data=built["missing_data"],
                           flags=built["flags"], **built["payload"])


def _pltr_like_facts() -> dict:
    facts = _base_facts()
    facts["CashAndCashEquivalentsAtCarryingValue"] = [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    facts["OperatingIncomeLoss"] = _flow3(100, 40, 80)                         # TTM 140
    facts["AvailableForSaleSecuritiesDebtSecurities"] = [_fx(900, "2026-06-30", "2026-08-01", form="10-Q",
                                                             accn="Q26")]      # an ASSET, not debt
    facts["LongTermDebtNoncurrent"] = [_fx(50, "2021-12-31", "2022-02-15")]    # retired long ago
    return facts


def _edgar_trim(cf: dict) -> dict:
    return _edgar.trim_companyfacts(cf)


def test_debt_free_issuer_zero_debt_flagged_and_net_debt_is_minus_cash(validator):
    built = _build(_pltr_like_facts())
    pay = built["payload"]
    debt, nd = pay["balance"]["total_debt"], pay["balance"]["net_debt"]
    assert debt["value"] == 0.0 and debt["na_reason"] is None
    assert debt["derivation"] == "no debt-related XBRL tags reported -> 0"
    assert debt["as_of"] == "2026-06-30" and debt["filed"] == "2026-08-01" and "formula" not in debt
    assert nd["value"] == -300.0 and nd["as_of"] == "2026-06-30"             # 0 - cash 300 (no ST investments)
    assert pay["tag_map"]["total_debt"]["assumed_zero"] is True and pay["tag_map"]["total_debt"]["formula"] is None
    flag = next(f for f in built["flags"] if f["code"] == "debt_not_reported_assumed_zero")
    assert flag["severity"] == "medium"
    codes = [f["code"] for f in built["flags"]]
    assert "debt_unavailable" not in codes and "mixed_instants" not in codes
    assert not any(m.startswith("total_debt") for m in built["missing_data"])
    # the whole EV / net-debt chain now exists: EV = 1000 (10 sh x 100) + 0 - 300 = 700
    v = pay["valuation"]
    assert v["ev"]["value"] == 700.0 and v["ev_ebit"]["value"] == pytest.approx(700.0 / 140.0)
    assert v["roic_pct"]["value"] is not None                                # equity 500 + net_debt -300
    _assert_valid(validator, _built_doc(built))


def test_debt_free_zero_includes_short_term_investments_in_net_debt():
    facts = _pltr_like_facts()
    facts["MarketableSecuritiesCurrent"] = [_fx(700, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    pay = _build(facts)["payload"]
    assert pay["balance"]["net_debt"]["value"] == -1000.0                    # 0 - 300 - 700
    assert pay["valuation"]["ev"]["value"] == 0.0                            # 1000 + 0 - 300 - 700


def test_company_with_current_uncovered_debt_tag_stays_null():
    facts = _pltr_like_facts()
    facts["ConvertibleNotesPayable"] = [_fx(120, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]
    built = _build(facts)
    pay = built["payload"]
    assert pay["balance"]["total_debt"]["value"] is None and pay["balance"]["net_debt"]["value"] is None
    assert pay["valuation"]["ev"]["value"] is None and pay["valuation"]["ev_ebit"]["value"] is None
    codes = {f["code"]: f for f in built["flags"]}
    assert "debt_unavailable" in codes and "debt_not_reported_assumed_zero" not in codes
    assert "ConvertibleNotesPayable" in codes["debt_unavailable"]["detail"]
    assert any(m.startswith("total_debt") for m in built["missing_data"])


def test_trimmed_cache_payload_keeps_debt_tags_end_to_end():
    cf = _mini_cf({**_pltr_like_facts(),
                   "ConvertibleNotesPayable": [_fx(5, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    built = FF.build_payload("ZZMM", cik=1, cf=_edgar_trim(cf), sub=None, price=100.0, price_as_of="2026-09-30",
                             as_of=datetime(2026, 10, 2).date(), yf_cap=lambda t: (None, "offline"))
    assert built["payload"]["balance"]["total_debt"]["value"] is None        # ConvertibleNotesPayable survived the trim
    clean = FF.build_payload("ZZMM", cik=1, cf=_edgar_trim(_mini_cf(_pltr_like_facts())), sub=None, price=100.0,
                             price_as_of="2026-09-30", as_of=datetime(2026, 10, 2).date(),
                             yf_cap=lambda t: (None, "offline"))
    assert clean["payload"]["balance"]["total_debt"]["value"] == 0.0


def test_cache_entry_from_before_the_debt_scan_is_refetched(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(common, "http_get_json", _fixture_server(calls))
    monkeypatch.setattr(common, "yf_info", lambda t: {"marketCap": 368513318912})
    cache = common.DiskCache(tmp_path / "cache")
    FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert len(calls) == 3
    entry = cache.get(FF.KIND_CF, "CIK0000021344", None)
    assert entry.get("debt_scan") is True and entry.get("trim_rev") == _edgar.TRIM_REV   # new trims carry the markers
    legacy = {k: v for k, v in dict(entry).items() if k not in ("debt_scan", "trim_rev")}
    cache.put(FF.KIND_CF, "CIK0000021344", legacy, "https://data.sec.gov/x")
    calls.clear()
    FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert any("companyfacts" in u for u in calls)                           # a legacy trim is not trusted
    assert cache.get(FF.KIND_CF, "CIK0000021344", None).get("trim_rev") == _edgar.TRIM_REV
    # a trim from an older revision (it has the debt scan but not every tag read today) is refetched as well
    older = dict(cache.get(FF.KIND_CF, "CIK0000021344", None), trim_rev=_edgar.TRIM_REV - 1)
    cache.put(FF.KIND_CF, "CIK0000021344", older, "https://data.sec.gov/x")
    calls.clear()
    FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert any("companyfacts" in u for u in calls)


def test_legacy_cache_is_still_the_stale_fallback_but_never_assumes_zero_debt(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(common, "http_get_json", _fixture_server(calls))
    monkeypatch.setattr(common, "yf_info", lambda t: {"marketCap": 368513318912})
    cache = common.DiskCache(tmp_path / "cache")
    FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    entry = cache.get(FF.KIND_CF, "CIK0000021344", None)
    legacy = {k: v for k, v in dict(entry).items() if k not in ("debt_scan", "trim_rev")}
    legacy["facts"] = {ns: {k: v for k, v in tags.items() if not k.startswith(("LongTermDebt", "CommercialPaper"))}
                       for ns, tags in legacy["facts"].items()}              # as if KO had no formula tags
    cache.put(FF.KIND_CF, "CIK0000021344", legacy, "https://data.sec.gov/x")

    def down(url, **kw):
        if "companyfacts" in url:
            raise common.DataUnavailable("HTTP 503", status_code=503, url=url)
        return _fixture_server([])(url, **kw)
    monkeypatch.setattr(common, "http_get_json", down)
    doc = FF.run("KO", price=REAL_PRICES["KO"], cache=cache)
    assert "stale_cache" in _codes(doc)
    assert doc["balance"]["total_debt"]["value"] is None
    assert "debt_not_reported_assumed_zero" not in _codes(doc)
    assert "debt_unavailable" in _codes(doc)


# =========================================================================== null Facts cite no dangling ids
def test_non_sec_null_facts_have_no_dangling_source_ids(real, validator):
    doc = FF.run("LUMI.TA", price=None)
    _assert_valid(validator, doc)
    assert doc["coverage"] == "non_sec"
    assert [s["id"] for s in doc["sources"]] == ["D:SEC:MAP"]
    _assert_no_dangling_ids(doc)
    for path, f in _walk_facts({k: v for k, v in doc.items() if k != "sources"}):
        assert f["value"] is None and f["source_id"] is None and f["as_of"] is None, path


def test_non_sec_price_fact_keeps_its_defined_price_source(real, validator):
    doc = FF.run("TEVA.TA", price=12.5, price_as_of="2026-10-01")
    _assert_valid(validator, doc)
    assert doc["price"]["value"] == 12.5 and doc["price"]["source_id"] == "D:PX:TEVA.TA"
    assert "D:PX:TEVA.TA" in {s["id"] for s in doc["sources"]}
    assert doc["shares"]["source_id"] is None and doc["balance"]["cash"]["source_id"] is None
    _assert_no_dangling_ids(doc)


def test_sec_doc_without_price_has_null_price_source(real, monkeypatch, validator):
    _fake_prices(monkeypatch, None)
    doc = FF.run("KO")
    _assert_valid(validator, doc)
    assert doc["price"]["value"] is None and doc["price"]["source_id"] is None
    assert doc["ttm"]["revenue"]["source_id"] == "D:SEC:KO:CF"                # SEC Facts keep their defined id
    _assert_no_dangling_ids(doc)


def test_offline_degraded_documents_have_no_dangling_ids(validator):
    doc = FF.run("KO", price=REAL_PRICES["KO"])                              # empty cache, offline
    _assert_valid(validator, doc)
    _assert_no_dangling_ids(doc)
    assert doc["price"]["source_id"] == "D:PX:KO"


# =========================================================================== live-data review fixes (C1 C3 C5 C8)
def _facts_with(extra: dict, *, drop=()) -> dict:
    f = _base_facts()
    for k in drop:
        f.pop(k, None)
    f.update(extra)
    return f


def test_payload_uses_the_newest_debt_formula_and_records_it(validator):
    facts = _facts_with({
        "CashAndCashEquivalentsAtCarryingValue": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
        "LongTermDebt": [_fx(129, "2025-12-31", "2026-02-10", accn="K25")],            # stale 10-K-only series
        "LongTermDebtNoncurrent": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
        "LongTermDebtCurrent": [_fx(40, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    built = _build(facts)
    pay = built["payload"]
    debt = pay["balance"]["total_debt"]
    assert debt["value"] == 340.0 and debt["formula"] == "b" and debt["as_of"] == "2026-06-30"
    assert pay["balance"]["net_debt"]["value"] == 40.0                      # 340 - cash 300
    assert pay["tag_map"]["total_debt"]["formula"] == "b"
    assert "debt_current_portion_missing" not in [f["code"] for f in built["flags"]]
    _assert_valid(validator, _built_doc(built))


def test_payload_flags_a_noncurrent_leg_used_alone():
    facts = _facts_with({
        "CashAndCashEquivalentsAtCarryingValue": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
        "LongTermDebtNoncurrent": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    built = _build(facts)
    assert built["payload"]["balance"]["total_debt"]["value"] == 300.0
    flag = next(f for f in built["flags"] if f["code"] == "debt_current_portion_missing")
    assert flag["severity"] == "medium"
    assert built["payload"]["tag_map"]["total_debt"]["current_portion_missing"] is True


def _retail_facts(extra: dict | None = None) -> dict:
    """52-week calendar (12/12/12/16-week quarters): FY ends 2026-01-31, latest 10-Q = 36-week 9M to 2026-10-09."""
    def trio(fy, h25, h26, nine25, nine26):
        return [_fx(fy, "2026-01-31", "2026-03-20", "2025-02-02", accn="K"),
                _fx(h25, "2025-07-19", "2025-08-20", "2025-02-02", "10-Q", accn="Q2a"),
                _fx(h26, "2026-07-18", "2026-08-20", "2026-02-01", "10-Q", accn="Q2b"),
                _fx(nine25, "2025-10-11", "2025-11-20", "2025-02-02", "10-Q", accn="Q3a"),
                _fx(nine26, "2026-10-09", "2026-11-20", "2026-02-01", "10-Q", accn="Q3b")]
    facts = {"Revenues": trio(1000, 450, 500, 700, 760),
             "NetIncomeLoss": trio(100, 45, 50, 70, 76),
             "OperatingIncomeLoss": trio(150, 66, 75, 105, 114),
             "StockholdersEquity": [_fx(500, "2026-10-09", "2026-11-20", form="10-Q", accn="Q3b")],
             "dei:EntityCommonStockSharesOutstanding": [_fx(10, "2026-11-10", "2026-11-20", form="10-Q", accn="Q3b")]}
    facts.update(extra or {})
    return facts


def test_52_week_calendar_company_gets_ttm_from_the_36_week_period(validator):
    built = FF.build_payload("ZZRT", cik=1, cf=_mini_cf(_retail_facts()), sub=None, price=100.0, price_as_of="2026-11-12",
                             as_of=datetime(2026, 11, 25).date(), yf_cap=lambda t: (None, "offline"))
    ttm = built["payload"]["ttm"]
    assert ttm["revenue"]["value"] == 1060.0 and ttm["revenue"]["as_of"] == "2026-10-09"
    assert ttm["revenue"]["derivation"] == "FY2026+9M2026-9M2025"
    assert ttm["net_income"]["value"] == 106.0 and ttm["operating_income"]["value"] == 159.0
    assert built["payload"]["margins_pct"]["operating"]["value"] == pytest.approx(159 / 1060 * 100)
    assert not any(m.startswith("ttm.revenue") for m in built["missing_data"])


def test_missing_operating_income_is_never_a_silent_null():
    facts = _facts_with({"CashAndCashEquivalentsAtCarryingValue":
                         [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    built = _build(facts)                         # NKE / JNJ style: no OperatingIncomeLoss, no D&A, no capex
    pay = built["payload"]
    assert pay["ttm"]["operating_income"]["value"] is None and pay["valuation"]["ev_ebit"]["value"] is None
    joined = " | ".join(built["missing_data"])
    for key in ("ttm.operating_income", "ttm.gross_profit", "ttm.da", "ttm.capex"):
        assert any(m.startswith(key + ":") for m in built["missing_data"]), (key, joined)
    assert "EV/EBIT" in next(m for m in built["missing_data"] if m.startswith("ttm.operating_income:"))
    assert any(m.startswith("total_debt") for m in built["missing_data"]) or \
        pay["balance"]["total_debt"]["value"] == 0.0      # debt either explained or (debt-free) assumed zero


def test_financials_not_meaningful_nulls_do_not_add_missing_entries():
    built = FF.build_payload("ZZBK", cik=1, cf=_mini_cf(_base_facts()), sub=None, price=100.0, price_as_of="2026-09-30",
                             as_of=datetime(2026, 10, 2).date(), sector_class="financials",
                             yf_cap=lambda t: (None, "offline"))
    assert not any(m.startswith(("ttm.operating_income", "ttm.gross_profit")) for m in built["missing_data"])


def test_da_from_depreciation_plus_amortization_reaches_ebitda_and_tag_map():
    facts = _facts_with({"OperatingIncomeLoss": _flow3(100, 40, 80),
                         "Depreciation": _flow3(40, 15, 25), "AmortizationOfIntangibleAssets": _flow3(10, 4, 6)})
    pay = _build(facts)["payload"]
    assert pay["ttm"]["da"]["value"] == pytest.approx(62.0)                     # (40+25-15) + (10+6-4)
    assert pay["ttm"]["da"]["tag"] == "Depreciation+AmortizationOfIntangibleAssets"
    assert pay["ttm"]["ebitda"]["value"] == pytest.approx(140 + 62.0)           # operating income TTM 140 + D&A 62
    assert pay["tag_map"]["da"]["derived"] == "Depreciation+AmortizationOfIntangibleAssets"


def test_roic_not_meaningful_when_equity_is_not_positive_sbux_like():
    facts = _facts_with({"OperatingIncomeLoss": _flow3(200, 80, 160),
                         "CashAndCashEquivalentsAtCarryingValue": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
                         "LongTermDebt": [_fx(15_000, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
                         "StockholdersEquity": [_fx(-8_000, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    roic = _build(facts)["payload"]["valuation"]["roic_pct"]
    # equity + net_debt = -8000 + 14700 = 6700 > 0 would still produce a ratio, but negative book equity is not meaningful
    assert roic["value"] is None and roic["na_reason"] == "not_meaningful"
    assert "equity <= 0" in roic["derivation"]


def test_roic_not_meaningful_when_invested_capital_is_not_positive():
    facts = _facts_with({"OperatingIncomeLoss": _flow3(200, 80, 160),
                         "CashAndCashEquivalentsAtCarryingValue": [_fx(900, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
                         "LongTermDebt": [_fx(100, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    roic = _build(facts)["payload"]["valuation"]["roic_pct"]           # equity 500 + net_debt -800 = -300
    assert roic["value"] is None and roic["na_reason"] == "not_meaningful"
    assert "equity + net_debt <= 0" in roic["derivation"]


def test_roic_still_computed_for_positive_invested_capital():
    facts = _facts_with({"OperatingIncomeLoss": _flow3(200, 80, 160),
                         "CashAndCashEquivalentsAtCarryingValue": [_fx(100, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")],
                         "LongTermDebt": [_fx(300, "2026-06-30", "2026-08-01", form="10-Q", accn="Q26")]})
    roic = _build(facts)["payload"]["valuation"]["roic_pct"]           # EBIT 280 x (1-0.21) / (500 + 200)
    assert roic["value"] == pytest.approx(280 * 0.79 / 700 * 100, rel=1e-6)
