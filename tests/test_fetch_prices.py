"""fetch_prices.py (DESIGN §4.1): provider chain cache -> yfinance -> Stooq -> stale cache,
bot-wall detection (never bypassed), prices/v1 payload + schema, load_series, CLI.

All offline: fixture mode (tests/fixtures/prices, SYNTHETIC data) or monkeypatched seams."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from conftest import REPO_ROOT, make_ohlcv
from scripts import common, fetch_prices

FIX = REPO_ROOT / "tests" / "fixtures" / "prices"
SCHEMAS = REPO_ROOT / "schemas"


# --------------------------------------------------------------------------- helpers
def _schema_errors(doc: dict, name: str) -> list[str]:
    resources = []
    for p in sorted(SCHEMAS.glob("*.json")) + sorted((SCHEMAS / "data").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "$id" in d:
            resources.append((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)))
    schema = json.loads((SCHEMAS / "data" / f"{name}.schema.json").read_text(encoding="utf-8"))
    v = Draft202012Validator(schema, registry=Registry().with_resources(resources),
                             format_checker=FormatChecker())
    doc = json.loads(common.dumps_json(doc))  # exactly what lands on disk
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(doc)]


def _fixture_frame(name: str) -> pd.DataFrame:
    return common.clean_ohlcv(pd.read_csv(FIX / "yf" / "history" / f"{name}.csv", encoding="utf-8"))


def _seed_cache(ticker: str, df: pd.DataFrame, *, hours_old: float, provider: str = "yfinance") -> None:
    cache = common.DiskCache()
    key = fetch_prices.cache_key(ticker)
    cache.put_frame("prices", key, df, common.yf_history_url(ticker),
                    meta={"provider": provider, "ticker": ticker, "period": "3y"})
    p = cache.path_for("prices", key)
    entry = common.read_json(p)
    entry["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat(timespec="seconds")
    common.write_json(p, entry)


def _codes(payload: dict) -> dict[str, str]:
    return {f["code"]: f["severity"] for f in payload["flags"]}


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def boom(*a, **k):
        calls.append(str(a[:1]))
        raise AssertionError("network seam must not be called")

    monkeypatch.setattr(common, "http_get", boom)
    monkeypatch.setattr(common, "http_get_text", boom)
    monkeypatch.setattr(common, "yf_history", boom)
    return calls


# --------------------------------------------------------------------------- yfinance path
def test_fixture_run_yfinance_payload_and_schema(use_fixtures, tmp_path):
    use_fixtures("prices")
    out = tmp_path / "phase1" / "prices.json"
    payload = fetch_prices.run(["ko", "SPY", "KO"], out)
    assert out.is_file()
    on_disk = common.read_json(out)
    assert on_disk["schema"] == "prices/v1"
    assert _schema_errors(on_disk, "prices") == []

    assert list(payload["tickers"]) == ["KO", "SPY"]
    assert payload["failed"] == {}
    assert payload["as_of"] == "2026-10-02"
    ko = payload["tickers"]["KO"]
    df = _fixture_frame("KO")
    assert ko["provider"] == "yfinance" and ko["stale"] is False
    assert ko["rows"] == len(df) == 300
    assert ko["last_date"] == "2026-10-02" and ko["first_date"] == common.date_str(df.index[0])
    lc = ko["last_close"]
    assert lc["value"] == pytest.approx(df["Close"].iloc[-1])
    assert (lc["unit"], lc["source_id"], lc["as_of"], lc["kind"]) == ("USD/share", "D:PX:KO", "2026-10-02", "price")

    tail = df.tail(63)
    assert ko["adv_usd_63d"]["value"] == pytest.approx(float((tail["Close"] * tail["Volume"]).mean()), rel=1e-9)
    assert ko["adv_usd_63d"]["unit"] == "USD"
    c = df["Close"]
    for label, bars in {"1m": 21, "3m": 63, "6m": 126, "12m": 252}.items():
        fact = ko["returns_pct"][label]
        assert fact["value"] == pytest.approx((c.iloc[-1] / c.iloc[-1 - bars] - 1) * 100, abs=1e-4)
        assert fact["unit"] == "pct" and fact["as_of"] == "2026-10-02"

    # series cached as data/cache/prices/prices_<T>.csv
    series = Path(ko["series_path"])
    assert series.name == "prices_KO.csv" and series.is_file()
    assert series.parent.name == "prices"

    # sources: one D:PX per served ticker + FX, yfinance URLs, local path
    ids = [s["id"] for s in payload["sources"]]
    assert ids == ["D:PX:KO", "D:PX:SPY", "D:PX:ILS=X"]
    ko_src = payload["sources"][0]
    assert ko_src["url"] == "https://finance.yahoo.com/quote/KO/history/"
    assert ko_src["kind"] == "dataset" and ko_src["path"].endswith("prices_KO.csv")

    # FX from the configured fx_ticker; not listed as a requested ticker
    fx = payload["fx"]["usdils"]
    assert fx["value"] == pytest.approx(_fixture_frame("ILS=X")["Close"].iloc[-1])
    assert (fx["unit"], fx["source_id"]) == ("ILS/USD", "D:PX:ILS=X")
    assert "ILS=X" not in payload["tickers"]
    assert not [f for f in payload["flags"] if f["severity"] == "high"]


def test_requested_fx_ticker_is_listed_and_reused(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["KO", "ILS=X"])
    fx_entry = payload["tickers"]["ILS=X"]
    assert fx_entry["last_close"]["unit"] == "ILS/USD"
    assert fx_entry["adv_usd_63d"]["value"] is None
    assert fx_entry["adv_usd_63d"]["na_reason"] == "not_applicable"
    assert payload["fx"]["usdils"]["value"] == fx_entry["last_close"]["value"]
    assert _schema_errors(payload, "prices") == []


def test_second_run_is_served_from_cache(use_fixtures, monkeypatch):
    use_fixtures("prices")
    first = fetch_prices.run(["KO"])
    calls = _no_network(monkeypatch)
    second = fetch_prices.run(["KO"])
    assert calls == []
    assert second["tickers"]["KO"]["provider"] == "yfinance"   # provider that originally served it
    assert second["tickers"]["KO"]["stale"] is False
    assert second["tickers"]["KO"]["last_close"] == first["tickers"]["KO"]["last_close"]
    assert second["fx"]["usdils"]["value"] == first["fx"]["usdils"]["value"]


def test_as_of_truncates_bars_and_price_lag_flag(use_fixtures):
    use_fixtures("prices")
    early = fetch_prices.run(["KO"], as_of="2026-09-30", include_fx=False)
    ko = early["tickers"]["KO"]
    assert ko["last_date"] == "2026-09-30" and ko["rows"] == 298
    assert early["as_of"] == "2026-09-30"
    late = fetch_prices.run(["KO"], as_of="2026-10-20", include_fx=False)
    assert _codes(late).get("price_lag") == "medium"
    assert "price_lag" not in _codes(early)


# --------------------------------------------------------------------------- Stooq path
def test_yfinance_failure_falls_back_to_stooq(use_fixtures):
    use_fixtures("prices")      # no yf/history/MSFT.csv -> FixtureMissing -> Stooq fixture
    payload = fetch_prices.run(["MSFT"], include_fx=False)
    msft = payload["tickers"]["MSFT"]
    assert msft["provider"] == "stooq" and msft["stale"] is False
    stooq = common.clean_ohlcv(pd.read_csv(FIX / "http" / "stooq" / "msft_us.csv", encoding="utf-8"))
    assert msft["last_close"]["value"] == pytest.approx(stooq["Close"].iloc[-1])
    src = payload["sources"][0]
    assert src["id"] == "D:PX:MSFT" and src["url"] == "https://stooq.com/q/d/l/?s=msft.us&i=d"
    assert _codes(payload).get("provider_fallback") == "low"
    assert _schema_errors(payload, "prices") == []
    # cached with its provider: a later cache hit still reports stooq
    again = fetch_prices.run(["MSFT"], include_fx=False)
    assert again["tickers"]["MSFT"]["provider"] == "stooq"
    assert again["sources"][0]["url"].startswith("https://stooq.com/")


def test_fx_via_stooq_symbol_when_yfinance_down(use_fixtures, monkeypatch):
    use_fixtures("prices")

    def down(ticker, period="3y", **_kw):
        raise common.DataUnavailable(f"yfinance down for {ticker}")

    monkeypatch.setattr(common, "yf_history", down)
    payload = fetch_prices.run(["MSFT"])
    usdils = common.clean_ohlcv(pd.read_csv(FIX / "http" / "stooq" / "usdils.csv", encoding="utf-8"))
    fx = payload["fx"]["usdils"]
    assert fx["value"] == pytest.approx(usdils["Close"].iloc[-1])
    assert fx["unit"] == "ILS/USD"
    fx_src = [s for s in payload["sources"] if s["id"] == "D:PX:ILS=X"][0]
    assert fx_src["url"] == "https://stooq.com/q/d/l/?s=usdils&i=d"
    assert _schema_errors(payload, "prices") == []


def test_bot_wall_detection_unit():
    html = (FIX / "http" / "stooq" / "bot_wall.html").read_text(encoding="utf-8")
    assert fetch_prices.is_bot_wall(html)
    assert fetch_prices.is_bot_wall("<html><body>Please enable JavaScript</body></html>")
    assert fetch_prices.is_bot_wall("  Checking your browser... verify your browser to continue")
    assert not fetch_prices.is_bot_wall("Date,Open,High,Low,Close,Volume\n2026-10-01,1,2,0.5,1.5,100\n")
    assert not fetch_prices.is_bot_wall("")
    with pytest.raises(common.ProviderBlocked):
        fetch_prices.parse_stooq_csv(html, symbol="blck.us")
    with pytest.raises(common.ProviderBlocked):
        fetch_prices.parse_stooq_csv("No data", symbol="nodt.us")
    with pytest.raises(common.ProviderBlocked):
        fetch_prices.parse_stooq_csv("Exceeded the daily hits limit", symbol="x.us")
    with pytest.raises(common.DataUnavailable) as ei:
        fetch_prices.parse_stooq_csv("something odd", symbol="x.us")
    assert not isinstance(ei.value, common.ProviderBlocked)
    df = fetch_prices.parse_stooq_csv("Date,Open,High,Low,Close\n2026-10-01,3.7,3.8,3.6,3.75\n", symbol="usdils")
    assert list(df.columns) == common.OHLCV_COLUMNS and df["Volume"].iloc[0] == 0.0


def test_fetch_stooq_bot_wall_raises_blocked_in_fixture_mode(use_fixtures):
    use_fixtures("prices")
    with pytest.raises(common.ProviderBlocked):
        fetch_prices.fetch_stooq("blck.us")


def test_bot_wall_then_stale_cache_is_flagged(use_fixtures):
    use_fixtures("prices")
    old = make_ohlcv(n=300, seed=4, end="2026-10-01")
    _seed_cache("BLCK", old, hours_old=24 * 30)
    payload = fetch_prices.run(["BLCK"], include_fx=False)
    blck = payload["tickers"]["BLCK"]
    assert blck["provider"] == "cache_stale" and blck["stale"] is True
    assert blck["last_close"]["value"] == pytest.approx(old["Close"].iloc[-1])
    assert blck["last_date"] == "2026-10-01"
    codes = _codes(payload)
    assert codes["stale_cache"] == "high"
    assert codes["provider_blocked"] == "medium"
    stale_flag = [f for f in payload["flags"] if f["code"] == "stale_cache"][0]
    assert "yfinance" in stale_flag["detail"] and "stooq blocked" in stale_flag["detail"]
    assert payload["failed"] == {}
    assert "(stale cache)" in payload["sources"][0]["title"]
    assert _schema_errors(payload, "prices") == []


def test_bot_wall_without_cache_lands_in_failed(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["BLCK", "KO"], include_fx=False)
    assert list(payload["tickers"]) == ["KO"]
    assert "BLCK" in payload["failed"] and "stooq blocked" in payload["failed"]["BLCK"]
    assert any("BLCK" in m for m in payload["missing_data"])
    assert _codes(payload)["fetch_failed"] == "high"
    assert _schema_errors(payload, "prices") == []


def test_no_data_and_http_404_are_failures(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["NODT", "GONE"], include_fx=False)
    assert "stooq blocked" in payload["failed"]["NODT"]
    assert "stooq:" in payload["failed"]["GONE"] and "404" in payload["failed"]["GONE"]


def test_stooq_fallback_disabled_in_config(tmp_env, use_fixtures, monkeypatch):
    def disable(cfg):
        cfg["prices"]["stooq_fallback"] = False

    tmp_env.update_config("committee", disable)
    use_fixtures("prices")
    calls: list[str] = []
    real = common.http_get

    def spy(url, **kw):
        calls.append(url)
        return real(url, **kw)

    monkeypatch.setattr(common, "http_get", spy)
    payload = fetch_prices.run(["MSFT"], include_fx=False)
    assert "MSFT" in payload["failed"] and "fallback disabled" in payload["failed"]["MSFT"]
    assert calls == []


def test_unmappable_ticker_skips_stooq(monkeypatch):
    calls = _no_network(monkeypatch)
    monkeypatch.setattr(common, "yf_history", lambda *a, **k: (_ for _ in ()).throw(common.DataUnavailable("x")))
    with pytest.raises(fetch_prices.PriceFetchError) as ei:
        fetch_prices.fetch_series("TEVA.TA")
    assert calls == []
    assert any("no Stooq symbol" in e for e in ei.value.errors)


@pytest.mark.parametrize("ticker,symbol", [
    ("KO", "ko.us"), ("brk.b", "brk-b.us"), ("ILS=X", "usdils"), ("^GSPC", None), ("TEVA.TA", None),
    ("EURUSD=X", None),
])
def test_stooq_symbol_mapping(ticker, symbol):
    assert fetch_prices.stooq_symbol(ticker) == symbol


def test_trim_period():
    df = make_ohlcv(n=1300, seed=3)
    last = df.index[-1]
    three = fetch_prices.trim_period(df, "3y")
    assert three.index[0] >= last - pd.DateOffset(years=3)
    assert len(three) < len(df)
    assert len(fetch_prices.trim_period(df, "max")) == len(df)
    six = fetch_prices.trim_period(df, "6mo")
    assert six.index[0] >= last - pd.DateOffset(months=6)
    assert len(fetch_prices.trim_period(df, "weird")) == len(df)


# --------------------------------------------------------------------------- offline / failures
def test_offline_without_cache_fails_cleanly_and_validates():
    payload = fetch_prices.run(["KO"])
    assert payload["tickers"] == {}
    assert "KO" in payload["failed"] and "offline" in payload["failed"]["KO"].lower()
    assert payload["fx"]["usdils"] is None
    assert any("USD/ILS" in m for m in payload["missing_data"])
    assert _codes(payload)["fx_unavailable"] == "medium"
    # only the requested ticker gets a fetch_failed flag, not the by-product FX series
    ff = [f for f in payload["flags"] if f["code"] == "fetch_failed"]
    assert len(ff) == 1 and "KO" in ff[0]["detail"]
    assert _schema_errors(payload, "prices") == []


def test_unrequested_fx_failure_is_medium_fx_unavailable_only():
    """FX fetched only to fill fx.usdils: a failure is one medium fx_unavailable flag, never a
    high-severity fetch_failed, and it does not land in failed{}."""
    _seed_cache("KO", make_ohlcv(n=300, seed=8), hours_old=1)
    payload = fetch_prices.run(["KO"])
    assert "KO" in payload["tickers"] and payload["failed"] == {}
    assert payload["fx"]["usdils"] is None
    codes = _codes(payload)
    assert "fetch_failed" not in codes
    assert codes["fx_unavailable"] == "medium"
    assert not [f for f in payload["flags"] if f["severity"] == "high"]
    fx_flag = next(f for f in payload["flags"] if f["code"] == "fx_unavailable")
    assert "ILS=X" in fx_flag["detail"] and "offline" in fx_flag["detail"].lower()
    assert _schema_errors(payload, "prices") == []


def test_requested_fx_failure_is_fetch_failed():
    payload = fetch_prices.run(["ILS=X"])
    assert "ILS=X" in payload["failed"]
    assert _codes(payload)["fetch_failed"] == "high"
    assert _codes(payload)["fx_unavailable"] == "medium"


def test_offline_with_stale_cache_serves_flagged_data():
    df = make_ohlcv(n=300, seed=8)
    _seed_cache("KO", df, hours_old=100)
    payload = fetch_prices.run(["KO"], include_fx=False)
    ko = payload["tickers"]["KO"]
    assert ko["provider"] == "cache_stale" and ko["stale"] is True
    assert _codes(payload)["stale_cache"] == "high"
    assert _schema_errors(payload, "prices") == []


def test_summarize_short_history_marks_insufficient():
    df = make_ohlcv(n=30, seed=2)
    s = fetch_prices.summarize("KO", df)
    assert s["rows"] == 30
    assert s["returns_pct"]["1m"]["value"] is not None
    assert s["returns_pct"]["3m"]["value"] is None
    assert s["returns_pct"]["3m"]["na_reason"] == "insufficient_history"
    assert s["adv_usd_63d"]["na_reason"] == "insufficient_history"
    df0 = make_ohlcv(n=80, seed=2)
    df0["Volume"] = 0.0
    assert fetch_prices.summarize("KO", df0)["adv_usd_63d"]["na_reason"] == "missing"


# --------------------------------------------------------------------------- load_series
def test_load_series_returns_cache_and_never_touches_network(monkeypatch):
    df = make_ohlcv(n=120, seed=9)
    _seed_cache("JPM", df, hours_old=1000)
    calls = _no_network(monkeypatch)
    got = fetch_prices.load_series("jpm")
    assert calls == []
    assert got is not None and len(got) == 120
    assert list(got.columns) == common.OHLCV_COLUMNS
    assert isinstance(got.index, pd.DatetimeIndex)
    assert np.allclose(got["Close"].to_numpy(), df["Close"].to_numpy())
    assert got.attrs["stale"] is True
    assert fetch_prices.load_series("NOPE") is None


def test_load_series_fresh_cache_not_stale(use_fixtures):
    use_fixtures("prices")
    fetch_prices.run(["KO"], include_fx=False)
    got = fetch_prices.load_series("KO")
    assert got is not None and got.attrs["stale"] is False


# --------------------------------------------------------------------------- CLI
def test_cli_main_fixture_mode(use_fixtures, tmp_path, capsys):
    use_fixtures("prices")
    out = tmp_path / "prices.json"
    rc = fetch_prices.main(["--tickers", "KO,SPY", "--ticker", "XLP", "--out", str(out)])
    assert rc == 0
    doc = common.read_json(out)
    assert sorted(doc["tickers"]) == ["KO", "SPY", "XLP"]
    assert _schema_errors(doc, "prices") == []
    summary = json.loads(capsys.readouterr().out)
    assert summary["tickers"]["KO"]["provider"] == "yfinance"
    assert summary["fx_usdils"] == pytest.approx(doc["fx"]["usdils"]["value"])


def test_cli_all_failed_returns_1(tmp_path, capsys):
    rc = fetch_prices.main(["--ticker", "KO", "--out", str(tmp_path / "p.json"), "--no-fx"])
    assert rc == 1
    assert "KO" in json.loads(capsys.readouterr().out)["failed"]


# --------------------------------------------------------------------------- non-USD quotes (Tel Aviv)
# LUMI.TA / TEVA.TA / AZN.L / SAP.DE fixtures are SYNTHETIC. LUMI.TA closes at 7522.0 agorot (ILS 75.22);
# the FX fixture's last USD/ILS close is 3.7958.
FX_LAST = 3.7958
LUMI_NATIVE_CLOSE = 7522.0


def _usd_per_agora() -> float:
    df = _fixture_frame("ILS=X")
    return 0.01 / float(df["Close"].iloc[-1])


def test_agorot_series_converted_to_usd_in_run(use_fixtures, tmp_path):
    use_fixtures("prices")
    out = tmp_path / "prices.json"
    payload = fetch_prices.run(["LUMI.TA", "SPY"], out)
    assert _schema_errors(common.read_json(out), "prices") == []
    e = payload["tickers"]["LUMI.TA"]
    expected = LUMI_NATIVE_CLOSE * 0.01 / FX_LAST
    assert e["last_close"]["unit"] == "USD/share"
    assert e["last_close"]["value"] == pytest.approx(expected, rel=1e-6)
    native = e["native"]
    assert native["quote_currency"] == "ILA" and native["currency"] == "ILS"
    assert native["last_close"]["value"] == pytest.approx(75.22, abs=1e-3)
    assert native["last_close"]["unit"] == "ILS/share"
    assert native["usdils"] == pytest.approx(FX_LAST, rel=1e-6)
    assert "converted_to_usd" in _codes(payload)
    # a US ticker is untouched and has no native block
    assert "native" not in payload["tickers"]["SPY"]


def test_conversion_preserves_local_returns_and_scales_dollar_volume(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["LUMI.TA"])
    e = payload["tickers"]["LUMI.TA"]
    raw = _fixture_frame("LUMI.TA")
    for label, bars in (("1m", 21), ("3m", 63)):
        local = (raw["Close"].iloc[-1] / raw["Close"].iloc[-1 - bars] - 1) * 100
        assert e["returns_pct"][label]["value"] == pytest.approx(local, abs=1e-3)
    tail = raw.tail(63)
    adv_usd = float((tail["Close"] * tail["Volume"]).mean()) * _usd_per_agora()
    assert e["adv_usd_63d"]["value"] == pytest.approx(adv_usd, rel=1e-4)
    # treating agorot as dollars would have inflated ADV by 1 / (0.01 / FX) ~ 380x
    assert e["adv_usd_63d"]["value"] < float((tail["Close"] * tail["Volume"]).mean()) / 10


def test_cache_keeps_native_quote_and_currency(use_fixtures):
    use_fixtures("prices")
    fetch_prices.run(["LUMI.TA"])
    cache = common.DiskCache()
    df = cache.get_frame_stale("prices", fetch_prices.cache_key("LUMI.TA"))
    assert float(df["Close"].iloc[-1]) == pytest.approx(LUMI_NATIVE_CLOSE)
    assert df.attrs["meta"]["currency"] == "ILA"


def test_load_series_converts_from_cache_without_network(use_fixtures, monkeypatch):
    use_fixtures("prices")
    fetch_prices.run(["LUMI.TA"])  # caches LUMI.TA and ILS=X
    _no_network(monkeypatch)
    df = fetch_prices.load_series("LUMI.TA")
    assert df is not None
    assert float(df["Close"].iloc[-1]) == pytest.approx(LUMI_NATIVE_CLOSE * 0.01 / FX_LAST, rel=1e-6)
    assert df.attrs["native_currency"] == "ILA"


def test_load_series_returns_none_not_a_wrong_unit_price_without_fx(use_fixtures):
    use_fixtures("prices")
    cache = common.DiskCache()
    raw = _fixture_frame("LUMI.TA")
    cache.put_frame("prices", fetch_prices.cache_key("LUMI.TA"), raw, common.yf_history_url("LUMI.TA"),
                    meta={"provider": "yfinance", "ticker": "LUMI.TA", "period": "3y", "currency": "ILA"})
    assert fetch_prices.load_series("LUMI.TA") is None  # no USD/ILS rate cached


def test_suffix_table_used_when_yahoo_has_no_currency(use_fixtures):
    use_fixtures("prices")  # no yf/info fixture for TEVA.TA -> falls back to the .TA suffix => agorot
    payload = fetch_prices.run(["TEVA.TA"])
    e = payload["tickers"]["TEVA.TA"]
    assert e["native"]["quote_currency"] == "ILA"
    assert e["last_close"]["value"] == pytest.approx(2050.0 * 0.01 / FX_LAST, rel=1e-6)


def test_unsupported_currency_is_refused_not_priced_as_usd(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["AZN.L"])
    assert "AZN.L" not in payload["tickers"]
    assert "not supported" in payload["failed"]["AZN.L"]
    assert _codes(payload)["fetch_failed"] == "high"


def test_unknown_currency_is_refused(use_fixtures):
    use_fixtures("prices")
    payload = fetch_prices.run(["SAP.DE"])
    assert "SAP.DE" not in payload["tickers"]
    assert "currency unknown" in payload["failed"]["SAP.DE"]


def test_ila_without_fx_rate_fails_cleanly(use_fixtures, monkeypatch):
    use_fixtures("prices")
    real = common.yf_history

    def no_fx(ticker, *a, **k):
        if ticker == "ILS=X":
            raise common.DataUnavailable("no fx")
        return real(ticker, *a, **k)

    def no_stooq(*a, **k):
        raise common.DataUnavailable("no stooq")

    monkeypatch.setattr(common, "yf_history", no_fx)
    monkeypatch.setattr(fetch_prices, "fetch_stooq", no_stooq)
    payload = fetch_prices.run(["LUMI.TA"], include_fx=False)
    assert "LUMI.TA" in payload["failed"] and "USD/ILS" in payload["failed"]["LUMI.TA"]


def test_technicals_on_agorot_series_are_in_usd_and_flagged(use_fixtures):
    from scripts import compute_technicals
    use_fixtures("prices")
    t = compute_technicals.run("LUMI.TA", sector_etf="XLP")
    assert t["close"]["value"] == pytest.approx(LUMI_NATIVE_CLOSE * 0.01 / FX_LAST, abs=1e-3)  # 4-dp output
    assert "converted_to_usd" in {f["code"] for f in t["flags"]}


# --------------------------------------------------------------------------- float32 noise in price Facts
def _float32_tree(root: Path, *, ko_last: float = 769.64, lumi_last: float = 7522.37,
                  fx_last: float = 3.7958) -> Path:
    """Fixture tree whose bars carry yfinance's float32 noise (769.64 arrives as 769.6400146484375)."""
    hist = root / "yf" / "history"
    hist.mkdir(parents=True)
    (root / "http").mkdir()
    (root / "http" / "index.json").write_text("{}", encoding="utf-8")
    (root / "yf" / "info").mkdir()
    (root / "yf" / "info" / "LUMI.TA.json").write_text(json.dumps({"currency": "ILA"}), encoding="utf-8")

    def write(name: str, last: float, seed: int, base: float, vol: float) -> None:
        df = make_ohlcv(n=300, seed=seed, start=base)
        df["Close"] = df["Close"] * (last / df["Close"].iloc[-1])  # land exactly on `last` before the cast
        for col in ("Open", "High", "Low"):
            df[col] = df["Close"]
        df["Volume"] = vol
        noisy = df[["Open", "High", "Low", "Close"]].astype("float32").astype("float64")
        noisy["Volume"] = df["Volume"]
        noisy.to_csv(hist / f"{name}.csv", index_label="Date", encoding="utf-8")

    write("KO", ko_last, 11, 700.0, 1e6)
    write("LUMI.TA", lumi_last, 12, 7000.0, 5e5)
    write("ILS=X", fx_last, 13, 3.7, 0.0)
    return root


