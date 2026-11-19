"""Shared pytest fixtures (DESIGN.md §3.1, §9).

Every test runs hermetically:
* real network is blocked (``socket.socket.connect`` raises) and ``AGINV_OFFLINE=1`` is set,
  so any accidental HTTP / yfinance call fails fast. Opt into fixture mode with the
  ``use_fixtures`` fixture (sets ``AGINV_FIXTURES_DIR`` and clears ``AGINV_OFFLINE``); the
  ``common`` seam serves fixtures before its offline check anyway.
* ``AGINV_AS_OF=2026-10-02`` (deterministic "today"); other ``AGINV_*`` vars from the shell are
  removed; cache / runs / ledger default to per-test temp dirs (the repo config dir is used
  read-only unless the test asks for ``tmp_env``).
* tests marked ``@pytest.mark.live`` are skipped unless ``--run-live`` is given; live tests
  get real network and no offline flag.

Helpers importable from tests: ``from conftest import make_ohlcv, make_ranging_ohlcv,
SAMPLE_PORTFOLIO, REPO_ROOT, FIXTURES_DIR``.
"""
from __future__ import annotations

import copy
import os
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DEFAULT_AS_OF = "2026-10-02"

if str(REPO_ROOT) not in sys.path:  # pytest.ini sets pythonpath=., this keeps ad-hoc runs working
    sys.path.insert(0, str(REPO_ROOT))

from scripts import common  # noqa: E402

