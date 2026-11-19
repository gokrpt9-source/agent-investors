"""Tests for scripts/_steps.py — the pipeline step logic behind committee.py.

Everything runs on synthetic run directories written by the helpers below. The integration-API
modules of the other units (fetch_prices, compute_technicals, aggregate, portfolio, ...) are
replaced by recording fakes installed in ``sys.modules`` (``_steps`` imports them lazily), so
these tests depend on no other unit's code and never touch the network.
"""
from __future__ import annotations

import copy
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from conftest import REPO_ROOT, SAMPLE_PORTFOLIO
from scripts import common
from scripts import _steps as steps

AS_OF = "2026-10-02"
RUN_ID = "2026-10-02_1000"
#: (ticker, asset_type, sector, look-through weight %)
HOLDINGS = [
    ("QQQ", "fund", "funds", 30.0),
    ("SPMO", "fund", "funds", 20.0),
    ("BRK-B", "stock", "financials", 10.0),
    ("KO", "stock", "consumer_staples", 15.0),
    ("PG", "stock", "consumer_staples", 10.0),
    ("JPM", "stock", "financials", 10.0),
]
SECTOR_WEIGHTS = {"information_technology": 30.0, "financials": 22.0, "consumer_staples": 25.0,
                  "health_care": 6.0, "industrials": 5.0, "communication_services": 7.0, "cash": 5.0}
TARGETS = {"information_technology": 30.0, "financials": 22.0, "consumer_staples": 20.0,
           "health_care": 10.0, "industrials": 6.0, "communication_services": 7.0, "cash": 5.0}
P3_AFTER_SCREEN = ["p3.light", "p3.finalists", "p3.full", "p3.da", "p3.aggregate", "p3.sector_heads"]


# =====================================================================================
# helpers
# =====================================================================================


def P(p: str | Path) -> str:
    """Absolute posix form used by _steps for every path it emits."""
    return Path(p).resolve().as_posix()


def src(sid: str, path: str | None = None) -> dict:
    sym = sid.split(":")[2]
    out = {"id": sid, "url": f"https://finance.yahoo.com/quote/{sym}/", "title": sid,
           "accessed": AS_OF, "kind": "dataset"}
    if path:
        out["path"] = path
    return out


def fact(value: float | None, unit: str = "USD/share", sid: str = "D:PX:X", kind: str = "price") -> dict:
    return common.fact(value, unit, sid, AS_OF, kind)


def env(schema: str, sources: list[dict] | None = None, **payload: Any) -> dict:
    return common.envelope(schema, AS_OF, sources=sources or [], **payload)


