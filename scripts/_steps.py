"""Pipeline step logic consumed by ``scripts/committee.py`` (DESIGN.md §2.3).

``committee.py`` is the thin state machine: it owns ``manifest.json``, renders work orders,
dispatches agents, validates outputs and handles retries. This module owns *what* every step
does:

* ``RunContext(run_dir, manifest)`` — run directory + manifest + lazily loaded config
  snapshots (``inputs/*.yaml``, falling back to the active config dir).
* ``step_sequence(ctx)`` — ordered ``[(name, "python"|"agents"), ...]`` for the run's mode.
* ``run_python_step(name, ctx)`` — runs a python step through the other scripts' integration
  API (imported lazily, so tests can substitute modules) and returns
  ``{"summary", "outputs", "warnings", "skip_steps"}``.
* ``build_orders(name, ctx)`` — work-order specs for an agents step.
* ``validation_context(ctx, spec)`` — the ``context`` dict ``validate.validate_doc`` expects.

Python steps mutate ``ctx.manifest`` (``holdings``, ``trial_candidate``, ``phase1_reuse``,
``expand_sectors``, ``expand_dropped``, ``candidates``, ``finalists``); the caller persists it.
``skip_steps`` names later steps the caller should mark ``skipped`` (nothing left to do).

Reports are located through ``manifest["orders"][*]["resolved_output"]``; when an order is not
in the manifest the canonical file on disk is used (failure stub > ``.r2.json`` > attempt 1).
Failure stubs (``*.failed.json``) are scored and aggregated (evidence 1) but never handed to
another agent as an input.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

# =====================================================================================
# Constants
# =====================================================================================

#: The full step machine (DESIGN §2.3), in order.
STEPS: tuple[tuple[str, str], ...] = (
    ("p1.data", "python"),
    ("p1.macro", "agents"),
    ("p1.cio", "agents"),
    ("p2.data", "python"),
    ("p2.wave_a", "agents"),
    ("p2.wave_b", "agents"),
    ("p2.aggregate", "python"),
    ("p2.sector_heads", "agents"),
    ("p3.screen", "python"),
    ("p3.light", "agents"),
    ("p3.finalists", "python"),
    ("p3.full", "agents"),
    ("p3.da", "agents"),
    ("p3.aggregate", "python"),
    ("p3.sector_heads", "agents"),
    ("p4.preview", "python"),
    ("p4.cio", "agents"),
    ("p4.finalize", "python"),
)
STEP_KINDS: dict[str, str] = dict(STEPS)
STEP_NAMES: tuple[str, ...] = tuple(n for n, _ in STEPS)
P1_AGENT_STEPS: tuple[str, ...] = ("p1.macro", "p1.cio")
P3_STEPS: tuple[str, ...] = tuple(n for n in STEP_NAMES if n.startswith("p3."))
#: /committee-holding: Phase 1 (or reuse), Phase 2 for one ticker, trial finalize.
HOLDING_STEPS: tuple[str, ...] = (
    "p1.data", "p1.macro", "p1.cio", "p2.data", "p2.wave_a", "p2.wave_b", "p2.aggregate",
    "p2.sector_heads", "p4.finalize",
)

#: Phase 1 files (canonical names) — copied as a set when a holding trial reuses Phase 1.
P1_DATA_FILES: tuple[str, ...] = ("prices.json", "macro.json", "lookthrough.json")
P1_AGENT_FILES: dict[str, tuple[str, str, str]] = {  # file -> (step, role, canonical name)
    "macro_view.json": ("p1.macro", "macro", "macro_view.json"),
    "cio_allocation.json": ("p1.cio", "cio", "cio_allocation.json"),
}

#: Configs that must be byte-identical (sha256) for a holding trial to reuse an earlier run's
#: Phase 1: they decide the look-through (positions, classification), the price set and the
#: CIO allocation inputs.
REUSE_CONFIGS: tuple[str, ...] = ("portfolio", "investor_profile", "committee", "etf_overrides")

FUNDS = "funds"
SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# Digest sizes for sector packets (presentation limits, not decision thresholds).
PACKET_TOP_RISKS = 3
PACKET_TOP_ATTACKS = 3
PACKET_KILL_CRITERIA = 3
PACKET_REPORT_ERRORS = 3

# Fallbacks used ONLY when a key is absent from committee.yaml / investor_profile.yaml (the
# config schemas require every one of them; minimal test configs may not carry them).
DEFAULT_ROLES: dict[str, Any] = {
    "phase2_stock": ["fundamental", "technical", "osint", "operator_ceo"],
    "phase2_fund": ["etf", "technical"],
    "quick_stock": ["fundamental", "technical"],
    "quick_fund": ["etf"],
    "phase3_light": ["fundamental", "technical"],
    "phase3_full": ["operator_ceo", "industry_ops", "osint"],
    "da_for_funds": False,
}
DEFAULT_PHASE1_REUSE_DAYS = 7
DEFAULT_MAX_EXPAND_SECTORS = 2
DEFAULT_ROTATION_LOOKBACK = 3
DEFAULT_FINALISTS = 4
DEFAULT_MAX_BUYS_PER_SECTOR = 3
DEFAULT_MIN_JUSTIFICATION_WORDS = 15
DEFAULT_BENCHMARK = "SPY"
DEFAULT_FX_TICKER = "ILS=X"

#: Which ``validation.word_limits`` keys apply to each role (mode-qualified for cio).
ROLE_WORD_LIMIT_KEYS: dict[str, tuple[str, ...]] = {
    "fundamental": ("thesis", "flag_response_note"),
    "technical": ("thesis", "technical_setup", "flag_response_note"),
    "operator_ceo": ("thesis", "extension_assessment", "flag_response_note"),
    "industry_ops": ("thesis", "extension_assessment", "flag_response_note"),
    "osint": ("thesis", "flag_response_note"),
    "devils_advocate": ("flag_response_note",),
    "etf": ("thesis", "flag_response_note"),
    "macro": ("regime_summary", "scenario_description"),
    "cio:allocation": ("cio_rationale",),
    "cio:reconciliation": ("executive_summary_item", "dissent_summary"),
    "sector_head": ("sector_summary", "sector_rationale"),
}

INPUT_PURPOSE: dict[str, str] = {
    "technicals": "computed technicals: indicators, regime, levels, stop candidates (cite with data_ref)",
    "fundamentals": "SEC EDGAR fundamentals, TTM/FY figures and ratios (cite with data_ref)",
    "valuation": "deterministic valuation grid: DCF/multiple cells and reverse DCF (cite cell ids)",
    "playbook": "sector playbook: KPIs, valuation methods, macro sensitivities, failure modes",
    "etf_playbook": "ETF playbook: how to review a fund holding",
    "lookthrough": "portfolio look-through: sector exposure, fund composition, overlap",
    "prices": "price snapshot: last close, returns, average dollar volume (cite with data_ref)",
    "macro": "FRED macro series: latest values and 3m/12m changes (cite with data_ref)",
    "macro_view": "macro view: regime, scenarios, sector constraints (min/max weights)",
    "cio_allocation": "Phase 1 CIO allocation: current vs target sector weights, expand/reduce sectors",
    "investor_profile": "investor profile: horizon, risk tolerance, position/sector limits",
    "portfolio": "current holdings as entered by the investor",
    "packet": "sector packet: aggregate rule output + digests of every report for each ticker",
    "cio_packet": "CIO packet: verdict summaries, preview sector table, macro bounds, conflicts",
    "portfolio_preview": "preview rebalancing plan built from the sector verdicts",
}


# =====================================================================================
# Small helpers
# =====================================================================================


def _mod(name: str) -> Any:
    """Lazily import ``scripts.<name>`` (tests substitute ``sys.modules`` entries)."""
    return importlib.import_module(f"scripts.{name}")


def _posix(p: str | Path) -> str:
    return Path(p).resolve().as_posix()


def _norm(t: Any) -> str | None:
    if t is None or str(t).strip() == "":
        return None
    try:
        return common.norm_ticker(t)
    except ValueError:
        return None


def _read(path: str | Path | None) -> Any:
    """Parse a JSON file; None when missing/unreadable (never raises)."""
    if path is None:
        return None
    try:
        return common.read_json(path)
    except (OSError, ValueError):
        return None


def _is_stub_path(path: str | Path | None) -> bool:
    return path is not None and Path(path).name.endswith(".failed.json")


def _is_stub(path: str | Path | None, doc: Any = None) -> bool:
    if _is_stub_path(path):
        return True
    return isinstance(doc, dict) and doc.get("status") == "failed"


def _usable(path: Path | None) -> bool:
    """An existing, non-stub file (safe to hand to an agent as an input)."""
    return path is not None and path.is_file() and not _is_stub_path(path)


def _dedupe(items: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def _cfg_get(cfg: Mapping[str, Any] | None, dotted: str, default: Any = None) -> Any:
    value = common.get_dotted(cfg or {}, dotted, None)
    return default if value is None else value


def _count(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _fmt_counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) if counts else "none"


def _result(summary: str, outputs: Iterable[str | Path] = (), warnings: Iterable[str] = (),
            skip_steps: Iterable[str] = (), **extra: Any) -> dict:
    out = {
        "summary": summary,
        "outputs": _dedupe(_posix(p) for p in outputs),
        "warnings": list(warnings),
        "skip_steps": list(skip_steps),
    }
    out.update(extra)
    return out


def _normalize_mode(manifest: Mapping[str, Any] | None) -> dict:
    """``{"dry_run","quick","sectors","holding"}`` from ``manifest["mode"]`` (dict, or a legacy
    string such as ``"quick"``) with top-level ``sectors``/``holding`` as fallbacks."""
    manifest = manifest or {}
    raw = manifest.get("mode")
    out: dict[str, Any] = {"dry_run": False, "quick": False, "sectors": None, "holding": None}
    if isinstance(raw, Mapping):
        for key in out:
            if key in raw:
                out[key] = raw[key]
    elif isinstance(raw, str):
        tokens = {t for t in raw.replace("+", ",").replace(" ", ",").split(",") if t}
        out["quick"] = "quick" in tokens
        out["dry_run"] = bool(tokens & {"dry_run", "dryrun", "dry-run"})
    if out["sectors"] is None and manifest.get("sectors"):
        out["sectors"] = manifest.get("sectors")
    if out["holding"] is None and manifest.get("holding"):
        out["holding"] = manifest.get("holding")
    sectors = out["sectors"]
    if isinstance(sectors, str):
        sectors = [s.strip() for s in sectors.split(",") if s.strip()]
    out["sectors"] = list(sectors) if sectors else None
    out["holding"] = _norm(out["holding"])
    out["dry_run"] = bool(out["dry_run"])
    out["quick"] = bool(out["quick"])
    return out


# =====================================================================================
# RunContext
# =====================================================================================


class RunContext:
    """A committee run: ``run_dir`` + its manifest dict + lazily loaded config snapshots."""

    _CONFIG_ATTRS = {
        "cfg": "committee",
        "profile": "investor_profile",
        "portfolio": "portfolio",
        "universe": "universe",
        "overrides": "etf_overrides",
    }
    _OPTIONAL_CONFIGS = ("universe", "etf_overrides")

    def __init__(self, run_dir: str | Path, manifest: dict | None):
        self.run_dir = Path(run_dir).resolve()
        self.manifest: dict = manifest if manifest is not None else {}
        self._configs: dict[str, Any] = {}

    # ---- identity -------------------------------------------------------------------
    @property
    def run_id(self) -> str:
        return str(self.manifest.get("run_id") or self.run_dir.name)

    @property
    def as_of(self) -> str:
        value = self.manifest.get("as_of")
        try:
            parsed = common.date_str(value) if value else None
        except ValueError:
            parsed = None
        return parsed or common.today().isoformat()

    @property
    def mode(self) -> dict:
        return _normalize_mode(self.manifest)

    # ---- configs (inputs/<name>.yaml snapshot, else the active config dir) -----------
    def _config(self, name: str) -> dict:
        if name not in self._configs:
            snap = self.p("inputs", f"{name}.yaml")
            data: Any
            if snap.is_file():
                data = common.load_yaml(snap)
            elif name == "committee":
                data = common.committee_cfg()
            else:
                try:
                    data = common.load_config(name)
                except common.ConfigError:
                    if name not in self._OPTIONAL_CONFIGS:
                        raise
                    data = {}
            self._configs[name] = data if isinstance(data, dict) else {}
        return self._configs[name]

    @property
    def cfg(self) -> dict:
        return self._config("committee")

    @property
    def profile(self) -> dict:
        return self._config("investor_profile")

    @property
    def portfolio(self) -> dict:
        return self._config("portfolio")

    @property
    def universe(self) -> dict:
        return self._config("universe")

    @property
    def overrides(self) -> dict:
        return self._config("etf_overrides")

    # ---- paths / log -----------------------------------------------------------------
    def p(self, *parts: str | Path) -> Path:
        return self.run_dir.joinpath(*[str(x) for x in parts])

    def log(self, msg: str) -> None:
        """Append ``- <utc ts> <msg>`` to ``run_log.md``."""
        path = self.p("run_log.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = " ".join(str(msg).splitlines())
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(f"- {common.now_iso()} {text}\n")


# =====================================================================================
# Step sequence
# =====================================================================================


def step_sequence(ctx: RunContext) -> list[tuple[str, str]]:
    """Ordered ``(name, kind)`` for the run's mode: full (18 steps), quick (no ``p3.*``),
    holding (Phase 1 / reuse, Phase 2 for one ticker, ``p4.finalize`` in trial mode).
    ``--sectors`` does not change the sequence (it narrows what the steps cover)."""
    mode = ctx.mode
    if mode["holding"]:
        names: Iterable[str] = HOLDING_STEPS
    elif mode["quick"]:
        names = [n for n in STEP_NAMES if not n.startswith("p3.")]
    else:
        names = STEP_NAMES
    return [(n, STEP_KINDS[n]) for n in names]


# =====================================================================================
# Paths and report lookup
# =====================================================================================


def _data_path(ctx: RunContext, phase: int, kind: str, ticker: str) -> Path:
    return ctx.p(f"phase{phase}", "data", common.data_filename(kind, ticker))


def _report_path(ctx: RunContext, phase: int, role: str, ticker: str) -> Path:
    return ctx.p(f"phase{phase}", "reports", common.data_filename(role, ticker))


def _verdict_path(ctx: RunContext, phase: int, sector: str) -> Path:
    return ctx.p(f"phase{phase}", f"{sector}_verdict.json")


def _packet_path(ctx: RunContext, phase: int, sector: str) -> Path:
    return ctx.p(f"phase{phase}", "packets", f"sector_{sector}.json")


def _playbook(sector: str) -> Path:
    if sector == FUNDS:
        return common.knowledge_dir() / "etf_playbook.md"
    return common.knowledge_dir() / "sectors" / f"{sector}.md"


def _input_yaml(ctx: RunContext, name: str) -> Path:
    snap = ctx.p("inputs", f"{name}.yaml")
    return snap if snap.is_file() else common.config_dir() / f"{name}.yaml"


def _disk_variants(path: Path) -> list[Path]:
    """Disk precedence for an order output: failure stub > retry output > attempt 1."""
    stem = path.name[: -len(".json")] if path.name.endswith(".json") else path.stem
    return [path.with_name(f"{stem}.failed.json"), path.with_name(f"{stem}.r2.json"), path]


def _abs_in(run_dir: Path, value: str | Path) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    for base in (run_dir, common.REPO_ROOT, Path.cwd()):
        cand = base / p
        if cand.exists():
            return cand
    return run_dir / p


def _find_output_in(run_dir: Path, manifest: Mapping[str, Any], step: str, role: str, default: Path,
                    *, ticker: str | None = None, sector: str | None = None) -> Path | None:
    """Resolved output of the order (step, role[, ticker][, sector]) from the manifest, else the
    canonical file on disk. None when nothing exists."""
    best: tuple[int, Path] | None = None
    for order in (manifest.get("orders") or {}).values():
        if not isinstance(order, Mapping):
            continue
        if order.get("step") != step or order.get("agent_role") != role:
            continue
        if ticker is not None and _norm(order.get("ticker")) != ticker:
            continue
        if sector is not None and order.get("sector") != sector:
            continue
        resolved = order.get("resolved_output")
        if not resolved:
            continue
        path = _abs_in(run_dir, resolved)
        if not path.is_file():
            continue
        try:
            attempt = int(order.get("attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        if best is None or attempt >= best[0]:
            best = (attempt, path)
    if best is not None:
        return best[1]
    for cand in _disk_variants(default):
        if cand.is_file():
            return cand
    return None


def _find_output(ctx: RunContext, step: str, role: str, default: Path, *, ticker: str | None = None,
                 sector: str | None = None) -> Path | None:
    return _find_output_in(ctx.run_dir, ctx.manifest, step, role, default, ticker=ticker, sector=sector)


def _p1_output(ctx: RunContext, role: str) -> Path | None:
    """Resolved Phase 1 agent output (``macro`` -> macro_view, ``cio`` -> cio_allocation)."""
    if role == "macro":
        return _find_output(ctx, "p1.macro", "macro", ctx.p("phase1", "macro_view.json"))
    return _find_output(ctx, "p1.cio", "cio", ctx.p("phase1", "cio_allocation.json"))


def _p1_doc(ctx: RunContext, role: str) -> dict | None:
    path = _p1_output(ctx, role)
    doc = _read(path)
    if not isinstance(doc, dict) or _is_stub(path, doc):
        return None
    return doc


# =====================================================================================
# Roles / subjects
# =====================================================================================


def _roles(ctx: RunContext, key: str) -> list[str]:
    value = _cfg_get(ctx.cfg, f"roles.{key}", DEFAULT_ROLES[key])
    return [str(r) for r in value] if isinstance(value, (list, tuple)) else list(DEFAULT_ROLES[key])


def _wave_a_roles(ctx: RunContext, sub: Mapping[str, Any]) -> list[str]:
    fund = sub.get("asset_type") == "fund"
    if ctx.mode["quick"]:
        return _roles(ctx, "quick_fund" if fund else "quick_stock")
    return _roles(ctx, "phase2_fund" if fund else "phase2_stock")


def _da_applies(ctx: RunContext, sub: Mapping[str, Any]) -> bool:
    if sub.get("asset_type") != "fund":
        return True
    return bool(_cfg_get(ctx.cfg, "roles.da_for_funds", DEFAULT_ROLES["da_for_funds"]))


def _light_roles(ctx: RunContext) -> list[str]:
    return _roles(ctx, "phase3_light")


def _full_roles(ctx: RunContext) -> list[str]:
    """Full-wave roles not already run in the light wave (same output path otherwise)."""
    light = set(_light_roles(ctx))
    return [r for r in _roles(ctx, "phase3_full") if r not in light]


def _p2_subjects(ctx: RunContext) -> list[dict]:
    """Phase 2 subjects: in-scope holdings (kind holding) + the trial candidate (holding mode,
    ticker not held; kind candidate)."""
    subs = [dict(h, kind="holding") for h in ctx.manifest.get("holdings") or []
            if isinstance(h, Mapping) and h.get("in_scope", True)]
    trial = ctx.manifest.get("trial_candidate")
    if isinstance(trial, Mapping) and trial.get("ticker"):
        subs.append(dict(trial, kind="candidate"))
    return subs


def _p3_subjects(ctx: RunContext, which: str) -> list[dict]:
    """Phase 3 subjects from ``manifest[which]`` (``candidates`` or ``finalists``)."""
    out: list[dict] = []
    seen: set[str] = set()
    for sector, tickers in (ctx.manifest.get(which) or {}).items():
        for t in tickers or []:
            ticker = _norm(t)
            if not ticker or ticker in seen:   # one ticker = one subject (order ids / report paths)
                continue
            seen.add(ticker)
            out.append({"ticker": ticker, "kind": "candidate", "asset_type": "stock", "sector": sector,
                        "sector_class": common.sector_class(sector, "stock"), "position_pct": 0.0,
                        "price_ticker": ticker, "classification_source": "screen"})
    return out


def _sector_order(sector: str) -> int:
    order = list(common.SECTOR_KEYS) + [FUNDS]
    return order.index(sector) if sector in order else len(order)


def _group_by_sector(subjects: Iterable[Mapping[str, Any]]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for sub in subjects:
        groups.setdefault(str(sub.get("sector")), []).append(dict(sub))
    return {s: groups[s] for s in sorted(groups, key=_sector_order)}


def _subject_reports(ctx: RunContext, phase: int, sub: Mapping[str, Any], *,
                     light_only: bool = False) -> tuple[list[tuple[str, Path | None]], tuple[bool, Path | None]]:
    """``([(role, resolved path|None)], (da_expected, da path|None))`` for one ticker."""
    ticker = str(sub["ticker"])
    if phase == 2:
        pairs = [("p2.wave_a", r) for r in _wave_a_roles(ctx, sub)]
        da_step: str | None = "p2.wave_b" if _da_applies(ctx, sub) else None
    else:
        pairs = [("p3.light", r) for r in _light_roles(ctx)]
        da_step = None
        if not light_only:
            pairs += [("p3.full", r) for r in _full_roles(ctx)]
            da_step = "p3.da"
    reports = [(role, _find_output(ctx, step, role, _report_path(ctx, phase, role, ticker), ticker=ticker))
               for step, role in pairs]
    da_path = None
    if da_step:
        da_path = _find_output(ctx, da_step, "devils_advocate",
                               _report_path(ctx, phase, "devils_advocate", ticker), ticker=ticker)
    return reports, (da_step is not None, da_path)


# =====================================================================================
# Order specs
# =====================================================================================


def _seed_sources(paths: Iterable[Path]) -> list[dict]:
    """Dataset sources copied from the envelopes (script outputs) among ``paths``. A source
    without ``path`` gets the data file it came from."""
    out: list[dict] = []
    for p in paths:
        if p.suffix.lower() != ".json" or not p.is_file() or _is_stub_path(p):
            continue
        doc = _read(p)
        if not isinstance(doc, dict) or not isinstance(doc.get("schema"), str):
            continue  # agent reports carry schema_version, not an envelope
        for s in doc.get("sources") or []:
            if not isinstance(s, Mapping) or not s.get("id") or not s.get("url"):
                continue
            src = dict(s)
            if not src.get("path"):
                src["path"] = _posix(p)
            out.append(src)
    return common.dedupe_sources(out)


def _required_fields(ctx: RunContext, role: str, sector_class: str | None) -> list[str]:
    table = _cfg_get(ctx.cfg, f"evidence.required_fields.{role}", None)
    if not isinstance(table, Mapping):
        return []
    sc = sector_class or "default"
    fields = table.get(sc) if sc in table else table.get("default")
    return [str(f) for f in fields or []]


def _word_limits(ctx: RunContext, role: str, mode: str | None) -> dict[str, int]:
    key = f"cio:{mode or 'allocation'}" if role == "cio" else role
    limits = _cfg_get(ctx.cfg, "validation.word_limits", {}) or {}
    return {k: int(limits[k]) for k in ROLE_WORD_LIMIT_KEYS.get(key, ()) if limits.get(k) is not None}


#: Work-order note for a ticker whose classification carries the ``conglomerate`` flag.
CONGLOMERATE_NOTE = ("flagged conglomerate: judge it as an insurance/holding company, see the financials "
                     "playbook (float, look-through earnings and capital allocation rather than "
                     "single-segment multiples).")


def _ticker_flags(ctx: RunContext, ticker: str) -> list[str]:
    """Classification flags of ``ticker`` from the manifest holdings, the holding-trial candidate,
    Phase 1 look-through and ``etf_overrides.yaml`` (de-duplicated, order kept)."""
    t = _norm(ticker)
    flags: list[str] = []
    if t is None:
        return flags

    def add(raw: Any) -> None:
        for f in raw or []:
            if isinstance(f, str) and f not in flags:
                flags.append(f)

    for h in ctx.manifest.get("holdings") or []:
        if isinstance(h, Mapping) and _norm(h.get("ticker")) == t:
            add(h.get("flags"))
    trial = ctx.manifest.get("trial_candidate")
    if isinstance(trial, Mapping) and _norm(trial.get("ticker")) == t:
        add(trial.get("flags"))
    lk = _read(ctx.p("phase1", "lookthrough.json"))
    for h in (lk.get("holdings") if isinstance(lk, Mapping) else None) or []:
        if isinstance(h, Mapping) and _norm(h.get("ticker")) == t:
            add(h.get("flags"))
    sec = ctx.overrides.get("securities") if isinstance(ctx.overrides, Mapping) else None
    if isinstance(sec, Mapping):
        for key, entry in sec.items():
            if _norm(key) == t and isinstance(entry, Mapping):
                add(entry.get("flags"))
    return flags


def _conversion_note(ctx: RunContext, ticker: str, price_ticker: str | None = None) -> str | None:
    """Note for a ticker whose prices.json entry carries a ``native`` block (the series was
    converted to USD at spot USD/ILS, ``converted_to_usd`` flag)."""
    prices = _prices_doc(ctx)
    table = prices.get("tickers") if isinstance(prices.get("tickers"), Mapping) else {}
    for key in _dedupe([price_ticker, ticker]):
        entry = (table or {}).get(key) if key else None
        native = entry.get("native") if isinstance(entry, Mapping) else None
        if not isinstance(native, Mapping):
            continue
        ccy = str(native.get("currency") or "ILS")
        detail = []
        last = common.fact_value(native.get("last_close"))
        if last is not None:
            when = common.date_str(native["last_close"].get("as_of")) if isinstance(native["last_close"], Mapping) else None
            detail.append(f"{last:g} {ccy}" + (f" as of {when}" if when else ""))
        usdils = common.safe_float(native.get("usdils"))
        if usdils is not None:
            detail.append(f"spot USD/ILS {usdils:g}")
        return (f"{ticker}: price series is converted from {ccy} to USD at spot USD/ILS; native {ccy} price "
                f"in prices.json native.last_close" + (f" ({'; '.join(detail)})" if detail else "") +
                ". Technical and valuation figures in the data files are USD.")
    return None


def _ticker_notes(ctx: RunContext, ticker: str | None, price_ticker: str | None = None) -> list[str]:
    """Data-quirk notes every order about ``ticker`` carries (conglomerate flag, USD conversion)."""
    if not ticker:
        return []
    if price_ticker is None:
        for h in ctx.manifest.get("holdings") or []:
            if isinstance(h, Mapping) and _norm(h.get("ticker")) == _norm(ticker):
                price_ticker = h.get("price_ticker")
    notes: list[str] = []
    if "conglomerate" in _ticker_flags(ctx, ticker):
        notes.append(f"{ticker}: {CONGLOMERATE_NOTE}")
    conv = _conversion_note(ctx, ticker, price_ticker)
    if conv:
        notes.append(conv)
    return notes


def _spec(ctx: RunContext, *, order_id: str, step: str, role: str, phase: int,
          inputs: Iterable[tuple[Path, str]], output: Path, mode: str | None = None,
          ticker: str | None = None, sector: str | None = None, sector_class: str | None = None,
          notes: Iterable[str] = ()) -> dict:
    seen: set[str] = set()
    ins: list[dict] = []
    paths: list[Path] = []
    missing: list[str] = []
    for path, purpose in inputs:
        key = _posix(path)
        if key in seen:
            continue
        seen.add(key)
        if not Path(path).is_file():          # e.g. a per-ticker data file whose collection failed
            missing.append(Path(path).name)
            continue
        ins.append({"path": key, "purpose": purpose})
        paths.append(Path(path))
    notes = [n for n in notes if n] + [n for n in _ticker_notes(ctx, ticker) if n not in notes]
    if missing:
        notes.append(f"Unavailable input(s), not produced in this run: {', '.join(missing)}. Do not invent "
                     "their figures; list the gap in missing_data.")
    schema_name = common.role_schema(role, mode)
    return {
        "order_id": order_id,
        "step": step,
        "agent_type": common.AGENT_TYPES[role],
        "agent_role": role,
        "mode": mode,
        "ticker": ticker,
        "sector": sector,
        "sector_class": sector_class,
        "phase": phase,
        "inputs": ins,
        "output": _posix(output),
        "schema": _posix(common.schema_path(schema_name)),
        "schema_name": schema_name,
        "seed_sources": _seed_sources(paths),
        "required_fields": _required_fields(ctx, role, sector_class),
        "word_limits": _word_limits(ctx, role, mode),
        "notes": notes,
    }


#: The data file a lens cannot work without: its order is skipped (the aggregate then counts
#: the role as missing) instead of dispatching an agent that is bound to fail twice.
LENS_CORE_INPUT: dict[str, str] = {"technical": "technicals", "fundamental": "fundamentals"}


def _lens_orders(ctx: RunContext, *, step: str, phase: int, subjects: Iterable[Mapping[str, Any]],
                 roles_for: Callable[[Mapping[str, Any]], list[str]], id_prefix: str,
                 note_for: Callable[[Mapping[str, Any]], str]) -> list[dict]:
    """Specialist orders (one per subject x role); a lens whose core data file is missing is
    skipped and logged."""
    specs: list[dict] = []
    skipped: list[str] = []
    for sub in subjects:
        ticker, sector = str(sub["ticker"]), str(sub["sector"])
        for role in roles_for(sub):
            core = LENS_CORE_INPUT.get(role)
            if core and not _data_path(ctx, phase, core, ticker).is_file():
                skipped.append(f"{role}:{ticker} (no {core})")
                continue
            specs.append(_spec(
                ctx, order_id=f"{id_prefix}-{role}-{ticker}", step=step, role=role, phase=phase,
                ticker=ticker, sector=sector, sector_class=sub.get("sector_class"),
                inputs=_specialist_inputs(ctx, role, phase, ticker, sector),
                output=_report_path(ctx, phase, role, ticker), notes=[note_for(sub)]))
    if skipped:
        ctx.log(f"{step}: orders skipped, core data missing: {', '.join(skipped)}")
    return specs


def _specialist_inputs(ctx: RunContext, role: str, phase: int, ticker: str, sector: str) -> list[tuple[Path, str]]:
    """Lens-specific, minimal inputs (the technical lens gets ONLY its technicals file)."""
    def data(kind: str) -> tuple[Path, str]:
        return _data_path(ctx, phase, kind, ticker), INPUT_PURPOSE[kind]

    playbook = (_playbook(sector), INPUT_PURPOSE["etf_playbook" if sector == FUNDS else "playbook"])
    if role == "technical":
        return [data("technicals")]
    if role == "fundamental":
        return [data("fundamentals"), data("valuation"), playbook]
    if role == "etf":
        return [(ctx.p("phase1", "lookthrough.json"), INPUT_PURPOSE["lookthrough"]),
                data("technicals"),
                (ctx.p("phase1", "prices.json"), INPUT_PURPOSE["prices"]),
                (_input_yaml(ctx, "investor_profile"), INPUT_PURPOSE["investor_profile"]),
                (_playbook(FUNDS), INPUT_PURPOSE["etf_playbook"])]
    # operator_ceo / industry_ops / osint (and any other lens): fundamentals + playbook
    return [data("fundamentals"), playbook]


def _subject_note(sub: Mapping[str, Any]) -> str:
    if sub.get("kind") == "holding":
        pos = common.safe_float(sub.get("position_pct"))
        where = f" ({pos:.2f}% of the portfolio)" if pos is not None else ""
        return f"Current holding{where}; asset type {sub.get('asset_type')}."
    return "Candidate (not held): reviewed under candidate rules (BUY/PASS)."


def _orders_p1_macro(ctx: RunContext) -> list[dict]:
    # DESIGN §7: macro reads macro.json + news (+ the playbooks' macro sensitivities). The
    # portfolio look-through is the CIO's input only: the macro lens must not anchor its sector
    # bounds to the current holdings.
    inputs = [(ctx.p("phase1", "macro.json"), INPUT_PURPOSE["macro"])]
    inputs += [(_playbook(s), f"{common.sector_display(s)} sector playbook (macro sensitivities)")
               for s in common.SECTOR_KEYS]
    notes = ["Set sector_constraints (min/max weight, percent 0-100) for the GICS sectors you have a "
             "view on; scenario probabilities must sum to 1."]
    return [_spec(ctx, order_id="p1-macro", step="p1.macro", role="macro", phase=1, inputs=inputs,
                  output=ctx.p("phase1", "macro_view.json"), notes=notes)]


def _orders_p1_cio(ctx: RunContext) -> list[dict]:
    max_expand = int(_cfg_get(ctx.cfg, "phase3.max_expand_sectors", DEFAULT_MAX_EXPAND_SECTORS))
    enabled = list(_cfg_get(ctx.cfg, "enabled_sectors", list(common.SECTOR_KEYS)))
    inputs: list[tuple[Path, str]] = [(ctx.p("phase1", "lookthrough.json"), INPUT_PURPOSE["lookthrough"])]
    macro_view = _p1_output(ctx, "macro")
    notes = [f"expand_sectors may list at most {max_expand} sectors (phase3.max_expand_sectors); "
             "any extra sector is dropped by the orchestrator.",
             f"Sectors enabled for Phase 3 stock picking: {', '.join(enabled)}."]
    if _usable(macro_view):
        inputs.append((macro_view, INPUT_PURPOSE["macro_view"]))
    else:
        notes.append("The macro view is unavailable (the macro agent failed): set targets without macro "
                     "bounds and list the gap in missing_data.")
    inputs += [(_input_yaml(ctx, "investor_profile"), INPUT_PURPOSE["investor_profile"]),
               (_input_yaml(ctx, "portfolio"), INPUT_PURPOSE["portfolio"])]
    mode = ctx.mode
    if mode["sectors"]:
        notes.append(f"This run limits Phase 3 to: {', '.join(mode['sectors'])}.")
    if mode["quick"] or mode["holding"]:
        notes.append("Phase 3 (stock picking) does not run in this mode; expand_sectors are recorded only.")
    return [_spec(ctx, order_id="p1-cio", step="p1.cio", role="cio", mode="allocation", phase=1,
                  inputs=inputs, output=ctx.p("phase1", "cio_allocation.json"), notes=notes)]


def _orders_p2_wave_a(ctx: RunContext) -> list[dict]:
    return _lens_orders(ctx, step="p2.wave_a", phase=2, subjects=_p2_subjects(ctx),
                        roles_for=lambda sub: _wave_a_roles(ctx, sub), id_prefix="p2-a", note_for=_subject_note)


def _da_spec(ctx: RunContext, *, phase: int, sub: Mapping[str, Any], order_id: str, step: str,
             reports: list[tuple[str, Path | None]]) -> dict:
    ticker, sector = str(sub["ticker"]), str(sub["sector"])
    inputs: list[tuple[Path, str]] = []
    failed: list[str] = []
    for role, path in reports:
        if _usable(path):
            inputs.append((path, f"{role} report on {ticker} (attack its strongest claims)"))  # type: ignore[arg-type]
        else:
            failed.append(role)
    inputs.append((_data_path(ctx, phase, "technicals", ticker), INPUT_PURPOSE["technicals"]))
    if sub.get("asset_type") == "fund":
        inputs.append((ctx.p("phase1", "lookthrough.json"), INPUT_PURPOSE["lookthrough"]))
    else:
        inputs.append((_data_path(ctx, phase, "fundamentals", ticker), INPUT_PURPOSE["fundamentals"]))
        inputs.append((_data_path(ctx, phase, "valuation", ticker), INPUT_PURPOSE["valuation"]))
    inputs.append((_playbook(sector), INPUT_PURPOSE["etf_playbook" if sector == FUNDS else "playbook"]))
    notes = [_subject_note(sub)]
    if failed:
        notes.append(f"No usable report from: {', '.join(failed)} (failed or missing); do not invent their views.")
    return _spec(ctx, order_id=order_id, step=step, role="devils_advocate", phase=phase, ticker=ticker,
                 sector=sector, sector_class=sub.get("sector_class"), inputs=inputs,
                 output=_report_path(ctx, phase, "devils_advocate", ticker), notes=notes)


def _orders_p2_wave_b(ctx: RunContext) -> list[dict]:
    specs: list[dict] = []
    for sub in _p2_subjects(ctx):
        if not _da_applies(ctx, sub):
            continue
        reports, _ = _subject_reports(ctx, 2, sub)
        specs.append(_da_spec(ctx, phase=2, sub=sub, order_id=f"p2-b-devils_advocate-{sub['ticker']}",
                              step="p2.wave_b", reports=reports))
    return specs


def _sector_head_spec(ctx: RunContext, phase: int, sector: str, subs: list[dict]) -> dict:
    inputs: list[tuple[Path, str]] = [(_packet_path(ctx, phase, sector), INPUT_PURPOSE["packet"])]
    failed: list[str] = []
    for sub in subs:
        reports, (_expected, da) = _subject_reports(ctx, phase, sub)
        for role, path in reports + ([("devils_advocate", da)] if _expected else []):
            if _usable(path):
                inputs.append((path, f"{role} report on {sub['ticker']}"))  # type: ignore[arg-type]
            else:
                failed.append(f"{role}:{sub['ticker']}")
    inputs.append((_playbook(sector), INPUT_PURPOSE["etf_playbook" if sector == FUNDS else "playbook"]))
    tickers = ", ".join(s["ticker"] for s in subs)
    min_words = int(_cfg_get(ctx.cfg, "validation.min_justification_words", DEFAULT_MIN_JUSTIFICATION_WORDS))
    if phase == 2:
        notes = [f"Phase 2 holdings review for {tickers}: actions ADD|HOLD|TRIM|SELL "
                 "(a non-held trial ticker is a candidate: BUY|PASS)."]
    else:
        max_buys = int(_cfg_get(ctx.cfg, "phase3.max_buys_per_sector", DEFAULT_MAX_BUYS_PER_SECTOR))
        notes = [f"Phase 3 candidates {tickers}: actions BUY|PASS; at most {max_buys} BUY in this sector; "
                 "zero BUYs is a valid outcome. Every BUY needs thesis, fair values and invalidation price."]
    notes.append(f"An action different from rule_action needs rule_deviation=true and a justification of at "
                 f"least {min_words} words; copy rule_action, agg values and rr from the packet.")
    if failed:
        notes.append(f"Failed or missing reports (scored evidence 1): {', '.join(failed)}.")
    for sub in subs:
        notes += [n for n in _ticker_notes(ctx, str(sub["ticker"]), sub.get("price_ticker")) if n not in notes]
    sc = "fund" if sector == FUNDS else common.sector_class(sector, "stock")
    return _spec(ctx, order_id=f"p{phase}-sh-{sector}", step=f"p{phase}.sector_heads", role="sector_head",
                 mode=f"phase{phase}", phase=phase, sector=sector, sector_class=sc, inputs=inputs,
                 output=_verdict_path(ctx, phase, sector), notes=notes)


def _orders_p2_sector_heads(ctx: RunContext) -> list[dict]:
    return [_sector_head_spec(ctx, 2, sector, subs)
            for sector, subs in _group_by_sector(_p2_subjects(ctx)).items()]


def _orders_p3_light(ctx: RunContext) -> list[dict]:
    return _lens_orders(ctx, step="p3.light", phase=3, subjects=_p3_subjects(ctx, "candidates"),
                        roles_for=lambda _sub: _light_roles(ctx), id_prefix="p3-light",
                        note_for=lambda sub: _subject_note(sub) + " Light wave: the top names go on to the "
                                                                  "full review.")


def _orders_p3_full(ctx: RunContext) -> list[dict]:
    return _lens_orders(ctx, step="p3.full", phase=3, subjects=_p3_subjects(ctx, "finalists"),
                        roles_for=lambda _sub: _full_roles(ctx), id_prefix="p3-full", note_for=_subject_note)


def _orders_p3_da(ctx: RunContext) -> list[dict]:
    specs: list[dict] = []
    for sub in _p3_subjects(ctx, "finalists"):
        reports, _ = _subject_reports(ctx, 3, sub)
        specs.append(_da_spec(ctx, phase=3, sub=sub, order_id=f"p3-da-{sub['ticker']}", step="p3.da",
                              reports=reports))
    return specs


def _orders_p3_sector_heads(ctx: RunContext) -> list[dict]:
    return [_sector_head_spec(ctx, 3, sector, subs)
            for sector, subs in _group_by_sector(_p3_subjects(ctx, "finalists")).items() if subs]


def _all_verdict_paths(ctx: RunContext) -> list[Path]:
    """Resolved, usable sector verdicts of Phase 2 and Phase 3."""
    out: list[Path] = []
    for phase, subs in ((2, _p2_subjects(ctx)), (3, _p3_subjects(ctx, "finalists"))):
        for sector in _group_by_sector(subs):
            path = _find_output(ctx, f"p{phase}.sector_heads", "sector_head",
                                _verdict_path(ctx, phase, sector), sector=sector)
            if _usable(path):
                out.append(path)  # type: ignore[arg-type]
    return out


def _orders_p4_cio(ctx: RunContext) -> list[dict]:
    inputs: list[tuple[Path, str]] = [(ctx.p("phase4", "cio_packet.json"), INPUT_PURPOSE["cio_packet"])]
    inputs += [(p, f"sector verdict ({p.name})") for p in _all_verdict_paths(ctx)]
    notes: list[str] = []
    preview = ctx.p("phase4", "portfolio_preview.json")
    if preview.is_file():
        inputs.append((preview, INPUT_PURPOSE["portfolio_preview"]))
    else:
        notes.append("The preview rebalancing plan is unavailable; reconcile from the verdicts and the packet.")
    for role, label in (("cio", "cio_allocation"), ("macro", "macro_view")):
        path = _p1_output(ctx, role)
        if _usable(path):
            inputs.append((path, INPUT_PURPOSE[label]))  # type: ignore[arg-type]
        else:
            notes.append(f"{label} is unavailable (agent failed); reconcile from the remaining inputs.")
    # The final judgment is made for a specific person: horizon, risk tolerance and income context.
    inputs.append((_input_yaml(ctx, "investor_profile"), INPUT_PURPOSE["investor_profile"]))
    max_items = _cfg_get(ctx.cfg, "validation.max_executive_summary_items", None)
    max_risks = _cfg_get(ctx.cfg, "validation.max_top_risks", None)
    if max_items is not None:
        notes.append(f"executive_summary: at most {max_items} items; top_risks: at most {max_risks}.")
    notes.append("Address every conflict listed in the CIO packet in reconciliation[]; an action different "
                 "from the sector verdict needs override=true and a justification.")
    return [_spec(ctx, order_id="p4-cio", step="p4.cio", role="cio", mode="reconciliation", phase=4,
                  inputs=inputs, output=ctx.p("phase4", "cio_final.json"), notes=notes)]


_ORDER_BUILDERS: dict[str, Callable[[RunContext], list[dict]]] = {
    "p1.macro": _orders_p1_macro,
    "p1.cio": _orders_p1_cio,
    "p2.wave_a": _orders_p2_wave_a,
    "p2.wave_b": _orders_p2_wave_b,
    "p2.sector_heads": _orders_p2_sector_heads,
    "p3.light": _orders_p3_light,
    "p3.full": _orders_p3_full,
    "p3.da": _orders_p3_da,
    "p3.sector_heads": _orders_p3_sector_heads,
    "p4.cio": _orders_p4_cio,
}


def build_orders(name: str, ctx: RunContext) -> list[dict]:
    """Work-order specs for agents step ``name``. Keys: order_id, step, agent_type, agent_role,
    mode, ticker, sector, sector_class, phase, inputs [{path, purpose}] (absolute posix paths),
    output, schema (absolute path), schema_name, seed_sources, required_fields, word_limits,
    notes. An empty list means there is nothing to dispatch."""
    builder = _ORDER_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"{name!r} is not an agents step; agents steps: {sorted(_ORDER_BUILDERS)}")
    return builder(ctx)


def describe_orders(name: str, specs: list[dict]) -> str:
    """One-line summary of a batch, e.g. ``p2.wave_a: 14 orders (KO x4, QQQ x2)``."""
    per: dict[str, int] = {}
    for s in specs:
        key = s.get("ticker") or s.get("sector") or s.get("agent_role")
        per[str(key)] = per.get(str(key), 0) + 1
    detail = ", ".join(f"{k} x{v}" if v > 1 else k for k, v in per.items())
    return f"{name}: {len(specs)} orders" + (f" ({detail})" if detail else "")


# =====================================================================================
# Validation context
# =====================================================================================


_PER_TICKER_CTX = ("technicals", "fundamentals", "valuation")


def validation_context(ctx: RunContext, spec: Mapping[str, Any]) -> dict:
    """Map an order's inputs to the ``context`` keys ``validate.validate_doc`` expects:
    technicals, fundamentals, valuation (the order ticker's file), aggregate (sector heads: the
    phase's aggregate.json), macro, macro_view, lookthrough, cio_allocation, prices, verdicts
    (list) and committee_cfg (the run's inputs/committee.yaml) — posix path strings; failure
    stubs never enter the context. Also carries what ``validate.build_order_context`` adds:
    strict_refs, word_limits, seed_sources, expect (order identity) and run_dir."""
    out: dict[str, Any] = {}
    verdicts: list[str] = []
    ticker = _norm(spec.get("ticker"))
    for item in spec.get("inputs") or []:
        raw = item.get("path") if isinstance(item, Mapping) else item
        if not raw:
            continue
        path = Path(str(raw))
        if _is_stub_path(path) or not path.name.endswith(".json"):
            continue
        stem = path.name[: -len(".json")]
        if stem.endswith(".r2"):                       # macro_view.r2.json -> macro_view
            stem = stem[: -len(".r2")]
        key: str | None = None
        kind, _sep, file_ticker = stem.partition("_")
        if kind in _PER_TICKER_CTX and file_ticker:
            if ticker and _norm(file_ticker) == ticker:
                out[kind] = path.as_posix()            # the order's own ticker always wins
                continue
            key = kind
        elif stem in ("macro", "macro_view", "lookthrough", "cio_allocation", "prices"):
            key = stem
        elif stem.endswith("_verdict"):
            verdicts.append(path.as_posix())
            continue
        if key and key not in out:
            out[key] = path.as_posix()
    if spec.get("agent_role") == "sector_head":
        phase = spec.get("phase") or (3 if spec.get("mode") == "phase3" else 2)
        out["aggregate"] = ctx.p(f"phase{phase}", "aggregate.json").as_posix()
    if verdicts:
        out["verdicts"] = verdicts
    snap = ctx.p("inputs", "committee.yaml")
    out["committee_cfg"] = (snap if snap.is_file() else common.config_dir() / "committee.yaml").as_posix()
    out["strict_refs"] = True
    out["word_limits"] = dict(spec.get("word_limits") or {})
    out["seed_sources"] = list(spec.get("seed_sources") or [])
    out["expect"] = {"order_id": spec.get("order_id"), "run_id": ctx.run_id, "agent_role": spec.get("agent_role"),
                     "ticker": spec.get("ticker"), "sector": spec.get("sector"), "mode": spec.get("mode")}
    out["run_dir"] = ctx.run_dir.as_posix()
    return out


# =====================================================================================
# Python steps — shared pieces
# =====================================================================================


def _sector_etf(ctx: RunContext, sector: str) -> str | None:
    etfs = ctx.cfg.get("sector_etfs") if isinstance(ctx.cfg.get("sector_etfs"), Mapping) else {}
    return (etfs or {}).get(sector) or common.sector_etf(sector)


def _prices_doc(ctx: RunContext) -> dict:
    doc = _read(ctx.p("phase1", "prices.json"))
    return doc if isinstance(doc, dict) else {}


def _fact_date(fact: Mapping[str, Any] | None) -> Any:
    try:
        return common.parse_date((fact or {}).get("as_of"))
    except (TypeError, ValueError):
        return None


def _price_fact(ctx: RunContext, sub: Mapping[str, Any], phase: int, prices: Mapping[str, Any] | None = None,
                tech_doc: Mapping[str, Any] | None = None) -> dict | None:
    """Latest close Fact: the NEWER of the prices.json close (proxy ticker when set) and this
    run's technicals close (ties -> prices.json). A Phase 1 reused by a holding trial can be up
    to ``phase1_reuse_days`` old, while the technicals are computed in this run."""
    prices = prices if prices is not None else _prices_doc(ctx)
    table = prices.get("tickers") if isinstance(prices.get("tickers"), Mapping) else {}
    fallback: dict | None = None
    snap: dict | None = None
    for key in _dedupe([sub.get("price_ticker"), sub.get("ticker")]):
        entry = (table or {}).get(key) if key else None
        if isinstance(entry, Mapping) and common.is_fact(entry.get("last_close")):
            fact = dict(entry["last_close"])
            if common.fact_value(fact) is not None:
                snap = fact
                break
            fallback = fallback or fact
    tech_fact: dict | None = None
    tech = _read(_data_path(ctx, phase, "technicals", str(sub["ticker"])))
    if not isinstance(tech, Mapping):
        tech = tech_doc                       # the technicals just computed (not on disk)
    if isinstance(tech, Mapping) and common.is_fact(tech.get("close")):
        fact = dict(tech["close"])
        if common.fact_value(fact) is not None:
            tech_fact = fact
    if snap is not None and tech_fact is not None:
        d_snap, d_tech = _fact_date(snap), _fact_date(tech_fact)
        if d_tech is not None and (d_snap is None or d_tech > d_snap):
            return tech_fact
        return snap
    return snap or tech_fact or fallback


def _collect_stock_data(ctx: RunContext, phase: int, sub: Mapping[str, Any], prices: Mapping[str, Any] | None,
                        warnings: list[str]) -> list[Path]:
    """technicals + fundamentals + valuation for one stock (idempotent: existing files kept).
    The price handed to fetch_fundamentals is the newest close (``_price_fact``: prices.json vs
    this run's technicals), resolved AFTER the technicals exist. Per-ticker failures become
    warnings."""
    tech_mod, fund_mod, val_mod = _mod("compute_technicals"), _mod("fetch_fundamentals"), _mod("valuation")
    ticker, sector = str(sub["ticker"]), str(sub["sector"])
    price_ticker = str(sub.get("price_ticker") or ticker)
    outputs: list[Path] = []

    tech_path = _data_path(ctx, phase, "technicals", ticker)
    tech: Any = None
    if tech_path.is_file():
        outputs.append(tech_path)
    else:
        try:
            tech = tech_mod.run(price_ticker, tech_path, sector_etf=_sector_etf(ctx, sector), as_of=ctx.as_of)
            outputs.append(tech_path)
        except Exception as exc:  # noqa: BLE001 - one ticker must not sink the step
            warnings.append(f"{ticker}: technicals failed: {exc}")
    # pass the price FACT (not a bare float): fetch_fundamentals then stamps the price with the
    # last bar date (e.g. Friday) instead of the run date (a Sunday). A null Fact is not passed, so
    # fetch_fundamentals keeps its fallback to the cached close.
    price_fact = _price_fact(ctx, sub, phase, prices if prices is not None else {},
                             tech_doc=tech if isinstance(tech, Mapping) else None)
    price = price_fact if common.fact_value(price_fact) is not None else None

    fund_path = _data_path(ctx, phase, "fundamentals", ticker)
    fund_ok = fund_path.is_file()
    if fund_ok:
        outputs.append(fund_path)
    else:
        try:
            fund_mod.run(ticker, fund_path, price=price, as_of=ctx.as_of, sector_class=sub.get("sector_class"))
            fund_ok = True
            outputs.append(fund_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{ticker}: fundamentals failed: {exc}")

    val_path = _data_path(ctx, phase, "valuation", ticker)
    if val_path.is_file():
        outputs.append(val_path)
    elif fund_ok:
        try:
            val_mod.run(ticker, fund_path, val_path, sector=sector, as_of=ctx.as_of)
            outputs.append(val_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{ticker}: valuation failed: {exc}")
    return outputs


def _collect_fund_data(ctx: RunContext, phase: int, sub: Mapping[str, Any], warnings: list[str]) -> list[Path]:
    ticker = str(sub["ticker"])
    tech_path = _data_path(ctx, phase, "technicals", ticker)
    if tech_path.is_file():
        return [tech_path]
    try:
        _mod("compute_technicals").run(str(sub.get("price_ticker") or ticker), tech_path, sector_etf=None,
                                       as_of=ctx.as_of)
        return [tech_path]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{ticker}: technicals failed: {exc}")
        return []


def _context_for(ctx: RunContext, sub: Mapping[str, Any], lookthrough: Mapping[str, Any] | None,
                 cio: Mapping[str, Any] | None) -> dict:
    fund = sub.get("asset_type") == "fund"
    cap_key = "max_fund_position_pct" if fund else "max_position_pct"
    sector = sub.get("sector")
    weights = (lookthrough or {}).get("sector_weights_pct") or {}
    targets = (cio or {}).get("target_sector_weights") or {}
    position = common.safe_float(sub.get("position_pct"))
    if position is None and sub.get("kind") == "candidate":
        position = 0.0
    return {
        "position_pct": position,
        "cap_pct": common.safe_float(ctx.profile.get(cap_key)),
        "sector_lookthrough_pct": None if fund else common.fact_value(weights.get(sector)),
        "cio_target_pct": None if fund else common.safe_float(targets.get(sector)),
        # from the run's profile snapshot (aggregate's limit-breach test reads it from context)
        "max_sector_pct": common.safe_float(ctx.profile.get("max_sector_pct")),
    }


def _risk_digest(doc: Mapping[str, Any]) -> list[dict]:
    risks = [r for r in doc.get("risks") or [] if isinstance(r, Mapping)]
    risks.sort(key=lambda r: SEVERITY_RANK.get(str(r.get("severity")), 3))
    return [{"point": r.get("point"), "severity": r.get("severity")} for r in risks[:PACKET_TOP_RISKS]]


def _flag_digest(role: str, doc: Mapping[str, Any]) -> list[dict]:
    out = []
    for f in doc.get("red_flags") or []:
        if not isinstance(f, Mapping):
            continue
        out.append({"flag_id": f"{role}:{f.get('id')}", "flag": f.get("flag"), "severity": f.get("severity"),
                    "must_address": f.get("must_address")})
    return out


def _report_digest(role: str, path: Path | None, evidence: Mapping[str, Any]) -> dict:
    ev_reports = evidence.get("reports") if isinstance(evidence.get("reports"), Mapping) else {}
    key = _posix(path) if path is not None else None
    score = ((ev_reports or {}).get(key) or {}).get("score") if key else None
    if path is None:
        return {"role": role, "path": None, "status": "missing", "evidence": score}
    doc = _read(path)
    if not isinstance(doc, Mapping):
        return {"role": role, "path": key, "status": "unreadable", "evidence": score}
    if _is_stub(path, doc):
        errors = [str(e) for e in (doc.get("errors") or [])][:PACKET_REPORT_ERRORS]
        return {"role": role, "path": key, "status": "failed", "evidence": score, "errors": errors}
    out = {"role": role, "path": key, "status": "ok", "evidence": score, "stance": doc.get("stance"),
           "conviction": doc.get("conviction"), "thesis": doc.get("thesis"), "top_risks": _risk_digest(doc),
           "red_flags": _flag_digest(role, doc), "invalidation_price": doc.get("invalidation_price")}
    if role == "etf":
        out["action"] = doc.get("action")
        out["role_in_portfolio"] = doc.get("role_in_portfolio")
    if role == "fundamental":
        val = common.get_dotted(doc, "extensions.valuation", {}) or {}
        out["fair_value"] = {k: val.get(f"fair_value_{k}") for k in ("low", "base", "high")}
    return out


def _da_digest(path: Path | None, expected: bool, evidence: Mapping[str, Any]) -> dict | None:
    if not expected:
        return None
    base = _report_digest("devils_advocate", path, evidence)
    if base["status"] != "ok":
        return {k: base[k] for k in base if k in ("path", "status", "evidence", "errors")} | {"verdict": None}
    doc = _read(path) or {}
    attacks = [a for a in doc.get("attacks") or [] if isinstance(a, Mapping)]
    attacks.sort(key=lambda a: SEVERITY_RANK.get(str(a.get("severity")), 3))
    return {
        "path": base["path"], "status": "ok", "evidence": base["evidence"],
        "verdict": doc.get("verdict_on_thesis"), "verdict_reason": doc.get("verdict_reason"),
        "bear_case_price": doc.get("bear_case_price"), "bear_case_ref": doc.get("bear_case_ref"),
        "top_attacks": [{"target_agent": a.get("target_agent"), "target_claim": a.get("target_claim"),
                         "counter_evidence": a.get("counter_evidence"), "severity": a.get("severity")}
                        for a in attacks[:PACKET_TOP_ATTACKS]],
        "kill_criteria": list(doc.get("kill_criteria") or [])[:PACKET_KILL_CRITERIA],
        "red_flags": _flag_digest("devils_advocate", doc),
    }


def _macro_bounds(macro_view: Mapping[str, Any] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in (macro_view or {}).get("sector_constraints") or []:
        if isinstance(c, Mapping) and c.get("sector"):
            out[str(c["sector"])] = {"min_weight": c.get("min_weight"), "max_weight": c.get("max_weight")}
    return out


def _write_packets(ctx: RunContext, phase: int, subjects: list[dict], aggregate: Mapping[str, Any],
                   evidence: Mapping[str, Any]) -> list[Path]:
    """``phaseN/packets/sector_<s>.json``: per ticker the aggregate entry + report digests + DA."""
    lookthrough = _read(ctx.p("phase1", "lookthrough.json")) or {}
    cio = _p1_doc(ctx, "cio") or {}
    bounds = _macro_bounds(_p1_doc(ctx, "macro"))
    agg_tickers = aggregate.get("tickers") if isinstance(aggregate.get("tickers"), Mapping) else {}
    written: list[Path] = []
    for sector, subs in _group_by_sector(subjects).items():
        entries: list[dict] = []
        data_files: list[Path] = []
        for sub in subs:
            ticker = sub["ticker"]
            reports, (expected, da_path) = _subject_reports(ctx, phase, sub)
            entries.append({
                "ticker": ticker, "kind": sub.get("kind"), "asset_type": sub.get("asset_type"),
                "sector_class": sub.get("sector_class"), "position_pct": sub.get("position_pct"),
                "aggregate": (agg_tickers or {}).get(ticker),
                "reports": [_report_digest(role, path, evidence) for role, path in reports],
                "devils_advocate": _da_digest(da_path, expected, evidence),
            })
            data_files += [_data_path(ctx, phase, k, ticker) for k in ("technicals", "fundamentals", "valuation")]
        if sector == FUNDS:
            data_files.append(ctx.p("phase1", "lookthrough.json"))
        fund = sector == FUNDS
        context = {
            "sector_lookthrough_pct": None if fund else common.fact_value(
                (lookthrough.get("sector_weights_pct") or {}).get(sector)),
            "cio_target_pct": None if fund else common.safe_float(
                (cio.get("target_sector_weights") or {}).get(sector)),
            "macro_bounds": bounds.get(sector),
            "actions": ctx.cfg.get("actions"),
        }
        if phase == 3:
            context["max_buys_per_sector"] = _cfg_get(ctx.cfg, "phase3.max_buys_per_sector",
                                                      DEFAULT_MAX_BUYS_PER_SECTOR)
        doc = common.envelope("sector_packet", ctx.as_of, sources=_seed_sources(data_files),
                              run_id=ctx.run_id, phase=phase, sector=sector,
                              sector_display=common.sector_display(sector), context=context, tickers=entries)
        path = _packet_path(ctx, phase, sector)
        common.write_json(path, doc)
        written.append(path)
    return written


def _aggregate_phase(ctx: RunContext, phase: int, subjects: list[dict], *, evidence_out: Path,
                     aggregate_out: Path, light_only: bool, packets: bool) -> dict:
    """evidence_score.score_reports + aggregate.run for ``subjects`` (+ sector packets)."""
    evidence_score, aggregate = _mod("evidence_score"), _mod("aggregate")
    warnings: list[str] = []
    lookthrough = _read(ctx.p("phase1", "lookthrough.json")) or {}
    cio = _p1_doc(ctx, "cio")
    prices = _prices_doc(ctx)
    ev_items: list[dict] = []
    agg_items: list[dict] = []
    for sub in subjects:
        ticker, sc = sub["ticker"], sub.get("sector_class")
        reports, (da_expected, da_path) = _subject_reports(ctx, phase, sub, light_only=light_only)
        rep_map: dict[str, str] = {}
        for role, path in reports:
            if path is None:
                warnings.append(f"{ticker}: no {role} report found")
                continue
            rep_map[role] = _posix(path)
            ev_items.append({"path": _posix(path), "role": role, "ticker": ticker, "sector_class": sc})
        da = None
        if da_expected:
            if da_path is None:
                warnings.append(f"{ticker}: no devils_advocate report found")
            else:
                da = _posix(da_path)
                ev_items.append({"path": da, "role": "devils_advocate", "ticker": ticker, "sector_class": sc})
        tech = _data_path(ctx, phase, "technicals", ticker)
        agg_items.append({
            "ticker": ticker, "kind": sub.get("kind") or "holding", "asset_type": sub.get("asset_type"),
            "sector": sub.get("sector"), "sector_class": sc, "price": _price_fact(ctx, sub, phase, prices),
            "reports": rep_map, "da_path": da, "technicals_path": _posix(tech) if tech.is_file() else None,
            "context": _context_for(ctx, sub, lookthrough, cio),
        })
    evidence = evidence_score.score_reports(ev_items, evidence_out, run_date=ctx.as_of, cfg=ctx.cfg)
    if not isinstance(evidence, Mapping):
        evidence = _read(evidence_out) or {"reports": {}}
    agg = aggregate.run(agg_items, aggregate_out, evidence=evidence, cfg=ctx.cfg, as_of=ctx.as_of)
    if not isinstance(agg, Mapping):
        agg = _read(aggregate_out) or {"tickers": {}}
    outputs: list[Path] = [evidence_out, aggregate_out]
    if packets:
        outputs += _write_packets(ctx, phase, subjects, agg, evidence)
    actions = _count((e or {}).get("rule_action") for e in (agg.get("tickers") or {}).values())
    return {"evidence": evidence, "aggregate": agg, "outputs": outputs, "warnings": warnings,
            "n_reports": len(ev_items), "rule_actions": actions}


# =====================================================================================
# Python steps
# =====================================================================================


def _p1_price_tickers(ctx: RunContext) -> list[str]:
    """Holdings (proxy_ticker if set; + the trial ticker), benchmark, the 11 sector ETFs, FX."""
    out: list[str] = []
    for h in ctx.portfolio.get("holdings") or []:
        if isinstance(h, Mapping):
            t = _norm(h.get("proxy_ticker")) or _norm(h.get("ticker"))
            if t:
                out.append(t)
    trial = ctx.mode["holding"]
    if trial:
        out.append(trial)
    out.append(str(ctx.cfg.get("benchmark") or DEFAULT_BENCHMARK).strip())
    out += [str(e) for e in (_sector_etf(ctx, s) for s in common.SECTOR_KEYS) if e]
    out.append(str(ctx.cfg.get("fx_ticker") or DEFAULT_FX_TICKER).strip())
    return _dedupe(out)


def _phase1_files_in(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Path] | None:
    """Canonical name -> source file of a run's Phase 1 outputs; None if any is missing/failed."""
    found: dict[str, Path] = {}
    for name in P1_DATA_FILES:
        path = run_dir / "phase1" / name
        if not path.is_file():
            return None
        found[name] = path
    for name, (step, role, canonical) in P1_AGENT_FILES.items():
        path = _find_output_in(run_dir, manifest, step, role, run_dir / "phase1" / canonical)
        if not _usable(path) or not isinstance(_read(path), dict):
            return None
        found[name] = path  # type: ignore[assignment]
    return found


def _config_sha(run_dir: Path, manifest: Mapping[str, Any], name: str, *,
                fallback_dir: Path | None = None) -> str | None:
    """sha256 of a run's ``<name>.yaml``: ``manifest.config_sha256`` (written by committee.py
    init), else a hash of the run's ``inputs/`` snapshot, else (current run only) the active
    config file. None when the file does not exist anywhere."""
    shas = manifest.get("config_sha256") if isinstance(manifest.get("config_sha256"), Mapping) else {}
    value = (shas or {}).get(f"{name}.yaml")
    if isinstance(value, str) and value:
        return value
    for cand in (run_dir / "inputs" / f"{name}.yaml",
                 (fallback_dir / f"{name}.yaml") if fallback_dir is not None else None):
        if cand is not None and cand.is_file():
            return hashlib.sha256(cand.read_bytes()).hexdigest()
    return None


def config_drift(run_dir: str | Path, manifest: Mapping[str, Any]) -> list[dict]:
    """Config files whose LIVE content differs from the snapshot taken when the run started.

    Compares ``manifest["config_sha256"]`` (sha256 of every ``*.yaml`` copied into ``inputs/`` by
    ``committee.py init``) with the same files in ``manifest["paths"]["config_dir"]`` (the config
    dir ``init`` read: the repo ``config/`` for a live run, the fixture config dir for a dry run).
    Agents and scripts always work from the ``inputs/`` snapshot, so a drift means the decisions
    used an older configuration than the one now on disk. Returns ``[{"file","change","message",
    "snapshot_sha256","live_sha256"}]`` (``change`` is ``changed`` or ``deleted``; hashes are the
    first 12 hex digits); ``[]`` when nothing changed or the check is impossible (no recorded
    hashes / config dir gone)."""
    shas = manifest.get("config_sha256") if isinstance(manifest.get("config_sha256"), Mapping) else {}
    paths = manifest.get("paths") if isinstance(manifest.get("paths"), Mapping) else {}
    raw = paths.get("config_dir")
    if not shas or not raw:
        return []
    live = Path(str(raw))
    if not live.is_dir():
        return []
    out: list[dict] = []
    for name in sorted(shas):
        recorded = str(shas[name] or "")
        f = live / name
        if not f.is_file():
            entry = {"file": name, "change": "deleted", "snapshot_sha256": recorded[:12], "live_sha256": None}
        else:
            try:
                now = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                continue
            if now == recorded:
                continue
            entry = {"file": name, "change": "changed", "snapshot_sha256": recorded[:12], "live_sha256": now[:12]}
        entry["message"] = config_drift_message(entry)
        out.append(entry)
    return out


def config_drift_message(entry: Mapping[str, Any]) -> str:
    """Human line for one ``config_drift`` entry (manifest warning, run_log and the memo)."""
    what = "was deleted" if entry.get("change") == "deleted" else "changed"
    return (f"config {entry.get('file')} {what} after this run started — decisions used the snapshot "
            f"in inputs/")


def _config_mismatch(ctx: RunContext, run_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """``REUSE_CONFIGS`` whose content differs between this run and an earlier run (a config the
    earlier run cannot vouch for counts as different)."""
    out: list[str] = []
    for name in REUSE_CONFIGS:
        mine = _config_sha(ctx.run_dir, ctx.manifest, name, fallback_dir=common.config_dir())
        theirs = _config_sha(run_dir, manifest, name)
        if mine != theirs:
            out.append(f"{name}.yaml")
    return out


def _try_reuse_phase1(ctx: RunContext) -> dict:
    """Holding mode: copy Phase 1 from the latest run <= phase1_reuse_days old (same dry-run
    flag, identical ``REUSE_CONFIGS`` — portfolio, profile, committee, ETF overrides — and
    complete, valid Phase 1 outputs). Rejected candidates are listed in ``rejected``."""
    days = int(_cfg_get(ctx.cfg, "phase1_reuse_days", DEFAULT_PHASE1_REUSE_DAYS))
    me = common.parse_date(ctx.as_of)
    best: tuple[tuple, Path, dict, dict[str, Path], int] | None = None
    rejected: list[dict] = []
    root = ctx.run_dir.parent
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not d.is_dir() or d.resolve() == ctx.run_dir:
            continue
        man = _read(d / "manifest.json")
        if not isinstance(man, dict):
            continue
        try:
            other = common.parse_date(man.get("as_of"))
        except ValueError:
            other = None
        if other is None or me is None:
            continue
        age = (me - other).days
        if age < 0 or age > days:
            continue
        if _normalize_mode(man)["dry_run"] != ctx.mode["dry_run"]:
            continue
        rid = str(man.get("run_id") or d.name)
        files = _phase1_files_in(d, man)
        if files is None:
            rejected.append({"run": rid, "reason": "incomplete or failed Phase 1 outputs"})
            continue
        changed = _config_mismatch(ctx, d, man)
        if changed:
            rejected.append({"run": rid, "reason": f"config changed since that run: {', '.join(changed)}"})
            continue
        key = (other.isoformat(), str(man.get("created_at") or ""), d.name)
        if best is None or key > best[0]:
            best = (key, d, man, files, age)
    if best is None:
        reason = f"no earlier run with complete Phase 1 outputs and unchanged config within {days} days"
        if rejected:
            reason += "; " + "; ".join(f"{r['run']}: {r['reason']}" for r in rejected)
        return {"reused": False, "reason": reason, "rejected": rejected}
    _key, src_dir, man, files, age = best
    copied: list[str] = []
    for name, src in files.items():
        dst = ctx.p("phase1", name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(_posix(dst))
    return {"reused": True, "from_run": str(man.get("run_id") or src_dir.name), "from_dir": _posix(src_dir),
            "from_as_of": common.date_str(man.get("as_of")), "age_days": age, "files": copied,
            "rejected": rejected}


def _step_p1_data(ctx: RunContext) -> dict:
    warnings: list[str] = []
    if ctx.mode["holding"]:
        reuse = _try_reuse_phase1(ctx)
        ctx.manifest["phase1_reuse"] = reuse
        if reuse.get("reused"):
            msg = (f"Phase 1 reused from run {reuse['from_run']} (as of {reuse['from_as_of']}, "
                   f"{reuse['age_days']} d old)")
            ctx.log(msg)
            return _result(msg, reuse["files"], warnings, skip_steps=P1_AGENT_STEPS)
        ctx.log(f"Phase 1 not reused: {reuse.get('reason')}")
    fetch_macro, fetch_prices, etf_lookthrough = _mod("fetch_macro"), _mod("fetch_prices"), _mod("etf_lookthrough")
    macro_out = ctx.p("phase1", "macro.json")
    prices_out = ctx.p("phase1", "prices.json")
    lk_out = ctx.p("phase1", "lookthrough.json")
    macro = fetch_macro.run(None, macro_out, as_of=ctx.as_of)
    tickers = _p1_price_tickers(ctx)
    prices = fetch_prices.run(tickers, prices_out, as_of=ctx.as_of, period=_cfg_get(ctx.cfg, "prices.period"))
    if not isinstance(prices, Mapping):
        prices = _read(prices_out) or {}
    failed = prices.get("failed") or {}
    if failed:
        warnings.append(f"prices unavailable for: {', '.join(sorted(map(str, failed)))}")
    lk = etf_lookthrough.run(lk_out, portfolio=ctx.portfolio, prices=prices, as_of=ctx.as_of)
    n_series = len((macro or {}).get("series") or {}) if isinstance(macro, Mapping) else 0
    n_hold = len((lk or {}).get("holdings") or []) if isinstance(lk, Mapping) else 0
    summary = (f"Phase 1 data: {len(tickers)} price series ({len(failed)} failed), "
               f"{n_series} macro series, look-through of {n_hold} holdings")
    return _result(summary, [macro_out, prices_out, lk_out], warnings)


def _classify_holdings(ctx: RunContext, lookthrough: Mapping[str, Any], warnings: list[str]) -> list[dict]:
    """manifest holdings[] from portfolio.yaml + look-through classification."""
    lk_rows: dict[str, Mapping[str, Any]] = {}
    for row in lookthrough.get("holdings") or []:
        if isinstance(row, Mapping) and _norm(row.get("ticker")):
            lk_rows[_norm(row.get("ticker"))] = row  # type: ignore[index]
    cfg_rows = [h for h in ctx.portfolio.get("holdings") or [] if isinstance(h, Mapping) and _norm(h.get("ticker"))]
    listed = {_norm(h.get("ticker")) for h in cfg_rows}
    rows: list[Mapping[str, Any]] = cfg_rows + [r for t, r in lk_rows.items() if t not in listed]
    mode = ctx.mode
    out: list[dict] = []
    for h in rows:
        ticker = _norm(h.get("ticker"))
        proxy = _norm(h.get("proxy_ticker"))
        lk = lk_rows.get(ticker) or (lk_rows.get(proxy) if proxy else None) or {}  # type: ignore[arg-type]
        asset_type = str(lk.get("asset_type") or h.get("asset_type") or "stock")
        fund = asset_type == "fund"
        override = h.get("sector_override")
        sector = FUNDS if fund else str(lk.get("sector") or override or "unclassified")
        sector_class = "fund" if fund else str(lk.get("sector_class") or common.sector_class(sector, asset_type))
        source = lk.get("classification_source") or ("portfolio_sector_override" if override else "unknown")
        entry = {
            "ticker": ticker, "asset_type": asset_type, "sector": sector, "sector_class": sector_class,
            "classification_source": source, "position_pct": common.fact_value(lk.get("weight_pct")),
            "price_ticker": proxy or ticker, "flags": list(lk.get("flags") or []),
            "in_scope": True, "scope_reason": None,
        }
        if mode["holding"] and ticker != mode["holding"]:
            entry.update(in_scope=False, scope_reason="holding_trial_other_ticker")
        elif not fund and sector not in common.SECTOR_KEYS:
            entry.update(in_scope=False, scope_reason="unclassified")
            warnings.append(f"{ticker}: sector could not be classified; set sector_override in portfolio.yaml "
                            "to review it")
        elif mode["sectors"] and not mode["holding"] and not fund and sector not in mode["sectors"]:
            entry.update(in_scope=False, scope_reason="outside_sectors_filter")
        out.append(entry)
    return out


def _classify_trial(ctx: RunContext, ticker: str, warnings: list[str]) -> dict:
    """Holding mode, ticker not held: classify it as a candidate."""
    info = None
    try:
        info = common.yf_info(ticker)
    except common.DataUnavailable as exc:
        warnings.append(f"{ticker}: Yahoo info unavailable for classification: {exc}")
    c = _mod("etf_lookthrough").classify_security(ticker, info=info, overrides=ctx.overrides)
    asset_type = str(c.get("asset_type") or "stock")
    fund = asset_type == "fund"
    sector = FUNDS if fund else str(c.get("sector") or "unclassified")
    if not fund and sector not in common.SECTOR_KEYS:
        raise common.ConfigError(f"{ticker}: sector could not be classified; add securities.\"{ticker}\".sector "
                                 "to config/etf_overrides.yaml and rerun")
    return {"ticker": ticker, "asset_type": asset_type, "sector": sector,
            "sector_class": "fund" if fund else str(c.get("sector_class") or common.sector_class(sector)),
            "classification_source": c.get("classification_source"), "position_pct": 0.0,
            "price_ticker": ticker, "flags": list(c.get("flags") or []), "in_scope": True}


def _step_p2_data(ctx: RunContext) -> dict:
    warnings: list[str] = []
    lookthrough = _read(ctx.p("phase1", "lookthrough.json"))
    if not isinstance(lookthrough, dict):
        raise common.DataUnavailable("phase1/lookthrough.json is missing or unreadable (run p1.data first)")
    prices = _prices_doc(ctx)
    holdings = _classify_holdings(ctx, lookthrough, warnings)
    ctx.manifest["holdings"] = holdings
    trial = ctx.mode["holding"]
    ctx.manifest.pop("trial_candidate", None)
    if trial and trial not in {h["ticker"] for h in holdings}:
        ctx.manifest["trial_candidate"] = _classify_trial(ctx, trial, warnings)
    outputs: list[Path] = []
    subjects = _p2_subjects(ctx)
    if trial and not subjects:
        raise common.ConfigError(f"{trial}: the holding could not be classified into a sector; set "
                                 "sector_override in portfolio.yaml and rerun")
    for sub in subjects:
        if sub["asset_type"] == "fund":
            outputs += _collect_fund_data(ctx, 2, sub, warnings)
        else:
            outputs += _collect_stock_data(ctx, 2, sub, prices, warnings)
    for w in warnings:
        ctx.log(f"p2.data warning: {w}")
    n_out = sum(1 for h in holdings if not h["in_scope"])
    summary = (f"Phase 2 data for {len(subjects)} tickers ({', '.join(s['ticker'] for s in subjects) or 'none'})"
               + (f"; {n_out} holdings out of scope" if n_out else ""))
    return _result(summary, outputs, warnings)


def _step_p2_aggregate(ctx: RunContext) -> dict:
    subjects = _p2_subjects(ctx)
    res = _aggregate_phase(ctx, 2, subjects, evidence_out=ctx.p("phase2", "evidence.json"),
                           aggregate_out=ctx.p("phase2", "aggregate.json"), light_only=False, packets=True)
    summary = (f"Phase 2 aggregate: {len(subjects)} tickers, {res['n_reports']} reports scored; "
               f"rule actions {_fmt_counts(res['rule_actions'])}")
    return _result(summary, res["outputs"], res["warnings"])


def _held_tickers(ctx: RunContext) -> list[str]:
    out: list[str] = []
    for h in ctx.portfolio.get("holdings") or []:
        if isinstance(h, Mapping):
            out += [t for t in (_norm(h.get("ticker")), _norm(h.get("proxy_ticker"))) if t]
    for h in ctx.manifest.get("holdings") or []:
        if isinstance(h, Mapping):
            out += [t for t in (_norm(h.get("ticker")), _norm(h.get("price_ticker"))) if t]
    return _dedupe(out)


def _select_expand_sectors(ctx: RunContext) -> tuple[list[str], list[dict]]:
    cio = _p1_doc(ctx, "cio") or {}
    enabled = set(_cfg_get(ctx.cfg, "enabled_sectors", list(common.SECTOR_KEYS)))
    flt = ctx.mode["sectors"]
    cap = int(_cfg_get(ctx.cfg, "phase3.max_expand_sectors", DEFAULT_MAX_EXPAND_SECTORS))
    kept: list[str] = []
    dropped: list[dict] = []
    for s in _dedupe(str(x) for x in cio.get("expand_sectors") or []):
        if s not in common.SECTOR_KEYS:
            dropped.append({"sector": s, "reason": "unknown_sector"})
        elif s not in enabled:
            dropped.append({"sector": s, "reason": "not_enabled"})
        elif flt and s not in flt:
            dropped.append({"sector": s, "reason": "outside_sectors_filter"})
        elif len(kept) >= cap:
            dropped.append({"sector": s, "reason": f"max_expand_sectors={cap}"})
        else:
            kept.append(s)
    return kept, dropped


def _step_p3_screen(ctx: RunContext) -> dict:
    warnings: list[str] = []
    kept, dropped = _select_expand_sectors(ctx)
    ctx.manifest["expand_sectors"] = kept
    ctx.manifest["expand_dropped"] = dropped
    for d in dropped:
        msg = f"expand sector {d['sector']} dropped ({d['reason']})"
        warnings.append(msg)
        ctx.log(msg)
    screen = _mod("screen")
    held = _held_tickers(ctx)
    rotation = _cfg_get(ctx.cfg, "phase3.rotation", None) or {}
    rotate = bool(rotation.get("enabled", True))
    lookback = int(rotation.get("lookback_runs", DEFAULT_ROTATION_LOOKBACK))
    candidates: dict[str, list[str]] = {}
    assigned: dict[str, str] = {}          # ticker -> the (earlier) sector that kept it
    outputs: list[Path] = []
    for sector in kept:
        out = ctx.p("phase3", f"screen_{sector}.json")
        recent = (screen.recent_shortlisted(sector, ctx.run_dir.parent, exclude_run=ctx.run_id, lookback=lookback)
                  if rotate else [])
        if recent:
            ctx.log(f"{sector}: rotation skips {len(recent)} recently shortlisted ticker(s): {', '.join(recent)}")
        try:
            res = screen.run(sector, out, held=held, as_of=ctx.as_of, recent=recent)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"screen {sector} failed: {exc}")
            candidates[sector] = []
            continue
        if not isinstance(res, Mapping):
            res = _read(out) or {}
        outputs.append(out)
        shortlist = _dedupe(t for t in (_norm(x) for x in res.get("shortlist") or []) if t and t not in held)
        kept_names: list[str] = []
        for ticker in shortlist:
            if ticker in assigned:
                msg = (f"{ticker}: shortlisted in {sector} but already a {assigned[ticker]} candidate; "
                       f"kept in {assigned[ticker]} only")
                warnings.append(msg)
                ctx.log(msg)
                continue
            sub = {"ticker": ticker, "sector": sector, "sector_class": common.sector_class(sector, "stock"),
                   "price_ticker": ticker, "asset_type": "stock"}
            files = _collect_stock_data(ctx, 3, sub, {}, warnings)   # candidates: technicals close
            names = {p.name for p in files}
            if (common.data_filename("technicals", ticker) not in names
                    and common.data_filename("fundamentals", ticker) not in names):
                warnings.append(f"{ticker}: no technicals and no fundamentals; dropped from the {sector} shortlist")
                continue
            outputs += files
            kept_names.append(ticker)
            assigned[ticker] = sector
        candidates[sector] = kept_names
    ctx.manifest["candidates"] = candidates
    total = sum(len(v) for v in candidates.values())
    ctx.log(f"Phase 3 candidates: {json.dumps(candidates)}")
    skip = [s for s in P3_STEPS if s != "p3.screen"] if total == 0 else []
    summary = (f"Phase 3 screen: expand {', '.join(kept) or 'none'}"
               + (f" (dropped {', '.join(d['sector'] for d in dropped)})" if dropped else "")
               + f"; {total} candidates")
    return _result(summary, outputs, warnings, skip_steps=skip)


def _rank_key(entry: Mapping[str, Any], ticker: str) -> tuple:
    ev = common.safe_float(entry.get("agg_evidence"))
    return (-(common.safe_float(entry.get("agg_conviction")) or 0.0), 0 if ev is not None else 1, -(ev or 0.0), ticker)


def select_finalists(candidates: Mapping[str, Iterable[str]], aggregate: Mapping[str, Any], n: int) -> dict[str, list[str]]:
    """Top ``n`` per sector by agg_conviction, then agg_evidence, then ticker (candidates with a
    null agg_conviction never become finalists)."""
    table = aggregate.get("tickers") if isinstance(aggregate.get("tickers"), Mapping) else {}
    out: dict[str, list[str]] = {}
    for sector, tickers in candidates.items():
        ranked = []
        for t in tickers or []:
            entry = (table or {}).get(t) or {}
            if common.safe_float(entry.get("agg_conviction")) is None:
                continue
            ranked.append((_rank_key(entry, t), t))
        ranked.sort()
        out[sector] = [t for _k, t in ranked[: max(0, int(n))]]
    return out


def _step_p3_finalists(ctx: RunContext) -> dict:
    subjects = _p3_subjects(ctx, "candidates")
    res = _aggregate_phase(ctx, 3, subjects, evidence_out=ctx.p("phase3", "evidence_light.json"),
                           aggregate_out=ctx.p("phase3", "aggregate_light.json"), light_only=True, packets=False)
    n = int(_cfg_get(ctx.cfg, "phase3.finalists", DEFAULT_FINALISTS))
    finalists = select_finalists(ctx.manifest.get("candidates") or {}, res["aggregate"], n)
    ctx.manifest["finalists"] = finalists
    ctx.log(f"Phase 3 finalists: {json.dumps(finalists)}")
    total = sum(len(v) for v in finalists.values())
    skip = ["p3.full", "p3.da", "p3.aggregate", "p3.sector_heads"] if total == 0 else []
    detail = "; ".join(f"{s}: {', '.join(v) or 'none'}" for s, v in finalists.items()) or "none"
    return _result(f"Phase 3 finalists ({total}): {detail}", res["outputs"], res["warnings"], skip_steps=skip)


def _step_p3_aggregate(ctx: RunContext) -> dict:
    subjects = _p3_subjects(ctx, "finalists")
    res = _aggregate_phase(ctx, 3, subjects, evidence_out=ctx.p("phase3", "evidence.json"),
                           aggregate_out=ctx.p("phase3", "aggregate.json"), light_only=False, packets=True)
    summary = (f"Phase 3 aggregate: {len(subjects)} finalists, {res['n_reports']} reports scored; "
               f"rule actions {_fmt_counts(res['rule_actions'])}")
    return _result(summary, res["outputs"], res["warnings"])


def _verdict_digests(ctx: RunContext) -> list[dict]:
    """Per sector and phase: the sector verdict's actions, or the aggregate rule actions when the
    verdict is missing/failed."""
    out: list[dict] = []
    for phase, subs in ((2, _p2_subjects(ctx)), (3, _p3_subjects(ctx, "finalists"))):
        agg = _read(ctx.p(f"phase{phase}", "aggregate.json")) or {}
        agg_tickers = agg.get("tickers") if isinstance(agg.get("tickers"), Mapping) else {}
        for sector, group in _group_by_sector(subs).items():
            path = _find_output(ctx, f"p{phase}.sector_heads", "sector_head", _verdict_path(ctx, phase, sector),
                                sector=sector)
            doc = _read(path)
            ok = isinstance(doc, Mapping) and not _is_stub(path, doc) and isinstance(doc.get("verdicts"), list)
            entry: dict[str, Any] = {"phase": phase, "sector": sector,
                                     "path": _posix(path) if path else None,
                                     "status": "ok" if ok else ("failed" if path else "missing"),
                                     "summary": doc.get("summary") if ok else None, "tickers": []}
            if ok:
                for v in doc["verdicts"]:  # type: ignore[index]
                    if isinstance(v, Mapping):
                        entry["tickers"].append({
                            "ticker": _norm(v.get("ticker")), "kind": v.get("kind"), "action": v.get("action"),
                            "rule_action": v.get("rule_action"), "agg_conviction": v.get("agg_conviction"),
                            "agg_evidence": v.get("agg_evidence"), "rr": v.get("rr"),
                            "rule_deviation": bool(v.get("rule_deviation")), "source": "verdict"})
            else:
                for sub in group:
                    a = (agg_tickers or {}).get(sub["ticker"]) or {}
                    entry["tickers"].append({
                        "ticker": sub["ticker"], "kind": a.get("kind") or sub.get("kind"),
                        "action": a.get("rule_action"), "rule_action": a.get("rule_action"),
                        "agg_conviction": a.get("agg_conviction"), "agg_evidence": a.get("agg_evidence"),
                        "rr": (a.get("rr") or {}).get("rr") if isinstance(a.get("rr"), Mapping) else a.get("rr"),
                        "rule_deviation": False, "source": "aggregate_rule"})
            out.append(entry)
    return out


def _conflicts(ctx: RunContext, cio: Mapping[str, Any], digests: list[dict]) -> list[dict]:
    out: list[dict] = []
    p2 = [d for d in digests if d["phase"] == 2]
    p3 = [d for d in digests if d["phase"] == 3]
    for sector in _dedupe(str(s) for s in cio.get("reduce_sectors") or []):
        adds = [t["ticker"] for d in p2 if d["sector"] == sector for t in d["tickers"] if t.get("action") == "ADD"]
        if adds:
            out.append({"type": "reduce_sector_but_add", "sector": sector, "tickers": adds,
                        "detail": f"The CIO allocation reduces {common.sector_display(sector)} but the sector "
                                  f"verdict says ADD for {', '.join(adds)}."})
    processed = set(ctx.manifest.get("expand_sectors") or [])
    phase3_ran = not ctx.mode["quick"] and not ctx.mode["holding"]
    for sector in _dedupe(str(s) for s in cio.get("expand_sectors") or []):
        buys = [t["ticker"] for d in p3 if d["sector"] == sector for t in d["tickers"] if t.get("action") == "BUY"]
        if buys:
            continue
        if not phase3_ran:
            why = "Phase 3 did not run in this mode"
        elif sector not in processed:
            why = "the sector was not screened (dropped by the sector cap or filter)"
        else:
            why = "no candidate earned a BUY"
        out.append({"type": "expand_sector_without_buys", "sector": sector, "tickers": [],
                    "detail": f"The CIO allocation expands {common.sector_display(sector)} but no BUY was "
                              f"found: {why}."})
    return out


def _step_p4_preview(ctx: RunContext) -> dict:
    """Preview plan (portfolio.run final=False) + phase4/cio_packet.json. A failing preview plan
    is a warning: the CIO packet is still written so the reconciliation can run."""
    warnings: list[str] = []
    preview_path = ctx.p("phase4", "portfolio_preview.json")
    try:
        plan = _mod("portfolio").run(ctx.run_dir, final=False)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"preview plan failed: {exc}")
        ctx.log(f"p4.preview warning: preview plan failed: {exc}")
        plan = None
    if not isinstance(plan, Mapping):
        plan = _read(preview_path) or {}
    cio = _p1_doc(ctx, "cio") or {}
    macro_view = _p1_doc(ctx, "macro")
    digests = _verdict_digests(ctx)
    conflicts = _conflicts(ctx, cio, digests)
    lookthrough_path = ctx.p("phase1", "lookthrough.json")
    sources = _seed_sources([lookthrough_path, ctx.p("phase1", "macro.json"), preview_path])
    missing = [f"phase {d['phase']} {d['sector']} verdict {d['status']} (aggregate rule actions shown)"
               for d in digests if d["status"] != "ok"]
    packet = common.envelope(
        "cio_packet", ctx.as_of, sources=sources, missing_data=missing, run_id=ctx.run_id,
        verdicts=digests,
        preview={"path": _posix(preview_path) if preview_path.is_file() else None, "cash": plan.get("cash"),
                 "sectors": plan.get("sectors") or [], "constraint_checks": plan.get("constraint_checks") or []},
        macro_bounds=[{"sector": s, **b} for s, b in _macro_bounds(macro_view).items()],
        cio_allocation={"target_sector_weights": cio.get("target_sector_weights"),
                        "expand_sectors": cio.get("expand_sectors") or [],
                        "reduce_sectors": cio.get("reduce_sectors") or []},
        phase3={"expand_sectors": ctx.manifest.get("expand_sectors") or [],
                "expand_dropped": ctx.manifest.get("expand_dropped") or [],
                "finalists": ctx.manifest.get("finalists") or {}},
        conflicts=conflicts,
    )
    packet_path = ctx.p("phase4", "cio_packet.json")
    common.write_json(packet_path, packet)
    summary = (f"Preview plan + CIO packet: {len(digests)} sector verdicts, {len(conflicts)} conflicts"
               + (f", {len(missing)} verdicts missing/failed" if missing else ""))
    outputs = ([preview_path] if preview_path.is_file() else []) + [packet_path]
    return _result(summary, outputs, warnings + missing)


def _final_actions(ctx: RunContext) -> dict[str, dict]:
    """ticker -> {"action","kind","source"}: aggregate rule < sector verdict < CIO final."""
    out: dict[str, dict] = {}
    for d in _verdict_digests(ctx):
        for t in d["tickers"]:
            if t.get("ticker"):
                out[t["ticker"]] = {"action": t.get("action"), "kind": t.get("kind"), "source": t.get("source")}
    path = _find_output(ctx, "p4.cio", "cio", ctx.p("phase4", "cio_final.json"))
    doc = _read(path)
    if isinstance(doc, Mapping) and not _is_stub(path, doc):
        for a in doc.get("final_actions") or []:
            t = _norm(a.get("ticker")) if isinstance(a, Mapping) else None
            if t:
                prev = out.get(t, {})
                out[t] = {"action": a.get("action"), "kind": prev.get("kind"), "source": "cio_final"}
    return out


def _persist_manifest(ctx: RunContext) -> None:
    """Write ``ctx.manifest`` to ``manifest.json`` when that file exists, so a renderer that reads
    the manifest from disk sees what a python step just added (committee.py saves the same dict
    again after the step, so this only matters for the step's own render)."""
    path = ctx.p("manifest.json")
    if not path.is_file():
        return
    try:
        common.write_json(path, ctx.manifest)
    except OSError as exc:
        ctx.log(f"WARNING manifest not persisted before render: {exc}")


def _check_config_drift(ctx: RunContext, warnings: list[str]) -> list[dict]:
    """Detect configs edited after the run started (live files vs the init-time hashes). Never
    aborts: a manifest warning, a run_log line and ``manifest["config_drift"]`` (rendered in the
    memo's data gaps) are the whole reaction."""
    drift = config_drift(ctx.run_dir, ctx.manifest)
    if not drift:
        ctx.manifest.pop("config_drift", None)
        return []
    ctx.manifest["config_drift"] = drift
    for entry in drift:
        msg = entry["message"]
        warnings.append(msg)
        ctx.log(f"WARNING {msg}")
    _persist_manifest(ctx)
    return drift


def _step_p4_finalize(ctx: RunContext) -> dict:
    warnings: list[str] = []
    mode = ctx.mode
    outputs: list[Path] = []
    plan: Mapping[str, Any] = {}
    trial = mode["holding"]
    drift = _check_config_drift(ctx, warnings)
    if trial:
        md_out = ctx.p("phase4", f"trial_{common.safe_name(trial)}.md")
        html_out = ctx.p("phase4", f"trial_{common.safe_name(trial)}.html")
    else:
        plan_path = ctx.p("phase4", "portfolio_plan.json")
        try:
            result = _mod("portfolio").run(ctx.run_dir, final=True)
        except Exception as exc:  # noqa: BLE001 - the memo still renders ("Not available")
            warnings.append(f"portfolio plan failed: {exc}")
            result = None
        plan = result if isinstance(result, Mapping) else (_read(plan_path) or {})
        if plan_path.is_file() or isinstance(result, Mapping):
            outputs.append(plan_path)
        md_out, html_out = ctx.p("phase4", "memo.md"), ctx.p("phase4", "memo.html")
    rendered = _mod("render_memo").render(ctx.run_dir, mode="trial" if trial else "full", ticker=trial,
                                          out_md=md_out, out_html=html_out)
    md_r, html_r = rendered if isinstance(rendered, (tuple, list)) and len(rendered) == 2 else (None, None)
    md, html = Path(md_r or md_out), Path(html_r or html_out)
    outputs += [md, html]
    ledger = _mod("ledger")
    ledger_target = ctx.p("phase4", "ledger_preview.csv") if mode["dry_run"] else None
    ledger_file = ledger_target if ledger_target is not None else common.ledger_path()
    appended: int | None
    try:
        rows = ledger.rows_from_run(ctx.run_dir)
        # a run finalized again after a chair re-run REPLACES its ledger rows (no duplicates, no stale rows)
        extra = {"replace_run_ids": [ctx.run_id]} if ctx.manifest.get("chair_rounds") else {}
        appended = ledger.append(rows, ledger_path=ledger_target, **extra)
    except Exception as exc:  # noqa: BLE001 - the memo exists; report the ledger problem
        warnings.append(f"ledger append failed: {exc}")
        appended = None
    if ledger_target is not None and ledger_target.is_file():
        outputs.append(ledger_target)

    finals = _final_actions(ctx)
    holdings = _count(v["action"] for v in finals.values() if v.get("kind") != "candidate")
    candidates = _count(v["action"] for v in finals.values() if v.get("kind") == "candidate")
    trades = [p for p in plan.get("positions") or [] if isinstance(p, Mapping)
              and (p.get("trade") or {}).get("side") in ("BUY", "SELL") and not p.get("dropped_reason")]
    if trial:
        act = finals.get(trial, {}).get("action") or "n/a"
        top_line = f"Trial {trial}: {act}."
    else:
        n_buy = candidates.get("BUY", 0)
        top_line = (f"{sum(holdings.values())} holdings reviewed ({_fmt_counts(holdings)}); "
                    f"{n_buy} new BUY proposal{'s' if n_buy != 1 else ''}; "
                    f"{len(trades)} trade{'s' if len(trades) != 1 else ''} in the plan.")
    digest: Mapping[str, Any] = {}
    digest_fn = getattr(_mod("render_memo"), "summary_digest", None)
    if callable(digest_fn):
        try:
            digest = digest_fn(ctx.run_dir, ticker=trial) or {}
        except Exception as exc:  # noqa: BLE001 - the summary is still written, with empty digests
            warnings.append(f"summary sources/missing_data digest failed: {exc}")
    summary_doc = common.envelope(
        "summary", ctx.as_of, run_id=ctx.run_id, mode=mode,
        memo_md=_posix(md), memo_html=_posix(html),
        plan=_posix(ctx.p("phase4", "portfolio_plan.json")) if not trial else None,
        ledger={"path": _posix(ledger_file), "rows_appended": appended, "preview": ledger_target is not None},
        counts={"holdings": holdings, "candidates": candidates, "trades": len(trades)},
        actions=finals, top_line=top_line,
        config_drift=[d["message"] for d in drift],
        missing_data=list(digest.get("missing_data") or []),
        missing_data_count=int(digest.get("missing_data_count") or 0),
    )
    # the memo's sources, de-duplicated by URL (the envelope helper would de-duplicate by id, and
    # the agents' W1..Wn ids repeat across reports)
    summary_doc["sources"] = list(digest.get("sources") or [])
    summary_path = ctx.p("phase4", "summary.json")
    common.write_json(summary_path, summary_doc)
    outputs.append(summary_path)
    ctx.log(f"finalize: {top_line} memo {_posix(md)}")
    return _result(top_line, outputs, warnings, memo_md=_posix(md), memo_html=_posix(html),
                   summary_path=_posix(summary_path))


_PY_STEPS: dict[str, Callable[[RunContext], dict]] = {
    "p1.data": _step_p1_data,
    "p2.data": _step_p2_data,
    "p2.aggregate": _step_p2_aggregate,
    "p3.screen": _step_p3_screen,
    "p3.finalists": _step_p3_finalists,
    "p3.aggregate": _step_p3_aggregate,
    "p4.preview": _step_p4_preview,
    "p4.finalize": _step_p4_finalize,
}


def run_python_step(name: str, ctx: RunContext) -> dict:
    """Execute python step ``name``. Returns ``{"summary","outputs","warnings","skip_steps"}``
    (+ ``memo_md``/``memo_html``/``summary_path`` from ``p4.finalize``). May mutate
    ``ctx.manifest``; exceptions propagate (the caller marks the step failed)."""
    fn = _PY_STEPS.get(name)
    if fn is None:
        raise ValueError(f"{name!r} is not a python step; python steps: {sorted(_PY_STEPS)}")
    return fn(ctx)


# =====================================================================================
# CLI (debugging aid: inspect a run's sequence / orders)
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(description="Inspect the committee step machine for a run (read-only).")
    ap.add_argument("--run", required=True, help="run id (under the runs dir) or a run directory")
    ap.add_argument("--orders", metavar="STEP", help="print the work-order specs an agents step would emit")
    common.add_common_args(ap)
    args = common.apply_common_args(ap.parse_args(argv))
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        run_dir = common.runs_dir() / args.run
    manifest = _read(run_dir / "manifest.json")
    if not isinstance(manifest, dict):
        print(f"no manifest.json in {run_dir}", file=sys.stderr)
        return 1
    ctx = RunContext(run_dir, manifest)
    if args.orders:
        print(common.dumps_json(build_orders(args.orders, ctx)))
    else:
        print(common.dumps_json({"run_id": ctx.run_id, "mode": ctx.mode, "steps": step_sequence(ctx)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
