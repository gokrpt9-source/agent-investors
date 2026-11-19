"""Tests for scripts/common.py, the config files/schemas and the conftest fixtures (unit U0a)."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests
import yaml
from jsonschema import Draft202012Validator

from conftest import DEFAULT_AS_OF, REPO_ROOT, SAMPLE_PORTFOLIO, make_ohlcv, make_ranging_ohlcv
from scripts import common
from scripts.common import (
    ConfigError, DataUnavailable, DiskCache, FixtureMissing, OfflineError, ProviderBlocked,
)

LEDGER_COLUMNS = ("run_id,rec_date,price_date,ticker,asset_type,sector,kind,role,action,rule_action,stance,"
                  "conviction,evidence,rr,fair_value_base,invalidation_price,target_weight_pct,price_at_rec,"
                  "spy_at_rec,sector_etf,sector_etf_at_rec,thesis,run_mode,report_path").split(",")


# =====================================================================================
# helpers
# =====================================================================================


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes | str = b"{}", headers: dict | None = None,
                 url: str = "https://x.test/"):
        self.status_code = status
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers or {}
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def live_http(monkeypatch):
    """Turn offline mode off and replace requests.get with a scripted fake. Records calls and
    sleeps (time.sleep is patched so nothing actually waits)."""
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    state = types.SimpleNamespace(calls=[], sleeps=[], script=[])

    def fake_get(url, headers=None, timeout=None, **kw):
        state.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if not state.script:
            return FakeResponse(200, b'{"ok": true}', url=url)
        item = state.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(common.requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: state.sleeps.append(s))
    return state


def write_fixture_tree(root: Path) -> Path:
    """Minimal AGINV_FIXTURES_DIR tree."""
    (root / "http" / "files").mkdir(parents=True)
    (root / "http" / "files" / "tickers.json").write_text('{"0": {"cik_str": 21344, "ticker": "KO"}}',
                                                          encoding="utf-8")
    (root / "http" / "files" / "dgs10.csv").write_text("observation_date,DGS10\n2026-10-01,4.1\n",
                                                       encoding="utf-8")
    index = {
        "https://www.sec.gov/files/company_tickers.json": "files/tickers.json",
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10": "files/dgs10.csv",
        "https://stooq.com/q/d/l/?s=xyz.us&i=d": {"file": None, "status": 404},
        "https://example.org/blocked": {"status": 403},
    }
    (root / "http" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    hist = root / "yf" / "history"
    hist.mkdir(parents=True)
    df = make_ohlcv(n=30, seed=3)
    df.iloc[5, df.columns.get_loc("Close")] = np.nan
    df = df.iloc[::-1]  # unsorted on purpose
    df.to_csv(hist / "KO.csv", index_label="Date", encoding="utf-8")
    make_ohlcv(n=10, seed=4).to_csv(hist / "BRK-B.csv", index_label="Date", encoding="utf-8")
    info = root / "yf" / "info"
    info.mkdir(parents=True)
    (info / "KO.json").write_text(json.dumps({"sector": "Consumer Defensive", "marketCap": 2.9e11}),
                                  encoding="utf-8")
    funds = root / "yf" / "funds"
    funds.mkdir(parents=True)
    (funds / "QQQ.json").write_text(json.dumps({
        "sector_weightings": {"technology": 0.52, "realestate": 0.002},
        "top_holdings": [{"symbol": "NVDA", "name": "NVIDIA Corp", "holding_pct": 9.1}],
        "expense_ratio_pct": 0.18, "expense_ratio_field": "info.netExpenseRatio"}), encoding="utf-8")
    return root


# =====================================================================================
# constants
# =====================================================================================


def test_disclaimer_exact():
    assert common.DISCLAIMER == (
        "Research output for decision support only — not personalized financial advice. Figures come "
        "from free public sources that may be delayed or incomplete. The investor makes all decisions.")


def test_role_and_agent_constants():
    assert common.ROLE_KEYS == ("fundamental", "technical", "operator_ceo", "industry_ops", "osint",
                                "devils_advocate", "etf", "macro", "cio", "sector_head")
    assert set(common.AGENT_TYPES) == set(common.ROLE_KEYS)
    assert common.AGENT_TYPES["osint"] == "osint-sentiment"
    assert common.AGENT_TYPES["devils_advocate"] == "devils-advocate"
    assert common.AGENT_TYPES["macro"] == "macro-geopolitics"
    assert common.role_schema("cio", "allocation") == "cio_allocation"
    assert common.role_schema("cio", "reconciliation") == "cio_final"
    assert common.role_schema("technical") == "specialist_report"
    assert common.role_schema("etf") == "etf_review"


def test_schema_files_map():
    sf = common.SCHEMA_FILES
    assert sf["specialist_report"] == "specialist_report.json"
    assert sf["prices"] == "data/prices.schema.json"
    assert sf["config_committee"] == "config_committee.schema.json"
    for name in common.CONFIG_NAMES:
        assert f"config_{name}" in sf
    assert common.schema_path("config_portfolio") == REPO_ROOT / "schemas" / "config_portfolio.schema.json"
    with pytest.raises(KeyError):
        common.schema_path("nope")
    assert common.schema_id("specialist_report") == "urn:agent-investors:specialist_report"
    assert common.schema_id("prices") == "urn:agent-investors:data/prices"
    assert common.schema_id("config_portfolio") == "urn:agent-investors:config_portfolio"
    with pytest.raises(KeyError):
        common.schema_id("nope")


def test_schema_ids_match_files_on_disk():
    """Integration check: every schema file that exists declares the $id schema_id() predicts."""
    for name in common.SCHEMA_FILES:
        path = common.schema_path(name)
        if path.is_file():
            assert common.read_json(path).get("$id") == common.schema_id(name), name


def test_fact_kinds_and_na_reasons():
    assert common.FACT_KINDS == ("price", "fundamental", "macro", "other")
    assert str(common.FactKind.PRICE) == "price" and f"{common.FactKind.MACRO}" == "macro"
    assert common.FactKind.PRICE == "price"
    assert set(common.NA_UNPENALIZED) <= set(common.NA_REASONS)


# =====================================================================================
# paths + CLI flags
# =====================================================================================


def test_default_paths(monkeypatch):
    for v in (common.ENV_CONFIG_DIR, common.ENV_CACHE_DIR, common.ENV_RUNS_DIR, common.ENV_LEDGER):
        monkeypatch.delenv(v, raising=False)
    assert common.config_dir() == REPO_ROOT / "config"
    assert common.cache_dir() == REPO_ROOT / "data" / "cache"
    assert common.runs_dir() == REPO_ROOT / "runs"
    assert common.ledger_path() == REPO_ROOT / "ledger" / "recommendations.csv"
    assert common.schemas_dir() == REPO_ROOT / "schemas"
    assert common.knowledge_dir() == REPO_ROOT / "knowledge"
    assert common.templates_dir() == REPO_ROOT / "templates"


def test_env_paths(monkeypatch, tmp_path):
    monkeypatch.setenv(common.ENV_CONFIG_DIR, str(tmp_path / "c"))
    monkeypatch.setenv(common.ENV_CACHE_DIR, str(tmp_path / "k"))
    monkeypatch.setenv(common.ENV_RUNS_DIR, str(tmp_path / "r"))
    monkeypatch.setenv(common.ENV_LEDGER, str(tmp_path / "l.csv"))
    assert common.config_dir() == (tmp_path / "c").resolve()
    assert common.cache_dir() == (tmp_path / "k").resolve()
    assert common.runs_dir() == (tmp_path / "r").resolve()
    assert common.ledger_path() == (tmp_path / "l.csv").resolve()


def test_hermetic_defaults():
    assert os.environ[common.ENV_OFFLINE] == "1"
    assert os.environ[common.ENV_AS_OF] == DEFAULT_AS_OF
    assert common.is_offline()
    assert common.active_fixtures_dir() is None
    assert REPO_ROOT not in common.cache_dir().parents  # tests never write the repo cache


def test_common_args_roundtrip(monkeypatch, tmp_path):
    p = argparse.ArgumentParser()
    common.add_common_args(p)
    args = p.parse_args(["--config-dir", str(tmp_path / "cfg"), "--cache-dir", str(tmp_path / "cache"),
                         "--runs-dir", str(tmp_path / "runs"), "--ledger", str(tmp_path / "led.csv"),
                         "--as-of", "2026-09-30"])
    common.apply_common_args(args)
    assert os.environ[common.ENV_CONFIG_DIR] == str((tmp_path / "cfg").resolve())
    assert os.environ[common.ENV_CACHE_DIR] == str((tmp_path / "cache").resolve())
    assert os.environ[common.ENV_LEDGER] == str((tmp_path / "led.csv").resolve())
    assert common.today() == date(2026, 9, 30)
    # the autouse fixture restores every AGINV_* variable after the test


def test_env_vars_set_directly_do_not_leak_part1():
    os.environ[common.ENV_CONFIG_DIR] = "C:/leak/test"  # simulates apply_common_args


def test_env_vars_set_directly_do_not_leak_part2():
    assert common.ENV_CONFIG_DIR not in os.environ


def test_common_args_exclude_and_defaults():
    p = argparse.ArgumentParser()
    p.add_argument("--as-of")  # a script defining its own --as-of
    common.add_common_args(p, exclude=("as_of",))
    args = p.parse_args([])
    assert args.config_dir is None and args.ledger is None
    common.apply_common_args(args)  # all None -> no-op


def test_apply_common_args_bad_date():
    ns = argparse.Namespace(as_of="not-a-date")
    with pytest.raises(ConfigError, match="--as-of"):
        common.apply_common_args(ns)


# =====================================================================================
# pure helpers
# =====================================================================================


@pytest.mark.parametrize("value,expected", [
    (1, 1.0), ("2.5", 2.5), (np.float32(1.5), 1.5), (Decimal("3.25"), 3.25),
    (None, None), (True, None), (np.bool_(False), None), (float("nan"), None), (float("inf"), None),
    ("abc", None), (pd.NA, None), ([1], None),
])
def test_safe_float(value, expected):
    assert common.safe_float(value) == expected


def test_to_jsonable_types(tmp_path):
    class Color(Enum):
        RED = "red"

    df = pd.DataFrame({"a": [1.0, np.nan]}, index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
    obj = {
        "nan": float("nan"), "inf": -np.inf, "np_int": np.int64(7), "np_float": np.float64(1.5),
        "np_bool": np.bool_(True), "dec": Decimal("1.1"), "d": date(2026, 10, 2),
        "dt": datetime(2026, 10, 2, 13, 5, tzinfo=timezone.utc), "ts_mid": pd.Timestamp("2026-10-02"),
        "ts_time": pd.Timestamp("2026-10-02 10:30"), "nat": pd.NaT, "path": tmp_path / "x.json",
        "enum": Color.RED, "kind": common.FactKind.PRICE, "set": {3, 1, 2}, "tuple": (1, 2),
        "arr": np.array([1.0, np.nan]), "series": pd.Series({"x": 1.0, "y": np.nan}), "df": df,
        date(2026, 1, 1): "date-key", "bytes": "é".encode("utf-8"),
    }
    out = common.to_jsonable(obj)
    json.dumps(out, allow_nan=False)  # must not raise
    assert out["nan"] is None and out["inf"] is None
    assert out["np_int"] == 7 and isinstance(out["np_int"], int)
    assert out["np_float"] == 1.5 and type(out["np_float"]) is float
    assert out["np_bool"] is True
    assert out["dec"] == 1.1
    assert out["d"] == "2026-10-02"
    assert out["dt"] == "2026-10-02T13:05:00+00:00"
    assert out["ts_mid"] == "2026-10-02"
    assert out["ts_time"].startswith("2026-10-02T10:30")
    assert out["nat"] is None
    assert out["path"] == (tmp_path / "x.json").as_posix()
    assert out["enum"] == "red" and out["kind"] == "price"
    assert out["set"] == [1, 2, 3] and out["tuple"] == [1, 2]
    assert out["arr"] == [1.0, None]
    assert out["series"] == {"x": 1.0, "y": None}
    assert out["df"] == {"index": ["2026-01-02", "2026-01-05"], "columns": ["a"], "data": [[1.0], [None]]}
    assert out["2026-01-01"] == "date-key"
    assert out["bytes"] == "é"


@pytest.mark.parametrize("raw,expected", [
    ("brk.b", "BRK-B"), (" ko ", "KO"), ("BRK/B", "BRK-B"), ("bf.b", "BF-B"), ("TEVA.TA", "TEVA.TA"),
    ("ILS=X", "ILS=X"), ("^vix", "^VIX"), ("spy", "SPY"), ("rds.a.l", "RDS-A.L"),
])
def test_norm_ticker(raw, expected):
    assert common.norm_ticker(raw) == expected


def test_norm_ticker_rejects_empty():
    with pytest.raises(ValueError):
        common.norm_ticker("  ")
    with pytest.raises(ValueError):
        common.norm_ticker(None)


@pytest.mark.parametrize("raw,expected", [
    # US share classes that must map to the dash form (MKC.V is McCormick's class V, not TSX-Venture)
    ("MKC.V", "MKC-V"), ("mkc.v", "MKC-V"), (" MKC . V ", "MKC-V"), ("MKC/V", "MKC-V"),
    ("BF.B", "BF-B"), ("BRK.B", "BRK-B"), ("BRK.A", "BRK-A"), ("LEN.B", "LEN-B"), ("HEI.A", "HEI-A"),
    ("CRD.A", "CRD-A"), ("CRD.B", "CRD-B"), ("GEF.B", "GEF-B"), ("MOG.A", "MOG-A"), ("MOG.B", "MOG-B"),
    ("UHAL.B", "UHAL-B"), ("LGF.A", "LGF-A"), ("LGF.B", "LGF-B"), ("RDS.A", "RDS-A"), ("RDS.B", "RDS-B"),
    # real exchange suffixes keep the dot
    ("TEVA.TA", "TEVA.TA"), ("LUMI.TA", "LUMI.TA"), ("AZN.L", "AZN.L"), ("SAP.DE", "SAP.DE"),
    ("lumi.ta", "LUMI.TA"), ("BA.L", "BA.L"), ("RY.TO", "RY.TO"), ("ABX.V", "ABX.V"), ("7203.T", "7203.T"),
    ("0700.HK", "0700.HK"), ("BRK.B.TO", "BRK-B.TO"), ("RDS.A.L", "RDS-A.L"),
    # one-letter base: a one-letter suffix is a share-class letter, a multi-letter one an exchange
    ("X.V", "X-V"), ("A.L", "A-L"), ("T.TO", "T.TO"), ("L.TO", "L.TO"), ("H.TA", "H.TA"),
    # a share class whose letter is not an exchange suffix was always dashed
    ("ZZZ.B", "ZZZ-B"), ("AKO.A", "AKO-A"),
])
def test_norm_ticker_exchange_suffix_vs_share_class(raw, expected):
    assert common.norm_ticker(raw) == expected


def test_norm_ticker_share_class_allow_list_is_constant():
    assert {"MKC.V", "BF.B", "BRK.B", "BRK.A", "LEN.B", "HEI.A", "CRD.A", "CRD.B", "GEF.B", "MOG.A",
            "MOG.B", "UHAL.B", "LGF.A", "LGF.B", "RDS.A", "RDS.B"} == set(common.US_SHARE_CLASS_TICKERS)
    # every allow-listed ticker maps to the dash form, never to a suffix form
    for t in common.US_SHARE_CLASS_TICKERS:
        assert "." not in common.norm_ticker(t) and common.norm_ticker(t) == t.replace(".", "-")


def test_norm_ticker_is_idempotent():
    for t in ("MKC.V", "BRK.B", "TEVA.TA", "T.TO", "X.V", "KO", "ILS=X", "^VIX"):
        once = common.norm_ticker(t)
        assert common.norm_ticker(once) == once


@pytest.mark.parametrize("raw,expected", [
    (769.6400146484375, 769.64), (1500.1199951171875, 1500.12), (np.float32(769.64), 769.64),
    (0.05000000074505806, 0.05), (3.7958000183105469, 3.7958), (123.456789, 123.4568), (0, 0.0),
    (-0.0, 0.0), (-12.340000152587891, -12.34), (7522.0, 7522.0), ("12.5", 12.5),
])
def test_round_price_removes_float32_noise(raw, expected):
    assert common.round_price(raw) == expected


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, "abc", pd.NA])
def test_round_price_none_for_non_numbers(bad):
    assert common.round_price(bad) is None


def test_round_price_float32_roundtrip_of_cent_prices():
    # every quoted cent price up to 99,999.99 survives a float32 round trip
    rng = np.random.default_rng(7)
    cents = rng.integers(1, 10_000_000, size=2000)
    for c in cents:
        price = round(float(c) / 100.0, 2)
        assert common.round_price(float(np.float32(price))) == price


@pytest.mark.parametrize("raw,expected", [
    ("PRN", "PRN_"), ("con", "con_"), ("AUX", "AUX_"), ("NUL", "NUL_"), ("COM1", "COM1_"), ("lpt9", "lpt9_"),
    ("CON.json", "CON_.json"), ("KO", "KO"), ("BRK-B", "BRK-B"), ("ILS=X", "ILS=X"),
    ("a:b/c\\d*e?f\"g<h>i|j", "a_b_c_d_e_f_g_h_i_j"), ("trailing. ", "trailing"), ("", "_"),
    ("CONSOLE", "CONSOLE"), ("prices_PRN", "prices_PRN"), ("two words", "two_words"),
])
def test_safe_name(raw, expected):
    assert common.safe_name(raw) == expected


def test_safe_name_long_key_is_hashed_and_stable():
    key = "https://data.sec.gov/api/xbrl/companyfacts/" + "x" * 300
    a, b = common.safe_name(key), common.safe_name(key)
    assert a == b and len(a) <= 120
    assert common.safe_name(key + "y") != a


def test_data_filename():
    assert common.data_filename("technicals", "brk.b") == "technicals_BRK-B.json"
    assert common.data_filename("prices", "PRN", "csv") == "prices_PRN.csv"


def test_get_dotted_and_json_pointer():
    doc = {"extensions": {"valuation": {"fair_value_base": 61.0, "cells": [{"id": "A"}]}},
           "a/b": {"~x": 1}, "zero": 0}
    assert common.get_dotted(doc, "extensions.valuation.fair_value_base") == 61.0
    assert common.get_dotted(doc, "extensions.valuation.cells.0.id") == "A"
    assert common.get_dotted(doc, "extensions.nope.x", "dflt") == "dflt"
    assert common.get_dotted(doc, "zero") == 0
    assert common.json_pointer_get(doc, "/extensions/valuation/fair_value_base") == 61.0
    assert common.json_pointer_get(doc, "#/extensions/valuation/cells/0/id") == "A"
    assert common.json_pointer_get(doc, "/a~1b/~0x") == 1
    assert common.json_pointer_get(doc, "") is doc
    for bad in ("/nope", "/extensions/valuation/cells/5", "no-slash"):
        with pytest.raises(KeyError):
            common.json_pointer_get(doc, bad)


def test_fact_value_helper():
    f = common.fact(12.5, "pct", "D:X", "2026-01-01", "price")
    assert common.is_fact(f) and common.fact_value(f) == 12.5
    assert common.fact_value(3) == 3.0 and common.fact_value("x") is None


def test_retry_success_after_failures(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert common.retry(flaky, attempts=3, base_delay=1.0) == "ok"
    assert sleeps == [1.0, 2.0]


def test_retry_reraises_and_skips_no_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError):
        common.retry(lambda: (_ for _ in ()).throw(RuntimeError("x")), attempts=2, base_delay=0.5)
    assert sleeps == [0.5]
    calls = {"n": 0}

    def cfg_fail():
        calls["n"] += 1
        raise ConfigError("bad")

    with pytest.raises(ConfigError):
        common.retry(cfg_fail, attempts=5)
    assert calls["n"] == 1
    with pytest.raises(ValueError):
        common.retry(lambda: 1, attempts=0)


def test_retry_caps_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError):
        common.retry(lambda: (_ for _ in ()).throw(RuntimeError()), attempts=4, base_delay=20, max_delay=30)
    assert sleeps == [20, 30, 30]


# =====================================================================================
# IO
# =====================================================================================


def test_write_json_atomic_utf8_nan(tmp_path):
    p = tmp_path / "deep" / "out.json"
    common.write_json(p, {"name": "Société — ₪", "x": float("nan"), "d": date(2026, 1, 2)})
    raw = p.read_bytes()
    assert "Société — ₪".encode("utf-8") in raw  # ensure_ascii=False
    assert b"\r\n" not in raw  # no newline translation on Windows
    data = common.read_json(p)
    assert data == {"name": "Société — ₪", "x": None, "d": "2026-01-02"}
    assert [f.name for f in p.parent.iterdir()] == ["out.json"]  # no temp leftovers


def test_write_text_retries_permission_error(tmp_path, monkeypatch):
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("locked by antivirus")
        return real_replace(src, dst)

    monkeypatch.setattr(common.os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    p = common.write_text(tmp_path / "f.md", "hello\n")
    assert p.read_text(encoding="utf-8") == "hello\n" and attempts["n"] == 3
    assert sorted(f.name for f in tmp_path.iterdir()) == ["f.md"]


def test_write_text_gives_up_and_cleans_temp(tmp_path, monkeypatch):
    def always_locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(common.os, "replace", always_locked)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        common.write_text(tmp_path / "g.md", "x")
    assert list(tmp_path.iterdir()) == []


def test_read_json_tolerates_bom(tmp_path):
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8"))
    assert common.read_json(p) == {"a": 1}


def test_setup_stdio_does_not_crash():
    common.setup_stdio()
    print("ünïcödé ₪ —")


# =====================================================================================
# time
# =====================================================================================


def test_today_and_now(monkeypatch):
    assert common.today() == date(2026, 10, 2)
    monkeypatch.delenv(common.ENV_AS_OF)
    assert common.today() == date.today()
    monkeypatch.setenv(common.ENV_AS_OF, "garbage")
    with pytest.raises(ConfigError):
        common.today()
    ts = common.now_iso()
    assert ts.endswith("+00:00") and datetime.fromisoformat(ts).tzinfo is not None
    monkeypatch.setenv(common.ENV_NOW, "2026-10-02T12:00:00+00:00")
    assert common.now_iso() == "2026-10-02T12:00:00+00:00"


@pytest.mark.parametrize("value,expected", [
    ("2026-10-02", date(2026, 10, 2)), ("2026-10-02T15:30:00Z", date(2026, 10, 2)),
    ("2026-10-02 00:00:00", date(2026, 10, 2)), (date(2026, 1, 3), date(2026, 1, 3)),
    (datetime(2026, 1, 3, 9), date(2026, 1, 3)), (pd.Timestamp("2026-01-04"), date(2026, 1, 4)),
    (np.datetime64("2026-01-05"), date(2026, 1, 5)), (None, None), ("", None), (float("nan"), None),
    (pd.NaT, None),
])
def test_parse_date(value, expected):
    assert common.parse_date(value) == expected


@pytest.mark.parametrize("bad", ["2026-13-01", "yesterday", 20261002])
def test_parse_date_rejects(bad):
    with pytest.raises(ValueError):
        common.parse_date(bad)


@pytest.mark.parametrize("d1,d2,expected", [
    ("2026-10-02", "2026-10-02", 0),   # same day
    ("2026-10-02", "2026-10-05", 1),   # Fri -> Mon
    ("2026-10-01", "2026-10-02", 1),   # Thu -> Fri
    ("2026-10-03", "2026-10-04", 0),   # Sat -> Sun
    ("2026-09-25", "2026-10-02", 5),   # one full week
    ("2026-11-25", "2026-11-27", 1),   # Thanksgiving (11-26) skipped
    ("2026-07-02", "2026-07-06", 1),   # Jul 3 observed holiday + weekend
    ("2025-12-31", "2026-01-02", 1),   # New Year's Day skipped
    ("2026-10-05", "2026-10-02", -1),  # reversed
])
def test_trading_days_between_with_config_holidays(d1, d2, expected):
    assert common.trading_days_between(d1, d2) == expected


def test_trading_days_between_explicit_holidays():
    assert common.trading_days_between("2026-11-25", "2026-11-27", ()) == 2
    assert common.trading_days_between(date(2026, 11, 25), date(2026, 11, 27), ["2026-11-26"]) == 1
    with pytest.raises(ValueError):
        common.trading_days_between(None, "2026-01-01")


def test_is_trading_day_and_holidays():
    assert common.is_trading_day("2026-10-02")
    assert not common.is_trading_day("2026-10-03")
    assert not common.is_trading_day("2026-11-26")
    hols = common.market_holidays()
    assert date(2026, 11, 26) in hols and len(hols) == 31


# =====================================================================================
# sectors + SIC
# =====================================================================================


def test_sectors():
    assert list(common.SECTOR_KEYS) == [
        "information_technology", "health_care", "financials", "energy", "industrials",
        "consumer_discretionary", "consumer_staples", "utilities", "materials", "real_estate",
        "communication_services"]
    etfs = [common.sector_etf(k) for k in common.SECTOR_KEYS]
    assert etfs == ["XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
    assert common.sector_display("consumer_staples") == "Consumer Staples"
    assert common.sector_display("funds") == "Funds" and common.sector_display("cash") == "Cash"
    assert common.sector_display("unclassified") == "Unclassified"
    assert common.sector_etf("funds") is None
    assert common.sector_class("financials") == "financials"
    assert common.sector_class("real_estate") == "default"
    assert common.sector_class("information_technology", "fund") == "fund"


def test_yahoo_sector_maps_cover_all_sectors():
    assert set(common.YF_SECTOR_MAP.values()) == set(common.SECTOR_KEYS)
    assert set(common.YF_FUND_SECTOR_MAP.values()) == set(common.SECTOR_KEYS)
    assert common.YF_SECTOR_MAP["Consumer Defensive"] == "consumer_staples"
    assert common.YF_FUND_SECTOR_MAP["realestate"] == "real_estate"
    assert common.YF_FUND_SECTOR_MAP["technology"] == "information_technology"


@pytest.mark.parametrize("sic,expected", [
    (100, "consumer_staples"), (999, "consumer_staples"), (1040, "materials"), (1100, None),
    (1221, "energy"), (1311, "energy"), (1400, "materials"), (1531, "industrials"),
    (2080, "consumer_staples"), ("2086", "consumer_staples"), (2211, "consumer_discretionary"),
    (2421, "materials"), (2510, "consumer_discretionary"), (2650, "materials"),
    (2711, "communication_services"), (2821, "materials"), (2834, "health_care"),
    (2836, "health_care"), (2837, None), (2840, "consumer_staples"), (2844, "consumer_staples"),
    (2851, "materials"), (2911, "energy"), (3011, "materials"), (3140, "consumer_discretionary"),
    (3312, "materials"), (3420, "industrials"), (3569, "industrials"), (3571, "information_technology"),
    (3585, "industrials"), (3620, "industrials"), (3634, "consumer_discretionary"), (3651, "industrials"),
    (3674, "information_technology"), (3711, "consumer_discretionary"), (3716, "consumer_discretionary"),
    (3721, "industrials"), (3799, "industrials"), (3812, "industrials"), (3826, "information_technology"),
    (3841, "health_care"), (3851, "health_care"), (3861, "information_technology"),
    (3944, "consumer_discretionary"), (4011, "industrials"), (4813, "communication_services"),
    (4911, "utilities"), (5000, "industrials"), (5122, "health_care"), (5141, "consumer_staples"),
    (5199, "industrials"), (5200, "consumer_discretionary"), (5411, "consumer_staples"),
    (5912, "consumer_staples"), (5961, "consumer_discretionary"), (6021, "financials"),
    (6331, "financials"), (6499, "financials"), (6500, "real_estate"), (6553, "real_estate"),
    (6600, None), (6770, "financials"), (6798, "real_estate"), (7011, "consumer_discretionary"),
    (7200, "consumer_discretionary"), (7372, "information_technology"), (7311, "industrials"),
    (7812, "communication_services"), (7989, "communication_services"), (7997, "consumer_discretionary"),
    (8062, "health_care"), (8711, "industrials"), (8800, None), (9999, None), (None, None),
    ("abc", None), (3571.5, None),
])
def test_sic_to_sector(sic, expected):
    assert common.sic_to_sector(sic) == expected


# =====================================================================================
# provenance
# =====================================================================================


def test_fact_basic_and_extras():
    f = common.fact(np.float64(12.3), "pct", "D:SEC:KO:CF", date(2026, 4, 3), "fundamental",
                    filed="2026-04-30T00:00:00", tag="Revenues", derivation="FY2025+3M2026-3M2025",
                    note=None)
    assert f == {"value": 12.3, "unit": "pct", "source_id": "D:SEC:KO:CF", "as_of": "2026-04-03",
                 "kind": "fundamental", "na_reason": None, "filed": "2026-04-30", "tag": "Revenues",
                 "derivation": "FY2025+3M2026-3M2025"}
    assert type(f["value"]) is float
    assert common.fact(np.int64(5), "shares", "D:X", None, common.FactKind.OTHER)["value"] == 5


def test_fact_null_handling():
    assert common.fact(None, "x", "D:X", "2026-01-01", "price")["na_reason"] == "missing"
    nan = common.fact(float("nan"), "x", "D:X", "2026-01-01", "price", "not_meaningful")
    assert nan["value"] is None and nan["na_reason"] == "not_meaningful"
    assert common.fact(1.0, "x", "D:X", "2026-01-01", "price", "missing")["na_reason"] is None
    assert common.na_fact("x", "D:X", "2026-01-01", "macro", "insufficient_history")["na_reason"] == \
        "insufficient_history"
    with pytest.raises(ValueError):
        common.fact(None, "x", "D:X", "2026-01-01", "price", "made_up")
    with pytest.raises(ValueError):
        common.fact(1.0, "x", "D:X", "2026-01-01", "bogus_kind")
    with pytest.raises(TypeError):
        common.fact("12", "x", "D:X", "2026-01-01", "price")
    with pytest.raises(TypeError):
        common.fact(True, "x", "D:X", "2026-01-01", "price")


def test_source_and_dataset_sources():
    s = common.source("W1", "https://example.org/a", "An article", "2026-10-01", kind="web")
    assert s == {"id": "W1", "url": "https://example.org/a", "title": "An article",
                 "accessed": "2026-10-01", "kind": "web"}
    assert common.source("D:X", "http://x.org", "t")["accessed"] == DEFAULT_AS_OF
    kw = common.source(id="W3", url="https://x.org/p", title="T", accessed="2026-09-01", kind="web")
    assert kw["id"] == "W3" and common.source(source_id="W4", url="https://x.org")["title"] == "https://x.org"
    with pytest.raises(ValueError):
        common.source(url="https://x.org")
    for bad in ("ftp://x", "file:///c:/x", "/local/path", None):
        with pytest.raises(ValueError):
            common.source("W2", bad, "t")
    with pytest.raises(ValueError):
        common.source("W2", "https://x.org", "t", kind="blog")
    assert common.dataset_source("D:PX:KO")["url"] == "https://finance.yahoo.com/quote/KO/history/"
    assert common.dataset_source("D:YF:QQQ:FUND")["url"] == "https://finance.yahoo.com/quote/QQQ/holdings/"
    assert common.dataset_source("D:YF:BRK-B:INFO")["url"] == "https://finance.yahoo.com/quote/BRK-B/"
    cf = common.dataset_source("D:SEC:KO:CF", cik=21344, path=Path("C:/x/fundamentals_KO.json"))
    assert cf["url"] == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000021344.json"
    assert cf["path"] == "C:/x/fundamentals_KO.json" and cf["kind"] == "dataset"
    assert common.dataset_source("D:SEC:KO:SUB", cik="21344")["url"] == \
        "https://data.sec.gov/submissions/CIK0000021344.json"
    assert common.dataset_source("D:SEC:MAP")["url"] == "https://www.sec.gov/files/company_tickers.json"
    assert common.dataset_source("D:FRED:DGS10")["url"] == \
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    assert common.dataset_source("D:VAL:KO", cik=21344)["url"].endswith("CIK0000021344.json")
    stq = common.dataset_source("D:PX:KO", provider="stooq", url=common.stooq_csv_url("ko.us"))
    assert stq["url"] == "https://stooq.com/q/d/l/?s=ko.us&i=d"
    cfg = common.dataset_source("D:CFG:valuation.multiple_bands", url="https://github.com/x")
    assert cfg["title"].startswith("Configured assumption")
    for bad in ("W1", "D:NOPE:X", "D:SEC:KO:CF"):  # last one lacks cik/url
        with pytest.raises(ValueError):
            common.dataset_source(bad)


def test_envelope_shape(monkeypatch):
    monkeypatch.setenv(common.ENV_NOW, "2026-10-02T08:00:00+00:00")
    src = common.dataset_source("D:FRED:DGS10")
    env = common.envelope("macro", "2026-10-01", sources=[src, dict(src)], missing_data=["a", "a", "b"],
                          flags=[common.flag("stale_cache", "high", "x")], series={"DGS10": {}})
    assert list(env)[:6] == ["schema", "generated_at", "as_of", "sources", "missing_data", "flags"]
    assert env["schema"] == "macro/v1" and env["generated_at"] == "2026-10-02T08:00:00+00:00"
    assert env["as_of"] == "2026-10-01" and len(env["sources"]) == 1
    assert env["missing_data"] == ["a", "b"] and env["series"] == {"DGS10": {}}
    assert env["flags"] == [{"code": "stale_cache", "severity": "high", "detail": "x"}]
    bare = common.envelope("prices/v2", None)
    assert bare["schema"] == "prices/v2" and bare["as_of"] == DEFAULT_AS_OF
    assert bare["sources"] == [] and bare["flags"] == [] and bare["missing_data"] == []
    with pytest.raises(ValueError):
        common.flag("x", "critical")


# =====================================================================================
# YAML + config loading
# =====================================================================================


def test_load_yaml_scalars(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("a: ON\nb: no\nc: true\nd: 2026-10-02\ne: 10e9\nf: 1.5e-3\ng: 12\nh: .5\ni: null\n"
                 "j: [ON, OFF, Y, N]\nk: False\n", encoding="utf-8")
    d = common.load_yaml(p)
    assert d == {"a": "ON", "b": "no", "c": True, "d": "2026-10-02", "e": 1e10, "f": 0.0015, "g": 12,
                 "h": 0.5, "i": None, "j": ["ON", "OFF", "Y", "N"], "k": False}
    assert isinstance(d["g"], int) and isinstance(d["e"], float)


def test_load_yaml_errors_and_empty(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert common.load_yaml(empty) == {}
    bom = tmp_path / "bom.yaml"
    bom.write_bytes(b"\xef\xbb\xbfa: 1\n")
    assert common.load_yaml(bom) == {"a": 1}
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML parse error"):
        common.load_yaml(bad)
    with pytest.raises(ConfigError, match="not found"):
        common.load_yaml(tmp_path / "missing.yaml")
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("a: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        common.load_yaml(unsafe)


def test_repo_yaml_parses_identically_with_safe_load():
    """Configs are written so plain yaml.safe_load (YAML 1.1) agrees with the project loader."""
    for name in common.CONFIG_NAMES:
        path = REPO_ROOT / "config" / f"{name}.yaml"
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == common.load_yaml(path), name


def test_load_config_and_committee_cfg_cache(tmp_env):
    with pytest.raises(ValueError):
        common.load_config("nope")
    prof = common.load_config("investor_profile")
    assert prof["base_currency"] == "ILS"
    cfg1 = common.committee_cfg()
    cfg1["concurrency"] = 999  # deep copy: callers cannot poison the cache
    assert common.committee_cfg()["concurrency"] == 8
    tmp_env.update_config("committee", lambda c: c.update(concurrency=3))
    assert common.committee_cfg()["concurrency"] == 3
    (tmp_env.config_dir / "committee.yaml").unlink()
    with pytest.raises(ConfigError):
        common.committee_cfg()
    assert common.market_holidays() == []


# =====================================================================================
# config files + schemas
# =====================================================================================


def test_config_schemas_are_valid_2020_12():
    for name in common.CONFIG_NAMES:
        schema = common.load_schema(f"config_{name}")
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"urn:agent-investors:config_{name}"
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_repo_configs_valid_without_portfolio_values():
    result = common.validate_configs(require_portfolio_values=False)
    assert set(result) == set(common.CONFIG_NAMES)
    assert all(v == [] for v in result.values()), result


def test_prefilled_portfolio_requires_setup(tmp_env):
    result = common.validate_configs()
    assert result["portfolio"], "prefilled portfolio must be rejected"
    assert any("/committee-setup" in e for e in result["portfolio"])
    assert all(e.startswith("portfolio.yaml: ") for e in result["portfolio"])
    for name in ("investor_profile", "universe", "committee", "etf_overrides"):
        assert result[name] == []
    flat = common.config_errors(result)
    assert flat == result["portfolio"]


def test_sample_portfolio_is_valid(tmp_env):
    tmp_env.use_sample_portfolio()
    assert common.config_errors(common.validate_configs()) == []
    assert common.validate_config("portfolio", SAMPLE_PORTFOLIO) == []


def test_prefilled_portfolio_contents(tmp_env):
    p = common.load_config("portfolio")
    assert [h["ticker"] for h in p["holdings"]] == ["QQQ", "SPMO", "BRK-B", "KO", "PG", "JPM"]
    for h in p["holdings"]:
        for key in ("shares", "weight_pct", "cost_basis", "purchase_date", "proxy_ticker", "sector_override"):
            assert h[key] is None
        assert h["currency"] == "USD" and h["cost_basis_currency"] == "USD"
    assert {h["ticker"]: h["asset_type"] for h in p["holdings"]} == {
        "QQQ": "fund", "SPMO": "fund", "BRK-B": "stock", "KO": "stock", "PG": "stock", "JPM": "stock"}
    for key in ("as_of", "total_value_usd", "cash_usd", "cash_pct"):
        assert key in p


def test_investor_profile_values(tmp_env):
    p = common.load_config("investor_profile")
    assert p == {
        "horizon_years": 10, "risk_tolerance": "moderate", "max_position_pct": 10, "max_fund_position_pct": 40,
        "max_sector_pct": 35, "min_trade_usd": 200, "target_cash_pct": 5, "base_currency": "ILS",
        "tax_jurisdiction": "IL", "capital_gains_tax_pct": 25, "allow_short": False,
        "allow_fractional_shares": False, "sec_contact_email": "researcher@gmail.com"}


def test_universe_and_overrides_values():
    u = common.load_config("universe")
    assert u["region"] == "US" and u["min_market_cap_usd"] == 10_000_000_000
    assert u["min_avg_dollar_volume_usd"] == 50_000_000 and u["max_candidates_per_sector"] == 6
    assert u["exclusions"] == [] and set(u["additions"]) == set(common.SECTOR_KEYS)
    o = common.load_config("etf_overrides")
    brk = o["securities"]["BRK-B"]
    assert brk["sector"] == "financials" and brk["flags"] == ["conglomerate"] and brk["asset_type"] == "stock"
    assert brk["share_class"] == {"class_factor": None, "shares_tags": []}
    assert o["funds"] == {}


def test_committee_contains_every_design_parameter():
    c = common.committee_cfg()
    g = common.get_dotted
    expected = {
        "concurrency": 8, "benchmark": "SPY", "fx_ticker": "ILS=X", "phase1_reuse_days": 7,
        "cache_ttl_hours.prices": 24, "cache_ttl_hours.fundamentals": 168, "cache_ttl_hours.macro": 24,
        "cache_ttl_hours.etf": 168, "cache_ttl_hours.info": 24, "cache_ttl_hours.sec_map": 168,
        "roles.phase2_stock": ["fundamental", "technical", "osint", "operator_ceo"],
        "roles.phase2_fund": ["etf", "technical"], "roles.quick_stock": ["fundamental", "technical"],
        "roles.quick_fund": ["etf"], "roles.phase3_light": ["fundamental", "technical"],
        "roles.phase3_full": ["operator_ceo", "industry_ops", "osint"], "roles.da_for_funds": False,
        "evidence.start": 10, "evidence.floor": 1, "evidence.null_required_penalty": 2,
        "evidence.stale_penalty": 1, "evidence.few_sources_threshold": 3, "evidence.few_sources_penalty": 2,
        "evidence.missing_data_penalty": 1, "evidence.missing_data_max": 4,
        "evidence.price_max_trading_days": 3, "evidence.fundamental_max_days_after_filing": 120,
        "evidence.fundamental_filing_lag_days": 45, "evidence.macro_max_days": 45,
        "aggregation.neutral_value": 5, "aggregation.da_adjust": {"survives": 0, "damaged": -1, "broken": -3},
        "aggregation.unresolved_flag_roles": ["osint"], "aggregation.conviction_bounds": [1, 10],
        "actions.min_evidence": 5, "actions.holding.add_conviction": 7, "actions.holding.add_evidence": 6,
        "actions.holding.add_rr": 2.0, "actions.holding.sell_max": 3.0, "actions.holding.trim_below": 4.5,
        "actions.candidate.buy_conviction": 7, "actions.candidate.buy_evidence": 6,
        "actions.candidate.buy_rr": 2.5, "actions.candidate.block_on_unresolved_high_flag": False,
        "actions.sector_breach_tolerance_pp": 1.0,
        "phase3.shortlist_size": 6, "phase3.finalists": 4, "phase3.max_buys_per_sector": 3,
        "phase3.max_expand_sectors": 2,
        "screen.weights": {"quality": 0.4, "valuation": 0.3, "momentum": 0.3},
        "valuation.discount_rates": [0.09, 0.10], "valuation.years": 5,
        "valuation.growth_tiers": [0.0, 0.03, 0.06, 0.10, 0.15],
        "valuation.exit_multiple_by_growth": [[0.0, 10], [0.04, 12], [0.07, 15], [0.11, 18], [1.0, 22]],
        "valuation.reverse_dcf_exit_multiple": 15, "valuation.default_tax_rate": 0.21,
        "sizing.trim_fraction": 0.33, "sizing.breach_buffer_pp": 0.5, "sizing.add_step_pp": 2.0,
        "sizing.initial_position_pct": 3.0, "technicals.pivot_left": 3, "technicals.pivot_right": 3,
        "technicals.level_lookback": 250, "technicals.level_cluster_atr": 0.5,
        "technicals.regime.adx_trend_min": 25, "technicals.regime.adx_range_max": 20,
        "technicals.regime.choppy_min_sma50_crosses": 4, "technicals.regime.choppy_window": 20,
        "technicals.regime.choppy_atr_pctile_min": 80, "technicals.regime.range_max_width_pct": 10,
        "ledger.min_age_days": 20, "macro.chg_3m_days": 91, "macro.chg_12m_days": 365,
        "macro.history_tail": 13, "fundamentals.duration_days.Q": [80, 100],
        "fundamentals.duration_days.FY": [350, 380], "fundamentals.debt_formula_max_gap_days": 400,
        "fundamentals.share_count_divergence_pct": 10, "fundamentals.tax_rate_bounds": [0.0, 0.35],
        "valuation.reverse_dcf_growth_bounds": [-0.30, 0.60], "valuation.reverse_dcf_discount_rate": 0.10,
        "valuation.tv_share_flag_threshold": 0.9, "validation.data_ref_tolerance_pct": 0.5,
        "validation.min_justification_words": 15, "validation.max_quote_words": 25,
        "validation.word_limits.thesis": 60, "validation.word_limits.sector_rationale": 80,
        "orchestration.step_time_budget_seconds": 90, "orchestration.max_attempts": 2,
        "screen.etf_top_n": 10, "screen.impute_percentile": 0.5, "lookthrough.sum_tolerance_pct": 0.01,
        "risk_reward.round_dp": 2, "aggregation.round_dp": 1,
    }
    for path, value in expected.items():
        assert g(c, path, "<missing>") == value, path
    assert c["enabled_sectors"] == list(common.SECTOR_KEYS)
    assert c["sector_etfs"] == {k: v["etf"] for k, v in common.SECTORS.items()}


def test_committee_multiple_bands():
    bands = common.committee_cfg()["valuation"]["multiple_bands"]
    assert set(bands) == set(common.SECTOR_KEYS)
    for sector, b in bands.items():
        expected = {"pe", "p_b"} if sector == "financials" else {"pe", "ev_ebit"}
        assert set(b) == expected, sector
        for trio in b.values():
            assert len(trio) == 3 and 0 < trio[0] <= trio[1] <= trio[2]
    text = (REPO_ROOT / "config" / "committee.yaml").read_text(encoding="utf-8")
    assert "ASSUMPTIONS" in text and "P/FFO" in text


def test_committee_required_fields():
    rf = common.committee_cfg()["evidence"]["required_fields"]
    assert set(rf) == {"fundamental", "technical", "operator_ceo", "industry_ops", "osint",
                       "devils_advocate", "etf"}
    for role, classes in rf.items():
        assert set(classes) == {"default", "financials", "fund"}, role
    assert rf["fundamental"]["default"] == [
        "extensions.valuation.fair_value_base", "extensions.valuation.fair_value_low",
        "extensions.valuation.fair_value_high", "conviction"]
    assert rf["technical"]["default"] == ["extensions.trend_regime", "invalidation_price",
                                          "extensions.invalidation_ref"]
    assert rf["devils_advocate"]["default"] == ["bear_case_price", "verdict_on_thesis"]
    assert rf["etf"]["fund"] == ["expense_ratio_pct", "top10_concentration_pct", "action"]
    assert rf["osint"]["default"] == ["extensions.signals"]
    assert "extensions.insider_activity.assessment" in rf["operator_ceo"]["default"]
    assert "extensions.operational_moat.assessment" in rf["industry_ops"]["default"]


def test_committee_macro_series():
    series = common.committee_cfg()["macro"]["series"]
    by_id = {s["id"]: s for s in series}
    assert list(by_id) == ["DFF", "DGS2", "DGS10", "T10Y2Y", "CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS",
                           "INDPRO", "DTWEXBGS", "DCOILWTICO", "BAMLH0A0HYM2", "VIXCLS"]
    monthly = {"CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS", "INDPRO"}
    diff = {"DFF", "DGS2", "DGS10", "T10Y2Y", "UNRATE", "BAMLH0A0HYM2"}
    for sid, s in by_id.items():
        assert s["frequency"] == ("m" if sid in monthly else "d"), sid
        assert s["change"] == ("diff" if sid in diff else "pct"), sid
        assert s["yoy"] is (sid in {"CPIAUCSL", "CPILFESL"}), sid
        assert s["name"] and s["units"]


def test_committee_market_holidays():
    hols = common.committee_cfg()["market_holidays"]
    expected = ["2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
                "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25", "2026-01-01",
                "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
                "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15",
                "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25",
                "2027-12-24"]
    assert hols == expected
    assert "nyse.com" in (REPO_ROOT / "config" / "committee.yaml").read_text(encoding="utf-8")


# ---- validate_config: schema + semantic errors ---------------------------------------


def _profile():
    return common.load_config("investor_profile")


@pytest.mark.parametrize("email,fragment", [
    (None, "missing"), ("", "missing"), ("not-an-email", "does not look like"),
    ("you@example.com", "placeholder"), ("me@EXAMPLE.org", "placeholder"),
])
def test_validate_profile_email(email, fragment):
    p = _profile()
    p["sec_contact_email"] = email
    errs = common.validate_config("investor_profile", p)
    assert any(e.startswith("investor_profile.yaml: sec_contact_email: ") and fragment in e for e in errs), errs


def test_validate_profile_schema_errors():
    p = _profile()
    p["risk_tolerance"] = "yolo"
    p["max_position_pct"] = 50  # > max_sector_pct 35
    p["typo_key"] = 1
    del p["horizon_years"]
    errs = common.validate_config("investor_profile", p)
    joined = "\n".join(errs)
    assert "investor_profile.yaml: risk_tolerance: 'yolo' is not one of" in joined
    assert "investor_profile.yaml: (root): 'horizon_years' is a required property" in joined
    assert "typo_key" in joined
    assert "max_position_pct (50) must be <= max_sector_pct (35)" in joined
    p2 = _profile()
    p2["allow_short"] = True
    assert any("allow_short" in e for e in common.validate_config("investor_profile", p2))


def test_validate_portfolio_error_paths():
    p = {"holdings": [{"ticker": "KO", "shares": "ten"}, {"ticker": "PG", "weight_pct": 120},
                      {"tikcer": "X"}]}
    errs = common.validate_config("portfolio", p)
    joined = "\n".join(errs)
    assert "portfolio.yaml: holdings[0].shares: 'ten' is not of type 'number', 'null'" in joined
    assert "portfolio.yaml: holdings[1].weight_pct: 120 is greater than the maximum of 100" in joined
    assert "portfolio.yaml: holdings[2]: 'ticker' is a required property" in joined


def test_validate_portfolio_semantics():
    base = {"total_value_usd": None, "cash_usd": None, "cash_pct": None, "holdings": [
        {"ticker": "KO", "weight_pct": 60}, {"ticker": "PG", "weight_pct": 30}]}
    errs = common.validate_config("portfolio", base)
    assert any(e.startswith("portfolio.yaml: total_value_usd: required when holdings are given as weight_pct")
               and "/committee-setup" in e for e in errs), errs
    ok = dict(base, total_value_usd=100000.0, cash_pct=10)
    assert common.validate_config("portfolio", ok) == []
    over = dict(ok, cash_pct=11)  # 60 + 30 + 11 = 101 > 100.5
    assert any("sum to 101.00%" in e for e in common.validate_config("portfolio", over))
    edge = dict(ok, cash_pct=10.5)  # 100.5 is allowed
    assert common.validate_config("portfolio", edge) == []
    both_cash = dict(ok, cash_usd=100.0)
    assert any("either cash_usd or cash_pct" in e for e in common.validate_config("portfolio", both_cash))
    dup = {"holdings": [{"ticker": "BRK-B", "shares": 1}, {"ticker": "brk.b", "shares": 2}]}
    assert any("duplicate holding BRK-B" in e for e in common.validate_config("portfolio", dup))
    empty = {"holdings": []}
    assert any("no holdings defined" in e for e in common.validate_config("portfolio", empty))
    assert common.validate_config("portfolio", empty, require_portfolio_values=False) == []
    mixed = {"holdings": [{"ticker": "KO", "shares": 10}, {"ticker": "PG"}]}
    errs = common.validate_config("portfolio", mixed)
    assert len(errs) == 1 and "PG: neither shares nor weight_pct" in errs[0] and "/committee-setup" in errs[0]


def test_validate_portfolio_unquoted_on_ticker_is_kept_as_string(tmp_path):
    p = tmp_path / "portfolio.yaml"
    p.write_text("holdings:\n  - ticker: ON\n    shares: 3\n", encoding="utf-8")
    data = common.load_yaml(p)
    assert data["holdings"][0]["ticker"] == "ON"
    assert common.validate_config("portfolio", data) == []


def _committee():
    return common.committee_cfg()


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: c["actions"]["holding"].update(sell_max=5.0), "sell_max < trim_below < add_conviction"),
    (lambda c: c["actions"]["holding"].update(trim_below=8.0), "sell_max < trim_below < add_conviction"),
    (lambda c: c["actions"]["holding"].update(add_evidence=4), "must be >= actions.min_evidence"),
    (lambda c: c["evidence"].update(floor=10), "floor (10) must be < start (10)"),
    (lambda c: c["aggregation"]["da_adjust"].update(damaged=-5), "broken <= damaged <= survives <= 0"),
    (lambda c: c["phase3"].update(finalists=9), "finalists (9) must be <= shortlist_size (6)"),
    (lambda c: c["phase3"].update(max_buys_per_sector=5), "max_buys_per_sector (5) must be <= finalists (4)"),
    (lambda c: c["screen"]["weights"].update(momentum=0.5), "weights must sum to 1.0"),
    (lambda c: c["valuation"].update(growth_tiers=[0.1, 0.0]), "ascending order"),
    (lambda c: c["valuation"].update(exit_multiple_by_growth=[[0.1, 10], [0.0, 12]]), "strictly ascending"),
    (lambda c: c["valuation"]["multiple_bands"].pop("energy"), "missing sector 'energy'"),
    (lambda c: c["valuation"]["multiple_bands"]["financials"].pop("p_b"), "a 'p_b' band is required"),
    (lambda c: c["valuation"]["multiple_bands"]["utilities"].update(pe=[20, 15, 25]), "low <= mid <= high"),
    (lambda c: c["valuation"].update(default_tax_rate=0.25), "must equal fundamentals.default_tax_rate"),
    (lambda c: c["technicals"]["regime"].update(adx_range_max=30), "adx_range_max (30) must be <="),
    (lambda c: c["macro"]["series"].append(dict(c["macro"]["series"][0])), "duplicate series ids: DFF"),
    (lambda c: c["market_holidays"].append("2026-10-03"), "weekend day"),
    (lambda c: c["sector_etfs"].update(energy="VDE"), "expected XLE"),
    (lambda c: c["evidence"]["required_fields"].pop("osint"), "missing role 'osint'"),
    (lambda c: c["enabled_sectors"].append("crypto"), "enabled_sectors[11]: 'crypto' is not one of"),
    (lambda c: c.pop("sizing"), "'sizing' is a required property"),
    (lambda c: c["actions"]["holding"].update(add_conviction=11), "add_conviction: 11 is greater than"),
    (lambda c: c["market_holidays"].append("2026-02-30"), "is not a 'date'"),
])
def test_validate_committee_rejects(mutate, fragment):
    c = _committee()
    mutate(c)
    errs = common.validate_config("committee", c)
    assert any(fragment in e for e in errs), errs
    assert all(e.startswith("committee.yaml: ") for e in errs)


def test_validate_universe_and_overrides():
    u = common.load_config("universe")
    u["additions"]["crypto"] = ["BTC"]
    u["exclusions"] = ["brk.b"]
    errs = common.validate_config("universe", u)
    assert any("additions" in e and "'crypto'" in e for e in errs), errs
    assert any("exclusions[0]: use the normalized ticker 'BRK-B'" in e for e in errs), errs
    o = common.load_config("etf_overrides")
    o["funds"]["SPMO"] = {"sector_weights": {"information_technology": 50, "financials": 30}}
    errs = common.validate_config("etf_overrides", o)
    assert any("'source_url' is a required property" in e for e in errs), errs
    assert any("must sum to 100" in e for e in errs), errs
    o["funds"]["SPMO"] = {"sector_weights": {"information_technology": 70, "financials": 30},
                          "source_url": "https://www.invesco.com/x", "as_of": "2026-09-30"}
    assert common.validate_config("etf_overrides", o) == []
    o["securities"]["BRK-B"]["share_class"]["class_factor"] = 0.5
    assert any("source_url" in e for e in common.validate_config("etf_overrides", o))
    o["securities"]["BRK-B"]["sector"] = "conglomerates"
    assert any("securities.BRK-B.sector" in e for e in common.validate_config("etf_overrides", o))


def test_validate_config_unknown_name():
    with pytest.raises(ValueError):
        common.validate_config("nope", {})


def test_validate_configs_missing_and_broken_files(tmp_env):
    (tmp_env.config_dir / "universe.yaml").unlink()
    (tmp_env.config_dir / "etf_overrides.yaml").write_text("securities: [unclosed\n", encoding="utf-8")
    res = common.validate_configs(tmp_env.config_dir, require_portfolio_values=False)
    assert res["universe"] and "not found" in res["universe"][0]
    assert res["etf_overrides"] and "YAML parse error" in res["etf_overrides"][0]
    assert res["committee"] == [] and res["investor_profile"] == []


# =====================================================================================
# DiskCache
# =====================================================================================


def test_cache_put_get_ttl_and_stale(tmp_path):
    cache = DiskCache(tmp_path)
    path = cache.put("sec_map", "company_tickers", {"KO": 21344}, "https://www.sec.gov/files/company_tickers.json")
    assert path == tmp_path / "sec_map" / "company_tickers.json"
    entry = common.read_json(path)
    assert set(entry) == {"fetched_at", "source_url", "payload"}
    got = cache.get("sec_map", "company_tickers", 168)
    assert got == {"KO": 21344} and isinstance(got, dict)
    assert got.source_url.startswith("https://www.sec.gov") and got.stale is False
    assert got.age_hours is not None and got.age_hours < 1
    # age the entry by 30 h
    entry["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    common.write_json(path, entry)
    assert cache.get("sec_map", "company_tickers", 24) is None
    assert cache.get("sec_map", "company_tickers", 48) == {"KO": 21344}
    assert cache.get("sec_map", "company_tickers", None) == {"KO": 21344}
    stale = cache.get_stale("sec_map", "company_tickers")
    assert stale == {"KO": 21344} and stale.stale is True and 29 < stale.age_hours < 31
    # raw-entry style access keeps working (compatibility alias)
    assert stale["payload"] == {"KO": 21344} and stale["source_url"].startswith("https://www.sec.gov")
    assert stale.get("fetched_at") == stale.fetched_at and "payload" not in stale
    assert list(stale) == ["KO"] and json.loads(json.dumps(stale)) == {"KO": 21344}
    with pytest.raises(KeyError):
        stale["nope"]
    full = cache.get_entry("sec_map", "company_tickers", None)
    assert full["payload"] == {"KO": 21344} and "fetched_at" in full
    assert cache.get("sec_map", "absent", 24) is None and cache.get_stale("sec_map", "absent") is None


def test_cache_list_payload_and_default_root(tmp_path):
    cache = DiskCache()
    assert cache.root == common.cache_dir()
    cache.put("x", "lst", [1, 2], None)
    got = cache.get("x", "lst", 1)
    assert got == [1, 2] and isinstance(got, list) and got.source_url is None


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Point REPO_ROOT at a scratch dir so the 'real' cache (<repo>/data/cache) is disposable and the
    default cache dir can be exercised without ever touching the actual repository."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", root)
    monkeypatch.delenv(common.ENV_CACHE_DIR, raising=False)
    return root


def test_fixture_mode_with_real_cache_dir_is_refused(fake_repo, tmp_path, use_fixtures):
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    real = fake_repo / "data" / "cache"
    assert common.cache_dir() == common.default_cache_dir() == real.resolve()
    cache = DiskCache()
    for call in (lambda: cache.path_for("macro", "UNRATE"),
                 lambda: cache.put("macro", "UNRATE", {"x": 1}, None),
                 lambda: cache.get("macro", "UNRATE", 24),
                 lambda: cache.get_stale("macro", "UNRATE"),
                 lambda: cache.get_entry("macro", "UNRATE"),
                 lambda: cache.put_frame("prices", "prices_KO", make_ohlcv(n=5), None),
                 lambda: cache.get_frame("prices", "prices_KO"),
                 lambda: cache.get_frame_stale("prices", "prices_KO"),
                 lambda: cache.delete("prices", "prices_KO")):
        with pytest.raises(ConfigError, match="fixture mode with the real cache dir") as ei:
            call()
        assert common.ENV_CACHE_DIR in str(ei.value)
    assert not (fake_repo / "data").exists()  # nothing was created in the real cache


def test_fixture_mode_refuses_the_real_cache_via_an_explicit_root_or_subdir(fake_repo, tmp_path, use_fixtures):
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    real = fake_repo / "data" / "cache"
    for root in (real, real / "sub", str(real)):
        with pytest.raises(ConfigError, match="real cache dir"):
            DiskCache(root).put("k", "a", {"x": 1}, None)
    assert not (fake_repo / "data").exists()


def test_fixture_mode_with_scratch_cache_works(fake_repo, tmp_path, use_fixtures, monkeypatch):
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    # an explicit root outside the real cache needs no env var ...
    scratch = DiskCache(tmp_path / "scratch")
    scratch.put("macro", "UNRATE", {"x": 1}, None)
    assert scratch.get("macro", "UNRATE", 24) == {"x": 1}
    # ... and AGINV_CACHE_DIR makes the default root explicit (tests, dry run) - served from cache next time
    monkeypatch.setenv(common.ENV_CACHE_DIR, str(tmp_path / "envcache"))
    cache = DiskCache()
    assert cache.root == (tmp_path / "envcache").resolve()
    cache.put("macro", "UNRATE", {"x": 2}, None)
    assert DiskCache().get("macro", "UNRATE", 24) == {"x": 2}


def test_explicit_cache_dir_env_may_name_the_real_cache(fake_repo, tmp_path, use_fixtures, monkeypatch):
    """Explicitly choosing the real cache with AGINV_CACHE_DIR is the user's call."""
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    monkeypatch.setenv(common.ENV_CACHE_DIR, str(fake_repo / "data" / "cache"))
    DiskCache().put("macro", "UNRATE", {"x": 1}, None)
    assert (fake_repo / "data" / "cache" / "macro" / "UNRATE.json").is_file()


