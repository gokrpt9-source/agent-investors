"""screen.py (DESIGN §4.7): universe building, filters + exclusion reasons, percentile-rank composite
(checked by hand), imputation, financials inputs, deterministic tie-breaks, shortlist size and the
"never pad" rule, plus the default gatherer (ADV / RS / fundamentals wiring). Offline: ETF holdings
and Yahoo info come from tests/fixtures/screen (SYNTHETIC); prices/fundamentals are injected."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from conftest import make_ohlcv
from scripts import common, screen

REPO_ROOT = Path(__file__).resolve().parents[1]
SC_FIX = REPO_ROOT / "tests" / "fixtures" / "screen"
AS_OF = "2026-10-02"
XLV_TOP10 = ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "DHR"]
BIG = {"mcap": 100e9, "adv_usd": 500e6, "coverage": "sec"}


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


def _ucfg(**over) -> dict:
    base = {"region": "US", "min_market_cap_usd": 10e9, "min_avg_dollar_volume_usd": 50e6, "adv_window_days": 63,
            "max_candidates_per_sector": 6, "exclusions": [], "additions": {}}
    base.update(over)
    return base


def _gatherer(table: dict):
    def gather(t):
        v = table.get(t)
        if isinstance(v, Exception):
            raise v
        return None if v is None else dict(v)
    return gather


def _metrics(op, fcf, roic, fy, evebit, rs6, rs12, **extra):
    d = dict(BIG, operating_margin_pct=op, fcf_margin_pct=fcf, roic_pct=roic, fcf_yield_pct=fy, ev_ebit=evebit,
             rs_6m=rs6, rs_12m=rs12)
    d.update(extra)
    return d


@pytest.fixture
def fx(use_fixtures):
    return use_fixtures(SC_FIX)


# --------------------------------------------------------------------------- pure math
def test_percentile_ranks_with_ties_nulls_and_singletons():
    assert screen.percentile_ranks({"A": 1.0, "B": 2.0, "C": 3.0}) == {"A": 0.0, "B": 0.5, "C": 1.0}
    # A,B tie for ranks 1-2 (avg 1.5) -> (1.5-1)/3; D is rank 4 of 4
    out = screen.percentile_ranks({"A": 5.0, "B": 5.0, "C": None, "D": 9.0, "E": 7.0})
    assert out == {"A": pytest.approx(0.5 / 3), "B": pytest.approx(0.5 / 3), "C": None, "D": 1.0,
                   "E": pytest.approx(2 / 3)}
    assert screen.percentile_ranks({"A": 3.0, "B": None}) == {"A": 0.5, "B": None}
    assert screen.percentile_ranks({"A": None}) == {"A": None}


def test_relative_strength_and_adv_by_hand():
    df = make_ohlcv(n=300, seed=1)
    spy = make_ohlcv(n=300, seed=2)
    rs, day = screen.relative_strength(df, spy, 126)
    rt = df["Close"].iloc[-1] / df["Close"].iloc[-127] - 1
    rb = spy["Close"].iloc[-1] / spy["Close"].iloc[-127] - 1
    assert rs == pytest.approx(round(((1 + rt) / (1 + rb) - 1) * 100, 4)) and day == "2026-10-02"
    assert screen.relative_strength(df.iloc[-50:], spy, 126) == (None, None)  # not enough common bars
    # date alignment: drop a bar from the benchmark -> uses common dates only
    rs2, _ = screen.relative_strength(df, spy.drop(spy.index[-10]), 126)
    both = df[["Close"]].join(spy.drop(spy.index[-10])[["Close"]], rsuffix="_b", how="inner")
    rt2 = both["Close"].iloc[-1] / both["Close"].iloc[-127] - 1
    rb2 = both["Close_b"].iloc[-1] / both["Close_b"].iloc[-127] - 1
    assert rs2 == pytest.approx(round(((1 + rt2) / (1 + rb2) - 1) * 100, 4))
    adv = screen.average_dollar_volume(df, 63)
    assert adv == pytest.approx(float((df["Close"] * df["Volume"]).iloc[-63:].mean()))
    assert screen.average_dollar_volume(df.iloc[-10:], 63) is None


def test_ranking_math_by_hand_with_ticker_tiebreak():
    raws = {
        "LLY": _metrics(40, 20, 30, 1.0, 40, 10, 30),
        "MRK": _metrics(30, 25, 20, 5.0, 12, -5, -10),
        "JNJ": _metrics(25, 22, 15, 4.0, 15, 0, 5),
    }
    rows = screen.rank_candidates(raws, sector_class="default", weights={"quality": 0.4, "valuation": 0.3,
                                                                          "momentum": 0.3})
    by = {r["ticker"]: r for r in rows}
    # quality: op (JNJ0 MRK.5 LLY1), fcf (LLY0 JNJ.5 MRK1), roic (JNJ0 MRK.5 LLY1)
    assert by["LLY"]["components"]["quality"]["score"] == pytest.approx(0.6667)
    assert by["MRK"]["components"]["quality"]["score"] == pytest.approx(0.6667)
    assert by["JNJ"]["components"]["quality"]["score"] == pytest.approx(0.1667)
    # valuation: fcf yield (LLY0 JNJ.5 MRK1); EV/EBIT lower is better (LLY 40 -> 0, JNJ 15 -> .5, MRK 12 -> 1)
    assert [by[t]["components"]["valuation"]["score"] for t in ("LLY", "JNJ", "MRK")] == [0.0, 0.5, 1.0]
    assert by["MRK"]["components"]["valuation"]["inputs"]["ev_ebit"] == {
        "value": 12, "basis": "reciprocal", "pctile": 1.0, "imputed": False}
    # momentum: rs 6m and 12m both MRK0 JNJ.5 LLY1
    assert [by[t]["components"]["momentum"]["score"] for t in ("MRK", "JNJ", "LLY")] == [0.0, 0.5, 1.0]
    # composite = .4q + .3v + .3m ; LLY and MRK tie at 0.5667 -> ticker order decides
    assert by["LLY"]["composite"] == pytest.approx(0.5667)
    assert by["MRK"]["composite"] == pytest.approx(0.5667)
    assert by["JNJ"]["composite"] == pytest.approx(0.3667)
    assert [r["ticker"] for r in rows] == ["LLY", "MRK", "JNJ"]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert all(r["imputed"] == [] for r in rows)


def test_imputation_uses_neutral_percentile_and_is_listed():
    raws = {"X": _metrics(10, 10, 10, 2, 10, 1, 1), "Y": _metrics(20, 20, 20, 3, 20, 2, 2),
            "Z": dict(BIG, operating_margin_pct=15, fcf_margin_pct=15, roic_pct=None, fcf_yield_pct=2.5,
                      ev_ebit=None, rs_6m=1.5, rs_12m=1.5)}
    by = {r["ticker"]: r for r in screen.rank_candidates(raws)}
    z = by["Z"]
    assert z["imputed"] == ["quality.roic_pct", "valuation.ev_ebit"]
    assert z["components"]["quality"]["inputs"]["roic_pct"] == {"value": None, "basis": "direct", "pctile": None,
                                                                 "imputed": True}
    # Z: op .5, fcf .5, roic imputed .5 -> quality .5 ; fcf_yield .5 + ev_ebit imputed .5 -> valuation .5
    assert z["components"]["quality"]["score"] == 0.5 and z["components"]["valuation"]["score"] == 0.5
    # roic is ranked among X and Y only: X 0, Y 1
    assert by["X"]["components"]["quality"]["inputs"]["roic_pct"]["pctile"] == 0.0
    assert by["Y"]["components"]["quality"]["inputs"]["roic_pct"]["pctile"] == 1.0
    assert z["composite"] == pytest.approx(0.5)


def test_reciprocal_puts_negative_multiples_last():
    raws = {"A": dict(BIG, ev_ebit=-5.0), "B": dict(BIG, ev_ebit=30.0), "C": dict(BIG, ev_ebit=10.0),
            "D": dict(BIG, ev_ebit=0.0)}
    by = {r["ticker"]: r["components"]["valuation"]["inputs"]["ev_ebit"] for r in screen.rank_candidates(raws)}
    assert (by["A"]["pctile"], by["B"]["pctile"], by["C"]["pctile"]) == (0.0, 0.5, 1.0)
    assert by["D"]["imputed"] is True


# --------------------------------------------------------------------------- run: filters
def test_filters_and_exclusion_reasons(fx):
    ucfg = _ucfg(exclusions=["ABT"], additions={"health_care": ["VRTX", "LLY", "brk.b"]})
    table = {
        "LLY": _metrics(40, 20, 30, 1.0, 40, 10, 30),
        "JNJ": common.DataUnavailable("boom"),            # gatherer raises -> no_data
        "ABBV": dict(BIG, mcap=None),                     # unknown market cap -> no_data
        "MRK": _metrics(30, 25, 20, 5.0, 12, -5, -10),
        "TMO": dict(BIG, mcap=5e9),                       # below min market cap
        "ISRG": dict(BIG, adv_usd=1e6),                   # below min ADV
        "AMGN": dict(BIG, coverage="non_sec"),            # not an SEC filer
        "DHR": None,                                      # gatherer returns nothing -> no_data
        "VRTX": _metrics(35, 30, 25, 3.0, 20, 2, 4),
        "BRK-B": dict(BIG, adv_usd=None),                 # unknown ADV -> no_data
    }
    payload = screen.run("health_care", held=["UNH", "KO"], as_of=AS_OF, gather=_gatherer(table),
                         universe_cfg=ucfg)
    uni = payload["universe"]
    assert [u["ticker"] for u in uni] == XLV_TOP10 + ["VRTX", "BRK-B"]  # PFE is #11 -> outside top 10
    assert {u["origin"] for u in uni[:10]} == {"etf_top10"} and uni[-1]["origin"] == "universe_add"
    assert uni[0]["etf_weight_pct"] == pytest.approx(12.0) and uni[0]["also_in_additions"] is True
    reasons = {e["ticker"]: e["reason"] for e in payload["excluded"]}
    assert reasons == {"UNH": "held", "ABT": "exclusion", "JNJ": "no_data", "ABBV": "no_data", "TMO": "mcap",
                       "ISRG": "adv", "AMGN": "non_sec", "DHR": "no_data", "BRK-B": "no_data"}
    # by hand: LLY q=.6667 v=0 m=1 -> .5667 ; VRTX q=.6667 v=.5 m=.5 -> .5667 ; MRK q=.1667 v=1 m=0 -> .3667
    assert [(r["ticker"], r["composite"]) for r in payload["ranked"]] == [
        ("LLY", 0.5667), ("VRTX", 0.5667), ("MRK", 0.3667)]
    assert payload["shortlist"] == ["LLY", "VRTX", "MRK"]
    assert any(m.startswith("JNJ: screen data unavailable") for m in payload["missing_data"])
    assert payload["benchmark_etf"] == "XLV" and payload["sector_class"] == "default"
    assert payload["weights"] == {"quality": 0.4, "valuation": 0.3, "momentum": 0.3}
    assert {s["id"] for s in payload["sources"]} == {"D:YF:XLV:FUND"}
    assert not _schema_errors(payload, "screen")


def test_shortlist_size_from_config_and_ordering(fx, tmp_path):
    table = {t: _metrics(10 + i, 10 + i, 10 + i, 1 + i, 30 - i, i, i) for i, t in enumerate(XLV_TOP10)}
    out = tmp_path / "phase3" / "screen_health_care.json"
    payload = screen.run("health_care", out, held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg())
    assert len(payload["ranked"]) == 10
    # every input increases with i -> DHR (i=9) best ... LLY (i=0) worst
    assert [r["ticker"] for r in payload["ranked"]] == list(reversed(XLV_TOP10))
    assert payload["shortlist"] == list(reversed(XLV_TOP10))[:6]
    assert payload["ranked"][0]["composite"] == 1.0 and payload["ranked"][-1]["composite"] == 0.0
    assert payload["filters"]["shortlist_size"] == 6
    assert common.read_json(out)["shortlist"] == payload["shortlist"]
    assert not _schema_errors(payload, "screen")
    # a smaller universe.yaml cap wins
    payload = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer(table),
                         universe_cfg=_ucfg(max_candidates_per_sector=3))
    assert len(payload["shortlist"]) == 3


def test_tiny_universe_is_never_padded_and_missing_etf_is_reported(fx):
    # XLU has no fixture -> universe = additions only
    table = {"NEE": _metrics(30, 10, 8, 3, 18, 1, 2), "DUK": _metrics(25, 5, 6, 4, 16, -1, 0)}
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table),
                         universe_cfg=_ucfg(additions={"utilities": ["NEE", "DUK"]}))
    assert [u["origin"] for u in payload["universe"]] == ["universe_add", "universe_add"]
    assert len(payload["shortlist"]) == 2 and len(payload["ranked"]) == 2
    assert any(m.startswith("XLU top holdings unavailable") for m in payload["missing_data"])
    codes = {f["code"] for f in payload["flags"]}
    assert {"universe_etf_unavailable", "short_universe"} <= codes
    assert not _schema_errors(payload, "screen")
    # nothing survives -> empty shortlist, still a valid document
    empty = screen.run("utilities", held=["NEE", "DUK"], as_of=AS_OF, gather=_gatherer(table),
                       universe_cfg=_ucfg(additions={"utilities": ["NEE", "DUK"]}))
    assert empty["ranked"] == [] and empty["shortlist"] == []
    assert not _schema_errors(empty, "screen")


def test_financials_use_pe_and_pb_instead_of_ev_ebit(fx):
    fin = lambda pe, pb, roe, fy: dict(BIG, operating_margin_pct=30, fcf_margin_pct=20, roe_pct=roe,  # noqa: E731
                                       fcf_yield_pct=fy, pe=pe, p_b=pb, ev_ebit=1.0, roic_pct=99, rs_6m=0, rs_12m=0)
    table = {"JPM": fin(12, 2.0, 16, 5), "BAC": fin(10, 1.2, 10, 6), "GS": fin(15, 1.6, 12, 4)}
    payload = screen.run("financials", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg())
    assert payload["sector_class"] == "financials"
    universe = [u["ticker"] for u in payload["universe"]]
    assert universe[0] == "BRK-B"  # Yahoo "BRK.B" normalized
    r = {x["ticker"]: x for x in payload["ranked"]}
    assert set(r["JPM"]["components"]["valuation"]["inputs"]) == {"fcf_yield_pct", "pe", "p_b"}
    assert set(r["JPM"]["components"]["quality"]["inputs"]) == {"operating_margin_pct", "fcf_margin_pct", "roe_pct"}
    # P/E reciprocal: BAC 10 -> 1, JPM 12 -> .5, GS 15 -> 0 ; P/B: BAC 1.2 -> 1, GS 1.6 -> .5, JPM 2.0 -> 0
    # FCF yield: GS 4 -> 0, JPM 5 -> .5, BAC 6 -> 1
    assert r["BAC"]["components"]["valuation"]["score"] == 1.0
    assert r["JPM"]["components"]["valuation"]["score"] == pytest.approx(round((0.5 + 0.5 + 0) / 3, 4))
    assert payload["method"]["reciprocal_inputs"] == ["pe", "p_b"]
    assert "ev_ebit" not in r["JPM"]["raw"]
    # unlisted XLF tickers had no data in the table -> no_data
    assert {e["reason"] for e in payload["excluded"]} == {"no_data"}
    assert not _schema_errors(payload, "screen")


def test_unknown_sector_rejected():
    with pytest.raises(ValueError):
        screen.run("funds", held=[], gather=_gatherer({}))


# --------------------------------------------------------------------------- default gatherer
def test_default_gatherer_wiring(fx, monkeypatch):
    series = {"AAA": make_ohlcv(n=300, seed=11), "BBB": make_ohlcv(n=300, seed=12),
              "CCC": make_ohlcv(n=300, seed=13), "EEE": make_ohlcv(n=300, seed=15), "FFF": make_ohlcv(n=300, seed=16),
              "GGG": make_ohlcv(n=300, seed=17), "SPY": make_ohlcv(n=300, seed=99)}
    low = make_ohlcv(n=300, seed=14)
    low["Volume"] = low["Volume"] * 0.0001
    series["DDD"] = low
    calls = {"prefetch": [], "fund": []}
    monkeypatch.setattr(screen, "_load_series", lambda t: series.get(t))
    monkeypatch.setattr(screen, "_fetch_prices", lambda tickers, as_of: calls["prefetch"].append(list(tickers)))

    def fake_fund(t, *, price, as_of, sector_class):
        calls["fund"].append((t, sector_class, round(price, 6)))
        if t == "CCC":
            raise common.DataUnavailable("SEC down")
        if t == "EEE":
            return {"coverage": "non_sec", "market_cap": common.na_fact("USD", None, as_of, "price"),
                    "margins_pct": {}, "valuation": {}, "sources": []}
        if t == "GGG":  # 2 of 5 quality/valuation inputs missing (40%): ranked, but flagged
            g = lambda v, u="pct": common.fact(v, u, "D:SEC:GGG:CF", "2026-06-30", "fundamental")  # noqa: E731
            return {"coverage": "sec", "market_cap": g(45e9, "USD"),
                    "margins_pct": {"operating": g(10.0), "fcf": g(5.0)},
                    "valuation": {"roic_pct": g(8.0)}, "sources": [common.dataset_source("D:SEC:GGG:CF", cik=2)]}
        if t == "FFF":  # SEC unreachable: fetch_fundamentals degrades to coverage partial, all null
            return {"coverage": "partial", "market_cap": common.na_fact("USD", None, as_of, "price"),
                    "margins_pct": {}, "valuation": {}, "sources": [],
                    "missing_data": ["SEC ticker map unavailable - fundamentals not fetched"]}
        f = lambda v, u="pct": common.fact(v, u, "D:SEC:AAA:CF", "2026-06-30", "fundamental")  # noqa: E731
        return {"coverage": "sec", "market_cap": f(50e9, "USD"),
                "margins_pct": {"operating": f(25.0), "fcf": f(18.0)},
                "valuation": {"roic_pct": f(20.0), "roe_pct": f(30.0), "fcf_yield_pct": f(4.0), "ev_ebit": f(18.0, "x"),
                              "pe": f(22.0, "x"), "p_b": f(5.0, "x")},
                "sources": [common.dataset_source("D:SEC:AAA:CF", cik=1)]}

    monkeypatch.setattr(screen, "_run_fundamentals", fake_fund)
    ucfg = _ucfg(additions={"industrials": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]})
    payload = screen.run("industrials", held=[], as_of=AS_OF, universe_cfg=ucfg)

    assert calls["prefetch"] == [["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "SPY"]]
    # BBB (mcap 5e9) and DDD (tiny ADV) never reach the fundamentals fetch
    assert [c[0] for c in calls["fund"]] == ["AAA", "CCC", "EEE", "FFF", "GGG"]
    assert calls["fund"][0][1] == "default"
    assert calls["fund"][0][2] == pytest.approx(round(float(series["AAA"]["Close"].iloc[-1]), 6))
    reasons = {e["ticker"]: e["reason"] for e in payload["excluded"]}
    # CCC (fundamentals failed) and FFF (empty payload) have 5 of 5 quality/valuation inputs imputed: not ranked
    assert reasons == {"BBB": "mcap", "DDD": "adv", "EEE": "non_sec", "CCC": "no_data", "FFF": "no_data"}
    details = {e["ticker"]: e["detail"] for e in payload["excluded"]}
    assert details["CCC"].startswith("5 of 5 quality/valuation inputs missing (operating_margin_pct, fcf_margin_pct, roic_pct,")
    ranked = {r["ticker"]: r for r in payload["ranked"]}
    assert set(ranked) == {"AAA", "GGG"}
    aaa = ranked["AAA"]
    assert aaa["raw"]["mcap"]["value"] == pytest.approx(50e9) and aaa["raw"]["mcap"]["source_id"] == "D:YF:AAA:INFO"
    exp_adv = float((series["AAA"]["Close"] * series["AAA"]["Volume"]).iloc[-63:].mean())
    assert aaa["raw"]["adv_usd"]["value"] == pytest.approx(exp_adv)
    exp_rs, _ = screen.relative_strength(series["AAA"], series["SPY"], 252)
    assert aaa["raw"]["rs_12m"]["value"] == pytest.approx(exp_rs) and aaa["raw"]["rs_12m"]["unit"] == "pp"
    assert aaa["raw"]["roic_pct"]["source_id"] == "D:SEC:AAA:CF"
    assert aaa["imputed"] == [] and aaa["coverage"] == "sec"
    # CCC: fundamentals failed -> reason surfaced in missing_data and the candidate is excluded, not ranked
    assert any(m.startswith("CCC: fundamentals unavailable") for m in payload["missing_data"])
    assert any(m.startswith("CCC: 5 of 5 quality/valuation inputs missing") and m.endswith("excluded from the ranking")
               for m in payload["missing_data"])
    # FFF: fundamentals returned but empty -> surfaced in missing_data with the reason
    assert any(m.startswith("FFF: fundamentals returned no usable screen metrics (coverage partial: SEC ticker map")
               for m in payload["missing_data"])
    # GGG: 2 of 5 inputs imputed (40% > 25%, <= 50%) -> still ranked, flagged medium
    assert ranked["GGG"]["imputed"] == ["valuation.fcf_yield_pct", "valuation.ev_ebit"]
    heavy = [f for f in payload["flags"] if f["code"] == "screen_imputation_heavy"]
    assert [f["severity"] for f in heavy] == ["medium"] and heavy[0]["detail"].startswith("GGG: 2 of 5")
    assert not any(m.startswith("AAA:") for m in payload["missing_data"])
    ids = {s["id"] for s in payload["sources"]}
    assert {"D:YF:AAA:INFO", "D:PX:AAA", "D:PX:SPY", "D:SEC:AAA:CF"} <= ids
    assert payload["shortlist"] == [r["ticker"] for r in payload["ranked"]]
    assert not _schema_errors(payload, "screen")


def test_default_gatherer_is_point_in_time(fx, monkeypatch):
    # cached series run past the screen's as_of (replay / dry-run): no later bar may be used
    as_of = "2025-06-30"
    full = {"AAA": make_ohlcv(n=700, seed=11, end="2026-10-02"), "SPY": make_ohlcv(n=700, seed=99, end="2026-10-02")}
    cut = {t: df.loc[:as_of] for t, df in full.items()}
    seen_prices = []
    monkeypatch.setattr(screen, "_load_series", lambda t: full.get(t))
    monkeypatch.setattr(screen, "_fetch_prices", lambda tickers, as_of: None)

    def fake_fund(t, *, price, as_of, sector_class):
        seen_prices.append(price)
        f = lambda v, u="pct": common.fact(v, u, "D:SEC:AAA:CF", "2025-03-31", "fundamental")  # noqa: E731
        return {"coverage": "sec", "market_cap": f(50e9, "USD"), "margins_pct": {"operating": f(25.0), "fcf": f(18.0)},
                "valuation": {"roic_pct": f(20.0), "fcf_yield_pct": f(4.0), "ev_ebit": f(18.0, "x")}, "sources": []}

    monkeypatch.setattr(screen, "_run_fundamentals", fake_fund)
    payload = screen.run("industrials", held=[], as_of=as_of,
                         universe_cfg=_ucfg(additions={"industrials": ["AAA"]}))
    raw = payload["ranked"][0]["raw"]
    assert payload["as_of"] == as_of
    assert raw["adv_usd"]["as_of"] <= as_of and raw["rs_12m"]["as_of"] <= as_of
    exp_adv = float((cut["AAA"]["Close"] * cut["AAA"]["Volume"]).iloc[-63:].mean())
    assert raw["adv_usd"]["value"] == pytest.approx(exp_adv)
    exp_rs, day = screen.relative_strength(cut["AAA"], cut["SPY"], 252)
    assert raw["rs_12m"]["value"] == pytest.approx(exp_rs) and raw["rs_12m"]["as_of"] == day
    assert seen_prices == [pytest.approx(float(cut["AAA"]["Close"].iloc[-1]))]


def test_default_gatherer_survives_missing_modules(fx, monkeypatch):
    # no fetch_prices / fetch_fundamentals behaviour available: everything degrades to no_data
    monkeypatch.setattr(screen, "_load_series", lambda t: None)
    monkeypatch.setattr(screen, "_fetch_prices", lambda tickers, as_of: "fetch_prices unavailable")

    def boom(*a, **k):
        raise ImportError("no module")

    monkeypatch.setattr(screen, "_run_fundamentals", boom)
    payload = screen.run("industrials", held=[], as_of=AS_OF,
                         universe_cfg=_ucfg(additions={"industrials": ["AAA"]}))
    assert payload["excluded"] == [{"ticker": "AAA", "reason": "no_data", "detail": "average dollar volume unavailable",
                                    "mcap": 50e9, "adv_usd": None}]
    assert payload["shortlist"] == []
    assert not _schema_errors(payload, "screen")


def test_cli(fx, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(screen, "_load_series", lambda t: None)
    monkeypatch.setattr(screen, "_fetch_prices", lambda tickers, as_of: None)
    out = tmp_path / "screen.json"
    rc = screen.main(["--sector", "health_care", "--held", "LLY,UNH", "--out", str(out), "--as-of", AS_OF])
    assert rc == 0
    doc = common.read_json(out)
    assert {e["ticker"] for e in doc["excluded"] if e["reason"] == "held"} == {"LLY", "UNH"}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["sector"] == "health_care"
    assert not _schema_errors(doc, "screen")


# --------------------------------------------------------------------------- universe fallback: etf_overrides.funds.<ETF>.top_holdings
XLU_ROWS = [{"symbol": "NEE", "name": "NextEra Energy", "weight_pct": 12.5}, {"symbol": "DUK", "weight_pct": 8.0},
            {"symbol": "SO", "name": None, "weight_pct": 9.0}, {"symbol": "d", "weight_pct": 3.0}]


def _xlu_override(tmp_env, **extra) -> dict:
    entry = {"sector_weights": {"utilities": 100.0}, "source_url": "https://www.issuer.test/xlu-factsheet.pdf",
             "as_of": "2026-09-30", "top_holdings": XLU_ROWS}
    entry.update(extra)
    tmp_env.update_config("etf_overrides", lambda d: d.__setitem__("funds", {"XLU": entry}))
    assert not common.validate_config("etf_overrides", tmp_env.read_config("etf_overrides"))
    return entry


UTIL_TABLE = {"NEE": _metrics(30, 10, 8, 3, 18, 1, 2), "DUK": _metrics(25, 5, 6, 4, 16, -1, 0),
              "SO": _metrics(28, 6, 7, 3.5, 17, 0, 1), "D": _metrics(20, 4, 5, 4.5, 15, -2, -1)}


def test_universe_falls_back_to_etf_overrides_top_holdings(fx, tmp_env):
    _xlu_override(tmp_env)                                     # XLU has no Yahoo fixture -> no payload
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(UTIL_TABLE), universe_cfg=_ucfg())
    uni = payload["universe"]
    assert [(u["ticker"], u["origin"], u["etf_weight_pct"]) for u in uni] == [
        ("NEE", "etf_top10", 12.5), ("SO", "etf_top10", 9.0), ("DUK", "etf_top10", 8.0), ("D", "etf_top10", 3.0)]
    assert uni[0]["name"] == "NextEra Energy"
    src = next(s for s in payload["sources"] if s["id"] == "D:CFG:etf_overrides.funds.XLU")
    assert src["url"] == "https://www.issuer.test/xlu-factsheet.pdf" and src["kind"] == "dataset"
    flag = next(f for f in payload["flags"] if f["code"] == "universe_from_override")
    assert flag["severity"] == "medium" and "2026-09-30" in flag["detail"]
    assert "universe_etf_unavailable" not in {f["code"] for f in payload["flags"]}
    assert not any("top holdings unavailable" in m for m in payload["missing_data"])
    assert len(payload["ranked"]) == 4 and not _schema_errors(payload, "screen")


def test_override_fallback_respects_top_n_and_universe_additions(fx, tmp_env):
    rows = [{"symbol": f"T{i:02d}", "weight_pct": 20.0 - i} for i in range(12)]
    _xlu_override(tmp_env, top_holdings=rows)
    cfg = common.committee_cfg()
    assert cfg["screen"]["etf_top_n"] == 10
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer({}), universe_cfg=_ucfg(
        additions={"utilities": ["T00", "ZZZ"]}))
    assert [u["ticker"] for u in payload["universe"]] == [f"T{i:02d}" for i in range(10)] + ["ZZZ"]
    assert payload["universe"][0]["also_in_additions"] is True and payload["universe"][-1]["origin"] == "universe_add"


def test_unsourced_override_top_holdings_are_ignored(fx, tmp_env):
    _xlu_override(tmp_env)
    ov = tmp_env.read_config("etf_overrides")
    ov["funds"]["XLU"]["source_url"] = ""                      # the config schema would reject it; the screen must too
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(UTIL_TABLE), universe_cfg=_ucfg(),
                         overrides=ov)
    assert payload["universe"] == [] and payload["ranked"] == []
    codes = {f["code"] for f in payload["flags"]}
    assert {"override_unsourced", "universe_etf_unavailable"} <= codes
    assert "universe_from_override" not in codes
    assert not any(s["id"].startswith("D:CFG:") for s in payload["sources"])


def test_yahoo_holdings_win_over_the_override(fx, tmp_env):
    tmp_env.update_config("etf_overrides", lambda d: d.__setitem__("funds", {"XLV": {
        "sector_weights": {"health_care": 100.0}, "source_url": "https://www.issuer.test/xlv.pdf",
        "as_of": "2026-09-30", "top_holdings": [{"symbol": "ZZZ", "weight_pct": 50.0}]}}))
    payload = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer({}), universe_cfg=_ucfg())
    assert [u["ticker"] for u in payload["universe"]] == XLV_TOP10
    assert {s["id"] for s in payload["sources"]} == {"D:YF:XLV:FUND"}
    assert "universe_from_override" not in {f["code"] for f in payload["flags"]}


def test_override_without_top_holdings_changes_nothing(fx, tmp_env):
    tmp_env.update_config("etf_overrides", lambda d: d.__setitem__("funds", {"XLU": {
        "sector_weights": {"utilities": 100.0}, "source_url": "https://www.issuer.test/xlu.pdf",
        "as_of": "2026-09-30"}}))
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(UTIL_TABLE),
                         universe_cfg=_ucfg(additions={"utilities": ["NEE"]}))
    assert [u["origin"] for u in payload["universe"]] == ["universe_add"]
    assert {"universe_etf_unavailable", "short_universe"} <= {f["code"] for f in payload["flags"]}
    assert any(m.startswith("XLU top holdings unavailable") for m in payload["missing_data"])


def test_etf_override_holdings_helper_directly():
    ov = {"funds": {"xlu": {"source_url": "https://x.test/f", "as_of": "2026-09-30",
                            "top_holdings": [{"symbol": "a", "weight_pct": 1.0}, {"weight_pct": 9.0},
                                             {"symbol": "B", "holding_pct": 5.0}]}}}
    rows, src, flags = screen.etf_override_holdings("XLU", ov, 10, as_of=AS_OF)
    assert [(r["symbol"], r["weight_pct"]) for r in rows] == [("B", 5.0), ("A", 1.0)]    # sorted, symbol-less dropped
    assert src["id"] == "D:CFG:etf_overrides.funds.XLU" and flags[0]["code"] == "universe_from_override"
    assert screen.etf_override_holdings("XLK", ov, 10, as_of=AS_OF) == ([], None, [])
    assert screen.etf_override_holdings("XLU", {"funds": {"XLU": {"top_holdings": [{"symbol": "A", "weight_pct": 1}]}}},
                                        10, as_of=AS_OF)[2][0]["code"] == "override_unsourced"


# --------------------------------------------------------------------------- imputation guard (C6)
def _thin(**keep) -> dict:
    """BIG + momentum, with only the given quality/valuation inputs present."""
    d = dict(BIG, rs_6m=5.0, rs_12m=8.0)
    d.update(keep)
    return d


FULL_QV = dict(operating_margin_pct=20, fcf_margin_pct=15, roic_pct=12, fcf_yield_pct=4.0, ev_ebit=15)


def test_mostly_imputed_candidates_are_excluded_not_ranked(fx):
    # WELL / VTR style: 5 of 5 (or 3 of 5) quality+valuation inputs missing used to rank on momentum alone
    table = {
        "FULL": _thin(**FULL_QV),
        "MOST": _thin(operating_margin_pct=30, fcf_margin_pct=25),                       # 3 of 5 missing -> excluded
        "NONE": _thin(),                                                                 # 5 of 5 missing -> excluded
        "SOME": _thin(operating_margin_pct=30, fcf_margin_pct=25, roic_pct=15, fcf_yield_pct=6.0),  # 1 of 5 -> kept
        "HALF": _thin(operating_margin_pct=30, fcf_margin_pct=25, roic_pct=15),          # 2 of 5 (40%) -> kept + flag
    }
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table),
                         universe_cfg=_ucfg(additions={"utilities": list(table)}))
    reasons = {e["ticker"]: (e["reason"], e["detail"]) for e in payload["excluded"]}
    assert set(reasons) == {"MOST", "NONE"}
    assert reasons["MOST"][0] == "no_data" and reasons["MOST"][1].startswith(
        "3 of 5 quality/valuation inputs missing (roic_pct, fcf_yield_pct, ev_ebit)")
    assert reasons["NONE"][1].startswith("5 of 5 quality/valuation inputs missing")
    assert {r["ticker"] for r in payload["ranked"]} == {"FULL", "SOME", "HALF"}
    flagged = [f for f in payload["flags"] if f["code"] == "screen_imputation_heavy"]
    assert [(f["severity"], f["detail"].split(":")[0]) for f in flagged] == [("medium", "HALF")]    # SOME is 20% <= 25%
    assert any(m.startswith("MOST: 3 of 5 quality/valuation inputs missing") for m in payload["missing_data"])
    assert "NONE" not in payload["shortlist"] and "MOST" not in payload["shortlist"]
    assert not _schema_errors(payload, "screen")


def test_excluded_thin_candidates_do_not_move_the_survivors_percentiles(fx):
    table = {"AAA": _thin(**FULL_QV), "BBB": _thin(operating_margin_pct=5, fcf_margin_pct=5, roic_pct=5,
                                                      fcf_yield_pct=1.0, ev_ebit=30),
             "THIN": _thin(operating_margin_pct=99)}
    with_thin = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table),
                           universe_cfg=_ucfg(additions={"utilities": list(table)}))
    del table["THIN"]
    without = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table),
                         universe_cfg=_ucfg(additions={"utilities": list(table)}))
    assert [(r["ticker"], r["composite"]) for r in with_thin["ranked"]] == [(r["ticker"], r["composite"]) for r in without["ranked"]]


def test_imputation_boundary_exactly_half_is_kept_for_financials(fx):
    # financials have 6 quality+valuation inputs (3 quality, fcf_yield + pe + p_b): 3 missing = exactly 50% -> kept
    base = dict(BIG, rs_6m=1.0, rs_12m=2.0)
    table = {"AAA": dict(base, operating_margin_pct=30, fcf_margin_pct=20, roe_pct=15, fcf_yield_pct=5, pe=12, p_b=1.5),
             "HLF": dict(base, operating_margin_pct=30, fcf_margin_pct=20, roe_pct=15),
             "OVR": dict(base, operating_margin_pct=30, fcf_margin_pct=20)}                # 4 of 6 -> excluded
    payload = screen.run("financials", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg(
        additions={"financials": list(table)}, max_candidates_per_sector=6))
    assert {r["ticker"] for r in payload["ranked"]} >= {"AAA", "HLF"} and "OVR" not in {r["ticker"] for r in payload["ranked"]}
    assert {e["ticker"]: e["reason"] for e in payload["excluded"] if e["ticker"] in table} == {"OVR": "no_data"}
    heavy = [f["detail"].split(":")[0] for f in payload["flags"] if f["code"] == "screen_imputation_heavy"]
    assert heavy == ["HLF"]


def test_imputation_thresholds_are_configurable(fx):
    cfg = common.committee_cfg()
    cfg["screen"]["max_imputed_fraction"] = 0.1
    cfg["screen"]["warn_imputed_fraction"] = 0.0
    table = {"SOME": _thin(operating_margin_pct=30, fcf_margin_pct=25, roic_pct=15, fcf_yield_pct=6.0),
             "FULL": _thin(**FULL_QV)}
    payload = screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table), cfg=cfg,
                         universe_cfg=_ucfg(additions={"utilities": list(table)}))
    assert [e["ticker"] for e in payload["excluded"]] == ["SOME"] and [r["ticker"] for r in payload["ranked"]] == ["FULL"]


# --------------------------------------------------------------------------- ConfigError is never swallowed
def test_config_error_from_a_gatherer_propagates(fx):
    table = {"NEE": common.ConfigError("sec_contact_email is a placeholder; run /committee-setup")}
    with pytest.raises(common.ConfigError, match="committee-setup"):
        screen.run("utilities", held=[], as_of=AS_OF, gather=_gatherer(table),
                   universe_cfg=_ucfg(additions={"utilities": ["NEE"]}))


def test_config_error_from_fundamentals_propagates_through_the_default_gatherer(fx, monkeypatch):
    series = {"AAA": make_ohlcv(n=300, seed=11), "SPY": make_ohlcv(n=300, seed=99)}
    monkeypatch.setattr(screen, "_load_series", lambda t: series.get(t))
    monkeypatch.setattr(screen, "_fetch_prices", lambda tickers, as_of: None)

    def placeholder_email(t, *, price, as_of, sector_class):
        raise common.ConfigError("investor_profile.yaml: sec_contact_email must be a real address (run /committee-setup)")

    monkeypatch.setattr(screen, "_run_fundamentals", placeholder_email)
    with pytest.raises(common.ConfigError, match="sec_contact_email"):
        screen.run("industrials", held=[], as_of=AS_OF, universe_cfg=_ucfg(additions={"industrials": ["AAA"]}))
    # an ordinary data failure is still just missing data (and, with every input imputed, no_data)
    monkeypatch.setattr(screen, "_run_fundamentals", lambda *a, **k: (_ for _ in ()).throw(common.DataUnavailable("down")))
    payload = screen.run("industrials", held=[], as_of=AS_OF, universe_cfg=_ucfg(additions={"industrials": ["AAA"]}))
    assert [e["reason"] for e in payload["excluded"]] == ["no_data"] and payload["shortlist"] == []


def test_cli_reports_config_error_with_exit_code_2(fx, monkeypatch, capsys):
    def boom(*a, **k):
        raise common.ConfigError("sec_contact_email is a placeholder")
    monkeypatch.setattr(screen, "run", boom)
    assert screen.main(["--sector", "utilities", "--held", "", "--as-of", AS_OF]) == 2
    assert "config error: sec_contact_email" in capsys.readouterr().err


# --------------------------------------------------------------------------- rotation (new names every run)
def _write_screen(root: Path, run_id: str, sector: str, shortlist: list[str]) -> None:
    p = root / run_id / "phase3" / f"screen_{sector}.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"shortlist": shortlist}), encoding="utf-8")


def test_recent_shortlisted_reads_the_newest_runs_that_screened_the_sector(tmp_path):
    _write_screen(tmp_path, "2026-10-01_1000", "health_care", ["AAA", "BBB"])
    _write_screen(tmp_path, "2026-10-02_1000", "health_care", ["BBB", "CCC"])
    _write_screen(tmp_path, "2026-10-03_1000", "health_care", ["DDD"])
    _write_screen(tmp_path, "2026-10-04_1000_dryrun", "health_care", ["ZZZ"])      # dry runs never count
    _write_screen(tmp_path, "2026-10-05_1000", "energy", ["EEE"])                  # other sector
    (tmp_path / "2026-10-06_1000").mkdir()                                         # e.g. a --quick run: no screen file
    assert screen.recent_shortlisted("health_care", tmp_path, lookback=2) == ["DDD", "BBB", "CCC"]
    assert screen.recent_shortlisted("health_care", tmp_path, exclude_run="2026-10-03_1000",
                                     lookback=2) == ["BBB", "CCC", "AAA"]
    assert screen.recent_shortlisted("health_care", tmp_path, lookback=1) == ["DDD"]
    assert screen.recent_shortlisted("health_care", tmp_path, lookback=0) == []
    assert screen.recent_shortlisted("health_care", tmp_path / "missing", lookback=3) == []


def test_recent_shortlisted_ignores_unreadable_files(tmp_path):
    _write_screen(tmp_path, "2026-10-01_1000", "energy", ["EOG"])
    bad = tmp_path / "2026-10-02_1000" / "phase3" / "screen_energy.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json", encoding="utf-8")
    assert screen.recent_shortlisted("energy", tmp_path, lookback=2) == ["EOG"]


def test_recent_tickers_are_skipped_so_new_names_are_proposed(fx):
    table = {t: _metrics(10 + i, 10 + i, 10 + i, 1 + i, 30 - i, i, i) for i, t in enumerate(XLV_TOP10)}
    first = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg())
    second = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg(),
                        recent=first["shortlist"])
    assert not set(first["shortlist"]) & set(second["shortlist"])
    assert {e["ticker"] for e in second["excluded"] if e["reason"] == "recent"} == set(first["shortlist"])
    assert second["filters"]["recent"] == sorted(first["shortlist"])
    # 10 names, 6 skipped -> 4 fresh names for 6 slots: not padded with old ones, and the shortfall is flagged
    assert len(second["shortlist"]) == 4
    assert {"short_universe", "rotation_exhausted"} <= {f["code"] for f in second["flags"]}
    assert not _schema_errors(second, "screen")


def test_rotation_without_a_shortfall_raises_no_flag(fx):
    table = {t: _metrics(10 + i, 10 + i, 10 + i, 1 + i, 30 - i, i, i) for i, t in enumerate(XLV_TOP10)}
    payload = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg(),
                         recent=["lly"])                                            # normalised, one name skipped
    assert "LLY" not in payload["shortlist"] and len(payload["shortlist"]) == 6
    assert "rotation_exhausted" not in {f["code"] for f in payload["flags"]}
    # no recent list -> behaviour unchanged
    base = screen.run("health_care", held=[], as_of=AS_OF, gather=_gatherer(table), universe_cfg=_ucfg())
    assert base["filters"]["recent"] == [] and not [e for e in base["excluded"] if e["reason"] == "recent"]