#: A filled portfolio (shares for every prefilled holding, cash in USD) for tests that need a
#: valid portfolio.yaml. Values are synthetic test data, not the user's positions.
SAMPLE_PORTFOLIO: dict[str, Any] = {
    "as_of": "2026-10-01",
    "total_value_usd": None,
    "cash_usd": 5000.0,
    "cash_pct": None,
    "holdings": [
        {"ticker": "QQQ", "asset_type": "fund", "shares": 40, "weight_pct": None, "cost_basis": 400.0,
         "cost_basis_currency": "USD", "purchase_date": "2023-05-10", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
        {"ticker": "SPMO", "asset_type": "fund", "shares": 150, "weight_pct": None, "cost_basis": 70.0,
         "cost_basis_currency": "USD", "purchase_date": "2024-02-01", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
        {"ticker": "BRK-B", "asset_type": "stock", "shares": 20, "weight_pct": None, "cost_basis": 350.0,
         "cost_basis_currency": "USD", "purchase_date": "2022-08-15", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
        {"ticker": "KO", "asset_type": "stock", "shares": 100, "weight_pct": None, "cost_basis": 55.0,
         "cost_basis_currency": "USD", "purchase_date": "2021-03-01", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
        {"ticker": "PG", "asset_type": "stock", "shares": 40, "weight_pct": None, "cost_basis": 140.0,
         "cost_basis_currency": "USD", "purchase_date": "2021-03-01", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
        {"ticker": "JPM", "asset_type": "stock", "shares": 30, "weight_pct": None, "cost_basis": 150.0,
         "cost_basis_currency": "USD", "purchase_date": "2022-01-20", "currency": "USD",
         "proxy_ticker": None, "sector_override": None},
    ],
}

_CLEAN_ENV = [v for v in common.AGINV_ENV_VARS]


# =====================================================================================
# CLI option + live marker
# =====================================================================================


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-live", action="store_true", default=False,
                     help="run tests marked @pytest.mark.live (they hit the real network)")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: hits the real network (run with --run-live)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live network test: pass --run-live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# =====================================================================================
# Hermetic environment (autouse)
# =====================================================================================


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to open a real network connection."""


_real_connect = socket.socket.connect


def _is_loopback(addr: Any) -> bool:
    return isinstance(addr, tuple) and bool(addr) and addr[0] in ("127.0.0.1", "localhost", "::1")


def _blocked_connect(self: socket.socket, *args: Any, **kwargs: Any) -> None:
    if args and _is_loopback(args[0]):  # loopback never leaves the machine (local dashboard test)
        return _real_connect(self, *args, **kwargs)
    raise NetworkBlocked(f"network access is blocked in tests (attempted connect to {args!r}); "
                         "use fixtures (AGINV_FIXTURES_DIR) or monkeypatch common.http_get / common.yf_*")


@pytest.fixture(autouse=True)
def _hermetic_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                  tmp_path_factory: pytest.TempPathFactory) -> None:
    """Block network, force offline mode, pin AGINV_AS_OF, isolate cache/runs/ledger.
    Every AGINV_* variable is restored afterwards, even when code under test set it directly
    (e.g. ``common.apply_common_args``)."""
    original = {var: os.environ.get(var) for var in _CLEAN_ENV}
    for var in _CLEAN_ENV:
        monkeypatch.delenv(var, raising=False)
    live = request.node.get_closest_marker("live") is not None
    scratch = tmp_path_factory.mktemp("aginv")
    monkeypatch.setenv(common.ENV_CACHE_DIR, str(scratch / "cache"))
    monkeypatch.setenv(common.ENV_RUNS_DIR, str(scratch / "runs"))
    monkeypatch.setenv(common.ENV_LEDGER, str(scratch / "ledger" / "recommendations.csv"))
    if not live:
        monkeypatch.setenv(common.ENV_OFFLINE, "1")
        monkeypatch.setenv(common.ENV_AS_OF, DEFAULT_AS_OF)
        monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    common.clear_caches()
    yield
    for var, value in original.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    common.clear_caches()


# =====================================================================================
# Synthetic price data (ported from stock_dashboard/tests/conftest.py, fixed end date)
# =====================================================================================


def make_ohlcv(n: int = 800, seed: int = 0, drift: float = 0.0003, vol: float = 0.015,
               start: float = 100.0, end: str = DEFAULT_AS_OF) -> pd.DataFrame:
    """Seeded GBM-like daily bars on a business-day index ending at ``end`` (inclusive when
    ``end`` is a weekday). Columns Open, High, Low, Close, Volume; index named ``Date``.
    High >= max(Open, Close) and Low <= min(Open, Close) on every row."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    rets = rng.normal(drift, vol, size=n)
    closes = start * np.exp(np.cumsum(rets))
    opens = np.empty(n)
    opens[0] = start
    opens[1:] = closes[:-1]
    noise = np.abs(rng.normal(0, vol / 2, size=n))
    highs = np.maximum(opens, closes) * (1 + noise)
    lows = np.minimum(opens, closes) * (1 - noise)
    volume = rng.lognormal(mean=13.8, sigma=0.3, size=n)
    df = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volume},
                      index=idx)
    df.index.name = "Date"
    return df


def make_ranging_ohlcv(n: int = 400, seed: int = 0, mid: float = 100.0, band: float = 0.05,
                       vol: float = 0.01, end: str = DEFAULT_AS_OF) -> pd.DataFrame:
    """Mean-reverting series confined to ±1.5·band around ``mid`` (for 'range' regime tests)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    closes = np.empty(n)
    closes[0] = mid
    theta = 0.08
    for i in range(1, n):
        closes[i] = closes[i - 1] + theta * (mid - closes[i - 1]) + rng.normal(0, vol) * mid
        closes[i] = min(max(closes[i], mid * (1 - band * 1.5)), mid * (1 + band * 1.5))
    opens = np.empty(n)
    opens[0] = mid
    opens[1:] = closes[:-1]
    noise = np.abs(rng.normal(0, vol / 2, size=n))
    highs = np.maximum(opens, closes) * (1 + noise)
    lows = np.minimum(opens, closes) * (1 - noise)
    volume = rng.lognormal(mean=13.8, sigma=0.2, size=n)
    df = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volume},
                      index=idx)
    df.index.name = "Date"
    return df


@pytest.fixture(name="make_ohlcv")
def _make_ohlcv_fixture() -> Callable[..., pd.DataFrame]:
    """Fixture form of ``make_ohlcv`` (returns the factory)."""
    return make_ohlcv


@pytest.fixture(name="make_ranging_ohlcv")
def _make_ranging_ohlcv_fixture() -> Callable[..., pd.DataFrame]:
    return make_ranging_ohlcv


# =====================================================================================
# Paths / isolated environment
# =====================================================================================


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    """``<repo>/tests/fixtures`` (each unit keeps its fixtures in a subfolder)."""
    return FIXTURES_DIR


@pytest.fixture
def use_fixtures(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | Path], Path]:
    """``use_fixtures(dir)`` turns on fixture mode (``AGINV_FIXTURES_DIR=dir``) for this test and
    clears ``AGINV_OFFLINE`` (sockets stay blocked, so nothing can reach the real network).
    Relative paths resolve against ``tests/fixtures``."""
    def activate(path: str | Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = FIXTURES_DIR / p
        monkeypatch.setenv(common.ENV_FIXTURES_DIR, str(p))
        monkeypatch.delenv(common.ENV_OFFLINE, raising=False)
        common.clear_caches()
        return p
    return activate


@pytest.fixture
def sample_portfolio() -> dict[str, Any]:
    """A deep copy of ``SAMPLE_PORTFOLIO`` (valid, shares-based)."""
    return copy.deepcopy(SAMPLE_PORTFOLIO)


def _dump_yaml(path: Path, data: Any) -> None:
    common.write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolated config/cache/runs/ledger dirs under ``tmp_path`` with a copy of the repo's
    ``config/*.yaml`` and ledger header; the matching ``AGINV_*`` env vars point at them.

    Attributes: ``root, config_dir, cache_dir, runs_dir, ledger`` (Paths) and helpers
    ``read_config(name) -> dict``, ``write_config(name, data)``,
    ``update_config(name, fn)`` (fn mutates the loaded dict), ``use_sample_portfolio()``."""
    root = tmp_path / "env"
    cfg = root / "config"
    cfg.mkdir(parents=True)
    for f in (REPO_ROOT / "config").glob("*.yaml"):
        shutil.copy2(f, cfg / f.name)
    # The repo's portfolio.yaml / investor_profile.yaml hold the investor's real, changing data.
    # Tests run against the pristine shipped template instead (null holdings, placeholder profile).
    for name in ("portfolio.yaml", "investor_profile.yaml"):
        shutil.copy2(REPO_ROOT / "tests" / "fixtures" / "config_template" / name, cfg / name)
    cache = root / "cache"
    runs = root / "runs"
    ledger = root / "ledger" / "recommendations.csv"
    for d in (cache, runs, ledger.parent):
        d.mkdir(parents=True, exist_ok=True)
    repo_ledger = REPO_ROOT / "ledger" / "recommendations.csv"
    if repo_ledger.is_file():
        header = common.read_text(repo_ledger).splitlines()[0]
        common.write_text(ledger, header + "\n")
    monkeypatch.setenv(common.ENV_CONFIG_DIR, str(cfg))
    monkeypatch.setenv(common.ENV_CACHE_DIR, str(cache))
    monkeypatch.setenv(common.ENV_RUNS_DIR, str(runs))
    monkeypatch.setenv(common.ENV_LEDGER, str(ledger))
    common.clear_caches()

    def read_config(name: str) -> dict:
        return common.load_config(name, cfg)

    def write_config(name: str, data: Any) -> Path:
        path = cfg / f"{name}.yaml"
        _dump_yaml(path, data)
        common.clear_caches()
        return path

    def update_config(name: str, fn: Callable[[dict], Any]) -> dict:
        data = read_config(name)
        result = fn(data)
        data = result if isinstance(result, dict) else data
        write_config(name, data)
        return data

    def use_sample_portfolio() -> dict:
        data = copy.deepcopy(SAMPLE_PORTFOLIO)
        write_config("portfolio", data)
        return data

    return SimpleNamespace(root=root, config_dir=cfg, cache_dir=cache, runs_dir=runs, ledger=ledger,
                           read_config=read_config, write_config=write_config,
                           update_config=update_config, use_sample_portfolio=use_sample_portfolio)