def test_real_cache_is_fine_outside_fixture_mode(fake_repo):
    cache = DiskCache()  # AGINV_OFFLINE=1 (conftest) but no fixtures: the default cache is allowed
    cache.put("macro", "UNRATE", {"x": 1}, None)
    assert cache.path_for("macro", "UNRATE") == (fake_repo / "data" / "cache" / "macro" / "UNRATE.json").resolve()
    assert cache.get("macro", "UNRATE", 24) == {"x": 1}


def test_constructing_a_default_cache_in_fixture_mode_is_lazy(fake_repo, tmp_path, use_fixtures):
    """fetch_fundamentals builds DiskCache() and then bypasses it in fixture mode: construction alone
    must not raise, only disk access does."""
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    DiskCache()
    assert common.yf_info("KO")["sector"] == "Consumer Defensive"  # the fixture seam itself never uses the cache
    assert not (fake_repo / "data").exists()


def test_conftest_isolation_makes_fixture_mode_cache_usable(tmp_path, use_fixtures):
    """The hermetic conftest sets AGINV_CACHE_DIR per test, so fixture-mode tests (incl. 'second run is
    served from cache') keep working without any extra setup."""
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    assert os.environ.get(common.ENV_CACHE_DIR)
    cache = DiskCache()
    cache.put("macro", "UNRATE", {"x": 1}, None)
    assert DiskCache().get("macro", "UNRATE", 24) == {"x": 1}


