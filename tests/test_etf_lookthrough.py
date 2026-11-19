"""etf_lookthrough.py (DESIGN §4.6): classification order, fund decomposition (Yahoo → overrides →
unclassified), shares/weight/cash weighting, proxy pricing, sector weights summing to 100, top-10
concentration and overlap math. Offline: Yahoo data comes from tests/fixtures/lookthrough
(SYNTHETIC values in the normalized ``yf_funds`` / raw ``info`` shapes)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from conftest import SAMPLE_PORTFOLIO, make_ohlcv
from scripts import common, etf_lookthrough as lt

REPO_ROOT = Path(__file__).resolve().parents[1]
LT_FIX = REPO_ROOT / "tests" / "fixtures" / "lookthrough"
AS_OF = "2026-10-02"
PRICES = {"QQQ": 600.0, "SPMO": 100.0, "BRK-B": 500.0, "KO": 70.0, "PG": 160.0, "JPM": 300.0,
          "AAPL": 200.0, "ODDF": 50.0, "OVER": 10.0, "FBX": 20.0, "NOFUND": 30.0, "XYZ": 40.0}
QQQ_SPLIT = {"information_technology": 50, "communication_services": 16, "consumer_discretionary": 13,
             "health_care": 6, "consumer_staples": 5, "industrials": 5, "utilities": 1.5, "materials": 1.5,
             "financials": 0.5, "energy": 0.5, "real_estate": 1.0}
SPMO_SPLIT = {"information_technology": 30, "financials": 20, "communication_services": 15, "industrials": 12,
              "consumer_discretionary": 8, "health_care": 7, "consumer_staples": 4, "energy": 2, "utilities": 1,
              "real_estate": 0.5, "materials": 0.5}


# --------------------------------------------------------------------------- helpers
def _schema_errors(doc: dict, name: str) -> list[str]:
    resources = []
    for p in sorted((REPO_ROOT / "schemas").glob("*.json")) + sorted((REPO_ROOT / "schemas" / "data").glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        resources.append((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)))
    validator = Draft202012Validator(common.load_schema(name), registry=Registry().with_resources(resources),
                                     format_checker=FormatChecker())
    plain = json.loads(common.dumps_json(doc))
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(plain)]


def _prices(tickers=None, *, day: str = "2026-10-01") -> dict:
    """A prices.json-shaped payload (only the keys the look-through reads)."""
    tick = {}
    for t, px in PRICES.items():
        if tickers is not None and t not in tickers:
            continue
        tick[t] = {"last_close": common.fact(px, "USD/share", f"D:PX:{t}", day, "price"), "last_date": day}
    return {"schema": "prices/v1", "sources": [common.dataset_source(f"D:PX:{t}") for t in tick], "tickers": tick}


def _holding(ticker, asset_type=None, *, shares=None, weight_pct=None, proxy=None, sector_override=None):
    return {"ticker": ticker, "asset_type": asset_type, "shares": shares, "weight_pct": weight_pct,
            "cost_basis": None, "cost_basis_currency": "USD", "purchase_date": None, "currency": "USD",
            "proxy_ticker": proxy, "sector_override": sector_override}


def _portfolio(holdings, *, cash_usd=None, cash_pct=None, total=None):
    return {"as_of": "2026-10-01", "total_value_usd": total, "cash_usd": cash_usd, "cash_pct": cash_pct,
            "holdings": holdings}


def _by_ticker(payload) -> dict:
    return {h["ticker"]: h for h in payload["holdings"]}


@pytest.fixture
def yf(use_fixtures):
    return use_fixtures(LT_FIX)


# --------------------------------------------------------------------------- classify_security
def test_brk_b_override_conglomerate_without_network():
    c = lt.classify_security("brk.b")  # repo etf_overrides.yaml; offline -> no info needed
    assert c == {"sector": "financials", "asset_type": "stock", "sector_class": "financials",
                 "classification_source": "etf_overrides", "flags": ["conglomerate"]}


def test_portfolio_sector_override_beats_etf_overrides():
    c = lt.classify_security("BRK-B", sector_override="industrials")
    assert c["sector"] == "industrials" and c["classification_source"] == "portfolio_override"
    assert "conglomerate" in c["flags"]  # the override entry's flags still apply


def test_yahoo_sector_sic_and_unclassified_order():
    assert lt.classify_security("AAA", info={"quoteType": "EQUITY", "sector": "Technology"}, overrides={}) == {
        "sector": "information_technology", "asset_type": "stock", "sector_class": "default",
        "classification_source": "yfinance_info", "flags": []}
    c = lt.classify_security("BBB", info={"quoteType": "EQUITY"}, sic=2080, overrides={})
    assert (c["sector"], c["classification_source"]) == ("consumer_staples", "sic")
    c = lt.classify_security("CCC", info={"sector": "Widgets"}, overrides={})
    assert c["sector"] == "unclassified" and c["classification_source"] == "unclassified"
    assert set(c["flags"]) == {"unmapped_yahoo_sector", "unclassified"}
    c = lt.classify_security("DDD", info={}, sic=9999, overrides={})  # unmapped SIC
    assert c["sector"] == "unclassified"


def test_fund_detection_and_explicit_asset_type():
    c = lt.classify_security("ZZZ", info={"quoteType": "ETF"}, overrides={})
    assert c == {"sector": "funds", "asset_type": "fund", "sector_class": "fund",
                 "classification_source": "yfinance_info", "flags": []}
    c = lt.classify_security("QQQ", asset_type="fund", overrides={})
    assert c["sector"] == "funds" and c["classification_source"] == "portfolio"


def test_lazy_info_fetch_via_fixture(yf):
    c = lt.classify_security("KO", overrides={})
    assert (c["sector"], c["classification_source"]) == ("consumer_staples", "yfinance_info")
    assert lt.classify_security("MYETF", overrides={})["asset_type"] == "fund"
    # info is cached (kind "info") after the first call
    assert common.DiskCache().path_for("info", "info_KO").is_file()


def test_info_failure_falls_through_offline():
    c = lt.classify_security("KO", overrides={})  # offline, no fixtures, no cache
    assert c["sector"] == "unclassified" and "unclassified" in c["flags"]
    assert lt.classify_security("KO", overrides={}, sic=2086)["sector"] == "consumer_staples"


# --------------------------------------------------------------------------- pure helpers
def test_round_buckets_puts_residual_in_largest_bucket():
    third = 100.0 / 3
    out = lt.round_buckets({"information_technology": third, "health_care": third, "financials": third})
    assert out == {"information_technology": 33.34, "health_care": 33.33, "financials": 33.33}
    assert sum(out.values()) == pytest.approx(100.0, abs=1e-9)
    out = lt.round_buckets({"energy": 1 / 7, "cash": 2 / 7, "unclassified": 4 / 7})  # scaled to 100
    assert sum(out.values()) == pytest.approx(100.0, abs=0.01)
    assert out["unclassified"] == pytest.approx(57.14, abs=0.011)
    assert lt.round_buckets({"cash": 0.0})["unclassified"] == 100.0


def test_normalize_split_rules():
    split, flags = lt.normalize_split({"information_technology": 60, "health_care": 39.6})  # within 1 pp
    assert sum(split.values()) == pytest.approx(100.0, abs=1e-3) and "unclassified" not in split
    assert flags == []
    split, flags = lt.normalize_split({"information_technology": 60, "health_care": 30})  # residual
    assert split == {"information_technology": 60.0, "health_care": 30.0, "unclassified": 10.0}
    assert [f["code"] for f in flags] == ["fund_weights_residual"]
    split, flags = lt.normalize_split({"information_technology": 70, "health_care": 50})  # over 100
    assert split["information_technology"] == pytest.approx(58.3333) and split["health_care"] == pytest.approx(41.6667)
    assert [f["code"] for f in flags] == ["fund_weights_over_100"]
    assert lt.normalize_split({}) == (None, [])


def test_overlap_math_by_hand():
    a = [{"symbol": "X", "weight_pct": 10.0}, {"symbol": "Y", "weight_pct": 4.0}, {"symbol": "Z", "weight_pct": 1.0}]
    b = [{"symbol": "Y", "weight_pct": 6.0}, {"symbol": "Z", "weight_pct": 3.0}, {"symbol": "W", "weight_pct": 9.0}]
    pct, syms = lt.pair_overlap(a, b)
    assert pct == pytest.approx(4.0 + 1.0) and syms == ["Y", "Z"]
    assert lt.pair_overlap(a, []) == (None, [])
    ov = lt.compute_overlap({"F1": {"top10": a}, "F2": {"top10": b}}, {"Y": 2.0}, fund_weights={"F1": 50.0, "F2": 20.0})
    assert ov["pairs"] == [{"a": "F1", "b": "F2", "overlap_pct": 5.0, "basis": "top10", "common": ["Y", "Z"]}]
    assert ov["direct_overlap"] == [
        {"fund": "F1", "holding": "Y", "fund_weight_pct": 4.0, "implied_portfolio_pct": 2.0, "basis": "top10"},
        {"fund": "F2", "holding": "Y", "fund_weight_pct": 6.0, "implied_portfolio_pct": 1.2, "basis": "top10"}]


# --------------------------------------------------------------------------- full run
def test_sample_portfolio_by_hand(yf, tmp_path):
    out = tmp_path / "phase1" / "lookthrough.json"
    payload = lt.run(out, portfolio=copy.deepcopy(SAMPLE_PORTFOLIO), prices=_prices(), as_of=AS_OF)
    # values: QQQ 40x600=24000, SPMO 150x100=15000, BRK-B 20x500=10000, KO 100x70=7000,
    #         PG 40x160=6400, JPM 30x300=9000, cash 5000 -> total 76400
    total = 76400.0
    assert payload["total_value_usd"]["value"] == pytest.approx(total)
    h = _by_ticker(payload)
    assert h["QQQ"]["weight_pct"]["value"] == pytest.approx(round(24000 / total * 100, 4))
    assert h["KO"]["weight_pct"]["value"] == pytest.approx(round(7000 / total * 100, 4))
    assert h["KO"]["weight_pct"]["source_id"] == "D:PX:KO" and h["KO"]["weight_pct"]["as_of"] == "2026-10-01"
    assert payload["cash"]["weight_pct"]["value"] == pytest.approx(round(5000 / total * 100, 4))
    # classification
    assert (h["BRK-B"]["sector"], h["BRK-B"]["classification_source"], h["BRK-B"]["flags"]) == (
        "financials", "etf_overrides", ["conglomerate"])
    assert (h["KO"]["sector"], h["KO"]["classification_source"]) == ("consumer_staples", "yfinance_info")
    assert (h["JPM"]["sector"], h["JPM"]["sector_class"]) == ("financials", "financials")
    assert h["QQQ"]["sector"] == "funds" and h["QQQ"]["composition_source"] == "yfinance"
    assert h["QQQ"]["sector_split"] == pytest.approx(QQQ_SPLIT)
    assert h["KO"]["sector_split"] == {"consumer_staples": 100.0}
    # sector look-through, by hand (dollars)
    sw = payload["sector_weights_pct"]
    assert sum(sw.values()) == pytest.approx(100.0, abs=0.01)
    exp = {
        "information_technology": (24000 * .50 + 15000 * .30) / total * 100,
        "financials": (10000 + 9000 + 24000 * .005 + 15000 * .20) / total * 100,
        "consumer_staples": (7000 + 6400 + 24000 * .05 + 15000 * .04) / total * 100,
        "communication_services": (24000 * .16 + 15000 * .15) / total * 100,
        "cash": 5000 / total * 100,
        "unclassified": 0.0,
    }
    for k, v in exp.items():
        assert sw[k] == pytest.approx(v, abs=0.011), k
    assert set(sw) == set(lt.BUCKETS)
    # funds block: expense ratio, top10 (sorted, normalized), concentration
    q, s = payload["funds"]["QQQ"], payload["funds"]["SPMO"]
    assert q["expense_ratio_pct"]["value"] == pytest.approx(0.20) and q["source_id"] == "D:YF:QQQ:FUND"
    assert q["top10_concentration_pct"]["value"] == pytest.approx(51.0)
    assert [x["symbol"] for x in s["top10"]][:3] == ["NVDA", "META", "AVGO"] and len(s["top10"]) == 10
    assert "BRK-B" in [x["symbol"] for x in s["top10"]] and "ORCL" not in [x["symbol"] for x in s["top10"]]
    assert s["top10_concentration_pct"]["value"] == pytest.approx(46.6)
    # overlap QQQ/SPMO: min over NVDA 8.0, META 4.8, AVGO 5.0, AMZN 4.5, MSFT 4.0, COST 2.4 = 28.7
    pair = payload["overlap"]["pairs"][0]
    assert (pair["a"], pair["b"], pair["basis"]) == ("QQQ", "SPMO", "top10")
    assert pair["overlap_pct"] == pytest.approx(28.7)
    direct = {(d["fund"], d["holding"]): d for d in payload["overlap"]["direct_overlap"]}
    assert set(direct) == {("SPMO", "JPM"), ("SPMO", "BRK-B")}
    assert direct[("SPMO", "JPM")]["fund_weight_pct"] == pytest.approx(5.0)
    assert direct[("SPMO", "BRK-B")]["implied_portfolio_pct"] == pytest.approx(
        round(15000 / total * 100, 4) * 3.8 / 100, abs=1e-4)
    # provenance + schema + file
    ids = {src["id"] for src in payload["sources"]}
    assert {"D:PX:KO", "D:YF:QQQ:FUND", "D:YF:SPMO:FUND", "D:YF:KO:INFO"} <= ids
    assert not _schema_errors(payload, "lookthrough")
    assert common.read_json(out)["sector_weights_pct"] == sw


@pytest.mark.parametrize("seed", range(6))
def test_sector_weights_sum_to_100_for_varied_portfolios(yf, seed):
    import random
    rng = random.Random(seed)
    pool = [("QQQ", "fund"), ("SPMO", "fund"), ("ODDF", "fund"), ("OVER", "fund"), ("KO", "stock"),
            ("PG", "stock"), ("JPM", "stock"), ("BRK-B", "stock"), ("AAPL", "stock"), ("XYZ", "stock")]
    picks = rng.sample(pool, rng.randint(1, len(pool)))
    holdings = [_holding(t, at, shares=rng.randint(1, 500)) for t, at in picks]
    cash = rng.choice([None, 0.0, 1234.5, 99999.0])
    payload = lt.run(portfolio=_portfolio(holdings, cash_usd=cash), prices=_prices(), as_of=AS_OF, overrides=lt.load_overrides())
    sw = payload["sector_weights_pct"]
    assert sum(sw.values()) == pytest.approx(100.0, abs=0.01)
    assert all(v >= 0 for v in sw.values())
    weights = [h["weight_pct"]["value"] for h in payload["holdings"]] + [payload["cash"]["weight_pct"]["value"]]
    assert sum(weights) == pytest.approx(100.0, abs=0.01)
    assert not _schema_errors(payload, "lookthrough")


def test_weight_based_portfolio_with_cash_pct(yf):
    p = _portfolio([_holding("QQQ", "fund", weight_pct=40), _holding("KO", "stock", weight_pct=30),
                    _holding("JPM", "stock", weight_pct=25)], cash_pct=5, total=100000)
    payload = lt.run(portfolio=p, prices=None, as_of=AS_OF)
    h = _by_ticker(payload)
    assert h["QQQ"]["weight_pct"]["value"] == pytest.approx(40.0) and h["QQQ"]["input"] == "weight_pct"
    assert h["KO"]["value_usd"]["value"] == pytest.approx(30000.0)
    sw = payload["sector_weights_pct"]
    assert sw["cash"] == pytest.approx(5.0)
    assert sw["information_technology"] == pytest.approx(20.0)        # 40 x 50%
    assert sw["consumer_staples"] == pytest.approx(30.0 + 2.0)        # KO + 40 x 5%
    assert sw["financials"] == pytest.approx(25.0 + 0.2)              # JPM + 40 x 0.5%
    assert sw["unclassified"] == 0.0
    assert payload["total_value_usd"]["value"] == pytest.approx(100000.0)
    assert not _schema_errors(payload, "lookthrough")


def test_weight_only_without_total_uses_weights_directly(yf):
    p = _portfolio([_holding("KO", "stock", weight_pct=60), _holding("JPM", "stock", weight_pct=30)], cash_pct=4)
    payload = lt.run(portfolio=p, prices=None, as_of=AS_OF)
    sw = payload["sector_weights_pct"]
    assert (sw["consumer_staples"], sw["financials"], sw["cash"], sw["unclassified"]) == (60.0, 30.0, 4.0, 6.0)
    assert payload["total_value_usd"]["value"] is None
    assert "unallocated_residual" in {f["code"] for f in payload["flags"]}
    assert not _schema_errors(payload, "lookthrough")


def test_mixed_shares_and_weights_residual_goes_to_unclassified(yf):
    p = _portfolio([_holding("QQQ", "fund", weight_pct=40), _holding("KO", "stock", shares=100)],
                   cash_usd=3000, total=100000)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
    h = _by_ticker(payload)
    assert h["KO"]["weight_pct"]["value"] == pytest.approx(7.0)      # 100 x 70 / 100000
    sw = payload["sector_weights_pct"]
    assert sw["cash"] == pytest.approx(3.0)
    assert sw["unclassified"] == pytest.approx(50.0)                 # 100 - 40 - 7 - 3
    assert "unallocated_residual" in {f["code"] for f in payload["flags"]}
    assert any("not attributed" in m for m in payload["missing_data"])
    assert sum(sw.values()) == pytest.approx(100.0, abs=0.01)


def test_cash_pct_with_shares(yf):
    p = _portfolio([_holding("KO", "stock", shares=100)], cash_pct=30)  # 7000 = 70% -> total 10000
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
    assert payload["total_value_usd"]["value"] == pytest.approx(10000.0)
    assert payload["cash"]["value_usd"]["value"] == pytest.approx(3000.0)
    assert payload["sector_weights_pct"]["cash"] == pytest.approx(30.0)
    assert payload["sector_weights_pct"]["consumer_staples"] == pytest.approx(70.0)


def test_proxy_ticker_prices_and_decomposes_via_proxy(yf):
    p = _portfolio([_holding("1159250.TA", "fund", shares=10, proxy="QQQ"), _holding("KO", "stock", shares=100)],
                   cash_usd=0)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
    h = _by_ticker(payload)["1159250.TA"]
    assert h["price_ticker"] == "QQQ" and h["proxy_ticker"] == "QQQ"
    assert h["value_usd"]["value"] == pytest.approx(6000.0)
    assert h["weight_pct"]["source_id"] == "D:PX:QQQ"
    assert h["sector_split"] == pytest.approx(QQQ_SPLIT) and "proxy_priced" in h["flags"]
    assert payload["funds"]["1159250.TA"]["source_id"] == "D:YF:QQQ:FUND"
    assert payload["sector_weights_pct"]["information_technology"] == pytest.approx(6000 * .5 / 13000 * 100, abs=0.01)
    assert "proxy_ticker" in {f["code"] for f in payload["flags"]}
    assert not _schema_errors(payload, "lookthrough")


def test_fund_falls_back_to_overrides_then_unclassified(yf):
    overrides = {"securities": {}, "funds": {
        "FBX": {"sector_weights": {"industrials": 60, "materials": 40},
                "source_url": "https://www.issuer.test/fbx-factsheet.pdf", "as_of": "2026-09-30",
                "expense_ratio_pct": 0.1,
                "top_holdings": [{"symbol": "GE", "weight_pct": 7.0}, {"symbol": "LIN", "weight_pct": 6.0}]},
        "NOSRC": {"sector_weights": {"energy": 100}}}}
    p = _portfolio([_holding("FBX", "fund", shares=100), _holding("NOFUND", "fund", shares=100),
                    _holding("NOSRC", "fund", shares=100)], cash_usd=0)
    prices = _prices()
    prices["tickers"]["NOSRC"] = {"last_close": common.fact(30.0, "USD/share", "D:PX:NOSRC", "2026-10-01", "price")}
    payload = lt.run(portfolio=p, prices=prices, as_of=AS_OF, overrides=overrides)
    h = _by_ticker(payload)
    fbx = payload["funds"]["FBX"]
    assert h["FBX"]["composition_source"] == "etf_overrides"
    assert h["FBX"]["sector_split"] == {"industrials": 60.0, "materials": 40.0}
    assert fbx["source_id"] == "D:CFG:etf_overrides.funds.FBX"
    assert fbx["expense_ratio_pct"]["value"] == pytest.approx(0.1) and fbx["expense_ratio_pct"]["as_of"] == "2026-09-30"
    assert fbx["top10_concentration_pct"]["value"] == pytest.approx(13.0)
    src = next(s for s in payload["sources"] if s["id"] == "D:CFG:etf_overrides.funds.FBX")
    assert src["url"] == "https://www.issuer.test/fbx-factsheet.pdf"
    # no Yahoo data and no override -> 100% unclassified + missing_data
    assert h["NOFUND"]["sector_split"] == {"unclassified": 100.0}
    assert h["NOFUND"]["composition_source"] == "none"
    assert any(m.startswith("NOFUND: fund sector composition unavailable") for m in payload["missing_data"])
    # an override without source_url/as_of is ignored
    assert h["NOSRC"]["sector_split"] == {"unclassified": 100.0}
    assert "override_unsourced" in {f["code"] for f in payload["flags"]}
    sw = payload["sector_weights_pct"]  # FBX 2000, NOFUND 3000, NOSRC 3000 -> total 8000
    assert sw["industrials"] == pytest.approx(2000 * .6 / 8000 * 100, abs=0.01)
    assert sw["unclassified"] == pytest.approx(6000 / 8000 * 100, abs=0.01)
    assert sum(sw.values()) == pytest.approx(100.0, abs=0.01)
    assert not _schema_errors(payload, "lookthrough")


def test_fund_sector_override_is_last_resort(yf):
    p = _portfolio([_holding("NOFUND", "fund", shares=10, sector_override="health_care")], cash_usd=0)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF, overrides={})
    h = _by_ticker(payload)["NOFUND"]
    assert h["sector"] == "funds" and h["sector_split"] == {"health_care": 100.0}
    assert h["composition_source"] == "sector_override"


def test_unclassified_residual_and_unmapped_key_and_over_100(yf):
    p = _portfolio([_holding("ODDF", "fund", shares=100), _holding("OVER", "fund", shares=500)], cash_usd=0)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF, overrides={})
    h = _by_ticker(payload)
    assert h["ODDF"]["sector_split"] == {"information_technology": 60.0, "health_care": 30.0, "unclassified": 10.0}
    assert h["OVER"]["sector_split"]["information_technology"] == pytest.approx(58.3333)
    codes = {f["code"] for f in payload["flags"]}
    assert {"fund_weights_residual", "unmapped_fund_sector_key", "fund_weights_over_100"} <= codes
    sw = payload["sector_weights_pct"]  # ODDF 5000, OVER 5000
    assert sw["unclassified"] == pytest.approx(5.0)
    assert sw["information_technology"] == pytest.approx(30.0 + 29.1667, abs=0.01)
    assert any(m.startswith("ODDF: expense ratio unavailable") for m in payload["missing_data"])
    # ODDF has no top holdings -> overlap pair is null with a note
    assert payload["overlap"]["pairs"][0]["overlap_pct"] is None
    assert not _schema_errors(payload, "lookthrough")


def test_missing_price_uses_cached_series_else_missing_data(yf, monkeypatch):
    series = make_ohlcv(n=60, seed=3, end="2026-09-30")
    monkeypatch.setattr(lt, "_load_series", lambda t: series if t == "PG" else None)
    p = _portfolio([_holding("KO", "stock", shares=100), _holding("PG", "stock", shares=10),
                    _holding("AAPL", "stock", shares=5)], cash_usd=0)
    payload = lt.run(portfolio=p, prices=_prices(["KO"]), as_of=AS_OF)
    h = _by_ticker(payload)
    last = float(series["Close"].iloc[-1])
    assert h["PG"]["price"]["value"] == pytest.approx(last) and h["PG"]["price"]["as_of"] == "2026-09-30"
    assert h["PG"]["value_usd"]["value"] == pytest.approx(10 * last)
    assert h["AAPL"]["weight_pct"]["value"] is None
    assert any(m.startswith("AAPL: no price") for m in payload["missing_data"])
    codes = {f["code"] for f in payload["flags"]}
    assert {"holding_unpriced", "price_from_cached_series", "weights_unreliable"} <= codes
    # AAPL's value is unknown and no total_value_usd is declared: the priced subset must NOT be
    # renormalized to 100% (that would overstate KO/PG) -> weights null, look-through unclassified
    assert h["KO"]["weight_pct"]["value"] is None and h["PG"]["weight_pct"]["value"] is None
    assert h["KO"]["value_usd"]["value"] == pytest.approx(7000.0)
    assert payload["total_value_usd"]["value"] is None
    assert payload["sector_weights_pct"]["unclassified"] == pytest.approx(100.0)
    assert payload["sector_weights_pct"]["consumer_staples"] == 0.0
    assert any(m.startswith("portfolio total unknown (no value for AAPL") for m in payload["missing_data"])
    assert not _schema_errors(payload, "lookthrough")


def test_unpriced_holding_with_declared_total_goes_to_unclassified(yf):
    # verifier repro: KO 100 @70 + NOPX 1000 shares with no price, declared total 100000
    p = _portfolio([_holding("KO", "stock", shares=100), _holding("NOPX", "stock", shares=1000, sector_override="energy")],
                   total=100000)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF, overrides={})
    h = _by_ticker(payload)
    assert payload["total_value_usd"]["value"] == pytest.approx(100000.0)
    assert h["KO"]["weight_pct"]["value"] == pytest.approx(7.0)        # not 100%
    assert h["NOPX"]["weight_pct"]["value"] is None
    sw = payload["sector_weights_pct"]
    assert sw["consumer_staples"] == pytest.approx(7.0) and sw["unclassified"] == pytest.approx(93.0)
    assert sw["energy"] == 0.0
    codes = {f["code"] for f in payload["flags"]}
    assert {"holding_unpriced", "unvalued_holdings_unclassified"} <= codes
    assert "declared_total_ignored" not in codes
    assert any("no value for: NOPX" in m for m in payload["missing_data"])
    assert not _schema_errors(payload, "lookthrough")


def test_unpriced_holding_with_cash_pct_and_declared_total(yf):
    p = _portfolio([_holding("KO", "stock", shares=100), _holding("AAPL", "stock", shares=10)], cash_pct=10, total=20000)
    payload = lt.run(portfolio=p, prices=_prices(["KO"]), as_of=AS_OF)
    sw = payload["sector_weights_pct"]
    assert _by_ticker(payload)["KO"]["weight_pct"]["value"] == pytest.approx(35.0)   # 7000 / 20000
    assert (sw["cash"], sw["consumer_staples"], sw["unclassified"]) == pytest.approx((10.0, 35.0, 55.0))
    assert payload["cash"]["value_usd"]["value"] == pytest.approx(2000.0)


def test_declared_total_below_priced_value_with_unpriced_holding_is_unreliable(yf):
    p = _portfolio([_holding("KO", "stock", shares=100), _holding("AAPL", "stock", shares=10)], cash_pct=5, total=1000)
    payload = lt.run(portfolio=p, prices=_prices(["KO"]), as_of=AS_OF)
    assert _by_ticker(payload)["KO"]["weight_pct"]["value"] is None
    sw = payload["sector_weights_pct"]
    assert (sw["cash"], sw["unclassified"]) == (5.0, 95.0)
    flag = next(f for f in payload["flags"] if f["code"] == "weights_unreliable")
    assert flag["severity"] == "high" and "below the priced holdings" in flag["detail"]
    assert not _schema_errors(payload, "lookthrough")


def test_weight_pct_over_100_without_total_rescales_holdings(yf):
    p = _portfolio([_holding("KO", "stock", weight_pct=70), _holding("JPM", "stock", weight_pct=50)])
    payload = lt.run(portfolio=p, prices=None, as_of=AS_OF)
    h = _by_ticker(payload)
    assert h["KO"]["weight_pct"]["value"] == pytest.approx(58.3333)
    assert h["JPM"]["weight_pct"]["value"] == pytest.approx(41.6667)
    assert "rescaled" in h["KO"]["weight_pct"]["derivation"]
    sw = payload["sector_weights_pct"]
    assert (sw["consumer_staples"], sw["financials"]) == pytest.approx((58.33, 41.67))
    assert sum(x["weight_pct"]["value"] for x in payload["holdings"]) == pytest.approx(100.0, abs=1e-3)
    assert "weights_over_100" in {f["code"] for f in payload["flags"]}
    # with cash_pct the cash weight is rescaled too
    p = _portfolio([_holding("KO", "stock", weight_pct=80), _holding("JPM", "stock", weight_pct=30)], cash_pct=10)
    payload = lt.run(portfolio=p, prices=None, as_of=AS_OF)
    assert payload["cash"]["weight_pct"]["value"] == pytest.approx(10 * 100 / 120, abs=1e-4)
    assert payload["sector_weights_pct"]["consumer_staples"] == pytest.approx(80 * 100 / 120, abs=0.01)
    assert not _schema_errors(payload, "lookthrough")


def test_sic_fallback_inside_run(yf):
    # no Yahoo info fixture for MSFT / FINX / NOSEC: classification falls through to SEC SIC
    prices = _prices(["KO"])
    for t, px in (("MSFT", 400.0), ("FINX", 50.0), ("NOSEC", 10.0)):
        prices["tickers"][t] = {"last_close": common.fact(px, "USD/share", f"D:PX:{t}", "2026-10-01", "price")}
    p = _portfolio([_holding("MSFT", shares=10), _holding("FINX", shares=20), _holding("NOSEC", shares=100),
                    _holding("KO", shares=10)], cash_usd=0)
    payload = lt.run(portfolio=p, prices=prices, as_of=AS_OF, overrides={})
    h = _by_ticker(payload)
    assert (h["MSFT"]["sector"], h["MSFT"]["classification_source"]) == ("information_technology", "sic")  # SIC 7372
    assert (h["FINX"]["sector"], h["FINX"]["sector_class"]) == ("financials", "financials")             # SIC 6022
    assert h["NOSEC"]["sector"] == "unclassified"
    msg = next(m for m in payload["missing_data"] if m.startswith("NOSEC: sector could not be classified"))
    assert "not in the SEC ticker map" in msg
    assert not any(m.startswith("MSFT: sector could not") for m in payload["missing_data"])
    src = {s["id"]: s for s in payload["sources"]}
    assert src["D:SEC:MSFT:SUB"]["url"] == "https://data.sec.gov/submissions/CIK0000789019.json"
    assert "D:SEC:MAP" in src
    sw = payload["sector_weights_pct"]  # MSFT 4000, FINX 1000, NOSEC 1000, KO 700 -> 6700
    assert sw["information_technology"] == pytest.approx(4000 / 6700 * 100, abs=0.02)  # rounding residual
    assert sw["unclassified"] == pytest.approx(1000 / 6700 * 100, abs=0.011)
    assert "classified_by_sic" in {f["code"] for f in payload["flags"]}
    assert not _schema_errors(payload, "lookthrough")


def test_sec_sic_offline_degrades_without_raising():
    res = lt.sec_sic("MSFT")  # offline, no fixtures, no cache
    assert res["sic"] is None and res["error"]
    c = lt.classify_security("MSFT", overrides={}, sic_fn=lambda t: lt.sec_sic(t)["sic"])
    assert c["sector"] == "unclassified"


def test_cached_series_price_is_point_in_time(yf, monkeypatch):
    series = make_ohlcv(n=120, seed=5, end="2026-10-02")
    monkeypatch.setattr(lt, "_load_series", lambda t: series if t == "PG" else None)
    f, _src, origin = lt.lookup_price("PG", {}, as_of="2026-09-15")
    cut = series.loc[:"2026-09-15", "Close"]
    assert origin == "cached_series" and f["as_of"] == "2026-09-15"
    assert f["value"] == pytest.approx(float(cut.iloc[-1]))
    assert f["value"] != pytest.approx(float(series["Close"].iloc[-1]))
    assert lt.lookup_price("PG", {}, as_of="2020-01-01") == (None, None, None)   # nothing on/before as_of


def test_template_portfolio_with_null_values_does_not_crash(yf):
    template = common.load_yaml(common.REPO_ROOT / "tests" / "fixtures" / "config_template" / "portfolio.yaml")
    payload = lt.run(portfolio=template, prices=_prices(), as_of=AS_OF)
    assert payload["sector_weights_pct"]["unclassified"] == 100.0
    assert sum(payload["sector_weights_pct"].values()) == pytest.approx(100.0)
    assert any("run /committee-setup" in m for m in payload["missing_data"])
    assert not _schema_errors(payload, "lookthrough")


def test_stale_fund_cache_is_used_and_flagged(yf, monkeypatch):
    cache = common.DiskCache()
    data = common.read_json(LT_FIX / "yf" / "funds" / "QQQ.json")
    cache.put("etf", "funds_QQQ", data, common.yf_holdings_url("QQQ"))
    entry_path = cache.path_for("etf", "funds_QQQ")
    entry = common.read_json(entry_path)
    entry["fetched_at"] = "2026-01-15T00:00:00+00:00"
    common.write_json(entry_path, entry)
    monkeypatch.delenv(common.ENV_FIXTURES_DIR)
    monkeypatch.setenv(common.ENV_OFFLINE, "1")
    p = _portfolio([_holding("QQQ", "fund", shares=10)], cash_usd=0)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
    assert _by_ticker(payload)["QQQ"]["sector_split"] == pytest.approx(QQQ_SPLIT)
    assert "stale_cache" in {f["code"] for f in payload["flags"]}
    assert payload["funds"]["QQQ"]["expense_ratio_pct"]["as_of"] == "2026-01-15"


def test_cli_reads_prices_file(yf, tmp_path, tmp_env, capsys):
    tmp_env.use_sample_portfolio()
    prices_path = tmp_path / "prices.json"
    common.write_json(prices_path, _prices())
    out = tmp_path / "lookthrough.json"
    rc = lt.main(["--prices", str(prices_path), "--out", str(out), "--as-of", AS_OF])
    assert rc == 0
    doc = common.read_json(out)
    assert doc["total_value_usd"]["value"] == pytest.approx(76400.0)
    assert not _schema_errors(doc, "lookthrough")
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["out"] == str(out)


# --------------------------------------------------------------------------- provenance (cited ids are defined)
def _facts_in(node, path=""):
    if isinstance(node, dict):
        if common.is_fact(node):
            yield path, node
        for k, v in node.items():
            yield from _facts_in(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _facts_in(v, f"{path}/{i}")


def _assert_cited_ids_defined(payload: dict) -> set[str]:
    """Every non-null Fact source_id is listed in sources[]; returns the set of ids cited by Facts."""
    ids = {s["id"] for s in payload["sources"]}
    cited = {f["source_id"] for _p, f in _facts_in({k: v for k, v in payload.items() if k != "sources"})
             if f["source_id"] is not None}
    assert cited <= ids, sorted(cited - ids)
    return cited


def test_user_supplied_totals_and_cash_cite_the_portfolio_source(yf):
    payload = lt.run(portfolio=copy.deepcopy(SAMPLE_PORTFOLIO), prices=_prices(), as_of=AS_OF)
    src = next(s for s in payload["sources"] if s["id"] == "D:CFG:portfolio")
    assert src["url"].startswith("https://") and src["kind"] == "dataset" and src["accessed"] == AS_OF
    assert payload["total_value_usd"]["source_id"] == "D:CFG:portfolio"
    assert payload["cash"]["value_usd"]["source_id"] == "D:CFG:portfolio"
    assert payload["cash"]["weight_pct"]["source_id"] == "D:CFG:portfolio"
    assert "D:CFG:portfolio" in _assert_cited_ids_defined(payload)
    assert not _schema_errors(payload, "lookthrough")


def test_weight_based_holdings_cite_the_portfolio_source_and_define_price_sources(yf):
    p = _portfolio([_holding("QQQ", "fund", weight_pct=40), _holding("KO", "stock", weight_pct=30),
                    _holding("JPM", "stock", weight_pct=25)], cash_pct=5, total=100000)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)       # prices exist but values come from weights
    h = _by_ticker(payload)
    for t in ("QQQ", "KO", "JPM"):
        assert h[t]["weight_pct"]["source_id"] == "D:CFG:portfolio", t
        assert h[t]["value_usd"]["source_id"] == "D:CFG:portfolio", t
        assert h[t]["price"]["source_id"] == f"D:PX:{t}"                 # informational price Fact ...
    ids = {s["id"] for s in payload["sources"]}
    assert {"D:PX:QQQ", "D:PX:KO", "D:PX:JPM"} <= ids                    # ... whose source is now defined
    cited = _assert_cited_ids_defined(payload)
    assert "D:CFG:portfolio" in cited
    assert payload["total_value_usd"]["source_id"] == "D:CFG:portfolio"
    assert not _schema_errors(payload, "lookthrough")


def test_every_cited_source_id_is_defined_across_input_modes(yf):
    portfolios = [
        _portfolio([_holding("KO", "stock", weight_pct=60), _holding("JPM", "stock", weight_pct=30)], cash_pct=4),
        _portfolio([_holding("QQQ", "fund", weight_pct=40), _holding("KO", "stock", shares=100)],
                   cash_usd=3000, total=100000),
        _portfolio([_holding("1159250.TA", "fund", shares=10, proxy="QQQ"), _holding("KO", "stock", shares=100)],
                   cash_usd=0),
        _portfolio([_holding("KO", "stock", shares=100), _holding("NOPRICE", "stock", shares=5)], cash_pct=10),
        _portfolio([_holding("KO", "stock"), _holding("JPM", "stock", shares=3)], cash_usd=0),
    ]
    for p in portfolios:
        payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
        _assert_cited_ids_defined(payload)
        assert not _schema_errors(payload, "lookthrough")
        assert payload["total_value_usd"]["source_id"] == "D:CFG:portfolio"


def test_unpriced_holding_cites_no_price_source(yf):
    p = _portfolio([_holding("KO", "stock", shares=100), _holding("NOPRICE", "stock", shares=5)], cash_pct=10)
    payload = lt.run(portfolio=p, prices=_prices(), as_of=AS_OF)
    h = _by_ticker(payload)["NOPRICE"]
    assert h["price"] is None and h["weight_pct"]["source_id"] is None and h["value_usd"]["source_id"] is None
    assert "D:PX:NOPRICE" not in {s["id"] for s in payload["sources"]}