PRICE_LEVEL_UNITS = ("USD/share", "ILS/share", "ILS/USD")  # quoted levels (not ADV dollars or returns)


def _price_fact_values(node, path=""):
    """Yield (path, Fact) for every price-level Fact with a numeric value in a payload."""
    if isinstance(node, dict):
        if node.get("kind") == "price" and node.get("unit") in PRICE_LEVEL_UNITS                 and isinstance(node.get("value"), float):
            yield path, node
        for k, v in node.items():
            yield from _price_fact_values(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _price_fact_values(v, f"{path}/{i}")


def test_float32_noise_is_not_exposed_in_price_facts(tmp_path, use_fixtures):
    use_fixtures(_float32_tree(tmp_path / "fx"))
    raw = common.clean_ohlcv(pd.read_csv(tmp_path / "fx" / "yf" / "history" / "KO.csv", encoding="utf-8"))
    assert float(raw["Close"].iloc[-1]) == 769.6400146484375  # the input really is noisy
    out = tmp_path / "prices.json"
    payload = fetch_prices.run(["KO"], out)
    ko = payload["tickers"]["KO"]
    assert ko["last_close"]["value"] == 769.64
    assert payload["fx"]["usdils"]["value"] == 3.7958
    text = out.read_text(encoding="utf-8")
    assert "769.64001" not in text and "3.79580" not in text
    facts = list(_price_fact_values(payload))
    assert facts
    for path, f in facts:
        assert f["value"] == common.round_price(f["value"]), (path, f["value"])
        assert len(repr(f["value"]).replace("-", "").replace(".", "").lstrip("0")) <= 12, (path, f["value"])


def test_float32_noise_is_not_exposed_in_the_converted_native_block(tmp_path, use_fixtures):
    use_fixtures(_float32_tree(tmp_path / "fx"))
    payload = fetch_prices.run(["LUMI.TA"])
    e = payload["tickers"]["LUMI.TA"]
    native = e["native"]
    assert native["usdils"] == 3.7958
    assert native["last_close"]["value"] == 75.2237  # 7522.37 agorot = ILS 75.2237
    assert native["last_close"]["unit"] == "ILS/share"
    factor = native["fx_usd_per_native"]
    assert factor == pytest.approx(0.01 / 3.7958, rel=1e-9)
    assert float(f"{factor:.10g}") == factor  # no long float tail
    assert e["last_close"]["value"] == common.round_price(7522.37 * 0.01 / 3.7958)
    assert payload["fx"]["usdils"]["value"] == 3.7958
    for path, f in _price_fact_values(payload):
        assert f["value"] == common.round_price(f["value"]), (path, f["value"])


def test_summarize_rounds_last_close_for_fx_and_equity():
    idx = pd.bdate_range(end="2026-10-02", periods=80)
    close = np.full(80, np.float32(769.64), dtype="float32").astype("float64")
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6}, index=idx)
    df.index.name = "Date"
    s = fetch_prices.summarize("KO", df)
    assert s["last_close"]["value"] == 769.64
    fx = pd.DataFrame({c: np.full(80, np.float32(3.7958), dtype="float32").astype("float64")
                       for c in ("Open", "High", "Low", "Close")}, index=idx)
    fx["Volume"] = 0.0
    fx.index.name = "Date"
    assert fetch_prices.summarize("ILS=X", fx, is_fx=True)["last_close"]["value"] == 3.7958