def test_cache_dir_helpers():
    assert common.default_cache_dir() == (common.REPO_ROOT / "data" / "cache").resolve()
    assert common._is_within(common.default_cache_dir() / "prices", common.default_cache_dir())
    assert common._is_within(common.default_cache_dir(), common.default_cache_dir())
    assert not common._is_within(Path(str(common.default_cache_dir()) + "_other"), common.default_cache_dir())
    assert not common._is_within(common.default_cache_dir().parent, common.default_cache_dir())


def test_cache_never_caches_errors(tmp_path):
    cache = DiskCache(tmp_path)
    assert cache.put("k", "a", None, "https://x.org") is None
    assert cache.put("k", "b", ValueError("boom"), "https://x.org") is None
    assert cache.put("k", "c", {"error": "HTTP 500"}, "https://x.org") is None
    assert cache.put_frame("k", "d", pd.DataFrame(), "https://x.org") is None
    assert cache.put_frame("k", "e", None, "https://x.org") is None
    assert not (tmp_path / "k").exists()
    assert cache.put("k", "ok", {"error": None, "v": 1}, None) is not None


def test_cache_corrupt_entry_is_ignored(tmp_path):
    cache = DiskCache(tmp_path)
    p = cache.path_for("k", "bad")
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert cache.get("k", "bad", 24) is None and cache.get_stale("k", "bad") is None
    p.write_text(json.dumps({"payload": 1}), encoding="utf-8")  # no fetched_at
    assert cache.get("k", "bad", 24) is None