def write_snapshots(run_dir: Path, *, committee: Callable[[dict], None] | None = None,
                    portfolio: dict | None = None) -> None:
    """``inputs/<name>.yaml`` snapshots (deterministic bytes for identical content)."""
    for name in common.CONFIG_NAMES:
        data = common.load_config(name, REPO_ROOT / "config")
        if name == "portfolio":
            data = copy.deepcopy(portfolio if portfolio is not None else SAMPLE_PORTFOLIO)
        if name == "committee" and committee:
            committee(data)
        common.write_text(run_dir / "inputs" / f"{name}.yaml",
                          yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def make_ctx(root: Path, *, run_id: str = RUN_ID, as_of: str = AS_OF, mode: dict | None = None,
             committee: Callable[[dict], None] | None = None, portfolio: dict | None = None,
             snapshot: bool = True) -> steps.RunContext:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if snapshot:
        write_snapshots(run_dir, committee=committee, portfolio=portfolio)
    manifest = {"run_id": run_id, "created_at": f"{as_of}T10:00:00+00:00", "as_of": as_of,
                "mode": {"dry_run": False, "quick": False, "sectors": None, "holding": None, **(mode or {})},
                "orders": {}, "warnings": []}
    common.write_json(run_dir / "manifest.json", manifest)
    return steps.RunContext(run_dir, manifest)


def write_phase1(ctx: steps.RunContext, *, expand=("health_care", "industrials"), reduce=("consumer_staples",),
                 extra_prices: dict | None = None) -> None:
    p1 = ctx.p("phase1")
    tickers = {t: {"last_close": fact(100.0, sid=f"D:PX:{t}"), "last_date": AS_OF} for t, *_ in HOLDINGS}
    tickers.update(extra_prices or {})
    common.write_json(p1 / "prices.json", env("prices", [src(f"D:PX:{t}") for t in tickers],
                                              tickers=tickers, failed={}, fx={"usdils": None}))
    common.write_json(p1 / "macro.json", env("macro", [src("D:FRED:DGS10")], series={"DGS10": {}}))
    lk_holdings = [{"ticker": t, "asset_type": at, "weight_pct": fact(w, "pct", f"D:PX:{t}", "other"),
                    "sector": s if at == "stock" else "information_technology", "sector_split": {},
                    "classification_source": "test", "flags": []} for t, at, s, w in HOLDINGS]
    common.write_json(p1 / "lookthrough.json", env(
        "lookthrough", [src("D:YF:QQQ:FUND"), src("D:YF:SPMO:FUND")], total_value_usd=fact(1e5, "USD"),
        holdings=lk_holdings, sector_weights_pct=SECTOR_WEIGHTS, funds={}, overlap={"pairs": [], "direct_overlap": []}))
    common.write_json(p1 / "macro_view.json", {
        "schema_version": "1.0", "agent_role": "macro", "regime": "late_cycle",
        "sector_constraints": [{"sector": "consumer_staples", "min_weight": 5, "max_weight": 20, "rationale": "x"},
                               {"sector": "health_care", "min_weight": 5, "max_weight": 15, "rationale": "y"}]})
    common.write_json(p1 / "cio_allocation.json", {
        "schema_version": "1.0", "agent_role": "cio", "mode": "allocation", "target_sector_weights": TARGETS,
        "expand_sectors": list(expand), "reduce_sectors": list(reduce)})


def set_holdings(ctx: steps.RunContext, only: list[str] | None = None) -> None:
    ctx.manifest["holdings"] = [
        {"ticker": t, "asset_type": at, "sector": s, "sector_class": common.sector_class(s, at),
         "classification_source": "test", "position_pct": w, "price_ticker": t, "flags": [],
         "in_scope": only is None or t in only}
        for t, at, s, w in HOLDINGS]


def write_data(ctx: steps.RunContext, phase: int, ticker: str, *, fund: bool = False, close: float = 100.0) -> None:
    d = ctx.p(f"phase{phase}", "data")
    common.write_json(d / common.data_filename("technicals", ticker),
                      env("technicals", [src(f"D:PX:{ticker}"), src("D:PX:SPY")], ticker=ticker,
                          close=fact(close, sid=f"D:PX:{ticker}")))
    if not fund:
        common.write_json(d / common.data_filename("fundamentals", ticker),
                          env("fundamentals", [src(f"D:SEC:{ticker}:CF")], ticker=ticker))
        common.write_json(d / common.data_filename("valuation", ticker),
                          env("valuation", [src(f"D:VAL:{ticker}")], ticker=ticker))


def rpath(ctx: steps.RunContext, phase: int, role: str, ticker: str, suffix: str = ".json") -> Path:
    return ctx.p(f"phase{phase}", "reports", f"{role}_{ticker}{suffix}")


def write_report(path: Path, role: str, ticker: str, **extra: Any) -> Path:
    doc = {"schema_version": "1.0", "agent_role": role, "ticker": ticker, "stance": "bullish", "conviction": 7,
           "thesis": f"{role} thesis on {ticker}", "risks": [], "red_flags": [],
           "sources": [src(f"D:PX:{ticker}")]}
    doc.update(extra)
    common.write_json(path, doc)
    return path


def write_stub(path: Path, role: str, ticker: str | None) -> Path:
    common.write_json(path, {"order_id": "x", "agent_role": role, "ticker": ticker, "status": "failed",
                             "attempts": 2, "errors": ["schema error A", "schema error B"]})
    return path


def add_order(ctx: steps.RunContext, oid: str, step: str, role: str, resolved: Path | None, *,
              ticker: str | None = None, sector: str | None = None, attempt: int = 1) -> None:
    ctx.manifest.setdefault("orders", {})[oid] = {
        "step": step, "agent_type": common.AGENT_TYPES[role], "agent_role": role, "mode": None,
        "ticker": ticker, "sector": sector, "sector_class": None, "phase": int(step[1]), "attempt": attempt,
        "status": "done", "order_path": "", "output": "", "errors": [],
        "resolved_output": P(resolved) if resolved else None}


class Fakes:
    """Recording stand-ins for the integration API, installed as ``scripts.<module>``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.mp = monkeypatch
        self.calls: dict[str, list[tuple[tuple, dict]]] = {}

    def add(self, module: str, func: str, impl: Callable[..., Any] | None = None) -> None:
        key = f"{module}.{func}"
        name = f"scripts.{module}"
        mod = sys.modules.get(name)
        if mod is None or not getattr(mod, "_is_fake", False):
            mod = types.ModuleType(name)
            mod._is_fake = True  # type: ignore[attr-defined]
            self.mp.setitem(sys.modules, name, mod)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls.setdefault(key, []).append((args, kwargs))
            return impl(*args, **kwargs) if impl else None

        setattr(mod, func, wrapper)

    def __getitem__(self, key: str) -> list[tuple[tuple, dict]]:
        return self.calls.get(key, [])


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return Fakes(monkeypatch)


@pytest.fixture(autouse=True)
def knowledge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    k = tmp_path / "knowledge"
    for s in common.SECTOR_KEYS:
        common.write_text(k / "sectors" / f"{s}.md", f"# {s}\n")
    common.write_text(k / "etf_playbook.md", "# ETF playbook\n")
    monkeypatch.setenv(common.ENV_KNOWLEDGE_DIR, str(k))
    return k


def playbook(sector: str) -> str:
    return P(common.knowledge_dir() / "sectors" / f"{sector}.md")


def etf_playbook() -> str:
    return P(common.knowledge_dir() / "etf_playbook.md")


def by_id(specs: list[dict]) -> dict[str, dict]:
    return {s["order_id"]: s for s in specs}


def in_paths(spec: dict) -> list[str]:
    return [i["path"] for i in spec["inputs"]]


def committee_snapshot(ctx: steps.RunContext) -> dict:
    return common.load_yaml(ctx.p("inputs", "committee.yaml"))


# =====================================================================================
# step sequence / RunContext
# =====================================================================================


def test_step_sequence_full_quick_holding_sectors(tmp_path: Path) -> None:
    full = make_ctx(tmp_path / "a")
    seq = steps.step_sequence(full)
    assert [n for n, _ in seq] == [
        "p1.data", "p1.macro", "p1.cio", "p2.data", "p2.wave_a", "p2.wave_b", "p2.aggregate", "p2.sector_heads",
        "p3.screen", "p3.light", "p3.finalists", "p3.full", "p3.da", "p3.aggregate", "p3.sector_heads",
        "p4.preview", "p4.cio", "p4.finalize"]
    kinds = dict(seq)
    assert [n for n, k in seq if k == "python"] == [
        "p1.data", "p2.data", "p2.aggregate", "p3.screen", "p3.finalists", "p3.aggregate", "p4.preview",
        "p4.finalize"]
    assert kinds["p2.wave_a"] == "agents" and kinds["p4.cio"] == "agents"

    quick = steps.step_sequence(make_ctx(tmp_path / "b", mode={"quick": True}))
    assert [n for n, _ in quick] == [n for n, _ in seq if not n.startswith("p3.")]

    holding = steps.step_sequence(make_ctx(tmp_path / "c", mode={"holding": "ko"}))
    assert [n for n, _ in holding] == ["p1.data", "p1.macro", "p1.cio", "p2.data", "p2.wave_a", "p2.wave_b",
                                       "p2.aggregate", "p2.sector_heads", "p4.finalize"]

    sectors = steps.step_sequence(make_ctx(tmp_path / "d", mode={"sectors": ["financials"]}))
    assert sectors == seq


def test_run_context_snapshot_paths_mode_and_log(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, committee=lambda c: c["phase3"].update(finalists=3),
                   mode={"holding": "brk.b", "sectors": "financials, health_care"})
    assert ctx.run_id == RUN_ID and ctx.as_of == AS_OF
    assert ctx.cfg["phase3"]["finalists"] == 3            # read from inputs/committee.yaml snapshot
    assert ctx.profile["max_position_pct"] == 10
    assert ctx.portfolio["holdings"][0]["ticker"] == "QQQ"
    assert "securities" in ctx.overrides and "region" in ctx.universe
    assert ctx.mode == {"dry_run": False, "quick": False, "sectors": ["financials", "health_care"],
                        "holding": "BRK-B"}
    assert ctx.p("phase1", "prices.json") == ctx.run_dir / "phase1" / "prices.json"
    ctx.log("first line")
    ctx.log("second\nline")
    lines = common.read_text(ctx.p("run_log.md")).splitlines()
    assert len(lines) == 2 and lines[0].startswith("- ") and lines[0].endswith("first line")
    assert lines[1].endswith("second line")

    # no snapshot -> active config dir; legacy string mode
    bare = make_ctx(tmp_path / "bare", snapshot=False)
    bare.manifest["mode"] = "quick"
    assert bare.cfg["phase3"]["finalists"] == common.committee_cfg()["phase3"]["finalists"]
    assert bare.mode["quick"] is True and bare.mode["holding"] is None


def test_unknown_steps_raise(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    with pytest.raises(ValueError):
        steps.run_python_step("p2.wave_a", ctx)
    with pytest.raises(ValueError):
        steps.build_orders("p2.data", ctx)


# =====================================================================================
# p1.data
# =====================================================================================


def test_p1_data_fetches_prices_macro_lookthrough(tmp_path: Path, fakes: Fakes) -> None:
    portfolio = copy.deepcopy(SAMPLE_PORTFOLIO)
    portfolio["holdings"][1].update(ticker="1183441.TA", proxy_ticker="SPMO")
    ctx = make_ctx(tmp_path, portfolio=portfolio)
    prices_doc = {"tickers": {"KO": {}}, "failed": {"XLRE": "no data"}}
    fakes.add("fetch_macro", "run", lambda *a, **k: {"series": {"DGS10": {}, "DFF": {}}})
    fakes.add("fetch_prices", "run", lambda *a, **k: prices_doc)
    fakes.add("etf_lookthrough", "run", lambda *a, **k: {"holdings": [{}] * 6})

    res = steps.run_python_step("p1.data", ctx)

    (args, kw), = fakes["fetch_macro.run"]
    assert args[0] is None and P(args[1]) == P(ctx.p("phase1", "macro.json")) and kw["as_of"] == AS_OF
    (args, kw), = fakes["fetch_prices.run"]
    assert args[0] == ["QQQ", "SPMO", "BRK-B", "KO", "PG", "JPM", "SPY", "XLK", "XLV", "XLF", "XLE", "XLI",
                       "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "ILS=X"]
    assert P(args[1]) == P(ctx.p("phase1", "prices.json"))
    assert kw["as_of"] == AS_OF and kw["period"] == ctx.cfg["prices"]["period"]
    (args, kw), = fakes["etf_lookthrough.run"]
    assert P(args[0]) == P(ctx.p("phase1", "lookthrough.json"))
    assert kw["prices"] is prices_doc and kw["portfolio"]["holdings"][1]["proxy_ticker"] == "SPMO"
    assert res["outputs"] == [P(ctx.p("phase1", n)) for n in ("macro.json", "prices.json", "lookthrough.json")]
    assert res["skip_steps"] == [] and any("XLRE" in w for w in res["warnings"])
    assert "19 price series (1 failed)" in res["summary"]


def _sibling_run(root: Path, run_id: str, as_of: str, *, dry_run: bool = False,
                 portfolio: dict | None = None, snapshot: bool = True) -> Path:
    run_dir = root / "runs" / run_id
    if snapshot:
        write_snapshots(run_dir, portfolio=portfolio)
    p1 = run_dir / "phase1"
    for name in ("prices.json", "macro.json", "lookthrough.json"):
        common.write_json(p1 / name, env(name[:-5], [], marker=run_id))
    common.write_json(p1 / "macro_view.json", {"agent_role": "macro", "attempt": 1})
    common.write_json(p1 / "macro_view.r2.json", {"agent_role": "macro", "attempt": 2, "marker": run_id})
    common.write_json(p1 / "cio_allocation.json", {"agent_role": "cio", "marker": run_id})
    manifest = {"run_id": run_id, "as_of": as_of, "created_at": f"{as_of}T09:00:00+00:00",
                "mode": {"dry_run": dry_run, "quick": False, "sectors": None, "holding": None},
                "orders": {"p1-macro": {"step": "p1.macro", "agent_role": "macro", "attempt": 2,
                                        "resolved_output": P(p1 / "macro_view.r2.json")}}}
    common.write_json(run_dir / "manifest.json", manifest)
    return run_dir


def test_p1_data_holding_reuses_recent_phase1(tmp_path: Path, fakes: Fakes) -> None:
    _sibling_run(tmp_path, "2026-09-25_0900", "2026-09-25")        # 7 days: still eligible but older
    _sibling_run(tmp_path, "2026-09-29_0900", "2026-09-29")        # newest eligible
    _sibling_run(tmp_path, "2026-09-30_0900_dryrun", "2026-09-30", dry_run=True)  # other mode: ignored
    ctx = make_ctx(tmp_path, mode={"holding": "KO"})
    for m in ("fetch_macro", "fetch_prices", "etf_lookthrough"):
        fakes.add(m, "run")

    res = steps.run_python_step("p1.data", ctx)

    assert not fakes.calls                                          # nothing fetched
    assert res["skip_steps"] == ["p1.macro", "p1.cio"]
    reuse = ctx.manifest["phase1_reuse"]
    assert reuse["reused"] is True and reuse["from_run"] == "2026-09-29_0900" and reuse["age_days"] == 3
    assert common.read_json(ctx.p("phase1", "macro_view.json"))["attempt"] == 2   # the resolved retry output
    assert common.read_json(ctx.p("phase1", "prices.json"))["marker"] == "2026-09-29_0900"
    assert sorted(Path(p).name for p in res["outputs"]) == sorted(
        ["prices.json", "macro.json", "lookthrough.json", "macro_view.json", "cio_allocation.json"])
    assert "reused" in common.read_text(ctx.p("run_log.md"))


def test_p1_data_holding_without_recent_run_fetches(tmp_path: Path, fakes: Fakes) -> None:
    _sibling_run(tmp_path, "2026-09-20_0900", "2026-09-20")        # 12 days old: too old
    ctx = make_ctx(tmp_path, mode={"holding": "MSFT"})
    for m in ("fetch_macro", "fetch_prices", "etf_lookthrough"):
        fakes.add(m, "run")
    res = steps.run_python_step("p1.data", ctx)
    assert ctx.manifest["phase1_reuse"]["reused"] is False
    assert res["skip_steps"] == []
    (args, _kw), = fakes["fetch_prices.run"]
    assert "MSFT" in args[0] and args[0].index("MSFT") == 6       # holdings first, then the trial ticker


def test_p1_data_holding_rejects_reuse_when_config_changed(tmp_path: Path, fakes: Fakes) -> None:
    """A holding added to portfolio.yaml since the earlier run has no look-through row there:
    Phase 1 must be recomputed, not copied."""
    old_portfolio = copy.deepcopy(SAMPLE_PORTFOLIO)
    old_portfolio["holdings"] = old_portfolio["holdings"][:-1]
    _sibling_run(tmp_path, "2026-09-29_0900", "2026-09-29", portfolio=old_portfolio)
    _sibling_run(tmp_path, "2026-09-30_0900", "2026-09-30", snapshot=False)   # cannot vouch for its config
    ctx = make_ctx(tmp_path, mode={"holding": "KO"})
    for m in ("fetch_macro", "fetch_prices", "etf_lookthrough"):
        fakes.add(m, "run")

    res = steps.run_python_step("p1.data", ctx)

    reuse = ctx.manifest["phase1_reuse"]
    assert reuse["reused"] is False and res["skip_steps"] == []
    why = {r["run"]: r["reason"] for r in reuse["rejected"]}
    assert "portfolio.yaml" in why["2026-09-29_0900"] and "investor_profile.yaml" not in why["2026-09-29_0900"]
    assert "portfolio.yaml" in why["2026-09-30_0900"] and "committee.yaml" in why["2026-09-30_0900"]
    assert "config changed" in reuse["reason"]
    assert len(fakes["fetch_prices.run"]) == 1 and len(fakes["etf_lookthrough.run"]) == 1
    assert "Phase 1 not reused" in common.read_text(ctx.p("run_log.md"))


def test_p1_data_holding_reuse_uses_manifest_config_sha(tmp_path: Path, fakes: Fakes) -> None:
    import hashlib

    ctx = make_ctx(tmp_path, mode={"holding": "KO"})
    shas = {f"{n}.yaml": hashlib.sha256(ctx.p("inputs", f"{n}.yaml").read_bytes()).hexdigest()
            for n in common.CONFIG_NAMES}
    sib = _sibling_run(tmp_path, "2026-09-29_0900", "2026-09-29", snapshot=False)
    man = common.read_json(sib / "manifest.json")
    common.write_json(sib / "manifest.json", {**man, "config_sha256": shas})
    for m in ("fetch_macro", "fetch_prices", "etf_lookthrough"):
        fakes.add(m, "run")
    steps.run_python_step("p1.data", ctx)
    assert ctx.manifest["phase1_reuse"]["reused"] is True and not fakes.calls


# =====================================================================================
# p2.data
# =====================================================================================


def _data_fakes(fakes: Fakes, close: float = 55.0, fail: set[str] | None = None) -> None:
    fail = fail or set()

    def tech(ticker, out, **kw):
        if ticker in fail:
            raise common.DataUnavailable("no prices")
        return {"close": fact(close, sid=f"D:PX:{ticker}")}

    def fund(ticker, out, **kw):
        if ticker in fail:
            raise common.DataUnavailable("not an SEC filer")
        return {"ticker": ticker}

    fakes.add("compute_technicals", "run", tech)
    fakes.add("fetch_fundamentals", "run", fund)
    fakes.add("valuation", "run", lambda *a, **k: {"cells": []})


def test_p2_data_classifies_and_collects(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    _data_fakes(fakes)

    res = steps.run_python_step("p2.data", ctx)

    hold = {h["ticker"]: h for h in ctx.manifest["holdings"]}
    assert list(hold) == ["QQQ", "SPMO", "BRK-B", "KO", "PG", "JPM"]
    assert hold["QQQ"] | {} == {**hold["QQQ"], "sector": "funds", "sector_class": "fund", "asset_type": "fund"}
    assert hold["JPM"]["sector_class"] == "financials" and hold["KO"]["sector_class"] == "default"
    assert hold["KO"]["position_pct"] == 15.0 and hold["KO"]["classification_source"] == "test"
    assert all(h["in_scope"] for h in hold.values())

    tech = {a[0]: (a, k) for a, k in fakes["compute_technicals.run"]}
    assert list(tech) == ["QQQ", "SPMO", "BRK-B", "KO", "PG", "JPM"]
    assert tech["QQQ"][1]["sector_etf"] is None and tech["SPMO"][1]["sector_etf"] is None
    assert tech["KO"][1]["sector_etf"] == "XLP" and tech["JPM"][1]["sector_etf"] == "XLF"
    assert P(tech["KO"][0][1]) == P(ctx.p("phase2", "data", "technicals_KO.json"))
    assert all(k["as_of"] == AS_OF for _a, k in tech.values())

    fund = {a[0]: (a, k) for a, k in fakes["fetch_fundamentals.run"]}
    assert list(fund) == ["BRK-B", "KO", "PG", "JPM"]                        # funds: technicals only
    assert all(k["price"]["value"] == 100.0 and k["price"]["as_of"] == AS_OF   # the prices.json Fact itself
               for _a, k in fund.values())
    assert fund["BRK-B"][1]["sector_class"] == "financials" and fund["PG"][1]["sector_class"] == "default"
    assert P(fund["KO"][0][1]) == P(ctx.p("phase2", "data", "fundamentals_KO.json"))

    val = {a[0]: (a, k) for a, k in fakes["valuation.run"]}
    assert list(val) == ["BRK-B", "KO", "PG", "JPM"]
    a, k = val["KO"]
    assert P(a[1]) == P(ctx.p("phase2", "data", "fundamentals_KO.json"))
    assert P(a[2]) == P(ctx.p("phase2", "data", "valuation_KO.json"))
    assert k == {"sector": "consumer_staples", "as_of": AS_OF}
    assert len(res["outputs"]) == 6 + 4 + 4 and res["warnings"] == []


def test_p2_data_sectors_filter_keeps_funds(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, mode={"sectors": ["consumer_staples"]})
    write_phase1(ctx)
    _data_fakes(fakes, fail={"PG"})
    res = steps.run_python_step("p2.data", ctx)
    scope = {h["ticker"]: (h["in_scope"], h["scope_reason"]) for h in ctx.manifest["holdings"]}
    assert scope == {"QQQ": (True, None), "SPMO": (True, None), "BRK-B": (False, "outside_sectors_filter"),
                     "KO": (True, None), "PG": (True, None), "JPM": (False, "outside_sectors_filter")}
    assert [a[0] for a, _k in fakes["compute_technicals.run"]] == ["QQQ", "SPMO", "KO", "PG"]
    assert [a[0] for a, _k in fakes["valuation.run"]] == ["KO"]            # PG fundamentals failed
    assert any(w.startswith("PG: fundamentals failed") for w in res["warnings"])
    assert [s["ticker"] for s in steps._p2_subjects(ctx)] == ["QQQ", "SPMO", "KO", "PG"]


def test_p2_data_holding_mode_candidate_ticker(tmp_path: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx(tmp_path, mode={"holding": "MSFT"})
    write_phase1(ctx)
    monkeypatch.setattr(common, "yf_info", lambda t: {"symbol": t, "sector": "Technology"})
    fakes.add("etf_lookthrough", "classify_security",
              lambda t, **k: {"sector": "information_technology", "asset_type": "stock", "sector_class": "default",
                              "classification_source": "yfinance_info", "flags": []})
    _data_fakes(fakes, close=410.0)

    steps.run_python_step("p2.data", ctx)

    (args, kw), = fakes["etf_lookthrough.classify_security"]
    assert args == ("MSFT",) and kw["info"]["sector"] == "Technology" and "securities" in kw["overrides"]
    assert not any(h["in_scope"] for h in ctx.manifest["holdings"])
    trial = ctx.manifest["trial_candidate"]
    assert trial["ticker"] == "MSFT" and trial["sector"] == "information_technology" and trial["position_pct"] == 0.0
    subs = steps._p2_subjects(ctx)
    assert [(s["ticker"], s["kind"]) for s in subs] == [("MSFT", "candidate")]
    (args, kw), = fakes["fetch_fundamentals.run"]
    assert args[0] == "MSFT" and kw["price"]["value"] == 410.0               # technicals close (not in prices.json)
    assert kw["price"]["as_of"] == AS_OF and kw["price"]["unit"] == "USD/share"
    assert fakes["compute_technicals.run"][0][1]["sector_etf"] == "XLK"


def test_p2_data_holding_mode_held_ticker(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, mode={"holding": "KO"})
    write_phase1(ctx)
    _data_fakes(fakes)
    steps.run_python_step("p2.data", ctx)
    assert "trial_candidate" not in ctx.manifest
    assert [(s["ticker"], s["kind"]) for s in steps._p2_subjects(ctx)] == [("KO", "holding")]
    assert [a[0] for a, _k in fakes["compute_technicals.run"]] == ["KO"]


# =====================================================================================
# build_orders
# =====================================================================================


def test_orders_phase1_macro_and_cio(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, mode={"sectors": ["health_care"]})
    write_phase1(ctx)
    (macro,) = steps.build_orders("p1.macro", ctx)
    assert macro["order_id"] == "p1-macro" and macro["agent_type"] == "macro-geopolitics"
    assert macro["mode"] is None and macro["ticker"] is None and macro["phase"] == 1
    assert in_paths(macro) == [P(ctx.p("phase1", "macro.json"))] + [playbook(s) for s in common.SECTOR_KEYS]
    assert P(ctx.p("phase1", "lookthrough.json")) not in in_paths(macro)          # the CIO's input only
    assert macro["output"] == P(ctx.p("phase1", "macro_view.json"))
    assert macro["schema_name"] == "macro_view" and macro["schema"] == P(common.schema_path("macro_view"))
    seeds = {s["id"]: s for s in macro["seed_sources"]}
    assert set(seeds) == {"D:FRED:DGS10"}
    assert seeds["D:FRED:DGS10"]["path"] == P(ctx.p("phase1", "macro.json"))
    wl = ctx.cfg["validation"]["word_limits"]
    assert macro["word_limits"] == {"regime_summary": wl["regime_summary"],
                                    "scenario_description": wl["scenario_description"]}
    assert macro["required_fields"] == []

    # the macro agent needed a retry: the CIO must read the resolved .r2 output
    r2 = ctx.p("phase1", "macro_view.r2.json")
    common.write_json(r2, {"agent_role": "macro"})
    add_order(ctx, "p1-macro", "p1.macro", "macro", r2, attempt=2)
    (cio,) = steps.build_orders("p1.cio", ctx)
    assert cio["order_id"] == "p1-cio" and cio["mode"] == "allocation" and cio["schema_name"] == "cio_allocation"
    assert in_paths(cio) == [P(ctx.p("phase1", "lookthrough.json")), P(r2),
                             P(ctx.p("inputs", "investor_profile.yaml")), P(ctx.p("inputs", "portfolio.yaml"))]
    assert cio["output"] == P(ctx.p("phase1", "cio_allocation.json"))
    max_expand = ctx.cfg["phase3"]["max_expand_sectors"]
    assert any(f"at most {max_expand} sectors" in n for n in cio["notes"])
    assert any("health_care" in n for n in cio["notes"])
    assert cio["word_limits"] == {"cio_rationale": wl["cio_rationale"]}


def test_orders_p1_cio_without_macro_view(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    ctx.p("phase1", "macro_view.json").unlink()
    stub = write_stub(ctx.p("phase1", "macro_view.failed.json"), "macro", None)
    add_order(ctx, "p1-macro", "p1.macro", "macro", stub, attempt=2)
    (cio,) = steps.build_orders("p1.cio", ctx)
    assert P(stub) not in in_paths(cio) and len(cio["inputs"]) == 3
    assert any("macro view is unavailable" in n for n in cio["notes"])


def test_orders_wave_a_roles_inputs_and_fields(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx)
    for t, at, _s, _w in HOLDINGS:
        write_data(ctx, 2, t, fund=at == "fund")
    specs = steps.build_orders("p2.wave_a", ctx)
    ids = by_id(specs)
    assert len(specs) == 4 * 4 + 2 * 2
    assert set(ids) == {f"p2-a-{r}-{t}" for t in ("BRK-B", "KO", "PG", "JPM")
                        for r in ("fundamental", "technical", "osint", "operator_ceo")} | {
        f"p2-a-{r}-{t}" for t in ("QQQ", "SPMO") for r in ("etf", "technical")}
    d = lambda kind, t: P(ctx.p("phase2", "data", f"{kind}_{t}.json"))  # noqa: E731

    tech = ids["p2-a-technical-KO"]
    assert in_paths(tech) == [d("technicals", "KO")]                     # ONLY its technicals file
    assert tech["agent_type"] == "technical-analyst" and tech["step"] == "p2.wave_a" and tech["phase"] == 2
    assert tech["output"] == P(ctx.p("phase2", "reports", "technical_KO.json"))
    assert {s["id"] for s in tech["seed_sources"]} == {"D:PX:KO", "D:PX:SPY"}
    req = ctx.cfg["evidence"]["required_fields"]
    assert tech["required_fields"] == req["technical"]["default"]
    assert "technical_setup" in tech["word_limits"] and tech["schema_name"] == "specialist_report"

    fun = ids["p2-a-fundamental-KO"]
    assert in_paths(fun) == [d("fundamentals", "KO"), d("valuation", "KO"), playbook("consumer_staples")]
    assert {s["id"] for s in fun["seed_sources"]} == {"D:SEC:KO:CF", "D:VAL:KO"}
    assert fun["required_fields"] == req["fundamental"]["default"]
    assert ids["p2-a-fundamental-JPM"]["required_fields"] == req["fundamental"]["financials"]
    assert ids["p2-a-fundamental-JPM"]["sector_class"] == "financials"
    assert in_paths(ids["p2-a-fundamental-BRK-B"])[-1] == playbook("financials")

    for role in ("osint", "operator_ceo"):
        spec = ids[f"p2-a-{role}-PG"]
        assert in_paths(spec) == [d("fundamentals", "PG"), playbook("consumer_staples")]
        assert spec["required_fields"] == req[role]["default"]
        assert spec["seed_sources"] and spec["seed_sources"][0]["id"] == "D:SEC:PG:CF"
    assert "extension_assessment" in ids["p2-a-operator_ceo-PG"]["word_limits"]

    etf = ids["p2-a-etf-QQQ"]
    assert in_paths(etf) == [P(ctx.p("phase1", "lookthrough.json")), d("technicals", "QQQ"),
                             P(ctx.p("phase1", "prices.json")), P(ctx.p("inputs", "investor_profile.yaml")),
                             etf_playbook()]
    assert etf["agent_type"] == "etf-analyst" and etf["schema_name"] == "etf_review"
    assert etf["sector"] == "funds" and etf["sector_class"] == "fund"
    assert etf["required_fields"] == req["etf"]["fund"]
    assert {"D:YF:QQQ:FUND", "D:PX:QQQ", "D:PX:SPY"} <= {s["id"] for s in etf["seed_sources"]}
    ftech = ids["p2-a-technical-SPMO"]
    assert in_paths(ftech) == [d("technicals", "SPMO")]
    assert ftech["required_fields"] == req["technical"]["fund"]
    for spec in specs:
        assert all(Path(i["path"]).is_absolute() and "\\" not in i["path"] for i in spec["inputs"])
        assert spec["seed_sources"], spec["order_id"]


def test_orders_wave_a_quick_mode_roles(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, mode={"quick": True})
    write_phase1(ctx)
    set_holdings(ctx)
    for t, at, _s, _w in HOLDINGS:
        write_data(ctx, 2, t, fund=at == "fund")
    specs = steps.build_orders("p2.wave_a", ctx)
    assert sorted(s["order_id"] for s in specs) == sorted(
        [f"p2-a-{r}-{t}" for t in ("BRK-B", "KO", "PG", "JPM") for r in ("fundamental", "technical")]
        + ["p2-a-etf-QQQ", "p2-a-etf-SPMO"])


def test_orders_wave_b_uses_resolved_reports(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx)
    write_data(ctx, 2, "KO")
    fun = write_report(rpath(ctx, 2, "fundamental", "KO"), "fundamental", "KO")
    write_report(rpath(ctx, 2, "technical", "KO"), "technical", "KO")             # invalid attempt 1
    tech_r2 = write_report(rpath(ctx, 2, "technical", "KO", ".r2.json"), "technical", "KO")
    osint_stub = write_stub(rpath(ctx, 2, "osint", "KO", ".failed.json"), "osint", "KO")
    op = write_report(rpath(ctx, 2, "operator_ceo", "KO"), "operator_ceo", "KO")  # not in manifest: disk
    add_order(ctx, "p2-a-fundamental-KO", "p2.wave_a", "fundamental", fun, ticker="KO")
    add_order(ctx, "p2-a-technical-KO", "p2.wave_a", "technical", tech_r2, ticker="KO", attempt=2)
    add_order(ctx, "p2-a-osint-KO", "p2.wave_a", "osint", osint_stub, ticker="KO", attempt=2)

    specs = by_id(steps.build_orders("p2.wave_b", ctx))
    assert set(specs) == {f"p2-b-devils_advocate-{t}" for t in ("BRK-B", "KO", "PG", "JPM")}   # funds: no DA
    da = specs["p2-b-devils_advocate-KO"]
    d = lambda kind: P(ctx.p("phase2", "data", f"{kind}_KO.json"))  # noqa: E731
    assert in_paths(da) == [P(fun), P(tech_r2), P(op), d("technicals"), d("fundamentals"), d("valuation"),
                            playbook("consumer_staples")]
    assert da["agent_type"] == "devils-advocate" and da["schema_name"] == "devils_advocate_report"
    assert da["output"] == P(ctx.p("phase2", "reports", "devils_advocate_KO.json"))
    assert da["required_fields"] == ctx.cfg["evidence"]["required_fields"]["devils_advocate"]["default"]
    assert any("osint" in n for n in da["notes"])
    assert {"D:PX:KO", "D:SEC:KO:CF", "D:VAL:KO"} <= {s["id"] for s in da["seed_sources"]}


def test_orders_wave_b_funds_when_configured(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, committee=lambda c: c["roles"].update(da_for_funds=True))
    write_phase1(ctx)
    set_holdings(ctx, only=["QQQ"])
    write_data(ctx, 2, "QQQ", fund=True)
    (da,) = steps.build_orders("p2.wave_b", ctx)
    assert da["order_id"] == "p2-b-devils_advocate-QQQ"
    assert in_paths(da) == [P(ctx.p("phase2", "data", "technicals_QQQ.json")),
                            P(ctx.p("phase1", "lookthrough.json")), etf_playbook()]


def test_orders_p2_sector_heads(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx)
    ko_f = write_report(rpath(ctx, 2, "fundamental", "KO"), "fundamental", "KO")
    ko_da = write_report(rpath(ctx, 2, "devils_advocate", "KO"), "devils_advocate", "KO")
    write_stub(rpath(ctx, 2, "technical", "PG", ".failed.json"), "technical", "PG")
    qqq_etf = write_report(rpath(ctx, 2, "etf", "QQQ"), "etf", "QQQ")
    packet = ctx.p("phase2", "packets", "sector_consumer_staples.json")
    common.write_json(packet, env("sector_packet", [src("D:PX:KO")], tickers=[]))
    for other in ("financials", "funds"):
        common.write_json(ctx.p("phase2", "packets", f"sector_{other}.json"), env("sector_packet", [], tickers=[]))

    specs = steps.build_orders("p2.sector_heads", ctx)
    assert [s["order_id"] for s in specs] == ["p2-sh-financials", "p2-sh-consumer_staples", "p2-sh-funds"]
    cs = by_id(specs)["p2-sh-consumer_staples"]
    assert cs["agent_type"] == "sector-head" and cs["mode"] == "phase2" and cs["phase"] == 2
    assert cs["schema_name"] == "sector_verdict" and cs["ticker"] is None
    assert in_paths(cs) == [P(packet), P(ko_f), P(ko_da), playbook("consumer_staples")]
    assert cs["output"] == P(ctx.p("phase2", "consumer_staples_verdict.json"))
    assert [s["id"] for s in cs["seed_sources"]] == ["D:PX:KO"]
    assert any("technical:PG" in n for n in cs["notes"])
    wl = ctx.cfg["validation"]["word_limits"]
    assert cs["word_limits"]["sector_rationale"] == wl["sector_rationale"]
    funds = by_id(specs)["p2-sh-funds"]
    assert in_paths(funds) == [P(ctx.p("phase2", "packets", "sector_funds.json")), P(qqq_etf), etf_playbook()]
    assert funds["sector"] == "funds" and funds["sector_class"] == "fund"


def test_orders_phase3_and_p4_cio(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["KO", "PG"])
    ctx.manifest["candidates"] = {"health_care": ["MRK", "ABBV"]}
    ctx.manifest["finalists"] = {"health_care": ["MRK"]}
    write_data(ctx, 3, "MRK")
    # ABBV kept its fundamentals but its technicals and valuation failed in p3.screen
    common.write_json(ctx.p("phase3", "data", "fundamentals_ABBV.json"),
                      env("fundamentals", [src("D:SEC:ABBV:CF")], ticker="ABBV"))

    light = by_id(steps.build_orders("p3.light", ctx))
    assert set(light) == {"p3-light-fundamental-MRK", "p3-light-technical-MRK",
                          "p3-light-fundamental-ABBV"}                    # no technical order without technicals
    assert "p3-light-technical-ABBV" not in light
    assert "technical:ABBV (no technicals)" in common.read_text(ctx.p("run_log.md"))
    d3 = lambda kind, t: P(ctx.p("phase3", "data", f"{kind}_{t}.json"))  # noqa: E731
    abbv = light["p3-light-fundamental-ABBV"]
    assert in_paths(abbv) == [d3("fundamentals", "ABBV"), playbook("health_care")]   # missing file not listed
    assert any("valuation_ABBV.json" in n and "missing_data" in n for n in abbv["notes"])
    assert "valuation" not in steps.validation_context(ctx, abbv)
    assert in_paths(light["p3-light-technical-MRK"]) == [d3("technicals", "MRK")]
    assert in_paths(light["p3-light-fundamental-MRK"]) == [d3("fundamentals", "MRK"), d3("valuation", "MRK"),
                                                           playbook("health_care")]
    assert light["p3-light-fundamental-MRK"]["output"] == P(ctx.p("phase3", "reports", "fundamental_MRK.json"))
    assert light["p3-light-technical-MRK"]["step"] == "p3.light" and light["p3-light-technical-MRK"]["phase"] == 3

    full = by_id(steps.build_orders("p3.full", ctx))
    assert set(full) == {"p3-full-operator_ceo-MRK", "p3-full-industry_ops-MRK", "p3-full-osint-MRK"}
    ind = full["p3-full-industry_ops-MRK"]
    assert in_paths(ind) == [d3("fundamentals", "MRK"), playbook("health_care")]
    assert ind["required_fields"] == ctx.cfg["evidence"]["required_fields"]["industry_ops"]["default"]

    f = write_report(rpath(ctx, 3, "fundamental", "MRK"), "fundamental", "MRK")
    t = write_report(rpath(ctx, 3, "technical", "MRK"), "technical", "MRK")
    i = write_report(rpath(ctx, 3, "industry_ops", "MRK"), "industry_ops", "MRK")
    (da,) = steps.build_orders("p3.da", ctx)
    assert da["order_id"] == "p3-da-MRK" and da["step"] == "p3.da"
    assert in_paths(da) == [P(f), P(t), P(i), d3("technicals", "MRK"), d3("fundamentals", "MRK"),
                            d3("valuation", "MRK"), playbook("health_care")]
    assert da["output"] == P(ctx.p("phase3", "reports", "devils_advocate_MRK.json"))
    assert any("operator_ceo" in n and "osint" in n for n in da["notes"])

    common.write_json(ctx.p("phase3", "packets", "sector_health_care.json"), env("sector_packet", [], tickers=[]))
    (sh,) = steps.build_orders("p3.sector_heads", ctx)
    assert sh["order_id"] == "p3-sh-health_care" and sh["mode"] == "phase3" and sh["phase"] == 3
    assert in_paths(sh)[0] == P(ctx.p("phase3", "packets", "sector_health_care.json"))
    assert in_paths(sh)[-1] == playbook("health_care")
    assert sh["output"] == P(ctx.p("phase3", "health_care_verdict.json"))
    assert any(f"at most {ctx.cfg['phase3']['max_buys_per_sector']} BUY" in n for n in sh["notes"])

    v2 = ctx.p("phase2", "consumer_staples_verdict.json")
    common.write_json(v2, {"agent_role": "sector_head", "verdicts": []})
    v3 = ctx.p("phase3", "health_care_verdict.r2.json")
    common.write_json(v3, {"agent_role": "sector_head", "verdicts": []})
    add_order(ctx, "p3-sh-health_care", "p3.sector_heads", "sector_head", v3, sector="health_care", attempt=2)
    (no_preview,) = steps.build_orders("p4.cio", ctx)
    assert P(ctx.p("phase4", "portfolio_preview.json")) not in in_paths(no_preview)
    assert any("preview rebalancing plan is unavailable" in n for n in no_preview["notes"])
    common.write_json(ctx.p("phase4", "portfolio_preview.json"), env("portfolio_plan", [src("D:PX:KO")]))
    common.write_json(ctx.p("phase4", "cio_packet.json"), env("cio_packet", [src("D:FRED:DGS10")]))
    (cio,) = steps.build_orders("p4.cio", ctx)
    assert cio["order_id"] == "p4-cio" and cio["mode"] == "reconciliation" and cio["schema_name"] == "cio_final"
    assert in_paths(cio) == [P(ctx.p("phase4", "cio_packet.json")), P(v2), P(v3),
                             P(ctx.p("phase4", "portfolio_preview.json")),
                             P(ctx.p("phase1", "cio_allocation.json")), P(ctx.p("phase1", "macro_view.json")),
                             P(ctx.p("inputs", "investor_profile.yaml"))]  # the final judgment sees the profile
    assert cio["output"] == P(ctx.p("phase4", "cio_final.json")) and cio["phase"] == 4
    assert {"D:FRED:DGS10", "D:PX:KO"} <= {s["id"] for s in cio["seed_sources"]}
    assert steps.describe_orders("p4.cio", [cio]) == "p4.cio: 1 orders (cio)"
    wl = ctx.cfg["validation"]["word_limits"]
    assert cio["word_limits"] == {"executive_summary_item": wl["executive_summary_item"],
                                  "dissent_summary": wl["dissent_summary"]}


def test_validation_context_maps_inputs(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["KO"])
    write_data(ctx, 2, "KO")
    write_report(rpath(ctx, 2, "fundamental", "KO"), "fundamental", "KO")
    (da,) = steps.build_orders("p2.wave_b", ctx)
    vc = steps.validation_context(ctx, da)
    extra = {"strict_refs", "word_limits", "seed_sources", "expect", "run_dir"}
    assert {k: v for k, v in vc.items() if k not in extra} == {
        "technicals": P(ctx.p("phase2", "data", "technicals_KO.json")),
        "fundamentals": P(ctx.p("phase2", "data", "fundamentals_KO.json")),
        "valuation": P(ctx.p("phase2", "data", "valuation_KO.json")),
        "committee_cfg": P(ctx.p("inputs", "committee.yaml"))}
    assert vc["strict_refs"] is True and vc["run_dir"] == P(ctx.run_dir)
    assert vc["seed_sources"] == da["seed_sources"] and vc["word_limits"] == da["word_limits"]
    assert vc["expect"] == {"order_id": "p2-b-devils_advocate-KO", "run_id": RUN_ID, "agent_role": "devils_advocate",
                            "ticker": "KO", "sector": "consumer_staples", "mode": None}

    (macro,) = steps.build_orders("p1.macro", ctx)
    assert set(steps.validation_context(ctx, macro)) - extra == {"macro", "committee_cfg"}

    # the order's own ticker wins over another ticker's data file; prices.json is mapped too
    spec = {"agent_role": "etf", "ticker": "QQQ", "inputs": [
        {"path": P(ctx.p("phase2", "data", "technicals_SPMO.json"))},
        {"path": P(ctx.p("phase2", "data", "technicals_QQQ.json"))},
        {"path": P(ctx.p("phase1", "prices.json"))}]}
    vc = steps.validation_context(ctx, spec)
    assert vc["technicals"].endswith("technicals_QQQ.json") and vc["prices"].endswith("prices.json")

    (cs,) = [s for s in steps.build_orders("p2.sector_heads", ctx) if s["sector"] == "consumer_staples"]
    assert steps.validation_context(ctx, cs)["aggregate"] == P(ctx.p("phase2", "aggregate.json"))

    spec = {"agent_role": "cio", "mode": "reconciliation", "phase": 4, "inputs": [
        {"path": P(ctx.p("phase2", "financials_verdict.r2.json")), "purpose": ""},
        {"path": P(ctx.p("phase3", "energy_verdict.json")), "purpose": ""},
        {"path": P(ctx.p("phase2", "funds_verdict.failed.json")), "purpose": ""},
        {"path": P(ctx.p("phase1", "macro_view.r2.json")), "purpose": ""},
        {"path": P(ctx.p("phase1", "cio_allocation.json")), "purpose": ""}]}
    vc = steps.validation_context(ctx, spec)
    assert vc["verdicts"] == [P(ctx.p("phase2", "financials_verdict.r2.json")),
                              P(ctx.p("phase3", "energy_verdict.json"))]
    assert vc["macro_view"].endswith("macro_view.r2.json") and vc["cio_allocation"].endswith("cio_allocation.json")
    assert "aggregate" not in vc


# =====================================================================================
# p2.aggregate (+ packets)
# =====================================================================================


def _score_fake(items, out=None, *, run_date, cfg=None):
    reports = {i["path"]: {"agent_role": i["role"], "ticker": i["ticker"],
                           "status": "failed" if i["path"].endswith(".failed.json") else "ok",
                           "score": 1 if i["path"].endswith(".failed.json") else 7, "start": 10, "floor": 1,
                           "deductions": []} for i in items}
    payload = {"schema": "evidence/v1", "reports": reports}
    if out:
        common.write_json(out, payload)
    return payload


def _agg_fake(values: dict[str, tuple[float | None, float | None]] | None = None):
    def run(items, out=None, *, evidence, cfg=None, as_of=None):
        tickers = {}
        for it in items:
            conv, ev = (values or {}).get(it["ticker"], (6.0, 7.0))
            tickers[it["ticker"]] = {"kind": it["kind"], "sector": it["sector"], "agg_conviction": conv,
                                     "agg_evidence": ev, "rule_action": "HOLD" if it["kind"] == "holding" else "PASS",
                                     "rr": {"rr": 1.2}}
        payload = {"schema": "aggregate/v1", "tickers": tickers}
        if out:
            common.write_json(out, payload)
        return payload
    return run


def test_p2_aggregate_items_and_packets(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["KO", "QQQ"])
    write_data(ctx, 2, "KO")
    write_data(ctx, 2, "QQQ", fund=True)
    risks = [{"point": "low risk", "severity": "low", "evidence_refs": ["W1"]},
             {"point": "big risk", "severity": "high", "evidence_refs": ["W1"]},
             {"point": "mid risk", "severity": "medium", "evidence_refs": ["W1"]}]
    flags = [{"id": "F1", "flag": "debt", "severity": "high", "must_address": True, "source_ids": ["W1"]}]
    fun = write_report(rpath(ctx, 2, "fundamental", "KO"), "fundamental", "KO", risks=risks, red_flags=flags,
                       extensions={"valuation": {"fair_value_low": 50, "fair_value_base": 60, "fair_value_high": 70}})
    tech = write_report(rpath(ctx, 2, "technical", "KO"), "technical", "KO", stance="neutral", conviction=5)
    osint = write_stub(rpath(ctx, 2, "osint", "KO", ".failed.json"), "osint", "KO")
    op = write_report(rpath(ctx, 2, "operator_ceo", "KO"), "operator_ceo", "KO")
    attacks = [{"target_claim": f"claim {i}", "target_agent": "fundamental", "counter_evidence": "c",
                "source_ids": ["W1"], "severity": sev} for i, sev in enumerate(["low", "high", "medium", "high"])]
    da = write_report(rpath(ctx, 2, "devils_advocate", "KO"), "devils_advocate", "KO", attacks=attacks,
                      verdict_on_thesis="damaged", verdict_reason="r", bear_case_price=48.0,
                      kill_criteria=["k1", "k2", "k3", "k4"])
    etf = write_report(rpath(ctx, 2, "etf", "QQQ"), "etf", "QQQ", action="HOLD", role_in_portfolio="core")
    qtech = write_report(rpath(ctx, 2, "technical", "QQQ"), "technical", "QQQ")
    add_order(ctx, "p2-a-osint-KO", "p2.wave_a", "osint", osint, ticker="KO", attempt=2)
    fakes.add("evidence_score", "score_reports", _score_fake)
    fakes.add("aggregate", "run", _agg_fake())

    res = steps.run_python_step("p2.aggregate", ctx)

    (args, kw), = fakes["evidence_score.score_reports"]
    items = args[0]
    assert P(args[1]) == P(ctx.p("phase2", "evidence.json")) and kw["run_date"] == AS_OF and kw["cfg"] == ctx.cfg
    assert [(i["ticker"], i["role"], i["sector_class"]) for i in items] == [
        ("QQQ", "etf", "fund"), ("QQQ", "technical", "fund"),
        ("KO", "fundamental", "default"), ("KO", "technical", "default"), ("KO", "osint", "default"),
        ("KO", "operator_ceo", "default"), ("KO", "devils_advocate", "default")]
    assert [i["path"] for i in items] == [P(p) for p in (etf, qtech, fun, tech, osint, op, da)]

    (args, kw), = fakes["aggregate.run"]
    assert P(args[1]) == P(ctx.p("phase2", "aggregate.json")) and kw["as_of"] == AS_OF
    assert kw["evidence"]["reports"][P(fun)]["score"] == 7
    agg_items = {i["ticker"]: i for i in args[0]}
    ko, qqq = agg_items["KO"], agg_items["QQQ"]
    assert ko["kind"] == "holding" and ko["asset_type"] == "stock" and ko["sector"] == "consumer_staples"
    assert ko["sector_class"] == "default" and ko["price"]["value"] == 100.0 and ko["price"]["source_id"] == "D:PX:KO"
    assert ko["reports"] == {"fundamental": P(fun), "technical": P(tech), "osint": P(osint), "operator_ceo": P(op)}
    assert ko["da_path"] == P(da)
    assert ko["technicals_path"] == P(ctx.p("phase2", "data", "technicals_KO.json"))
    assert ko["context"] == {"position_pct": 15.0, "cap_pct": 10.0, "sector_lookthrough_pct": 25.0,
                             "cio_target_pct": 20.0, "max_sector_pct": 35.0}
    assert qqq["reports"] == {"etf": P(etf), "technical": P(qtech)} and qqq["da_path"] is None
    assert qqq["context"] == {"position_pct": 30.0, "cap_pct": 40.0, "sector_lookthrough_pct": None,
                              "cio_target_pct": None, "max_sector_pct": 35.0}
    assert qqq["sector"] == "funds" and qqq["asset_type"] == "fund"

    packet = common.read_json(ctx.p("phase2", "packets", "sector_consumer_staples.json"))
    assert packet["schema"] == "sector_packet/v1" and packet["sector"] == "consumer_staples" and packet["phase"] == 2
    assert {"D:PX:KO", "D:SEC:KO:CF", "D:VAL:KO"} <= {s["id"] for s in packet["sources"]}
    assert packet["context"]["sector_lookthrough_pct"] == 25.0 and packet["context"]["cio_target_pct"] == 20.0
    assert packet["context"]["macro_bounds"] == {"min_weight": 5, "max_weight": 20}
    (entry,) = packet["tickers"]
    assert entry["ticker"] == "KO" and entry["aggregate"]["rule_action"] == "HOLD"
    reps = {r["role"]: r for r in entry["reports"]}
    assert list(reps) == ["fundamental", "technical", "osint", "operator_ceo"]
    f = reps["fundamental"]
    assert {k: f[k] for k in ("role", "path", "stance", "conviction", "evidence", "thesis")} == {
        "role": "fundamental", "path": P(fun), "stance": "bullish", "conviction": 7, "evidence": 7,
        "thesis": "fundamental thesis on KO"}
    assert [r["severity"] for r in f["top_risks"]] == ["high", "medium", "low"]
    assert f["red_flags"] == [{"flag_id": "fundamental:F1", "flag": "debt", "severity": "high", "must_address": True}]
    assert f["fair_value"] == {"low": 50, "base": 60, "high": 70}
    assert reps["osint"]["status"] == "failed" and reps["osint"]["evidence"] == 1 and reps["osint"]["errors"]
    assert reps["technical"]["stance"] == "neutral"
    dad = entry["devils_advocate"]
    assert dad["verdict"] == "damaged" and dad["bear_case_price"] == 48.0 and dad["evidence"] == 7
    assert [a["severity"] for a in dad["top_attacks"]] == ["high", "high", "medium"]
    assert len(dad["kill_criteria"]) == 3

    funds = common.read_json(ctx.p("phase2", "packets", "sector_funds.json"))
    (fentry,) = funds["tickers"]
    assert fentry["devils_advocate"] is None
    assert {r["role"]: r.get("action") for r in fentry["reports"]} == {"etf": "HOLD", "technical": None}
    assert "D:YF:QQQ:FUND" in {s["id"] for s in funds["sources"]}
    assert res["outputs"][:2] == [P(ctx.p("phase2", "evidence.json")), P(ctx.p("phase2", "aggregate.json"))]
    assert "HOLD 2" in res["summary"]


# =====================================================================================
# Phase 3 python steps
# =====================================================================================


def test_p3_screen_caps_expand_sectors_and_collects_data(tmp_path: Path, fakes: Fakes) -> None:
    enabled = [s for s in common.SECTOR_KEYS if s != "energy"]
    ctx = make_ctx(tmp_path, committee=lambda c: c.update(enabled_sectors=enabled))
    write_phase1(ctx, expand=("health_care", "energy", "industrials", "utilities", "health_care"))
    set_holdings(ctx)
    shortlists = {"health_care": ["MRK", "abbv", "KO"], "industrials": ["GE"]}
    fakes.add("screen", "run", lambda sector, out, **kw: {"sector": sector, "shortlist": shortlists[sector]})
    fakes.add("screen", "recent_shortlisted", lambda sector, runs_root, **kw: [])
    _data_fakes(fakes, close=50.0, fail={"ABBV"})

    res = steps.run_python_step("p3.screen", ctx)

    assert ctx.manifest["expand_sectors"] == ["health_care", "industrials"]
    assert ctx.manifest["expand_dropped"] == [{"sector": "energy", "reason": "not_enabled"},
                                              {"sector": "utilities", "reason": "max_expand_sectors=2"}]
    log = common.read_text(ctx.p("run_log.md"))
    assert "expand sector utilities dropped (max_expand_sectors=2)" in log
    calls = fakes["screen.run"]
    assert [a[0] for a, _k in calls] == ["health_care", "industrials"]
    assert P(calls[0][0][1]) == P(ctx.p("phase3", "screen_health_care.json"))
    held = calls[0][1]["held"]
    assert {"QQQ", "SPMO", "BRK-B", "KO", "PG", "JPM"} <= set(held) and calls[0][1]["as_of"] == AS_OF

    tech = {a[0]: (a, k) for a, k in fakes["compute_technicals.run"]}
    assert list(tech) == ["MRK", "ABBV", "GE"]                               # KO is held: never screened
    assert tech["MRK"][1]["sector_etf"] == "XLV" and tech["GE"][1]["sector_etf"] == "XLI"
    assert P(tech["MRK"][0][1]) == P(ctx.p("phase3", "data", "technicals_MRK.json"))
    fund = {a[0]: k for a, k in fakes["fetch_fundamentals.run"]}
    assert fund["MRK"]["price"]["value"] == 50.0 and fund["MRK"]["sector_class"] == "default"
    val = fakes["valuation.run"]
    assert [a[0] for a, _k in val] == ["MRK", "GE"] and val[1][1]["sector"] == "industrials"
    assert ctx.manifest["candidates"] == {"health_care": ["MRK"], "industrials": ["GE"]}   # ABBV: no data
    assert any("ABBV" in w and "dropped" in w for w in res["warnings"])
    assert res["skip_steps"] == []


def test_p3_screen_sectors_filter_and_empty_expand(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, mode={"sectors": ["industrials"]})
    write_phase1(ctx, expand=("health_care", "industrials"))
    fakes.add("screen", "run", lambda sector, out, **kw: {"shortlist": []})
    fakes.add("screen", "recent_shortlisted", lambda sector, runs_root, **kw: [])
    res = steps.run_python_step("p3.screen", ctx)
    assert ctx.manifest["expand_sectors"] == ["industrials"]
    assert ctx.manifest["expand_dropped"] == [{"sector": "health_care", "reason": "outside_sectors_filter"}]
    assert ctx.manifest["candidates"] == {"industrials": []}
    assert res["skip_steps"] == P3_AFTER_SCREEN

    ctx2 = make_ctx(tmp_path / "two")
    write_phase1(ctx2, expand=())
    res2 = steps.run_python_step("p3.screen", ctx2)
    assert ctx2.manifest["expand_sectors"] == [] and ctx2.manifest["candidates"] == {}
    assert res2["skip_steps"] == P3_AFTER_SCREEN


def test_select_finalists_ranking_and_ties() -> None:
    agg = {"tickers": {"AAA": {"agg_conviction": 7.5, "agg_evidence": 6.0},
                       "BBB": {"agg_conviction": 7.5, "agg_evidence": 7.0},
                       "CCC": {"agg_conviction": 7.5, "agg_evidence": 7.0},
                       "DDD": {"agg_conviction": 8.0, "agg_evidence": 5.0},
                       "EEE": {"agg_conviction": None, "agg_evidence": 9.0},
                       "FFF": {"agg_conviction": 6.0, "agg_evidence": None},
                       "GGG": {"agg_conviction": 6.0, "agg_evidence": 2.0}}}
    cands = {"health_care": ["FFF", "EEE", "CCC", "BBB", "AAA", "DDD", "GGG"], "energy": []}
    assert steps.select_finalists(cands, agg, 4) == {"health_care": ["DDD", "BBB", "CCC", "AAA"], "energy": []}
    assert steps.select_finalists(cands, agg, 7)["health_care"] == ["DDD", "BBB", "CCC", "AAA", "GGG", "FFF"]


def test_p3_finalists_step(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, committee=lambda c: c["phase3"].update(finalists=2))
    write_phase1(ctx)
    set_holdings(ctx)
    ctx.manifest["candidates"] = {"health_care": ["MRK", "ABBV", "LLY"], "industrials": ["GE"]}
    for t in ("MRK", "ABBV", "LLY", "GE"):
        write_data(ctx, 3, t, close=80.0)
        write_report(rpath(ctx, 3, "fundamental", t), "fundamental", t)
        write_report(rpath(ctx, 3, "technical", t), "technical", t)
    fakes.add("evidence_score", "score_reports", _score_fake)
    fakes.add("aggregate", "run", _agg_fake({"MRK": (7.0, 8.0), "ABBV": (7.0, 9.0), "LLY": (7.5, 5.0),
                                             "GE": (None, 3.0)}))

    res = steps.run_python_step("p3.finalists", ctx)

    assert ctx.manifest["finalists"] == {"health_care": ["LLY", "ABBV"], "industrials": []}
    (args, kw), = fakes["evidence_score.score_reports"]
    assert {i["role"] for i in args[0]} == {"fundamental", "technical"} and len(args[0]) == 8
    assert P(args[1]) == P(ctx.p("phase3", "evidence_light.json"))
    (args, kw), = fakes["aggregate.run"]
    assert P(args[1]) == P(ctx.p("phase3", "aggregate_light.json"))
    mrk = {i["ticker"]: i for i in args[0]}["MRK"]
    assert mrk["kind"] == "candidate" and mrk["da_path"] is None and mrk["price"]["value"] == 80.0
    assert mrk["context"]["position_pct"] == 0.0 and mrk["context"]["cap_pct"] == 10.0
    assert mrk["context"]["sector_lookthrough_pct"] == 6.0 and mrk["context"]["cio_target_pct"] == 10.0
    assert set(mrk["reports"]) == {"fundamental", "technical"}
    assert not ctx.p("phase3", "packets").exists()
    assert res["skip_steps"] == []
    assert "health_care: LLY, ABBV" in res["summary"]
    assert '"health_care": ["LLY", "ABBV"]' in common.read_text(ctx.p("run_log.md"))

    fakes.add("aggregate", "run", _agg_fake({t: (None, None) for t in ("MRK", "ABBV", "LLY", "GE")}))
    res2 = steps.run_python_step("p3.finalists", ctx)
    assert res2["skip_steps"] == ["p3.full", "p3.da", "p3.aggregate", "p3.sector_heads"]


def test_p3_aggregate_items_and_packet(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx)
    ctx.manifest["candidates"] = {"health_care": ["MRK", "ABBV"]}
    ctx.manifest["finalists"] = {"health_care": ["MRK"]}
    write_data(ctx, 3, "MRK")
    paths = {r: write_report(rpath(ctx, 3, r, "MRK"), r, "MRK")
             for r in ("fundamental", "technical", "operator_ceo", "industry_ops", "osint")}
    da = write_report(rpath(ctx, 3, "devils_advocate", "MRK"), "devils_advocate", "MRK",
                      verdict_on_thesis="survives", attacks=[])
    fakes.add("evidence_score", "score_reports", _score_fake)
    fakes.add("aggregate", "run", _agg_fake())

    res = steps.run_python_step("p3.aggregate", ctx)

    (args, _kw), = fakes["evidence_score.score_reports"]
    assert len(args[0]) == 6 and P(args[1]) == P(ctx.p("phase3", "evidence.json"))
    (args, _kw), = fakes["aggregate.run"]
    (item,) = args[0]
    assert item["ticker"] == "MRK" and item["kind"] == "candidate" and item["da_path"] == P(da)
    assert item["reports"] == {r: P(p) for r, p in paths.items()}
    assert P(args[1]) == P(ctx.p("phase3", "aggregate.json"))
    packet = common.read_json(ctx.p("phase3", "packets", "sector_health_care.json"))
    assert packet["phase"] == 3 and packet["context"]["max_buys_per_sector"] == ctx.cfg["phase3"]["max_buys_per_sector"]
    assert packet["tickers"][0]["devils_advocate"]["verdict"] == "survives"
    assert len(packet["tickers"][0]["reports"]) == 5
    assert P(ctx.p("phase3", "packets", "sector_health_care.json")) in res["outputs"]


# =====================================================================================
# Phase 4
# =====================================================================================


def _verdict(sector: str, phase: int, rows: list[tuple[str, str, str]]) -> dict:
    return {"schema_version": "1.0", "agent_role": "sector_head", "phase": phase, "sector": sector,
            "summary": f"{sector} summary",
            "verdicts": [{"ticker": t, "kind": kind, "action": act, "rule_action": act, "agg_conviction": 7.0,
                          "agg_evidence": 7.0, "rr": 2.0, "rule_deviation": False} for t, kind, act in rows]}


def test_p4_preview_writes_cio_packet_with_conflicts(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx, expand=("health_care", "industrials"), reduce=("consumer_staples",))
    set_holdings(ctx)
    ctx.manifest["expand_sectors"] = ["health_care"]
    ctx.manifest["expand_dropped"] = [{"sector": "industrials", "reason": "max_expand_sectors=1"}]
    ctx.manifest["finalists"] = {"health_care": ["MRK"]}
    cs = ctx.p("phase2", "consumer_staples_verdict.json")
    common.write_json(cs, _verdict("consumer_staples", 2, [("KO", "holding", "ADD"), ("PG", "holding", "HOLD")]))
    add_order(ctx, "p2-sh-consumer_staples", "p2.sector_heads", "sector_head", cs, sector="consumer_staples")
    write_stub(ctx.p("phase2", "financials_verdict.failed.json"), "sector_head", None)
    common.write_json(ctx.p("phase2", "funds_verdict.json"),
                      _verdict("funds", 2, [("QQQ", "holding", "HOLD"), ("SPMO", "holding", "HOLD")]))
    common.write_json(ctx.p("phase2", "aggregate.json"), {"tickers": {
        "BRK-B": {"kind": "holding", "rule_action": "TRIM", "agg_conviction": 4.0, "agg_evidence": 6.0,
                  "rr": {"rr": None}},
        "JPM": {"kind": "holding", "rule_action": "HOLD", "agg_conviction": 6.0, "agg_evidence": 6.0,
                "rr": {"rr": 1.5}}}})
    common.write_json(ctx.p("phase3", "health_care_verdict.json"),
                      _verdict("health_care", 3, [("MRK", "candidate", "PASS")]))
    plan = env("portfolio_plan", [src("D:PX:KO")], cash={"current_pct": 5.0, "target_pct": 5.0, "post_pct": 5.0},
               sectors=[{"sector": "consumer_staples", "cur_lookthrough_pct": 25.0}], constraint_checks=[],
               positions=[])
    fakes.add("portfolio", "run", lambda run_dir, *, final: (common.write_json(
        Path(run_dir) / "phase4" / "portfolio_preview.json", plan), plan)[1])

    res = steps.run_python_step("p4.preview", ctx)

    (args, kw), = fakes["portfolio.run"]
    assert P(args[0]) == P(ctx.run_dir) and kw == {"final": False}
    packet = common.read_json(ctx.p("phase4", "cio_packet.json"))
    assert packet["schema"] == "cio_packet/v1"
    verdicts = {(v["phase"], v["sector"]): v for v in packet["verdicts"]}
    assert list(verdicts) == [(2, "financials"), (2, "consumer_staples"), (2, "funds"), (3, "health_care")]
    fin = verdicts[(2, "financials")]
    assert fin["status"] == "failed" and fin["path"].endswith("financials_verdict.failed.json")
    assert [(t["ticker"], t["action"], t["source"]) for t in fin["tickers"]] == [
        ("BRK-B", "TRIM", "aggregate_rule"), ("JPM", "HOLD", "aggregate_rule")]
    assert verdicts[(2, "consumer_staples")]["summary"] == "consumer_staples summary"
    conflicts = {(c["type"], c["sector"]): c for c in packet["conflicts"]}
    assert conflicts[("reduce_sector_but_add", "consumer_staples")]["tickers"] == ["KO"]
    assert "no candidate earned a BUY" in conflicts[("expand_sector_without_buys", "health_care")]["detail"]
    assert "not screened" in conflicts[("expand_sector_without_buys", "industrials")]["detail"]
    assert {"sector": "health_care", "min_weight": 5, "max_weight": 15} in packet["macro_bounds"]
    assert packet["preview"]["sectors"] == plan["sectors"] and packet["preview"]["cash"]["post_pct"] == 5.0
    assert packet["cio_allocation"]["reduce_sectors"] == ["consumer_staples"]
    assert packet["phase3"]["finalists"] == {"health_care": ["MRK"]}
    assert {"D:YF:QQQ:FUND", "D:FRED:DGS10", "D:PX:KO"} <= {s["id"] for s in packet["sources"]}
    assert any("financials" in m for m in packet["missing_data"])
    assert res["outputs"] == [P(ctx.p("phase4", "portfolio_preview.json")), P(ctx.p("phase4", "cio_packet.json"))]
    assert "3 conflicts" in res["summary"]


def _finalize_setup(ctx: steps.RunContext, fakes: Fakes) -> None:
    write_phase1(ctx)
    set_holdings(ctx, only=["KO", "PG"])
    ctx.manifest["finalists"] = {"health_care": ["MRK"]}
    common.write_json(ctx.p("phase2", "consumer_staples_verdict.json"),
                      _verdict("consumer_staples", 2, [("KO", "holding", "ADD"), ("PG", "holding", "HOLD")]))
    common.write_json(ctx.p("phase3", "health_care_verdict.json"),
                      _verdict("health_care", 3, [("MRK", "candidate", "BUY")]))
    common.write_json(ctx.p("phase4", "cio_final.json"), {
        "agent_role": "cio", "mode": "reconciliation",
        "final_actions": [{"ticker": "KO", "action": "HOLD", "source_verdict_action": "ADD", "override": True}]})
    plan = {"positions": [
        {"ticker": "KO", "trade": {"side": "NONE", "shares": 0, "value_usd": 0}},
        {"ticker": "MRK", "trade": {"side": "BUY", "shares": 10, "value_usd": 1000}},
        {"ticker": "PG", "trade": {"side": "SELL", "shares": 2, "value_usd": 300}},
        {"ticker": "XYZ", "trade": {"side": "BUY", "shares": 1, "value_usd": 50}, "dropped_reason": "min_trade"}]}
    fakes.add("portfolio", "run", lambda run_dir, *, final: plan)
    fakes.add("render_memo", "render", lambda run_dir, **kw: (kw["out_md"], kw["out_html"]))
    fakes.add("ledger", "rows_from_run", lambda run_dir: [{"ticker": "KO"}, {"ticker": "MRK"}])
    fakes.add("ledger", "append", lambda rows, ledger_path=None: len(rows))


def test_p4_finalize_dry_run_uses_ledger_preview(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, mode={"dry_run": True})
    _finalize_setup(ctx, fakes)

    res = steps.run_python_step("p4.finalize", ctx)

    (args, kw), = fakes["portfolio.run"]
    assert P(args[0]) == P(ctx.run_dir) and kw == {"final": True}
    (args, kw), = fakes["render_memo.render"]
    assert kw["mode"] == "full" and kw["ticker"] is None
    assert P(kw["out_md"]) == P(ctx.p("phase4", "memo.md")) and P(kw["out_html"]) == P(ctx.p("phase4", "memo.html"))
    (args, kw), = fakes["ledger.append"]
    assert args[0] == [{"ticker": "KO"}, {"ticker": "MRK"}]
    assert P(kw["ledger_path"]) == P(ctx.p("phase4", "ledger_preview.csv"))
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["memo_md"] == P(ctx.p("phase4", "memo.md")) and summary["memo_html"] == P(ctx.p("phase4", "memo.html"))
    assert summary["ledger"] == {"path": P(ctx.p("phase4", "ledger_preview.csv")), "rows_appended": 2, "preview": True}
    assert summary["counts"] == {"holdings": {"HOLD": 2}, "candidates": {"BUY": 1}, "trades": 2}
    assert summary["actions"]["KO"] == {"action": "HOLD", "kind": "holding", "source": "cio_final"}
    assert summary["top_line"] == "2 holdings reviewed (HOLD 2); 1 new BUY proposal; 2 trades in the plan."
    assert res["summary"] == summary["top_line"] and res["memo_md"] == summary["memo_md"]
    assert P(ctx.p("phase4", "summary.json")) in res["outputs"]
    assert P(ctx.p("phase4", "portfolio_plan.json")) in res["outputs"]


def test_p4_finalize_live_uses_default_ledger(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    _finalize_setup(ctx, fakes)
    steps.run_python_step("p4.finalize", ctx)
    (args, kw), = fakes["ledger.append"]
    assert kw["ledger_path"] is None
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["ledger"]["path"] == P(common.ledger_path()) and summary["ledger"]["preview"] is False


def test_p4_finalize_holding_trial(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, mode={"holding": "KO", "dry_run": True})
    _finalize_setup(ctx, fakes)
    set_holdings(ctx, only=["KO"])
    ctx.manifest.pop("finalists")
    res = steps.run_python_step("p4.finalize", ctx)
    assert fakes["portfolio.run"] == []                                     # no rebalancing plan in a trial
    (args, kw), = fakes["render_memo.render"]
    assert kw["mode"] == "trial" and kw["ticker"] == "KO"
    assert P(kw["out_md"]) == P(ctx.p("phase4", "trial_KO.md"))
    assert P(kw["out_html"]) == P(ctx.p("phase4", "trial_KO.html"))
    (args, kw), = fakes["ledger.append"]
    assert P(kw["ledger_path"]) == P(ctx.p("phase4", "ledger_preview.csv"))
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["top_line"] == "Trial KO: HOLD." and summary["plan"] is None
    assert res["memo_md"] == P(ctx.p("phase4", "trial_KO.md"))


def test_p4_preview_survives_plan_failure(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["KO"])

    def boom(run_dir, *, final):
        raise RuntimeError("no prices")

    fakes.add("portfolio", "run", boom)
    res = steps.run_python_step("p4.preview", ctx)
    packet = common.read_json(ctx.p("phase4", "cio_packet.json"))
    assert packet["preview"]["path"] is None and packet["preview"]["sectors"] == []
    assert res["outputs"] == [P(ctx.p("phase4", "cio_packet.json"))]
    assert any("preview plan failed: no prices" in w for w in res["warnings"])


def test_p4_finalize_survives_plan_and_ledger_failures(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    _finalize_setup(ctx, fakes)

    def boom(*a, **k):
        raise RuntimeError("broken")

    fakes.add("portfolio", "run", boom)
    fakes.add("ledger", "append", boom)
    res = steps.run_python_step("p4.finalize", ctx)
    assert len(fakes["render_memo.render"]) == 1                            # memo still rendered
    assert any(w.startswith("portfolio plan failed") for w in res["warnings"])
    assert any(w.startswith("ledger append failed") for w in res["warnings"])
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["ledger"]["rows_appended"] is None and summary["counts"]["trades"] == 0
    assert P(ctx.p("phase4", "portfolio_plan.json")) not in res["outputs"]


def test_main_cli_prints_sequence_and_orders(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ctx = make_ctx(tmp_path, mode={"quick": True})
    write_phase1(ctx)
    assert steps.main(["--run", str(ctx.run_dir)]) == 0
    out = capsys.readouterr().out
    assert '"p2.wave_a"' in out and "p3.screen" not in out
    assert steps.main(["--run", str(ctx.run_dir), "--orders", "p1.macro"]) == 0
    assert '"order_id": "p1-macro"' in capsys.readouterr().out
    assert steps.main(["--run", str(tmp_path / "nope")]) == 1


# =====================================================================================
# regressions: stale reused prices, missing data inputs, cross-sector duplicates
# =====================================================================================


def test_price_fact_prefers_the_newer_technicals_close(tmp_path: Path, fakes: Fakes) -> None:
    """A reused Phase 1 (prices.json as of 2026-09-26) must not beat this run's technicals close."""
    ctx = make_ctx(tmp_path, mode={"holding": "KO"})
    stale = {"KO": {"last_close": common.fact(100.0, "USD/share", "D:PX:KO", "2026-09-26", "price"),
                    "last_date": "2026-09-26"}}
    write_phase1(ctx, extra_prices=stale)
    _data_fakes(fakes, close=110.0)                 # technicals close 110 as of AS_OF (2026-10-02)

    steps.run_python_step("p2.data", ctx)

    (_args, kw), = fakes["fetch_fundamentals.run"]
    assert kw["price"]["value"] == 110.0 and kw["price"]["as_of"] == AS_OF
    write_data(ctx, 2, "KO", close=110.0)           # the technicals file as compute_technicals writes it
    fact_ = steps._price_fact(ctx, {"ticker": "KO", "price_ticker": "KO"}, 2)
    assert fact_["value"] == 110.0 and fact_["as_of"] == AS_OF

    # same date: the prices.json snapshot wins; no technicals: the snapshot is used
    write_phase1(ctx)
    assert steps._price_fact(ctx, {"ticker": "KO", "price_ticker": "KO"}, 2)["value"] == 100.0
    ctx.p("phase2", "data", "technicals_KO.json").unlink()
    write_phase1(ctx, extra_prices=stale)
    assert steps._price_fact(ctx, {"ticker": "KO", "price_ticker": "KO"}, 2)["value"] == 100.0


def test_orders_skip_lens_without_core_data_and_note_missing_inputs(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["KO", "QQQ"])
    write_data(ctx, 2, "KO")
    write_data(ctx, 2, "QQQ", fund=True)
    ctx.p("phase2", "data", "technicals_KO.json").unlink()          # compute_technicals failed for KO
    ctx.p("phase2", "data", "technicals_QQQ.json").unlink()

    ids = by_id(steps.build_orders("p2.wave_a", ctx))
    assert set(ids) == {"p2-a-fundamental-KO", "p2-a-osint-KO", "p2-a-operator_ceo-KO", "p2-a-etf-QQQ"}
    log = common.read_text(ctx.p("run_log.md"))
    assert "technical:KO (no technicals)" in log and "technical:QQQ (no technicals)" in log
    etf = ids["p2-a-etf-QQQ"]
    assert P(ctx.p("phase2", "data", "technicals_QQQ.json")) not in in_paths(etf)
    assert any("technicals_QQQ.json" in n for n in etf["notes"])
    assert not any("Unavailable" in n for n in ids["p2-a-fundamental-KO"]["notes"])

    write_report(rpath(ctx, 2, "fundamental", "KO"), "fundamental", "KO")
    (da,) = steps.build_orders("p2.wave_b", ctx)
    assert all(Path(x).is_file() for x in in_paths(da))
    assert any("technicals_KO.json" in n for n in da["notes"])
    assert "technicals" not in steps.validation_context(ctx, da)


def test_p3_screen_rotation_passes_recent_shortlists_and_can_be_disabled(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx, expand=("health_care",))
    set_holdings(ctx)
    fakes.add("screen", "run", lambda sector, out, **kw: {"sector": sector, "shortlist": []})
    fakes.add("screen", "recent_shortlisted", lambda sector, runs_root, **kw: ["EOG", "MPC"])

    steps.run_python_step("p3.screen", ctx)

    (rargs, rkw), = fakes["screen.recent_shortlisted"]
    assert rargs[0] == "health_care" and Path(rargs[1]) == ctx.run_dir.parent
    assert rkw == {"exclude_run": ctx.run_id, "lookback": 3}
    assert fakes["screen.run"][0][1]["recent"] == ["EOG", "MPC"]
    assert "rotation skips 2 recently shortlisted ticker(s): EOG, MPC" in common.read_text(ctx.p("run_log.md"))

    # phase3.rotation.enabled: false -> nothing is read, nothing is skipped
    ctx2 = make_ctx(tmp_path / "off", committee=lambda c: c["phase3"].update(rotation={"enabled": False}))
    write_phase1(ctx2, expand=("health_care",))
    set_holdings(ctx2)
    steps.run_python_step("p3.screen", ctx2)
    assert len(fakes["screen.recent_shortlisted"]) == 1                   # not called again
    assert fakes["screen.run"][1][1]["recent"] == []


def test_p3_ticker_in_two_expand_sectors_is_kept_once(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx, expand=("health_care", "industrials"))
    set_holdings(ctx)
    shortlists = {"health_care": ["XYZ", "MRK"], "industrials": ["xyz", "GE"]}
    fakes.add("screen", "run", lambda sector, out, **kw: {"sector": sector, "shortlist": shortlists[sector]})
    fakes.add("screen", "recent_shortlisted", lambda sector, runs_root, **kw: [])
    _data_fakes(fakes)

    res = steps.run_python_step("p3.screen", ctx)

    assert ctx.manifest["candidates"] == {"health_care": ["XYZ", "MRK"], "industrials": ["GE"]}
    assert any("XYZ" in w and "health_care only" in w for w in res["warnings"])
    assert [a[0] for a, _k in fakes["compute_technicals.run"]] == ["XYZ", "MRK", "GE"]

    # a manifest that still lists a ticker under two sectors yields one subject (first sector wins)
    ctx.manifest["candidates"] = {"health_care": ["XYZ"], "industrials": ["XYZ"]}
    for t in ("XYZ",):
        write_data(ctx, 3, t)
    specs = steps.build_orders("p3.light", ctx)
    assert [s["order_id"] for s in specs] == ["p3-light-fundamental-XYZ", "p3-light-technical-XYZ"]
    assert {s["sector"] for s in specs} == {"health_care"}


# =====================================================================================
# regressions: price Fact date, stale config, work-order data notes, summary digest
# =====================================================================================


def test_p2_data_passes_the_price_fact_so_its_date_is_the_last_bar_not_the_run_date(
        tmp_path: Path, fakes: Fakes) -> None:
    """The run date may be a Sunday; the close it prices is Friday's. fetch_fundamentals stamps the
    price with the Fact's own as_of, so a bare float would have dated it with the run date."""
    ctx = make_ctx(tmp_path, as_of="2026-10-04", mode={"holding": "KO"})
    friday = {"KO": {"last_close": common.fact(100.0, "USD/share", "D:PX:KO", "2026-10-02", "price"),
                     "last_date": "2026-10-02"}}
    write_phase1(ctx, extra_prices=friday)
    seen: list[Any] = []
    fakes.add("compute_technicals", "run", lambda t, out, **kw: {
        "close": common.fact(100.0, "USD/share", f"D:PX:{t}", "2026-10-02", "price")})
    fakes.add("fetch_fundamentals", "run", lambda t, out, **kw: seen.append(kw["price"]) or {})
    fakes.add("valuation", "run", lambda *a, **k: {})

    steps.run_python_step("p2.data", ctx)

    (price,) = seen
    assert isinstance(price, dict) and price["value"] == 100.0
    assert price["as_of"] == "2026-10-02" != ctx.as_of
    assert price["kind"] == "price"

    # a null Fact is not passed (fetch_fundamentals then falls back to its cached close)
    ctx2 = make_ctx(tmp_path / "n", mode={"holding": "KO"})
    write_phase1(ctx2, extra_prices={"KO": {"last_close": common.fact(None, "USD/share", "D:PX:KO", AS_OF, "price"),
                                            "last_date": AS_OF}})
    seen.clear()
    fakes.add("compute_technicals", "run", lambda t, out, **kw: {"close": common.fact(None, "USD/share", "D:PX:KO",
                                                                                      AS_OF, "price")})
    steps.run_python_step("p2.data", ctx2)
    assert seen == [None]


def _drift_ctx(tmp_path: Path, *, live: Path | None = None, **kw: Any) -> tuple[steps.RunContext, Path]:
    """A run whose manifest carries the init-time config hashes of a (copied) live config dir."""
    import hashlib

    ctx = make_ctx(tmp_path, **kw)
    live = live or (tmp_path / "live_config")
    live.mkdir(parents=True, exist_ok=True)
    shas = {}
    for f in sorted(ctx.p("inputs").glob("*.yaml")):          # what committee.py init does
        (live / f.name).write_bytes(f.read_bytes())
        shas[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    ctx.manifest["config_sha256"] = shas
    ctx.manifest["paths"] = {"config_dir": live.as_posix()}
    common.write_json(ctx.p("manifest.json"), ctx.manifest)
    return ctx, live


def test_config_drift_compares_live_files_with_the_init_hashes(tmp_path: Path) -> None:
    ctx, live = _drift_ctx(tmp_path)
    assert steps.config_drift(ctx.run_dir, ctx.manifest) == []

    (live / "investor_profile.yaml").write_text("horizon_years: 55\n", encoding="utf-8")
    (live / "universe.yaml").unlink()
    drift = steps.config_drift(ctx.run_dir, ctx.manifest)
    assert [(d["file"], d["change"]) for d in drift] == [("investor_profile.yaml", "changed"),
                                                         ("universe.yaml", "deleted")]
    assert drift[0]["message"] == ("config investor_profile.yaml changed after this run started \u2014 decisions "
                                   "used the snapshot in inputs/")
    assert drift[0]["snapshot_sha256"] != drift[0]["live_sha256"] and len(drift[0]["live_sha256"]) == 12
    assert "was deleted" in drift[1]["message"]

    # no hashes / no recorded or existing config dir: the check is impossible, never an error
    assert steps.config_drift(ctx.run_dir, {}) == []
    assert steps.config_drift(ctx.run_dir, {**ctx.manifest, "config_sha256": "0" * 64}) == []
    assert steps.config_drift(ctx.run_dir, {**ctx.manifest, "paths": {"config_dir": str(tmp_path / "gone")}}) == []


def test_p4_finalize_reports_a_config_changed_after_the_run_started(tmp_path: Path, fakes: Fakes) -> None:
    ctx, live = _drift_ctx(tmp_path)
    _finalize_setup(ctx, fakes)
    seen: dict[str, Any] = {}

    def render(run_dir, **kw):      # the memo is rendered from the manifest ON DISK
        seen["on_disk"] = common.read_json(ctx.p("manifest.json")).get("config_drift")
        return kw["out_md"], kw["out_html"]

    fakes.add("render_memo", "render", render)
    (live / "investor_profile.yaml").write_text("horizon_years: 55\nrisk_tolerance: aggressive\n", encoding="utf-8")

    res = steps.run_python_step("p4.finalize", ctx)

    msg = ("config investor_profile.yaml changed after this run started \u2014 decisions used the snapshot in "
           "inputs/")
    assert msg in res["warnings"]                                           # -> manifest warning (committee.py)
    assert ctx.manifest["config_drift"][0]["file"] == "investor_profile.yaml"
    assert [d["message"] for d in seen["on_disk"]] == [msg], "the renderer must see the drift (persisted)"
    assert f"WARNING {msg}" in common.read_text(ctx.p("run_log.md"))        # -> run_log line
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["config_drift"] == [msg]
    assert P(ctx.p("phase4", "memo.md")) in res["outputs"], "the run is not aborted"

    # unchanged config: no warning, and a stale key from an earlier finalize is dropped
    (live / "investor_profile.yaml").write_bytes(ctx.p("inputs", "investor_profile.yaml").read_bytes())
    res = steps.run_python_step("p4.finalize", ctx)
    assert not any("changed after this run started" in w for w in res["warnings"])
    assert "config_drift" not in ctx.manifest
    assert common.read_json(ctx.p("phase4", "summary.json"))["config_drift"] == []


def test_p4_finalize_summary_carries_sources_and_missing_data_from_the_memo_context(
        tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    _finalize_setup(ctx, fakes)
    digest = {"sources": [{"ids": ["D:PX:KO"], "url": "https://finance.yahoo.com/quote/KO/", "title": "KO",
                           "kind": "dataset", "accessed": AS_OF, "cited_by": 3}],
              "missing_data": ["data · phase1/prices.json: XLRE price unavailable"], "missing_data_count": 31}
    fakes.add("render_memo", "summary_digest", lambda run_dir, **kw: digest)

    steps.run_python_step("p4.finalize", ctx)

    (args, kw), = fakes["render_memo.summary_digest"]
    assert P(args[0]) == P(ctx.run_dir) and kw == {"ticker": None}
    summary = common.read_json(ctx.p("phase4", "summary.json"))
    assert summary["sources"] == digest["sources"]
    assert summary["missing_data"] == digest["missing_data"] and summary["missing_data_count"] == 31

    # a failing digest never loses the summary: the keys are empty, the problem is a warning
    def boom(run_dir, **kw):
        raise RuntimeError("no context")

    fakes.add("render_memo", "summary_digest", boom)
    res = steps.run_python_step("p4.finalize", ctx)
    assert any("digest failed: no context" in w for w in res["warnings"])
    assert common.read_json(ctx.p("phase4", "summary.json"))["sources"] == []


def test_orders_for_a_conglomerate_carry_the_judge_it_as_an_insurer_note(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write_phase1(ctx)
    set_holdings(ctx, only=["BRK-B", "KO"])
    for h in ctx.manifest["holdings"]:
        if h["ticker"] == "BRK-B":
            h["flags"] = ["conglomerate"]
    for t in ("BRK-B", "KO"):
        write_data(ctx, 2, t)

    wave_a = by_id(steps.build_orders("p2.wave_a", ctx))
    for oid in ("p2-a-fundamental-BRK-B", "p2-a-technical-BRK-B", "p2-a-osint-BRK-B"):
        assert any("flagged conglomerate: judge it as an insurance/holding company, see the financials playbook"
                   in n for n in wave_a[oid]["notes"]), oid
    assert not any("conglomerate" in n for o in wave_a.values() if o["ticker"] == "KO" for n in o["notes"])

    for t in ("BRK-B", "KO"):
        write_report(rpath(ctx, 2, "fundamental", t), "fundamental", t)
    da = by_id(steps.build_orders("p2.wave_b", ctx))
    assert any("flagged conglomerate" in n for n in da["p2-b-devils_advocate-BRK-B"]["notes"])
    assert not any("conglomerate" in n for n in da["p2-b-devils_advocate-KO"]["notes"])
    sh = by_id(steps.build_orders("p2.sector_heads", ctx))
    assert any(n.startswith("BRK-B: flagged conglomerate") for n in sh["p2-sh-financials"]["notes"])
    assert not any("conglomerate" in n for n in sh["p2-sh-consumer_staples"]["notes"])

    # the flag is also found in the Phase 1 look-through and in etf_overrides (phase 3 candidates)
    ctx2 = make_ctx(tmp_path / "b")
    write_phase1(ctx2)
    set_holdings(ctx2, only=["BRK-B"])
    lk = common.read_json(ctx2.p("phase1", "lookthrough.json"))
    next(h for h in lk["holdings"] if h["ticker"] == "BRK-B")["flags"] = ["conglomerate"]
    common.write_json(ctx2.p("phase1", "lookthrough.json"), lk)
    write_data(ctx2, 2, "BRK-B")
    assert any("flagged conglomerate" in n for n in by_id(steps.build_orders("p2.wave_a", ctx2))
               ["p2-a-fundamental-BRK-B"]["notes"])
    ctx2.overrides.setdefault("securities", {})["XYZ"] = {"flags": ["conglomerate"]}
    assert "conglomerate" in steps._ticker_flags(ctx2, "xyz")


def test_orders_for_a_ticker_priced_from_a_converted_series_carry_the_native_price_note(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    native = {"quote_currency": "ILA", "currency": "ILS",
              "last_close": common.fact(75.22, "ILS/share", "D:PX:LUMI.TA", "2026-10-02", "price"),
              "usdils": 3.0532, "usdils_as_of": "2026-10-02", "fx_usd_per_native": 0.3275}
    write_phase1(ctx, extra_prices={"LUMI.TA": {"last_close": fact(24.64, sid="D:PX:LUMI.TA"),
                                                "last_date": "2026-10-02", "native": native}})
    ctx.manifest["holdings"] = [
        {"ticker": "LUMI.TA", "asset_type": "stock", "sector": "financials", "sector_class": "financials",
         "classification_source": "test", "position_pct": 3.0, "price_ticker": "LUMI.TA", "flags": [],
         "in_scope": True},
        {"ticker": "KO", "asset_type": "stock", "sector": "consumer_staples", "sector_class": "default",
         "classification_source": "test", "position_pct": 5.0, "price_ticker": "KO", "flags": [], "in_scope": True}]
    for t in ("LUMI.TA", "KO"):
        write_data(ctx, 2, t)

    wave_a = by_id(steps.build_orders("p2.wave_a", ctx))
    note = next(n for n in wave_a["p2-a-technical-LUMI.TA"]["notes"] if "converted" in n)
    assert note.startswith("LUMI.TA: price series is converted from ILS to USD at spot USD/ILS; native ILS price "
                           "in prices.json native.last_close")
    assert "75.22 ILS as of 2026-10-02" in note and "spot USD/ILS 3.0532" in note
    assert any("converted" in n for n in wave_a["p2-a-fundamental-LUMI.TA"]["notes"])
    assert not any("converted" in n for o in wave_a.values() if o["ticker"] == "KO" for n in o["notes"])
    sh = by_id(steps.build_orders("p2.sector_heads", ctx))
    assert any("LUMI.TA: price series is converted" in n for n in sh["p2-sh-financials"]["notes"])