def test_load_series_conversion_matches_fetch_series_with_noisy_rate(tmp_path, use_fixtures):
    use_fixtures(_float32_tree(tmp_path / "fx"))
    res = fetch_prices.fetch_series("LUMI.TA")
    df = fetch_prices.load_series("LUMI.TA")
    assert df is not None
    assert float(df["Close"].iloc[-1]) == pytest.approx(float(res["frame"]["Close"].iloc[-1]), rel=1e-12)
    assert res["usdils"] == 3.7958 and df.attrs["fx_usd_per_native"] == res["fx_usd_per_native"]


def test_cli_fixture_mode_with_real_cache_dir_is_refused(use_fixtures, tmp_path, monkeypatch, capsys):
    """Fixture data must not be written into <repo>/data/cache: rc 2 with a one-line hint."""
    use_fixtures("prices")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", root)
    monkeypatch.delenv(common.ENV_CACHE_DIR)
    assert fetch_prices.main(["--tickers", "KO"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "real cache dir" in err and "Traceback" not in err
    assert not (root / "data").exists()
    # with an explicit scratch cache the same fixture run works and a second run is served from it
    cache = tmp_path / "scratch"
    assert fetch_prices.main(["--tickers", "KO", "--cache-dir", str(cache)]) == 0
    assert (cache / "prices" / "prices_KO.csv").is_file()
    second = fetch_prices.run(["KO"])
    assert second["tickers"]["KO"]["provider"] == "yfinance" and second["tickers"]["KO"]["stale"] is False