def test_cache_reserved_and_unsafe_keys(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("prices", "PRN", {"v": 1}, None)
    assert (tmp_path / "prices" / "PRN_.json").is_file()
    assert cache.get("prices", "PRN", 1) == {"v": 1}
    cache.put("http", "https://a.b/c?d=e", {"v": 2}, None)
    assert cache.get("http", "https://a.b/c?d=e", 1) == {"v": 2}
    cache.delete("prices", "PRN")
    assert cache.get("prices", "PRN", 1) is None


def test_cache_frames_roundtrip(tmp_path):
    cache = DiskCache(tmp_path)
    df = make_ohlcv(n=50, seed=1)
    csv_path = cache.put_frame("prices", "prices_KO", df, "https://finance.yahoo.com/quote/KO/history/",
                               meta={"provider": "yfinance"})
    assert csv_path == tmp_path / "prices" / "prices_KO.csv"
    first = csv_path.read_text(encoding="utf-8").splitlines()
    assert first[0] == "Date,Open,High,Low,Close,Volume"
    assert first[1].startswith(df.index[0].strftime("%Y-%m-%d") + ",")
    back = cache.get_frame("prices", "prices_KO", 24)
    assert isinstance(back.index, pd.DatetimeIndex) and back.index.name == "Date"
    pd.testing.assert_frame_equal(back, df, check_freq=False, check_exact=False, rtol=1e-12)
    assert back.attrs["meta"] == {"provider": "yfinance"} and back.attrs["stale"] is False
    assert back.attrs["source_url"].startswith("https://finance.yahoo.com")
    entry = common.read_json(tmp_path / "prices" / "prices_KO.json")
    assert entry["frame"] == "prices_KO.csv" and entry["rows"] == 50
    entry["fetched_at"] = "2020-01-01T00:00:00+00:00"
    common.write_json(tmp_path / "prices" / "prices_KO.json", entry)
    assert cache.get_frame("prices", "prices_KO", 24) is None
    stale = cache.get_frame_stale("prices", "prices_KO")
    assert stale is not None and stale.attrs["stale"] is True and len(stale) == 50
    assert cache.get_frame("prices", "absent", None) is None


def test_cache_frame_tz_aware_index(tmp_path):
    cache = DiskCache(tmp_path)
    df = make_ohlcv(n=5, seed=2)
    df.index = df.index.tz_localize("America/New_York")
    cache.put_frame("prices", "tz", df, None)
    back = cache.get_frame("prices", "tz")
    assert back.index.tz is None and back.index[0] == pd.Timestamp(df.index[0].date())


# =====================================================================================
# network seam: offline, fixtures, live (mocked)
# =====================================================================================


def test_offline_refuses_everything(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("requests.get must not be called offline")

    monkeypatch.setattr(common.requests, "get", boom)
    with pytest.raises(OfflineError):
        common.http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10")
    with pytest.raises(OfflineError):
        common.http_get_json("https://www.sec.gov/files/company_tickers.json", sec=True)
    for fn in (common.yf_history, common.yf_info, common.yf_funds):
        with pytest.raises(OfflineError):
            fn("KO")
    assert issubclass(OfflineError, DataUnavailable)  # fetchers can fall back on DataUnavailable


def test_socket_is_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network access is blocked"):
            s.connect(("192.0.2.1", 80))
    finally:
        s.close()


def test_fixture_http(tmp_path, use_fixtures):
    fx = use_fixtures(write_fixture_tree(tmp_path / "fx"))
    assert common.active_fixtures_dir() == fx.resolve()
    assert not common.is_offline()  # use_fixtures opts out of offline mode
    data = common.http_get_json("https://www.sec.gov/files/company_tickers.json", sec=True)
    assert data == {"0": {"cik_str": 21344, "ticker": "KO"}}
    text = common.http_get_text("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": "DGS10"})
    assert text.startswith("observation_date,DGS10")
    resp = common.http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10")
    assert resp.status_code == 200 and resp.ok and resp.headers["Content-Type"] == "text/csv"
    with pytest.raises(FixtureMissing):
        common.http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=NOPE")
    with pytest.raises(DataUnavailable) as ei:
        common.http_get("https://stooq.com/q/d/l/?s=xyz.us&i=d")
    assert ei.value.status_code == 404
    r404 = common.http_get("https://stooq.com/q/d/l/?s=xyz.us&i=d", raise_for_status=False)
    assert r404.status_code == 404 and not r404.ok
    with pytest.raises(ProviderBlocked):
        common.http_get("https://example.org/blocked")


def test_fixtures_win_over_offline(tmp_path, monkeypatch):
    monkeypatch.setenv(common.ENV_FIXTURES_DIR, str(write_fixture_tree(tmp_path / "fx")))
    assert common.is_offline()
    assert common.yf_info("KO")["marketCap"] == 2.9e11
    assert common.http_get_json("https://www.sec.gov/files/company_tickers.json")


def test_fixture_mode_ignores_sec_email(tmp_env, tmp_path, use_fixtures):
    """No real request happens in fixture mode, so a placeholder email does not block it."""
    tmp_env.update_config("investor_profile", lambda p: p.update(sec_contact_email="x@example.com"))
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    assert common.http_get_json("https://www.sec.gov/files/company_tickers.json", sec=True)


def test_fixture_yf(tmp_path, use_fixtures):
    use_fixtures(write_fixture_tree(tmp_path / "fx"))
    df = common.yf_history("KO")
    assert list(df.columns) == common.OHLCV_COLUMNS and df.index.name == "Date"
    assert df.index.is_monotonic_increasing and len(df) == 29  # NaN close dropped
    assert df.index[-1] == pd.Timestamp(DEFAULT_AS_OF)
    assert df.dtypes.eq("float64").all()
    assert len(common.yf_history("brk.b")) == 10  # normalized-ticker lookup
    assert common.yf_info("KO")["sector"] == "Consumer Defensive"
    funds = common.yf_funds("QQQ")
    assert funds == {"sector_weightings": {"technology": 0.52, "realestate": 0.002},
                     "top_holdings": [{"symbol": "NVDA", "name": "NVIDIA Corp", "holding_pct": 9.1}],
                     "expense_ratio_pct": 0.18, "expense_ratio_field": "info.netExpenseRatio"}
    for fn in (common.yf_history, common.yf_info, common.yf_funds):
        with pytest.raises(FixtureMissing):
            fn("ZZZZ")


def test_fixture_index_missing(tmp_path, use_fixtures):
    use_fixtures(tmp_path / "empty")
    with pytest.raises(FixtureMissing, match="fixture index not found"):
        common.http_get("https://x.org/")


def test_live_http_retries_429_with_retry_after(live_http):
    live_http.script = [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(503),
                        FakeResponse(200, b'{"a": 1}')]
    assert common.http_get_json("https://fred.stlouisfed.org/x") == {"a": 1}
    assert len(live_http.calls) == 3
    assert live_http.sleeps == [7.0, 2.0]  # Retry-After honoured, then exponential backoff (attempt 2)
    assert live_http.calls[0]["headers"]["User-Agent"] == common.DEFAULT_USER_AGENT


def test_live_http_retry_after_is_capped_and_http_date(live_http):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    live_http.script = [FakeResponse(429, headers={"Retry-After": "3600"}),
                        FakeResponse(429, headers={"Retry-After": future}), FakeResponse(200, b"ok")]
    assert common.http_get_text("https://x.org/a") == "ok"
    assert live_http.sleeps == [common.HTTP_MAX_SLEEP_S, common.HTTP_MAX_SLEEP_S]


def test_live_http_connection_errors_then_give_up(live_http):
    live_http.script = [requests.ConnectionError("reset")] * 4
    with pytest.raises(DataUnavailable, match="network error"):
        common.http_get("https://x.org/b")
    assert len(live_http.calls) == 4 and live_http.sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("exc", [
    requests.exceptions.ContentDecodingError("bad gzip"), requests.exceptions.ChunkedEncodingError("cut"),
    requests.exceptions.ReadTimeout("slow"), requests.exceptions.ConnectTimeout("slow"),
    requests.exceptions.SSLError("handshake"),
], ids=lambda e: type(e).__name__)
def test_live_http_retries_transient_request_errors(live_http, exc):
    live_http.script = [exc, FakeResponse(200, b'{"a": 2}')]
    assert common.http_get_json("https://x.org/t") == {"a": 2}
    assert len(live_http.calls) == 2 and live_http.sleeps == [1.0]


@pytest.mark.parametrize("exc", [
    requests.exceptions.ContentDecodingError("bad gzip"), requests.exceptions.ChunkedEncodingError("cut"),
], ids=lambda e: type(e).__name__)
def test_live_http_exhausted_transient_errors_become_data_unavailable(live_http, exc):
    live_http.script = [exc] * 4
    with pytest.raises(DataUnavailable, match="network error") as ei:
        common.http_get("https://x.org/u")
    assert ei.value.url == "https://x.org/u" and type(exc).__name__ in str(ei.value)
    assert not isinstance(ei.value, requests.exceptions.RequestException)
    assert len(live_http.calls) == 4 and live_http.sleeps == [1.0, 2.0, 4.0]


def test_live_http_content_decoding_error_retries_uncompressed(live_http):
    live_http.script = [requests.exceptions.ContentDecodingError("bad gzip"), FakeResponse(200, b"ok")]
    assert common.http_get_text("https://x.org/enc") == "ok"
    assert live_http.calls[0]["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert live_http.calls[1]["headers"]["Accept-Encoding"] == "identity"


def test_live_http_content_decoding_error_keeps_caller_accept_encoding(live_http):
    live_http.script = [requests.exceptions.ContentDecodingError("bad gzip"), FakeResponse(200, b"ok")]
    common.http_get("https://x.org/enc2", headers={"accept-encoding": "br"})
    assert live_http.calls[1]["headers"] == live_http.calls[0]["headers"]  # the caller's choice is not overridden
    assert "identity" not in live_http.calls[1]["headers"].values()


@pytest.mark.parametrize("exc", [
    requests.exceptions.InvalidURL("Failed to parse"), requests.exceptions.MissingSchema("no scheme"),
    requests.exceptions.InvalidSchema("no adapter"), requests.exceptions.TooManyRedirects("30 redirects"),
    requests.exceptions.InvalidHeader("bad header"), requests.exceptions.RequestException("generic"),
], ids=lambda e: type(e).__name__)
def test_live_http_other_request_errors_are_data_unavailable_not_retried(live_http, exc):
    live_http.script = [exc]
    with pytest.raises(DataUnavailable, match="request failed") as ei:
        common.http_get("https://x.org/bad")
    assert ei.value.url == "https://x.org/bad" and type(exc).__name__ in str(ei.value)
    assert ei.value.__cause__ is exc
    assert len(live_http.calls) == 1 and live_http.sleeps == []


def test_live_http_get_text_and_json_never_leak_requests_exceptions(live_http):
    for fn in (common.http_get_text, common.http_get_json):
        live_http.script = [requests.exceptions.TooManyRedirects("loop")]
        with pytest.raises(DataUnavailable):
            fn("https://x.org/loop")


def test_live_http_sec_request_error_still_throttled_and_converted(live_http, tmp_env):
    tmp_env.update_config("investor_profile", lambda p: p.update(sec_contact_email="researcher@gmail.com"))
    live_http.script = [requests.exceptions.ContentDecodingError("bad gzip")] * 4
    with pytest.raises(DataUnavailable):
        common.http_get("https://data.sec.gov/x.json", sec=True)


def test_live_http_final_status_errors(live_http):
    live_http.script = [FakeResponse(404)]
    with pytest.raises(DataUnavailable) as ei:
        common.http_get("https://x.org/c")
    assert ei.value.status_code == 404 and ei.value.url == "https://x.org/c"
    assert live_http.sleeps == []  # 404 is not retried
    live_http.script = [FakeResponse(403)]
    with pytest.raises(ProviderBlocked):
        common.http_get("https://x.org/d")
    live_http.script = [FakeResponse(500)] * 4
    with pytest.raises(DataUnavailable) as ei:
        common.http_get("https://x.org/e")
    assert ei.value.status_code == 500 and len(live_http.sleeps) == 3
    live_http.script = [FakeResponse(410, b"gone")]
    resp = common.http_get("https://x.org/f", raise_for_status=False)
    assert resp.status_code == 410


def test_live_http_params_and_headers(live_http):
    common.http_get("https://x.org/g", params={"id": "DGS10", "x": "a b"}, headers={"Accept": "text/csv"},
                    timeout=5)
    call = live_http.calls[0]
    assert call["url"] == "https://x.org/g?id=DGS10&x=a+b" and call["timeout"] == 5
    assert call["headers"]["Accept"] == "text/csv"


def test_sec_user_agent_and_throttle(live_http, tmp_env):
    common.http_get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000021344.json", sec=True)
    common.http_get("https://data.sec.gov/submissions/CIK0000021344.json", sec=True)
    ua = live_http.calls[0]["headers"]["User-Agent"]
    assert ua == "agent-investors research researcher@gmail.com"
    assert common.sec_user_agent() == ua
    # second call came right after the first: the limiter must have slept ~0.11 s
    assert live_http.sleeps and 0.05 < live_http.sleeps[-1] <= common.SEC_MIN_INTERVAL_S
    assert common.SEC_MIN_INTERVAL_S >= 0.1


def test_sec_throttle_spacing_real_clock(monkeypatch):
    """With a fake clock, consecutive SEC calls are spaced by >= SEC_MIN_INTERVAL_S."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(time, "sleep", fake_sleep)
    stamps = []
    for _ in range(5):
        common._sec_throttle()
        stamps.append(clock["t"])
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= common.SEC_MIN_INTERVAL_S - 1e-9 for g in gaps), gaps


@pytest.mark.parametrize("email", [None, "", "you@example.com", "garbage"])
def test_sec_requires_real_email(live_http, tmp_env, email):
    tmp_env.update_config("investor_profile", lambda p: p.update(sec_contact_email=email))
    with pytest.raises(ConfigError, match="sec_contact_email"):
        common.http_get("https://data.sec.gov/x.json", sec=True)
    assert live_http.calls == []
    # non-SEC requests are unaffected
    common.http_get("https://fred.stlouisfed.org/x")


def test_sec_missing_profile(live_http, tmp_env):
    (tmp_env.config_dir / "investor_profile.yaml").unlink()
    with pytest.raises(ConfigError, match="/committee-setup"):
        common.http_get("https://data.sec.gov/x.json", sec=True)


def test_http_get_json_invalid(live_http):
    live_http.script = [FakeResponse(200, b"<html>bot check</html>")]
    with pytest.raises(DataUnavailable, match="invalid JSON"):
        common.http_get_json("https://x.org/h")


def test_response_text_bom_and_latin1():
    assert common.response_text(FakeResponse(200, b"\xef\xbb\xbfa,b")) == "a,b"

    class Latin:
        content = "café".encode("latin-1")
        text = "café"

    assert common.response_text(Latin()) == "café"


# ---- yfinance normalisation with a fake module -------------------------------------------


class _FakeFundsData:
    def __init__(self, sector=None, top=None, ops=None):
        self.sector_weightings = sector if sector is not None else {"technology": 0.5, "realestate": 0.01}
        self.top_holdings = top if top is not None else pd.DataFrame(
            {"Name": ["NVIDIA Corp", "Microsoft Corp"], "Holding Percent": [0.0912, 0.0801]},
            index=pd.Index(["NVDA", "MSFT"], name="Symbol"))
        self.fund_operations = ops if ops is not None else pd.DataFrame(
            {"QQQ": [0.002, 0.08, 3.0e11], "Category Average": [0.009, 0.5, 1.0e9]},
            index=pd.Index(["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"],
                           name="Attributes"))


class _FakeTicker:
    registry: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol
        spec = self.registry.get(symbol, {})
        self._spec = spec
        self.info = spec.get("info", {})
        self.history_calls = []

    @property
    def funds_data(self):
        if "funds_error" in self._spec:
            raise self._spec["funds_error"]
        return self._spec.get("funds", _FakeFundsData())

    def history(self, **kw):
        _FakeTicker.last_history_kwargs = kw
        return self._spec.get("history", pd.DataFrame())


@pytest.fixture
def fake_yf(monkeypatch):
    monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    module = types.SimpleNamespace(Ticker=_FakeTicker)
    monkeypatch.setitem(sys.modules, "yfinance", module)
    _FakeTicker.registry = {}
    return _FakeTicker.registry


def test_yf_funds_uses_net_expense_ratio_as_percent(fake_yf):
    fake_yf["QQQ"] = {"info": {"netExpenseRatio": 0.18}}
    out = common.yf_funds("QQQ")
    assert out["expense_ratio_pct"] == 0.18 and out["expense_ratio_field"] == "info.netExpenseRatio"
    assert out["sector_weightings"] == {"technology": 0.5, "realestate": 0.01}
    assert out["top_holdings"] == [{"symbol": "NVDA", "name": "NVIDIA Corp", "holding_pct": 9.12},
                                   {"symbol": "MSFT", "name": "Microsoft Corp", "holding_pct": 8.01}]


def test_yf_funds_falls_back_to_fund_operations_fraction(fake_yf):
    fake_yf["QQQ"] = {"info": {"netExpenseRatio": None}}
    out = common.yf_funds("QQQ")
    assert out["expense_ratio_pct"] == pytest.approx(0.2)
    assert out["expense_ratio_field"] == "funds_data.fund_operations.Annual Report Expense Ratio"


def test_yf_funds_no_expense_and_no_data(fake_yf):
    empty_ops = pd.DataFrame()
    fake_yf["XYZ"] = {"info": {}, "funds": _FakeFundsData(ops=empty_ops)}
    out = common.yf_funds("XYZ")
    assert out["expense_ratio_pct"] is None and out["expense_ratio_field"] == "unavailable"
    fake_yf["EMPTY"] = {"funds": _FakeFundsData(sector={}, top=pd.DataFrame())}
    with pytest.raises(DataUnavailable, match="no fund composition"):
        common.yf_funds("EMPTY")
    fake_yf["KO"] = {"funds_error": KeyError("not a fund")}
    with pytest.raises(DataUnavailable, match="funds_data failed"):
        common.yf_funds("KO")


def test_yf_history_normalises(fake_yf):
    raw = make_ohlcv(n=20, seed=5)
    raw.index = raw.index.tz_localize("America/New_York")
    raw["Dividends"] = 0.0
    raw.iloc[3, raw.columns.get_loc("Close")] = np.inf
    raw.iloc[4, raw.columns.get_loc("High")] = np.nan
    fake_yf["KO"] = {"history": raw}
    df = common.yf_history("KO", period="1y")
    assert _FakeTicker.last_history_kwargs == {"period": "1y", "interval": "1d", "auto_adjust": True}
    assert list(df.columns) == common.OHLCV_COLUMNS and df.index.tz is None and len(df) == 19
    assert (df.index == df.index.normalize()).all()
    assert df["High"].notna().all()
    fake_yf["NONE"] = {"history": pd.DataFrame()}
    with pytest.raises(DataUnavailable, match="no price history"):
        common.yf_history("NONE")


def test_yf_info(fake_yf):
    fake_yf["KO"] = {"info": {"sector": "Consumer Defensive"}}
    assert common.yf_info("KO") == {"sector": "Consumer Defensive"}
    fake_yf["BAD"] = {"info": {"trailingPegRatio": None}}
    with pytest.raises(DataUnavailable):
        common.yf_info("BAD")


def test_clean_ohlcv_variants():
    df = pd.DataFrame({"date": ["2026-01-05 00:00:00-05:00", "2026-01-02", "2026-01-05"],
                       "close": [10.0, 9.0, 11.0], "volume": [None, 5, 6]})
    out = common.clean_ohlcv(df)
    assert list(out.index) == [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-05")]
    assert out.loc["2026-01-05", "Close"] == 11.0  # duplicate date keeps last
    assert out.loc["2026-01-02", "Open"] == 9.0 and out["Volume"].tolist() == [5.0, 6.0]
    with pytest.raises(DataUnavailable):
        common.clean_ohlcv(pd.DataFrame({"Open": [1.0]}))
    with pytest.raises(DataUnavailable):
        common.clean_ohlcv(None)


# =====================================================================================
# conftest fixtures
# =====================================================================================


def test_make_ohlcv_contract(make_ohlcv):
    df = make_ohlcv()
    assert len(df) == 800 and df.index.name == "Date" and list(df.columns) == common.OHLCV_COLUMNS
    assert df.index[-1] == pd.Timestamp("2026-10-02") and df.index.is_monotonic_increasing
    assert (df.index.dayofweek < 5).all()
    assert (df["High"] >= df[["Open", "Close"]].max(axis=1)).all()
    assert (df["Low"] <= df[["Open", "Close"]].min(axis=1)).all()
    pd.testing.assert_frame_equal(df, make_ohlcv())  # deterministic
    other = make_ohlcv(n=10, seed=1, end="2025-06-30")
    assert len(other) == 10 and other.index[-1] == pd.Timestamp("2025-06-30")
    assert not other["Close"].equals(make_ohlcv(n=10, seed=2, end="2025-06-30")["Close"])


def test_make_ranging_ohlcv_stays_in_band():
    df = make_ranging_ohlcv(n=300, seed=3)
    assert df["Close"].between(100 * (1 - 0.075), 100 * (1 + 0.075)).all()
    assert df.index[-1] == pd.Timestamp(DEFAULT_AS_OF)


def test_tmp_env_isolation(tmp_env):
    assert common.config_dir() == tmp_env.config_dir.resolve()
    assert common.cache_dir() == tmp_env.cache_dir.resolve()
    assert common.runs_dir() == tmp_env.runs_dir.resolve()
    assert common.ledger_path() == tmp_env.ledger.resolve()
    assert sorted(p.name for p in tmp_env.config_dir.glob("*.yaml")) == sorted(
        f"{n}.yaml" for n in common.CONFIG_NAMES)
    tmp_env.write_config("universe", {"region": "US"})
    assert tmp_env.read_config("universe") == {"region": "US"}
    assert common.load_config("universe", REPO_ROOT / "config")["max_candidates_per_sector"] == 6


def test_fixtures_dir_and_repo_root(fixtures_dir, repo_root):
    assert repo_root == REPO_ROOT and fixtures_dir == REPO_ROOT / "tests" / "fixtures"


def test_run_live_option_registered(request):
    assert request.config.getoption("--run-live") in (True, False)
    assert any(line.startswith("live:") for line in request.config.getini("markers"))


# =====================================================================================
# repo files owned by U0a
# =====================================================================================


def test_ledger_header_exact():
    with open(REPO_ROOT / "ledger" / "recommendations.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    # The ledger accumulates real recommendations after live runs; only the header is a contract.
    assert rows[0] == LEDGER_COLUMNS
    assert all(len(r) == len(LEDGER_COLUMNS) for r in rows[1:])


def test_repo_scaffold_files():
    for rel in (".gitignore", "requirements.txt", "pytest.ini", "CLAUDE.md", "CONTRACTS.md",
                "scripts/__init__.py", "data/cache/.gitkeep", "runs/.gitkeep"):
        assert (REPO_ROOT / rel).is_file(), rel
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for line in ("data/cache/*", "!data/cache/.gitkeep", "runs/*", "!runs/.gitkeep", "__pycache__/",
                 "*.pyc", ".pytest_cache/"):
        assert line in gi.splitlines(), line
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "Python 3.11+" in req
    for pkg in ("pandas", "numpy", "yfinance", "requests", "pyyaml", "jsonschema", "referencing", "jinja2",
                "pytest"):
        assert pkg in req, pkg


def test_no_forbidden_model_name_in_owned_files():
    forbidden = "o" + "pus"
    owned = [".gitignore", "requirements.txt", "pytest.ini", "CLAUDE.md", "CONTRACTS.md", "scripts/common.py",
             "tests/conftest.py", "tests/test_common.py"]
    owned += [f"config/{n}.yaml" for n in common.CONFIG_NAMES]
    owned += [f"schemas/config_{n}.schema.json" for n in common.CONFIG_NAMES]
    for rel in owned:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
        assert forbidden not in text, rel


def test_common_never_uses_requests_or_yfinance_outside_seam():
    src = (REPO_ROOT / "scripts" / "common.py").read_text(encoding="utf-8")
    assert src.count("requests.get(") == 1
    assert src.count("import yfinance") == 1


# =====================================================================================
# live checks (skipped unless --run-live)
# =====================================================================================


@pytest.mark.live
def test_live_fred_csv():
    text = common.http_get_text(common.fred_csv_url("DGS10"))
    assert text.splitlines()[0].strip() == "observation_date,DGS10"


@pytest.mark.live
def test_live_sec_ticker_map():
    data = common.http_get_json(common.SEC_TICKERS_URL, sec=True)
    assert any(v.get("ticker") == "KO" and v.get("cik_str") == 21344 for v in data.values())


@pytest.mark.live
def test_live_yf_history_and_info():
    df = common.yf_history("KO", period="1mo")
    assert list(df.columns) == common.OHLCV_COLUMNS and len(df) > 10 and df.index.tz is None
    assert common.yf_info("KO").get("sector") in common.YF_SECTOR_MAP


@pytest.mark.live
def test_live_yf_funds_qqq():
    out = common.yf_funds("QQQ")
    assert set(out["sector_weightings"]) <= set(common.YF_FUND_SECTOR_MAP)
    assert 0.9 < sum(out["sector_weightings"].values()) < 1.1
    assert out["top_holdings"] and all(0 < (h["holding_pct"] or 0) < 30 for h in out["top_holdings"])
    assert out["expense_ratio_pct"] is not None and 0 < out["expense_ratio_pct"] < 1


def test_live_investor_profile_is_valid_and_has_investor_context():
    """The repo's own config/investor_profile.yaml validates and, once /committee-setup has filled it,
    carries the personal context the CIO and ETF analyst read (skipped while it is the empty template)."""
    prof = common.load_yaml(common.REPO_ROOT / "config" / "investor_profile.yaml")
    assert common.validate_config("investor_profile", prof) == []
    if not prof.get("investor"):
        pytest.skip("config/investor_profile.yaml is still the empty template (run /committee-setup)")
    assert prof["risk_tolerance"] in ("conservative", "moderate", "aggressive")
    assert prof["horizon_years"] >= 40  # lifelong horizon


def test_investor_block_rejects_unknown_keys_and_bad_types(tmp_env):
    prof = tmp_env.read_config("investor_profile")
    prof["investor"] = {"age": "twenty-four"}
    assert common.validate_config("investor_profile", prof)
    prof["investor"] = {"salary_dreams": 1}
    assert common.validate_config("investor_profile", prof)
    prof["investor"] = {"age": 24, "expected_income": {"monthly_ils": 20000, "starts": None, "note": None}}
    assert common.validate_config("investor_profile", prof) == []


def test_yf_quote_fixture_and_offline(tmp_env, use_fixtures, tmp_path):
    fx = tmp_path / "fx"
    (fx / "yf" / "quote").mkdir(parents=True)
    (fx / "yf" / "quote" / "KO.json").write_text(
        '{"price": 61.5, "previous_close": 60.0, "currency": "USD", "retrieved_at": "2026-10-27T00:00:00+00:00"}',
        encoding="utf-8")
    use_fixtures(fx)
    assert common.yf_quote("KO")["price"] == 61.5
    with pytest.raises(common.FixtureMissing):
        common.yf_quote("NOPE")


def test_yf_quote_offline_raises(monkeypatch):
    monkeypatch.setenv(common.ENV_OFFLINE, "1")
    monkeypatch.delenv(common.ENV_FIXTURES_DIR, raising=False)
    with pytest.raises(common.OfflineError):
        common.yf_quote("KO")
