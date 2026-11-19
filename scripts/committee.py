"""committee.py — thin orchestration CLI and run state machine (DESIGN §2.1–§2.3).

Subcommands (all print ONE line of JSON unless noted)::

    init [--quick] [--sectors a,b] [--holding T] [--dry-run] [--run-id ID]   -> {run_id, run_dir, mode}
    next --run ID          run pending python steps / dispatch or check agent orders
    redispatch --run ID    re-emit the dispatched orders that still need an agent (no attempt is counted)
    status --run ID        compact progress JSON
    holding T              init in holding mode (one-ticker trial)
    dry-run [--fixtures-dir DIR] [--as-of D]   whole pipeline offline with fixture agent outputs
                           (DIR needs config/ (or --config-dir), data/, agent_outputs/; else exit 2;
                           --quick / --sectors / --holding are rejected with exit 2: the fixtures
                           cover the full pipeline only; exit 1 / ok=false when ANY order failed)
    scorecard              ledger.score wrapper -> ledger/scorecard_<date>.md
    validate-config        prints config errors (one per line) or "ok"
    reopen --run ID --order O [--order O ...] --feedback TEXT [--feedback-file F] [--spec-file S] [--reason R]
                           chair review: send finished orders back to their agents with written feedback and
                           reset everything downstream (see "Chair review" below)
    audit --run ID         scripts.audit.run(run_dir) -> JSON summary (module imported lazily)
    chair-save --run ID --file F    validate F against schemas/chair_review.schema.json and store it as
                           phase4/chair_review.json (earlier rounds kept as chair_review.r<round>.json)

``next`` results:
  * ``{"type":"continue"}``  python work was done and the time budget
    (``orchestration.step_time_budget_seconds``) is used up -> call ``next`` again;
  * ``{"type":"agents","step","concurrency","orders":[{order_id, agent_type, order_path}],"summary"}``
    -> launch every order (prompt ``Execute work order <order_path>``), then call ``next``;
  * ``{"type":"done","memo_md","memo_html","summary"}``;
  * ``{"type":"error","step","message"}`` (exit code 1; the manifest is saved, the run is resumable).

Order semantics (documented contract, tested in tests/test_committee.py):
  * the orders of an agents step are built ONCE (``_steps.build_orders``) and rendered to
    ``orders/<order_id>.md``; the step is then ``dispatched``;
  * the orchestrator calls ``next`` only after every emitted order finished. That call checks each
    dispatched order: a valid output -> ``done``; an invalid or missing output on attempt
    ``< orchestration.max_attempts`` -> a retry order (same order id and order file, ``attempt+1``,
    output ``<stem>.r<attempt>.json``, ``prior_errors`` = the validation errors or
    ``output file missing: <path>``) which is re-emitted; on the last attempt -> a
    ``<stem>.failed.json`` stub (schema ``failed_report``) and status ``failed``;
  * so calling ``next`` twice without outputs DOES count an attempt (missing output = failed
    attempt). Crash recovery that must not count an attempt uses ``redispatch``, which re-emits the
    dispatched orders that still need an agent: orders whose output is already valid are marked
    done and skipped; an existing but invalid output is set aside (``<name>.json.invalid``) so the
    agent can write the path again;
  * circuit breaker: when, on the FIRST collection of a step, at least
    ``orchestration.circuit_breaker_fraction`` (default 0.5) of its orders — and at least
    ``orchestration.circuit_breaker_min_orders`` (default 3) — have no output file at all, ``next``
    does not burn attempts: it returns ``{"type":"error",...}`` ("N of M agent orders produced no
    output - likely a usage limit or outage ...") and leaves every attempt counter untouched.
    ``redispatch`` re-emits the missing orders; ``next --ignore-circuit-breaker`` accepts the
    failures. Retries (attempt >= 2) are never affected.

Chair review (``reopen``, DESIGN §10): the chair is the main session reviewing a FINISHED run (status
``done``). ``reopen`` turns every named ``done``/``failed`` order into a retry order exactly like the retry
machinery does (same order id / order file, ``attempt+1``, output ``<stem>.r<attempt>.json``, the earlier
output stays on disk) whose ``prior_errors`` are the feedback lines prefixed ``CHAIR FEEDBACK:`` (order record:
``reopened_by: "chair"``, ``reopen_round``). Every step that depends on a reopened step (``STEP_DEPENDS``)
goes back to ``pending`` and its outputs move to ``_superseded/round<n>/``: python steps re-run, agent steps
are re-dispatched as fresh orders with ``prior_errors`` ``inputs changed after chair re-run of <order ids>;
re-check your conclusions against the updated reports`` (per-ticker / per-sector steps only redo the touched
tickers / sectors). The manifest status is ``reopened`` until ``next`` reaches ``done`` again;
``manifest["chair_rounds"]`` records ``{round, at, reason, orders, feedback_sha, ...}``. Caps:
``orchestration.max_chair_rounds`` (default 2) and ``max_attempts + 2`` attempts per order. After ``reopen``
the chair launches the orders it lists, then runs the normal ``next`` loop.

Python-step logic and order specs live in ``scripts/_steps.py``; ``_steps``, ``validate``,
``ledger`` and ``render_memo`` are imported lazily so this module always imports.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import common  # noqa: E402

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any, Callable, Iterable  # noqa: E402

COMMITTEE_VERSION = "1.0"

# Fallbacks used only when committee.yaml lacks a key (config is the source of truth).
DEFAULT_STEP_BUDGET_S = 90.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_CONCURRENCY = 8
DEFAULT_VALIDATION_RULES: dict[str, Any] = {
    "data_ref_tolerance_pct": 0.5,
    "level_match_tolerance_pct": 0.5,
    "cell_mean_tolerance_pct": 0.5,
    "min_base_method_families": 2,
    "probability_sum_tolerance": 0.01,
    "sector_weight_sum_tolerance_pct": 0.5,
    "min_justification_words": 15,
    "max_quote_words": 25,
    "max_executive_summary_items": 5,
    "max_top_risks": 5,
}
DEFAULT_PHASE3_RULES: dict[str, Any] = {"max_buys_per_sector": 3, "max_expand_sectors": 2}

#: Circuit breaker defaults (config: orchestration.circuit_breaker_fraction / _min_orders).
DEFAULT_CIRCUIT_BREAKER_FRACTION = 0.5
DEFAULT_CIRCUIT_BREAKER_MIN_ORDERS = 3
CIRCUIT_BREAKER_LIST_MAX = 20     # ids listed in the breaker payload / manifest

DRY_RUN_DEFAULT_AS_OF = "2026-10-02"
DRY_RUN_DEFAULT_FIXTURES = Path("tests") / "fixtures" / "dryrun"
DRY_RUN_MAX_ITERATIONS = 500
#: Required sub-directories of a dry-run fixtures root (config/ may be replaced by --config-dir).
DRY_RUN_LAYOUT = (
    ("config", "the *.yaml configs of the dry run"),
    ("data", "provider fixtures served through AGINV_FIXTURES_DIR"),
    ("agent_outputs", "one <order_id>.json (or <output file name>) per agent order"),
)
#: Tokens replaced in fixture agent outputs before they are copied to an order's output path.
DRY_RUN_TOKENS = ("${RUN_DIR}", "${REPO_ROOT}", "${RUN_ID}", "${AS_OF}")

WORK_ORDER_TEMPLATE = "work_order.md.j2"
PRIOR_ERRORS_MAX = 40          # cap on prior_errors copied into a retry order
ERROR_TEXT_MAX = 600           # cap on a single error line in orders / stubs

# ---- chair review (reopen / chair-save) ----
CHAIR_PREFIX = "CHAIR FEEDBACK:"                 # prior_errors entries written by `reopen`
INPUTS_CHANGED_PREFIX = "inputs changed after chair re-run of"
CHAIR_LINE_MAX = 520           # one CHAIR FEEDBACK entry stays below ERROR_TEXT_MAX (never clipped)
CHAIR_FEEDBACK_MAX_LINES = 28  # per order (prior_errors is capped at PRIOR_ERRORS_MAX)
CHAIR_FEEDBACK_MAX_CHARS = 6000
DEFAULT_MAX_CHAIR_ROUNDS = 2   # config: orchestration.max_chair_rounds
CHAIR_ORDER_EXTRA_ATTEMPTS = 2  # an order may use at most max_attempts + 2 attempts in total
SUPERSEDED_DIR = "_superseded"  # runs/<id>/_superseded/round<n>/<original relative path>
CHAIR_REVIEW_SCHEMA = "chair_review.schema.json"
CHAIR_WORD_LIMITS = {"headline": 40, "executive_summary": 45, "chair_view": 90, "what_would_change_it": 40}
#: Files never archived by a reopen (shared, append-only state outside the step outputs' meaning).
KEEP_IN_PLACE_NAMES = ("ledger_preview.csv",)

#: Direct upstream steps of every pipeline step (a step is re-run when one of them changed). A step
#: name missing here (stand-in sequences) is treated as depending on EVERYTHING before it.
STEP_DEPENDS: dict[str, tuple[str, ...]] = {
    "p1.macro": ("p1.data",),
    "p1.cio": ("p1.data", "p1.macro"),
    "p2.data": ("p1.data",),
    "p2.wave_a": ("p2.data",),
    "p2.wave_b": ("p2.data", "p2.wave_a"),
    "p2.aggregate": ("p2.wave_a", "p2.wave_b", "p1.cio", "p1.macro"),
    "p2.sector_heads": ("p2.aggregate", "p2.wave_a", "p2.wave_b"),
    "p3.screen": ("p2.data", "p1.cio"),
    "p3.light": ("p3.screen",),
    "p3.finalists": ("p3.screen", "p3.light", "p1.cio", "p1.macro"),
    "p3.full": ("p3.finalists",),
    "p3.da": ("p3.finalists", "p3.light", "p3.full"),
    "p3.aggregate": ("p3.finalists", "p3.light", "p3.full", "p3.da", "p1.cio", "p1.macro"),
    "p3.sector_heads": ("p3.aggregate", "p3.light", "p3.full", "p3.da"),
    "p4.preview": ("p1.cio", "p1.macro", "p2.aggregate", "p2.sector_heads", "p3.aggregate",
                   "p3.sector_heads"),
    "p4.cio": ("p4.preview", "p1.cio", "p1.macro", "p2.sector_heads", "p3.sector_heads"),
    "p4.finalize": ("p4.cio", "p4.preview", "p1.cio", "p1.macro", "p2.aggregate", "p2.sector_heads",
                    "p3.aggregate", "p3.sector_heads"),
}
#: Agent steps whose orders are per ticker / per sector: a downstream order is only redone when its
#: ticker / sector is touched by the reopened orders (every other order of the step is kept).
SCOPE_KEY: dict[str, str] = {"p2.wave_b": "ticker", "p3.da": "ticker",
                             "p2.sector_heads": "sector", "p3.sector_heads": "sector"}
#: Python steps that decide WHICH orders later steps contain: when one of them is re-run, no
#: scoped keeping is done (the order set may change).
SET_CHANGING_STEPS = ("p2.data", "p3.screen", "p3.finalists")

RUN_SUBDIRS = (
    "inputs", "orders", "phase1",
    "phase2/data", "phase2/reports", "phase2/packets",
    "phase3/data", "phase3/reports", "phase3/packets",
    "phase4",
)
RUN_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}(_[A-Za-z0-9-]+)*$")
STEP_STATUSES = ("pending", "running", "dispatched", "done", "failed", "skipped")
ORDER_STATUSES = ("pending", "dispatched", "done", "failed")
_STEP_NAME_RE = re.compile(r"^p([0-9])\.")

#: monotonic clock used for the per-call time budget (tests patch it).
_clock: Callable[[], float] = time.monotonic


# =====================================================================================
# Lazy module seams (tests monkeypatch these functions)
# =====================================================================================


def _steps_module() -> Any:
    return importlib.import_module("scripts._steps")


def _validate_module() -> Any:
    return importlib.import_module("scripts.validate")


def _ledger_module() -> Any:
    return importlib.import_module("scripts.ledger")


def _render_module() -> Any:
    return importlib.import_module("scripts.render_memo")


# =====================================================================================
# Errors
# =====================================================================================


class InitError(Exception):
    """init failed for a user-fixable reason (exit code 2): ``{"error": message, "details": [...]}``."""

    def __init__(self, message: str, details: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details)


# =====================================================================================
# Small helpers
# =====================================================================================


def _emit(obj: Any) -> None:
    """Print one line of JSON to stdout."""
    print(common.dumps_json(obj, indent=None), flush=True)


def _posix(path: Any) -> str:
    return Path(os.path.abspath(str(path))).as_posix()


def _clip(text: Any, limit: int = ERROR_TEXT_MAX) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_path(run_dir: Path) -> Path:
    return Path(run_dir) / "manifest.json"


def load_manifest(run_dir: Path) -> dict:
    path = manifest_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"run not found (no manifest.json): {Path(run_dir).as_posix()}")
    doc = common.read_json(path)
    if not isinstance(doc, dict):
        raise ValueError(f"manifest is not a JSON object: {path.as_posix()}")
    return doc


def save_manifest(run_dir: Path, manifest: dict) -> Path:
    manifest["updated_at"] = common.now_iso()
    return common.write_json(manifest_path(run_dir), manifest)


def log(run_dir: Path, message: str, *, block: str | None = None) -> None:
    """Append ``- <timestamp> <message>`` (plus an optional fenced block) to run_log.md."""
    path = Path(run_dir) / "run_log.md"
    if not path.parent.is_dir():
        return
    text = f"- {common.now_iso()} {message}\n"
    if block:
        text += "\n```text\n" + block.rstrip() + "\n```\n\n"
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def resolve_run_dir(run: str | None) -> Path:
    """``--run`` value -> run directory: a run id under ``runs_dir()``, an existing run directory
    path, or ``latest`` (newest run with a manifest)."""
    if run is None or not str(run).strip():
        raise ValueError("--run is required (run id, run directory or 'latest')")
    run = str(run).strip()
    root = common.runs_dir()
    if run == "latest":
        cands = [d for d in root.iterdir() if (d / "manifest.json").is_file()] if root.is_dir() else []
        if not cands:
            raise FileNotFoundError(f"no runs found in {root.as_posix()}")

        def key(d: Path) -> tuple[str, str]:
            try:
                created = str(common.read_json(d / "manifest.json").get("created_at") or "")
            except (OSError, ValueError):
                created = ""
            return created, d.name

        return max(cands, key=key)
    direct = Path(run)
    if direct.is_dir() and (direct / "manifest.json").is_file():
        return direct.resolve()
    candidate = root / run
    if not (candidate / "manifest.json").is_file():
        raise FileNotFoundError(f"run not found: {run} (looked in {root.as_posix()})")
    return candidate


def run_cfg(run_dir: Path) -> dict:
    """committee.yaml of the run (the inputs/ snapshot), else the active config."""
    snap = Path(run_dir) / "inputs"
    if (snap / "committee.yaml").is_file():
        return common.committee_cfg(snap)
    return common.committee_cfg()


def _orchestration(cfg: dict) -> tuple[float, int, int]:
    orch = cfg.get("orchestration") or {}
    budget = float(orch.get("step_time_budget_seconds") or DEFAULT_STEP_BUDGET_S)
    attempts = int(orch.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    concurrency = int(cfg.get("concurrency") or DEFAULT_CONCURRENCY)
    return budget, max(1, attempts), max(1, concurrency)


def _cfg_get(cfg: dict | None, dotted: str, default: Any = None) -> Any:
    value = common.get_dotted(cfg or {}, dotted, None)
    return default if value is None else value


def _breaker_cfg(cfg: dict) -> tuple[float, int]:
    """``(fraction, min_orders)`` of the circuit breaker; a fraction above 1 disables it."""
    frac = common.safe_float(_cfg_get(cfg, "orchestration.circuit_breaker_fraction",
                                      DEFAULT_CIRCUIT_BREAKER_FRACTION))
    minimum = common.safe_float(_cfg_get(cfg, "orchestration.circuit_breaker_min_orders",
                                         DEFAULT_CIRCUIT_BREAKER_MIN_ORDERS))
    return (DEFAULT_CIRCUIT_BREAKER_FRACTION if frac is None else float(frac),
            DEFAULT_CIRCUIT_BREAKER_MIN_ORDERS if minimum is None else max(1, int(minimum)))


def apply_run_env(manifest: dict, run_dir: Path) -> None:
    """Point the AGINV_* environment at the run: config = the inputs/ snapshot, as-of = the run's
    as-of, cache / ledger from the manifest, plus the stored dry-run env (fixtures, now) when the
    caller did not set it."""
    snap = Path(run_dir) / "inputs"
    if snap.is_dir():
        os.environ[common.ENV_CONFIG_DIR] = str(snap.resolve())
    if manifest.get("as_of"):
        os.environ[common.ENV_AS_OF] = str(manifest["as_of"])
    paths = manifest.get("paths") or {}
    if paths.get("cache_dir"):
        os.environ[common.ENV_CACHE_DIR] = str(paths["cache_dir"])
    if paths.get("ledger"):
        os.environ[common.ENV_LEDGER] = str(paths["ledger"])
    for var, value in (manifest.get("env") or {}).items():
        if var in common.AGINV_ENV_VARS and value and not os.environ.get(var, "").strip():
            os.environ[var] = str(value)
    common.clear_caches()


# =====================================================================================
# JSON-schema validation (work orders, failed stubs) — registry of every schema by $id
# =====================================================================================

_REGISTRY_CACHE: dict[tuple, Any] = {}


def _registry() -> Any:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    root = common.schemas_dir()
    files = sorted(root.glob("*.json")) + sorted((root / "data").glob("*.json"))
    key = tuple((str(f), f.stat().st_mtime_ns) for f in files)
    if key not in _REGISTRY_CACHE:
        resources = []
        for f in files:
            doc = common.read_json(f)
            sid = doc.get("$id") if isinstance(doc, dict) else None
            if sid:
                resources.append((sid, Resource.from_contents(doc, default_specification=DRAFT202012)))
        _REGISTRY_CACHE.clear()
        _REGISTRY_CACHE[key] = Registry().with_resources(resources)
    return _REGISTRY_CACHE[key]


def schema_errors(doc: Any, schema_name: str) -> list[str]:
    """Plain JSON-Schema errors of ``doc`` against schema ``schema_name`` (no semantic checks)."""
    from jsonschema import Draft202012Validator, FormatChecker

    validator = Draft202012Validator(common.load_schema(schema_name), registry=_registry(),
                                     format_checker=FormatChecker())
    errs = sorted(validator.iter_errors(doc),
                  key=lambda e: (common.format_error_path(e.absolute_path), e.message))
    return [f"{common.format_error_path(e.absolute_path)}: {_clip(e.message, 300)}" for e in errs]


# =====================================================================================
# init
# =====================================================================================


def _mode_label(mode: dict) -> str:
    if mode.get("holding"):
        return "holding"
    if mode.get("quick"):
        return "quick"
    if mode.get("sectors"):
        return "sectors"
    return "full"


def _parse_sectors(raw: str | Iterable[str] | None) -> list[str] | None:
    if raw is None:
        return None
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        key = str(item).strip().lower().replace("-", "_").replace(" ", "_")
        if key and key not in out:
            out.append(key)
    return out or None


def _run_stamp() -> str:
    """``YYYY-MM-DD_HHMM``: date from ``common.today()`` (AGINV_AS_OF aware), time from
    ``AGINV_NOW`` when set (verbatim wall time), else the local clock."""
    raw = os.environ.get(common.ENV_NOW, "").strip()
    moment = None
    if raw:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            moment = None
    if moment is None:
        moment = datetime.now()
    return f"{common.today().isoformat()}_{moment:%H%M}"


def _create_run_dir(run_id: str | None, *, dry_run: bool) -> tuple[str, Path]:
    root = common.runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    if run_id:
        if not RUN_ID_RE.match(run_id):
            raise InitError(f"invalid --run-id {run_id!r}",
                            ["expected YYYY-MM-DD_HHMM with optional _suffixes, e.g. 2026-10-03_1015"])
        target = root / run_id
        try:
            target.mkdir()
        except FileExistsError as exc:
            raise InitError(f"run {run_id} already exists", [target.as_posix()]) from exc
        return run_id, target
    base = _run_stamp()
    for n in range(1, 1000):
        rid = base + (f"_{n}" if n > 1 else "") + ("_dryrun" if dry_run else "")
        target = root / rid
        try:
            target.mkdir()
        except FileExistsError:
            continue
        return rid, target
    raise RuntimeError(f"could not allocate a run id for {base} in {root.as_posix()}")


def _versions(cfg: dict) -> dict:
    from importlib import metadata

    out: dict[str, Any] = {"committee": COMMITTEE_VERSION, "python": platform.python_version(),
                           "committee_yaml": cfg.get("version")}
    for dist in ("pandas", "numpy", "yfinance", "jsonschema", "jinja2", "PyYAML", "requests"):
        try:
            out[dist.lower()] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist.lower()] = None
    return out


def _new_step(name: str, kind: str) -> dict:
    if kind not in ("python", "agents"):
        raise ValueError(f"step {name!r}: unknown kind {kind!r} (expected python|agents)")
    return {"name": name, "kind": kind, "status": "pending", "started": None, "ended": None,
            "summary": None, "outputs": []}


def _ensure_steps(manifest: dict, ctx: Any, steps_mod: Any) -> None:
    """Fill ``manifest["steps"]`` from ``_steps.step_sequence`` once (kept for the whole run)."""
    if manifest.get("steps"):
        return
    seq = steps_mod.step_sequence(ctx)
    steps = []
    seen: set[str] = set()
    for item in seq:
        if isinstance(item, dict):
            name, kind = item.get("name"), item.get("kind")
        else:
            name, kind = item[0], item[1]
        if name in seen:
            raise ValueError(f"step_sequence lists {name!r} twice")
        seen.add(name)
        steps.append(_new_step(str(name), str(kind)))
    if not steps:
        raise ValueError("step_sequence returned no steps")
    manifest["steps"] = steps


def _make_ctx(steps_mod: Any, run_dir: Path, manifest: dict) -> tuple[Any, dict]:
    ctx = steps_mod.RunContext(Path(run_dir), manifest)
    shared = getattr(ctx, "manifest", None)
    return ctx, (shared if isinstance(shared, dict) else manifest)


def init_run(*, quick: bool = False, sectors: Iterable[str] | str | None = None,
             holding: str | None = None, dry_run: bool = False, run_id: str | None = None,
             fixtures_dir: str | Path | None = None) -> dict:
    """Validate configs, create ``runs/<run_id>/`` with the config snapshot, manifest and run_log.
    Returns ``{"run_id","run_dir","mode","mode_label"}``. Raises ``InitError`` for config / argument
    problems (CLI exit code 2)."""
    result = common.validate_configs()
    errors = common.config_errors(result)
    if errors:
        if any("/committee-setup" in e for e in errors):
            msg = ("configuration incomplete — run /committee-setup to fill config/portfolio.yaml "
                   "(details below; re-check with: python scripts/committee.py validate-config)")
        else:
            msg = ("configuration invalid — fix the config files listed in details "
                   "(re-check with: python scripts/committee.py validate-config)")
        raise InitError(msg, errors)

    sector_list = _parse_sectors(sectors)
    if sector_list:
        unknown = [s for s in sector_list if s not in common.SECTOR_KEYS]
        if unknown:
            raise InitError(f"unknown sector key(s): {', '.join(unknown)}",
                            [f"valid keys: {', '.join(common.SECTOR_KEYS)}"])
    holding_t = None
    if holding:
        try:
            holding_t = common.norm_ticker(holding)
        except ValueError as exc:
            raise InitError(f"invalid ticker {holding!r}", [str(exc)]) from exc

    rid, run_dir = _create_run_dir(run_id, dry_run=dry_run)
    for sub in RUN_SUBDIRS:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    src_cfg = common.config_dir()
    shas: dict[str, str] = {}
    for f in sorted(src_cfg.glob("*.yaml")):
        dest = run_dir / "inputs" / f.name
        dest.write_bytes(f.read_bytes())
        shas[f.name] = _sha256(dest)
    cfg = common.committee_cfg(run_dir / "inputs")

    as_of = common.today().isoformat()
    mode = {"dry_run": bool(dry_run), "quick": bool(quick), "sectors": sector_list, "holding": holding_t}
    cache_dir = (run_dir / ".cache") if dry_run else common.cache_dir()
    # a dry run can never touch the real ledger: its effective ledger is the preview CSV
    ledger = (run_dir / "phase4" / "ledger_preview.csv") if dry_run else common.ledger_path()
    env: dict[str, str] = {}
    init_warnings: list[str] = []
    if dry_run:
        # A dry run never touches the network: fixture providers when a fixtures dir is known,
        # and AGINV_OFFLINE=1 always (fixture mode wins over offline in common's seams, so with
        # fixtures this changes nothing; without them every later `next` is cache-only).
        fx = fixtures_dir or os.environ.get(common.ENV_FIXTURES_DIR, "").strip()
        if fx:
            env[common.ENV_FIXTURES_DIR] = _posix(fx)
        else:
            init_warnings.append(f"init: dry run without a fixtures dir — data steps are cache-only "
                                 f"({common.ENV_OFFLINE}=1); use `committee.py dry-run` or set "
                                 f"{common.ENV_FIXTURES_DIR} for fixture data")
        env[common.ENV_OFFLINE] = "1"
        if os.environ.get(common.ENV_NOW, "").strip():
            env[common.ENV_NOW] = os.environ[common.ENV_NOW].strip()
    manifest: dict[str, Any] = {
        "run_id": rid,
        "created_at": common.now_iso(),
        "as_of": as_of,
        "mode": mode,
        "mode_label": _mode_label(mode),
        "status": "in_progress",
        "paths": {"run_dir": _posix(run_dir), "config_dir": _posix(src_cfg),
                  "cache_dir": _posix(cache_dir), "ledger": _posix(ledger)},
        "env": env,
        "config_sha256": shas,
        "versions": _versions(cfg),
        "holdings": [],
        "candidates": {},
        "finalists": {},
        "expand_sectors": [],
        "expand_dropped": [],
        "steps": [],
        "orders": {},
        "warnings": list(init_warnings),
    }
    common.write_text(run_dir / "run_log.md",
                      f"# Run log — {rid}\n\n"
                      f"> {common.DISCLAIMER}\n\n"
                      f"- {manifest['created_at']} init: mode={manifest['mode_label']} as_of={as_of} "
                      f"dry_run={str(bool(dry_run)).lower()} sectors={','.join(sector_list or []) or '-'} "
                      f"holding={holding_t or '-'} config={src_cfg.as_posix()}\n")
    for w in init_warnings:
        log(run_dir, f"WARNING {w}")
    save_manifest(run_dir, manifest)

    # Plan the steps now when _steps is importable (otherwise the first `next` does it).
    try:
        steps_mod = _steps_module()
    except ImportError as exc:
        manifest["warnings"].append(f"init: step plan deferred ({exc})")
        log(run_dir, f"init: step plan deferred ({exc})")
    else:
        ctx, manifest = _make_ctx(steps_mod, run_dir, manifest)
        _ensure_steps(manifest, ctx, steps_mod)
        log(run_dir, "init: steps " + ", ".join(f"{s['name']}({s['kind']})" for s in manifest["steps"]))
    save_manifest(run_dir, manifest)
    return {"run_id": rid, "run_dir": _posix(run_dir), "mode": mode, "mode_label": manifest["mode_label"]}


# =====================================================================================
# Work orders
# =====================================================================================


def _variant_path(base_output: str, tag: str) -> str:
    """``.../fundamental_KO.json`` + ``r2`` -> ``.../fundamental_KO.r2.json``."""
    p = Path(base_output)
    stem = p.name[:-5] if p.name.lower().endswith(".json") else p.name
    return (p.parent / f"{stem}.{tag}.json").as_posix()


def _phase_of(spec: dict, step: str) -> int | None:
    ph = spec.get("phase")
    if isinstance(ph, int) and not isinstance(ph, bool):
        return ph
    m = _STEP_NAME_RE.match(step or "")
    return int(m.group(1)) if m else None


def _abs_path(run_dir: Path, raw: Any) -> str:
    p = Path(str(raw))
    if not p.is_absolute():
        p = Path(run_dir) / p
    return _posix(p)


def _schema_file(name: str) -> str:
    try:
        return _posix(common.schema_path(name))
    except KeyError:
        return str(name)


def _order_index_path(run_dir: Path) -> Path:
    return Path(run_dir) / "orders" / "index.json"


def _load_order_index(run_dir: Path) -> dict:
    path = _order_index_path(run_dir)
    if path.is_file():
        doc = common.read_json(path)
        if isinstance(doc, dict) and isinstance(doc.get("orders"), dict):
            return doc
    return {"run_id": None, "orders": {}}


def _spec_for(run_dir: Path, order_id: str, index: dict | None = None) -> dict:
    idx = index if index is not None else _load_order_index(run_dir)
    entry = (idx.get("orders") or {}).get(order_id)
    if not entry or not isinstance(entry.get("spec"), dict):
        raise KeyError(f"order {order_id}: spec missing from {_order_index_path(run_dir).as_posix()}")
    return entry["spec"]


def _schema_name(raw: Any) -> str | None:
    """Schema name from a ``SCHEMA_FILES`` key or a schema file path (``.../specialist_report.json``,
    ``.../data/work_order.schema.json``); None if unknown."""
    s = str(raw or "").strip().replace("\\", "/")
    if not s:
        return None
    if s in common.SCHEMA_FILES:
        return s
    base = s.rsplit("/", 1)[-1]
    for suffix in (".schema.json", ".json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base if base in common.SCHEMA_FILES else None


def _notes_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    return [str(n).strip() for n in items if n is not None and str(n).strip()]


def _normalize_spec(spec: dict, step: str, run_dir: Path) -> dict:
    """Validated, JSON-safe copy of a ``_steps.build_orders`` spec. ``schema`` may be a schema
    name or a schema file path (``schema_name`` wins when given); it is normalized to the name.
    ``notes`` may be a string or a list of strings (normalized to a list)."""
    if not isinstance(spec, dict):
        raise TypeError(f"build_orders({step}) returned a non-dict order spec: {spec!r}")
    s = common.to_jsonable(copy.deepcopy(spec))
    for key in ("order_id", "agent_role", "output"):
        if not s.get(key):
            raise ValueError(f"order spec from {step} lacks {key!r}: {s.get('order_id')!r}")
    role = s["agent_role"]
    name = _schema_name(s.get("schema_name")) or _schema_name(s.get("schema"))
    if name is None:
        try:
            name = common.role_schema(role, s.get("mode"))
        except KeyError as exc:
            raise ValueError(f"order {s['order_id']}: unknown schema {s.get('schema')!r}") from exc
    s["schema"] = name
    s["schema_name"] = name
    s.setdefault("step", step)
    if s["step"] != step:
        raise ValueError(f"order {s['order_id']}: spec step {s['step']!r} != {step!r}")
    s["agent_type"] = s.get("agent_type") or common.AGENT_TYPES.get(role)
    for key in ("mode", "ticker", "sector", "sector_class"):
        s.setdefault(key, None)
    s["phase"] = _phase_of(s, step)
    s["output"] = _abs_path(run_dir, s["output"])
    s["inputs"] = [dict(i, path=_abs_path(run_dir, i.get("path"))) for i in (s.get("inputs") or [])
                   if isinstance(i, dict) and i.get("path")]
    for src in s.get("seed_sources") or []:
        if isinstance(src, dict) and src.get("path"):
            src["path"] = _abs_path(run_dir, src["path"])
    s["seed_sources"] = list(s.get("seed_sources") or [])
    s["required_fields"] = [str(f) for f in (s.get("required_fields") or [])]
    s["word_limits"] = dict(s.get("word_limits") or {})
    s["notes"] = _notes_list(s.get("notes"))
    return s


def _new_order_record(spec: dict, run_dir: Path) -> dict:
    return {
        "step": spec["step"], "agent_type": spec["agent_type"], "agent_role": spec["agent_role"],
        "mode": spec.get("mode"), "ticker": spec.get("ticker"), "sector": spec.get("sector"),
        "sector_class": spec.get("sector_class"), "phase": spec.get("phase"),
        "attempt": 1, "status": "dispatched",
        "order_path": _posix(Path(run_dir) / "orders" / f"{common.safe_name(spec['order_id'])}.md"),
        "output": spec["output"], "base_output": spec["output"], "resolved_output": None,
        "schema": spec["schema"], "errors": [],
    }


def front_matter(spec: dict, rec: dict, *, run_id: str, as_of: str, prior_errors: list[str]) -> dict:
    """The work-order front-matter dict (DESIGN §2.1), JSON/YAML-safe (dates as strings)."""
    fm = OrderedDict([
        ("order_id", spec["order_id"]),
        ("run_id", run_id),
        ("step", spec["step"]),
        ("agent_type", spec["agent_type"]),
        ("agent_role", spec["agent_role"]),
        ("mode", spec.get("mode")),
        ("ticker", spec.get("ticker")),
        ("sector", spec.get("sector")),
        ("sector_class", spec.get("sector_class")),
        ("attempt", int(rec["attempt"])),
        ("as_of", as_of),
        ("inputs", [{"path": i["path"], "purpose": str(i.get("purpose") or ""),
                     **{k: v for k, v in i.items() if k not in ("path", "purpose")}}
                    for i in spec.get("inputs") or []]),
        ("output", rec["output"]),
        ("schema", _schema_file(spec["schema"])),
        ("schema_name", spec["schema"]),
        ("seed_sources", spec.get("seed_sources") or []),
        ("required_fields", spec.get("required_fields") or []),
        ("prior_errors", [_clip(e) for e in prior_errors[:PRIOR_ERRORS_MAX]]),
        ("word_limits", spec.get("word_limits") or {}),
    ])
    return common.to_jsonable(fm)


def chair_feedback_lines(text: Any) -> list[str]:
    """Chair feedback text -> ``prior_errors`` entries, each ``CHAIR FEEDBACK: <line>`` (one per
    non-empty line, long lines split at word boundaries so no entry is truncated)."""
    out: list[str] = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = " ".join(raw.split())
        while len(s) > CHAIR_LINE_MAX:
            cut = s.rfind(" ", 0, CHAIR_LINE_MAX)
            cut = cut if cut > CHAIR_LINE_MAX // 3 else CHAIR_LINE_MAX
            out.append(f"{CHAIR_PREFIX} {s[:cut].strip()}")
            s = s[cut:].strip()
        if s:
            out.append(f"{CHAIR_PREFIX} {s}")
    return out


def split_prior_errors(prior: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    """``prior_errors`` -> ``(chair feedback without the prefix, inputs-changed lines, validator errors)``."""
    chair: list[str] = []
    changed: list[str] = []
    validator: list[str] = []
    for e in prior:
        s = str(e)
        if s.startswith(CHAIR_PREFIX):
            chair.append(s[len(CHAIR_PREFIX):].strip())
        elif s.startswith(INPUTS_CHANGED_PREFIX):
            changed.append(s)
        else:
            validator.append(s)
    return chair, changed, validator


def _template_env() -> Any:
    import jinja2

    dirs: list[str] = []
    for d in (common.templates_dir(), common.REPO_ROOT / "templates"):
        if str(d) not in dirs:
            dirs.append(str(d))
    return jinja2.Environment(loader=jinja2.FileSystemLoader(dirs), autoescape=False,
                              keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
                              undefined=jinja2.StrictUndefined)


def _heading(fm: dict) -> str:
    who = fm["agent_type"]
    if fm.get("ticker"):
        where = f" on {fm['ticker']}" + (f" ({fm['sector']})" if fm.get("sector") else "")
    elif fm.get("sector"):
        where = f" — {fm['sector']}"
    else:
        where = ""
    mode = f" [{fm['mode']}]" if fm.get("mode") else ""
    return f"{who}{where}{mode}"


def render_order_text(fm: dict, spec: dict, cfg: dict, *, order_path: str) -> str:
    """Front matter (``yaml.safe_dump``) + the rules body from ``templates/work_order.md.j2``.
    Thresholds quoted in the rules come from committee.yaml (``validation``, ``phase3``)."""
    import yaml

    fm_yaml = yaml.safe_dump(dict(fm), sort_keys=False, allow_unicode=True, default_flow_style=False,
                             width=4096)
    rules = dict(DEFAULT_VALIDATION_RULES)
    rules.update({k: v for k, v in (cfg.get("validation") or {}).items() if k in DEFAULT_VALIDATION_RULES})
    p3 = dict(DEFAULT_PHASE3_RULES)
    p3.update({k: v for k, v in (cfg.get("phase3") or {}).items() if k in DEFAULT_PHASE3_RULES})
    chair_fb, changed, validator = split_prior_errors(fm.get("prior_errors") or [])
    template = _template_env().get_template(WORK_ORDER_TEMPLATE)
    return template.render(front_matter=fm_yaml, o=fm, heading=_heading(fm),
                           notes=_notes_list(spec.get("notes")), order_path=order_path, v=rules, p3=p3,
                           disclaimer=common.DISCLAIMER, chair_feedback=chair_fb, inputs_changed=changed,
                           validator_errors=validator)


def build_order_file(spec: dict, rec: dict, *, run_id: str, as_of: str, prior_errors: list[str],
                     cfg: dict) -> tuple[dict, str]:
    """``(front_matter, text)`` of ``orders/<order_id>.md`` without writing anything; the front
    matter must validate against work_order."""
    fm = front_matter(spec, rec, run_id=run_id, as_of=as_of, prior_errors=prior_errors)
    errs = schema_errors(fm, "work_order")
    if errs:
        raise ValueError(f"work order {spec['order_id']}: front matter invalid: " + "; ".join(errs[:8]))
    return fm, render_order_text(fm, spec, cfg, order_path=rec["order_path"])


def write_order_file(run_dir: Path, spec: dict, rec: dict, *, run_id: str, as_of: str,
                     prior_errors: list[str], cfg: dict) -> dict:
    """Render and write ``orders/<order_id>.md``; the front matter must validate against work_order."""
    fm, text = build_order_file(spec, rec, run_id=run_id, as_of=as_of, prior_errors=prior_errors, cfg=cfg)
    common.write_text(rec["order_path"], text)
    return fm


def _batch_summary(step: str, recs: list[tuple[str, dict]], *, retry: bool) -> str:
    """One line: ``p2.wave_a: 14 orders (KO, PG, JPM, BRK-B x4; QQQ, SPMO x2)``."""
    counts: "OrderedDict[str, int]" = OrderedDict()
    for oid, rec in recs:
        label = rec.get("ticker") or rec.get("sector") or rec.get("agent_role") or oid
        counts[label] = counts.get(label, 0) + 1
    by_count: "OrderedDict[int, list[str]]" = OrderedDict()
    for label, n in counts.items():
        by_count.setdefault(n, []).append(str(label))
    parts = [", ".join(labels) + (f" x{n}" if n > 1 else "") for n, labels in by_count.items()]
    n = len(recs)
    head = f"{step}: {n} order{'s' if n != 1 else ''}"
    if retry:
        attempts = sorted({int(r.get("attempt") or 1) for _oid, r in recs})
        head = f"{step} retry (attempt {', '.join(map(str, attempts))}): {n} order{'s' if n != 1 else ''}"
    return f"{head} ({'; '.join(parts)})" if parts else head


def _agents_payload(manifest: dict, step: str, recs: list[tuple[str, dict]], concurrency: int,
                    *, retry: bool) -> dict:
    return {
        "run_id": manifest["run_id"],
        "step": step,
        "type": "agents",
        "concurrency": concurrency,
        "orders": [{"order_id": oid, "agent_type": rec["agent_type"], "order_path": rec["order_path"]}
                   for oid, rec in recs],
        "summary": _batch_summary(step, recs, retry=retry),
    }


# =====================================================================================
# The state machine
# =====================================================================================


class _Run:
    """Mutable state of one ``next`` call."""

    def __init__(self, run_dir: Path, manifest: dict, ctx: Any, steps_mod: Any) -> None:
        self.run_dir = Path(run_dir)
        self.manifest = manifest
        self.ctx = ctx
        self.steps_mod = steps_mod
        self.cfg = run_cfg(run_dir)
        self.budget, self.max_attempts, self.concurrency = _orchestration(self.cfg)
        self.breaker_fraction, self.breaker_min_orders = _breaker_cfg(self.cfg)
        self.ignore_breaker = False
        self.current: str | None = None

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]

    @property
    def as_of(self) -> str:
        return str(self.manifest.get("as_of") or common.today().isoformat())

    def save(self) -> None:
        save_manifest(self.run_dir, self.manifest)

    def log(self, msg: str, **kw: Any) -> None:
        log(self.run_dir, msg, **kw)


def _step_by_name(manifest: dict, name: str) -> dict | None:
    for s in manifest.get("steps") or []:
        if s.get("name") == name:
            return s
    return None


def _run_python_step(st: _Run, step: dict) -> None:
    name = step["name"]
    step.update(status="running", started=common.now_iso(), ended=None)
    step.pop("error", None)
    st.save()
    st.log(f"[{name}] python step started")
    res = st.steps_mod.run_python_step(name, st.ctx) or {}
    if not isinstance(res, dict):
        raise TypeError(f"run_python_step({name}) returned {type(res).__name__}, expected dict")
    outputs = [Path(o).as_posix() if isinstance(o, Path) else str(o) for o in (res.get("outputs") or []) if o]
    step.update(status="done", ended=common.now_iso(), summary=res.get("summary"), outputs=outputs)
    extra = {k: v for k, v in res.items() if k not in ("summary", "outputs", "warnings", "skip_steps")}
    if extra:
        step["result"] = common.to_jsonable(extra)  # e.g. memo_md / memo_html / summary_path
    for w in res.get("warnings") or []:
        st.manifest.setdefault("warnings", []).append(f"{name}: {w}")
    for skip in res.get("skip_steps") or []:
        other = _step_by_name(st.manifest, str(skip))
        if other is not None and other.get("status") in ("pending", "failed"):
            other.update(status="skipped", summary=f"skipped by {name}", ended=common.now_iso())
            other.pop("error", None)
            st.log(f"[{other['name']}] skipped by {name}")
    st.save()
    st.log(f"[{name}] done: {res.get('summary') or ''}".rstrip())


def _dispatch_step(st: _Run, step: dict) -> dict | None:
    """Build and render the orders of an agents step. Returns the agents payload, or None when the
    step has no orders to launch (then it is marked done).

    A step reset by a chair ``reopen`` carries ``step["invalidated"]``: orders listed in ``keep`` keep
    their finished record and output (nothing is rebuilt for them); every other order is built as a
    fresh order whose ``prior_errors`` say the inputs changed, and an order the chair named itself
    additionally carries the chair's feedback and continues its attempt numbering."""
    name = step["name"]
    specs_raw = st.steps_mod.build_orders(name, st.ctx) or []
    if not specs_raw:
        step.update(status="done", started=step.get("started") or common.now_iso(), ended=common.now_iso(),
                    summary="no orders", outputs=[], orders=[])
        step.pop("error", None)
        step.pop("invalidated", None)
        st.save()
        st.log(f"[{name}] no orders — step done")
        return None
    inv = step.get("invalidated") if isinstance(step.get("invalidated"), dict) else None
    keep = set(inv.get("keep") or []) if inv else set()
    carry = (inv.get("carry") or {}) if inv else {}
    existing = st.manifest.setdefault("orders", {})
    records: "OrderedDict[str, dict]" = OrderedDict()
    fresh: "OrderedDict[str, dict]" = OrderedDict()
    specs: dict[str, dict] = {}
    for raw in specs_raw:
        spec = _normalize_spec(raw, name, st.run_dir)
        oid = spec["order_id"]
        if oid in records or (oid in existing and existing[oid].get("step") != name):
            raise ValueError(f"duplicate order id {oid!r} in step {name}")
        if oid in keep and (existing.get(oid) or {}).get("status") in ("done", "failed"):
            records[oid] = existing[oid]                 # unaffected by the chair re-run: keep as is
            continue
        rec = _new_order_record(spec, st.run_dir)
        prior: list[str] = []
        if inv:
            prior = [str(inv["text"])]
            rec["rechecked_round"] = inv.get("round")
            c = carry.get(oid)
            if c:
                rec["attempt"] = int(c["attempt"])
                rec["output"] = _variant_path(rec["base_output"], f"r{rec['attempt']}")
                rec["reopened_by"] = "chair"
                rec["reopen_round"] = c.get("round")
                rec["reopen_history"] = list(c.get("history") or [])
                if c.get("attempt_limit"):
                    rec["attempt_limit"] = int(c["attempt_limit"])
                prior += list(c.get("feedback") or [])
        if prior:
            rec["chair_prior"] = list(prior)
        write_order_file(st.run_dir, spec, rec, run_id=st.run_id, as_of=st.as_of, prior_errors=prior, cfg=st.cfg)
        records[oid] = rec
        fresh[oid] = rec
        specs[oid] = spec
    # commit only after every order rendered
    if specs:
        index = _load_order_index(st.run_dir)
        index["run_id"] = st.run_id
        for oid, spec in specs.items():
            index["orders"][oid] = {"step": name, "spec": spec}
        common.write_json(_order_index_path(st.run_dir), index)
    existing.update(fresh)
    step.pop("invalidated", None)
    step.update(status="dispatched", started=step.get("started") or common.now_iso(), ended=None,
                orders=list(records))
    step.pop("error", None)
    if not fresh:                                         # everything was kept: nothing to launch
        _finish_agent_step(st, step)
        return None
    payload = _agents_payload(st.manifest, name, list(fresh.items()), st.concurrency, retry=False)
    step["summary"] = payload["summary"]
    st.save()
    st.log(f"[{name}] dispatched {payload['summary']}"
           + (f" ({len(records) - len(fresh)} unaffected order(s) kept)" if len(records) > len(fresh) else ""))
    return payload


def _finish_agent_step(st: _Run, step: dict) -> None:
    """Mark an agents step ``done`` once every one of its orders is done or failed."""
    orders = st.manifest.setdefault("orders", {})
    ids = step.get("orders") or []
    n_done = sum(1 for o in ids if orders[o]["status"] == "done")
    n_failed = sum(1 for o in ids if orders[o]["status"] == "failed")
    step.update(status="done", ended=common.now_iso(),
                summary=f"{n_done} done, {n_failed} failed ({len(ids)} order{'s' if len(ids) != 1 else ''})",
                outputs=[orders[o]["resolved_output"] for o in ids])
    step.pop("error", None)
    st.save()
    st.log(f"[{step['name']}] step done: {step['summary']}")


def _check_output(st: _Run, oid: str, rec: dict, spec: dict,
                  warnings: list[str] | None = None) -> list[str]:
    """Validation errors of the order's current output ([] = valid). Never mutates the manifest:
    a validator crash is reported through ``warnings`` (the caller commits them)."""
    out = Path(rec["output"])
    if not out.is_file():
        return [f"output file missing: {out.as_posix()}"]
    try:
        doc = common.read_json(out)
    except (ValueError, OSError) as exc:
        return [f"output is not valid JSON: {_clip(exc, 300)}"]
    errors: list[str] = []
    if isinstance(doc, dict):
        for key, expected in (("run_id", st.run_id), ("order_id", oid)):
            got = doc.get(key)
            if got is not None and got != expected:
                errors.append(f"{key}: expected {expected!r} (got {got!r})")
    vspec = dict(spec, output=rec["output"], attempt=rec["attempt"])
    try:
        context = st.steps_mod.validation_context(st.ctx, vspec)
        found = _validate_module().validate_doc(doc, rec.get("schema") or spec["schema"], context=context)
    except ImportError:
        raise
    except Exception as exc:  # validator crash on a strange document: count it as invalid, but tell
        msg = f"validator error: {type(exc).__name__}: {_clip(exc, 300)}"
        if warnings is not None:
            warnings.append(f"{st.current}: {oid}: {msg}")
        st.log(f"[{st.current}] {oid}: {msg}", block=traceback.format_exc(limit=6))
        found = [msg]
    errors.extend(str(e) for e in (found or []))
    return errors


_TICKER_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.=-]{0,19}$")


def failed_stub(st: _Run, oid: str, rec: dict, errors: list[str]) -> dict:
    """``failed_report`` stub written after the last failed attempt (DESIGN §5)."""
    sector = rec.get("sector")
    ticker = rec.get("ticker")
    if ticker is not None and not _TICKER_RE.match(str(ticker)):
        try:
            ticker = common.norm_ticker(ticker)
        except ValueError:
            ticker = None
        if ticker is not None and not _TICKER_RE.match(ticker):
            ticker = None
    return {
        "schema_version": "1.0",
        "run_id": st.run_id,
        "order_id": oid,
        "agent_role": rec["agent_role"],
        "ticker": ticker,
        "sector": sector if sector in common.SECTOR_KEYS or sector == "funds" else None,
        "as_of": st.as_of,
        "status": "failed",
        "attempts": int(rec["attempt"]),
        "errors": [_clip(e) for e in errors],
        "step": rec.get("step"),
        "agent_type": rec.get("agent_type"),
        "outputs_tried": [rec["base_output"]] + [_variant_path(rec["base_output"], f"r{n}")
                                                for n in range(2, int(rec["attempt"]) + 1)],
        "note": "Written by committee.py after the last failed attempt; scored as evidence "
                "failed and excluded from the conviction mean. Never fabricate the missing report.",
    }


def _circuit_breaker(st: _Run, step: dict,
                     checked: list[tuple[str, dict, dict, list[str]]]) -> dict | None:
    """Error payload when the step looks like an agent outage, else None.

    Trips on the FIRST collection only (every pending order is still on attempt 1) when at least
    ``breaker_min_orders`` orders are pending and at least ``breaker_fraction`` of them have no
    output FILE at all. A tripped breaker changes nothing but ``step["circuit_breaker"]`` /
    ``step["error"]``: no attempt is counted, no retry order or stub is written."""
    if st.ignore_breaker or not checked or len(checked) < st.breaker_min_orders:
        return None
    if any(int(rec.get("attempt") or 1) > 1 for _oid, rec, _spec, _errs in checked):
        return None                                   # retries behave as before
    missing = [oid for oid, rec, _spec, _errs in checked if not Path(rec["output"]).is_file()]
    total = len(checked)
    if not missing or len(missing) / total < st.breaker_fraction - 1e-9:
        return None
    message = (f"{len(missing)} of {total} agent orders produced no output - likely a usage limit or outage; "
               f"re-run `redispatch --run {st.run_id}` then `next` once agents are available")
    info = {"no_output": len(missing), "orders": total, "step": step["name"],
            "missing": missing[:CIRCUIT_BREAKER_LIST_MAX], "at": common.now_iso()}
    step["circuit_breaker"] = info
    step["error"] = message
    st.save()
    st.log(f"[{step['name']}] CIRCUIT BREAKER: {message} (attempt counters unchanged)")
    return {"type": "error", "run_id": st.run_id, "step": step["name"], "message": message,
            "hint": f"launch the missing orders again (`redispatch --run {st.run_id}` lists them), then call "
                    f"`next`; `next --run {st.run_id} --ignore-circuit-breaker` accepts the failures instead",
            "circuit_breaker": info}


def _collect_step(st: _Run, step: dict) -> dict | None:
    """Check every dispatched order of the step. Returns the retry payload, or None when every
    order is done/failed (the step is then marked done).

    All-or-nothing: every output is checked and every retry order / failed stub is built in memory
    first; files are written and the manifest is updated only after that succeeded. An exception
    part-way (validator import error, invalid stub ...) therefore leaves the manifest exactly as it
    was, so the next ``next`` re-checks the same attempts — no attempt is consumed by a retry that
    was never emitted."""
    name = step["name"]
    orders = st.manifest.setdefault("orders", {})
    index = _load_order_index(st.run_dir)
    warnings: list[str] = []

    # 1) check outputs (no side effects)
    checked: list[tuple[str, dict, dict, list[str]]] = []
    for oid in step.get("orders") or []:
        rec = orders.get(oid)
        if rec is None:
            raise KeyError(f"step {name}: order {oid} missing from manifest")
        if rec.get("status") in ("done", "failed"):
            continue
        spec = _spec_for(st.run_dir, oid, index)
        checked.append((oid, rec, spec, _check_output(st, oid, rec, spec, warnings)))

    tripped = _circuit_breaker(st, step, checked)
    if tripped is not None:
        return tripped
    step.pop("circuit_breaker", None)
    step.pop("error", None)

    # 2) build the new records, retry order texts and failed stubs (still no side effects)
    updates: "OrderedDict[str, dict]" = OrderedDict()
    order_files: list[tuple[str, str]] = []          # (order_path, text)
    stub_files: list[tuple[str, dict]] = []          # (stub_path, stub)
    messages: list[str] = []
    retries: list[tuple[str, dict]] = []
    for oid, rec, spec, errors in checked:
        if not errors:
            updates[oid] = dict(copy.deepcopy(rec), status="done", resolved_output=rec["output"])
            messages.append(f"[{name}] {oid} done (attempt {rec['attempt']})")
            continue
        tagged = [f"attempt {rec['attempt']}: {_clip(e)}" for e in errors]
        if int(rec["attempt"]) < int(rec.get("attempt_limit") or st.max_attempts):
            new = copy.deepcopy(rec)
            new["attempt"] = int(rec["attempt"]) + 1
            new["output"] = _variant_path(rec["base_output"], f"r{new['attempt']}")
            new["errors"] = list(rec.get("errors") or []) + tagged
            new["status"] = "dispatched"
            _fm, text = build_order_file(spec, new, run_id=st.run_id, as_of=st.as_of,
                                         prior_errors=list(rec.get("chair_prior") or []) + list(errors),
                                         cfg=st.cfg)
            order_files.append((new["order_path"], text))
            updates[oid] = new
            retries.append((oid, new))
            messages.append(f"[{name}] {oid} attempt {rec['attempt']} rejected ({len(errors)} error(s)) — "
                            f"retry as attempt {new['attempt']} -> {Path(new['output']).name}")
        else:
            all_errors = list(rec.get("errors") or []) + tagged
            final = dict(rec, errors=all_errors)
            stub = failed_stub(st, oid, final, all_errors)
            stub_errs = schema_errors(stub, "failed_report")
            if stub_errs:
                raise ValueError(f"{oid}: failed stub does not validate: " + "; ".join(stub_errs))
            stub_path = _variant_path(rec["base_output"], "failed")
            stub_files.append((stub_path, stub))
            updates[oid] = dict(copy.deepcopy(rec), status="failed", resolved_output=stub_path,
                                errors=all_errors)
            messages.append(f"[{name}] {oid} FAILED after {rec['attempt']} attempt(s) -> {Path(stub_path).name}")

    # 3) write files, then 4) commit to the manifest
    for path, text in order_files:
        common.write_text(path, text)
    for path, stub in stub_files:
        common.write_json(path, stub)
    orders.update(updates)
    if warnings:
        st.manifest.setdefault("warnings", []).extend(warnings)
    for msg in messages:
        st.log(msg)
    if retries:
        payload = _agents_payload(st.manifest, name, retries, st.concurrency, retry=True)
        st.save()
        st.log(f"[{name}] re-dispatched {payload['summary']}")
        return payload
    _finish_agent_step(st, step)
    return None


def _memo_paths(manifest: dict, run_dir: Path) -> tuple[str | None, str | None]:
    md = html = None
    for step in reversed(manifest.get("steps") or []):
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        if md is None and result.get("memo_md"):
            md = str(result["memo_md"])
        if html is None and result.get("memo_html"):
            html = str(result["memo_html"])
        for out in step.get("outputs") or []:
            nm = Path(str(out)).name
            if md is None and (nm == "memo.md" or (nm.startswith("trial_") and nm.endswith(".md"))):
                md = str(out)
            if html is None and (nm == "memo.html" or (nm.startswith("trial_") and nm.endswith(".html"))):
                html = str(out)
    p4 = Path(run_dir) / "phase4"
    holding = (manifest.get("mode") or {}).get("holding")
    if md is None:
        for cand in ([p4 / f"trial_{common.safe_name(holding)}.md"] if holding else []) + [p4 / "memo.md"]:
            if cand.is_file():
                md = _posix(cand)
                break
    if html is None:
        for cand in ([p4 / f"trial_{common.safe_name(holding)}.html"] if holding else []) + [p4 / "memo.html"]:
            if cand.is_file():
                html = _posix(cand)
                break
    return md, html


def _order_counts(manifest: dict) -> dict:
    counts = {s: 0 for s in ORDER_STATUSES}
    for rec in (manifest.get("orders") or {}).values():
        counts[rec.get("status", "pending")] = counts.get(rec.get("status", "pending"), 0) + 1
    counts["total"] = sum(counts[s] for s in ORDER_STATUSES)
    return counts


def _done_payload(st: _Run) -> dict:
    m = st.manifest
    md, html = _memo_paths(m, st.run_dir)
    counts = _order_counts(m)
    summary = None
    summary_file = st.run_dir / "phase4" / "summary.json"
    if summary_file.is_file():
        try:
            doc = common.read_json(summary_file)
        except (ValueError, OSError):
            doc = None
        if isinstance(doc, dict):
            for key in ("summary", "top_line", "headline", "text"):
                if isinstance(doc.get(key), str) and doc[key].strip():
                    summary = doc[key].strip()
                    break
    if summary is None:
        n_steps = sum(1 for s in m.get("steps") or [] if s.get("status") == "done")
        summary = (f"Run {m['run_id']} complete: {n_steps} steps, {counts['total']} agent orders "
                   f"({counts['failed']} failed), {len(m.get('warnings') or [])} warnings.")
    if m.get("status") != "done":
        m["status"] = "done"
        m["finished_at"] = common.now_iso()
        st.save()
        st.log(f"run done: {summary}")
    return {"type": "done", "run_id": m["run_id"], "run_dir": _posix(st.run_dir), "memo_md": md,
            "memo_html": html, "summary": summary,
            "orders": {"done": counts["done"], "failed": counts["failed"]},
            "warnings": len(m.get("warnings") or []),
            **({"chair_rounds": len(m["chair_rounds"])} if m.get("chair_rounds") else {})}


def _advance(st: _Run) -> dict:
    _ensure_steps(st.manifest, st.ctx, st.steps_mod)
    t0 = _clock()
    worked = False
    for step in st.manifest["steps"]:
        status = step.get("status", "pending")
        if status in ("done", "skipped"):
            continue
        st.current = step["name"]
        if worked and (_clock() - t0) >= st.budget:
            st.save()
            return {"type": "continue", "run_id": st.run_id, "next_step": step["name"],
                    "message": f"time budget ({st.budget:g}s) used — call next again"}
        if step["kind"] == "python":
            _run_python_step(st, step)
            worked = True
            continue
        if step["kind"] != "agents":
            raise ValueError(f"step {step['name']}: unknown kind {step['kind']!r}")
        if status == "dispatched":
            payload = _collect_step(st, step)
        else:
            payload = _dispatch_step(st, step)
        if payload is not None:
            return payload
    st.current = None
    return _done_payload(st)


def _record_error(run_dir: Path | None, manifest: dict | None, step_name: str | None, exc: BaseException) -> dict:
    message = f"{type(exc).__name__}: {exc}"
    payload = {"type": "error", "run_id": (manifest or {}).get("run_id"), "step": step_name,
               "message": _clip(message, 2000)}
    if manifest is not None and step_name:
        step = _step_by_name(manifest, step_name)
        if step is not None:
            step["error"] = _clip(message, 2000)
            if step.get("status") in ("pending", "running", "failed"):
                step["status"] = "failed"
                step["ended"] = common.now_iso()
            # a dispatched agents step stays dispatched: its orders are out; re-check next time
    if run_dir is not None and manifest is not None:
        try:
            manifest.setdefault("warnings", [])
            save_manifest(run_dir, manifest)
        except Exception:  # pragma: no cover - best effort
            pass
    if run_dir is not None:
        try:
            log(run_dir, f"ERROR [{step_name or '-'}] {_clip(message, 500)}",
                block="".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)))
        except Exception:  # pragma: no cover - best effort
            pass
    return payload


def run_next(run: str | Path, *, cli_args: Any = None, ignore_circuit_breaker: bool = False) -> dict:
    """One ``next`` call. Never raises: failures come back as ``{"type":"error",...}`` with the
    manifest saved (failed python steps are re-run by the following ``next``). A tripped circuit
    breaker (see the module docstring) is also an error payload; ``ignore_circuit_breaker`` accepts
    the missing outputs as ordinary failed attempts."""
    run_dir: Path | None = None
    manifest: dict | None = None
    st: _Run | None = None
    try:
        run_dir = resolve_run_dir(str(run))
        manifest = load_manifest(run_dir)
        apply_run_env(manifest, run_dir)
        if cli_args is not None:
            common.apply_common_args(cli_args)
        steps_mod = _steps_module()
        ctx, manifest = _make_ctx(steps_mod, run_dir, manifest)
        st = _Run(run_dir, manifest, ctx, steps_mod)
        st.ignore_breaker = bool(ignore_circuit_breaker)
        return _advance(st)
    except Exception as exc:
        step_name = st.current if st is not None else None
        return _record_error(run_dir, st.manifest if st is not None else manifest, step_name, exc)


def _set_aside_invalid_output(st: _Run, oid: str, out: Path) -> str | None:
    """Move an existing but invalid output next to itself as ``<name>.json.invalid`` (numbered when
    taken), so the re-launched agent can Write the path without having to read the old file first.
    Returns the new path, or None when the file could not be moved (it is then left in place)."""
    target = out.with_name(out.name + ".invalid")
    n = 1
    while target.exists():
        n += 1
        target = out.with_name(f"{out.name}.invalid{n}")
    try:
        os.replace(out, target)
    except OSError as exc:
        st.log(f"[{st.current}] {oid}: could not set aside the invalid output {out.name}: {exc}")
        return None
    st.log(f"[{st.current}] {oid}: invalid output set aside as {target.name}")
    return _posix(target)


def run_redispatch(run: str | Path) -> dict:
    """Re-emit the orders of the current agents step that still need an agent (no attempt counted).

    ``next`` only changes an order's status when it collects, so after a stop every order of a
    dispatched step still reads ``dispatched`` even though most agents finished. Each dispatched
    order's CURRENT output is therefore checked exactly like ``next`` does:

    * valid  -> the order is marked ``done`` and NOT re-emitted (re-running it would redo finished
      work, and its agent could not Write over a file it never read);
    * missing -> re-emitted unchanged (the order file is re-rendered if it was lost);
    * present but invalid -> the bad file is set aside (``<name>.json.invalid``) and the order is
      re-emitted, so the agent can write the path again.

    The payload lists ``skipped_valid`` / ``set_aside``. ``{"type":"continue"}`` when nothing is
    left to launch (call ``next``)."""
    run_dir = resolve_run_dir(str(run))
    manifest = load_manifest(run_dir)
    apply_run_env(manifest, run_dir)
    steps_mod = _steps_module()
    ctx, manifest = _make_ctx(steps_mod, run_dir, manifest)
    st = _Run(run_dir, manifest, ctx, steps_mod)
    orders = manifest.get("orders") or {}
    for step in manifest.get("steps") or []:
        if step.get("kind") != "agents" or step.get("status") != "dispatched":
            continue
        st.current = step["name"]
        index = _load_order_index(run_dir)
        pending: list[tuple[str, dict]] = []
        skipped: list[str] = []
        set_aside: list[str] = []
        warnings: list[str] = []
        for oid in step.get("orders") or []:
            rec = orders.get(oid)
            if not rec or rec.get("status") != "dispatched":
                continue
            spec = _spec_for(run_dir, oid, index)
            errors = _check_output(st, oid, rec, spec, warnings)
            if not errors:
                rec.update(status="done", resolved_output=rec["output"])
                skipped.append(oid)
                continue
            out = Path(rec["output"])
            if out.is_file() and _set_aside_invalid_output(st, oid, out):
                set_aside.append(oid)
            pending.append((oid, rec))
        if warnings:
            manifest.setdefault("warnings", []).extend(warnings)
        if skipped:
            st.log(f"[{step['name']}] redispatch: {len(skipped)} order(s) already have a valid output "
                   f"(marked done, not re-emitted): {', '.join(skipped)}")
        if skipped or warnings:
            st.save()
        if not pending:
            if not skipped:
                continue
            return {"type": "continue", "run_id": manifest["run_id"], "skipped_valid": skipped,
                    "message": f"every dispatched order of {step['name']} already has a valid output - call next"}
        cfg = st.cfg
        for oid, rec in pending:
            if not Path(rec["order_path"]).is_file():
                spec = _spec_for(run_dir, oid, index)
                prior = list(rec.get("chair_prior") or []) + [
                    e.split(": ", 1)[1] if e.startswith("attempt ") and ": " in e else e
                    for e in (rec.get("errors") or [])
                    if e.startswith(f"attempt {int(rec['attempt']) - 1}:")]
                write_order_file(run_dir, spec, rec, run_id=manifest["run_id"],
                                 as_of=str(manifest.get("as_of")), prior_errors=prior, cfg=cfg)
        retry = any(int(rec.get("attempt") or 1) > 1 for _oid, rec in pending)
        payload = _agents_payload(manifest, step["name"], pending, st.concurrency, retry=retry)
        payload["redispatch"] = True
        if skipped:
            payload["skipped_valid"] = skipped
        if set_aside:
            payload["set_aside"] = set_aside
        log(run_dir, f"[{step['name']}] redispatch: {payload['summary']}")
        return payload
    return {"type": "continue", "run_id": manifest["run_id"],
            "message": "no dispatched orders — call next"}


def run_status(run: str | Path) -> dict:
    run_dir = resolve_run_dir(str(run))
    m = load_manifest(run_dir)
    steps = OrderedDict((s["name"], s.get("status", "pending")) for s in m.get("steps") or [])
    current = next((n for n, s in steps.items() if s not in ("done", "skipped")), None)
    counts = _order_counts(m)
    failed_orders = [oid for oid, r in (m.get("orders") or {}).items() if r.get("status") == "failed"]
    out: dict[str, Any] = {
        "type": "status", "run_id": m["run_id"], "run_dir": _posix(run_dir), "as_of": m.get("as_of"),
        "mode": m.get("mode_label") or _mode_label(m.get("mode") or {}),
        "dry_run": bool((m.get("mode") or {}).get("dry_run")),
        "status": m.get("status", "in_progress"), "current_step": current,
        "steps": steps, "orders": counts, "failed_orders": failed_orders,
        "warnings": len(m.get("warnings") or []),
    }
    if m.get("chair_rounds"):
        out["chair_round"] = len(m["chair_rounds"])
    pending_orders = [oid for oid, r in (m.get("orders") or {}).items() if r.get("status") == "dispatched"]
    if pending_orders:
        out["pending_orders"] = pending_orders
    cur = _step_by_name(m, current) if current else None
    if cur is not None and cur.get("error"):
        out["last_error"] = cur["error"]
    if current is None:
        md, html = _memo_paths(m, run_dir)
        out["memo_md"], out["memo_html"] = md, html
    elif cur is not None and cur.get("status") == "dispatched":
        out["hint"] = "orders dispatched: launch them (redispatch re-lists them), then call next"
        if m.get("status") == "reopened":
            out["hint"] = "chair reopen in progress: " + out["hint"]
        breaker = cur.get("circuit_breaker")
        if isinstance(breaker, dict):
            out["circuit_breaker"] = breaker
            out["hint"] = (f"circuit breaker: {breaker.get('no_output')} of {breaker.get('orders')} orders produced "
                           f"no output (usage limit / outage?); run redispatch, launch what it lists, then call next")
    else:
        out["hint"] = "call next"
    # configs edited since the run started (the run keeps using its inputs/ snapshot)
    drift = _config_drift(run_dir, m)
    if drift:
        out["config_drift"] = [{k: d[k] for k in ("file", "change", "message")} for d in drift]
        sep = "; " if out.get("hint") else ""
        out["hint"] = out.get("hint", "") + sep + "config changed after the run started - decisions use the inputs/ snapshot"
    return out


def _config_drift(run_dir: Path, manifest: dict) -> list[dict]:
    """``_steps.config_drift`` (live config files vs the hashes taken at init); [] when it cannot be
    computed. Falls back to the real module when the ``_steps`` seam is a stand-in without it."""
    try:
        fn = getattr(_steps_module(), "config_drift", None)
        if fn is None:
            fn = importlib.import_module("scripts._steps").config_drift
        return list(fn(run_dir, manifest) or [])
    except Exception:  # noqa: BLE001 - status must never fail because of the drift check
        return []


# =====================================================================================
# chair review: reopen / audit / chair-save (DESIGN §10)
# =====================================================================================


class ReopenError(Exception):
    """``reopen`` / ``chair-save`` refused for a user-fixable reason (CLI exit code 2)."""

    def __init__(self, message: str, details: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details)


def _max_chair_rounds(cfg: dict) -> int:
    raw = common.safe_float(_cfg_get(cfg, "orchestration.max_chair_rounds", DEFAULT_MAX_CHAIR_ROUNDS))
    return DEFAULT_MAX_CHAIR_ROUNDS if raw is None else max(0, int(raw))


def inputs_changed_text(order_ids: Iterable[str]) -> str:
    """The standard ``prior_errors`` line of every order re-run because upstream work was redone."""
    return (f"{INPUTS_CHANGED_PREFIX} {', '.join(order_ids)}; "
            f"re-check your conclusions against the updated reports")


def _step_dependents(names: list[str], sources: Iterable[str]) -> list[str]:
    """Steps (in sequence order) that must be re-run because a step in ``sources`` changes: the
    transitive closure over ``STEP_DEPENDS`` (a step missing from the table depends on every step
    before it). A source step is included only when it depends on ANOTHER source."""
    changed = set(sources)
    affected: list[str] = []
    for i, name in enumerate(names):
        deps = STEP_DEPENDS.get(name)
        if deps is None:
            deps = tuple(names[:i])
        if any(d in changed for d in deps):
            affected.append(name)
            changed.add(name)
    return affected


def _order_output_files(rec: dict) -> list[Path]:
    """Every file an order may have left next to its base output: ``<stem>.json``, ``.rN.json``,
    ``.failed.json`` and set-aside ``.json.invalid*`` copies."""
    base = Path(rec.get("base_output") or rec["output"])
    stem = base.name[:-5] if base.name.lower().endswith(".json") else base.name
    out: list[Path] = []
    if not base.parent.is_dir():
        return out
    for f in sorted(base.parent.iterdir()):
        n = f.name
        if not f.is_file():
            continue
        is_retry = n.startswith(f"{stem}.r") and n.endswith(".json") and n[len(stem) + 2:-5].isdigit()
        if n in (f"{stem}.json", f"{stem}.failed.json") or is_retry or n.startswith(f"{stem}.json.invalid"):
            out.append(f)
    return out


def _archive_files(run_dir: Path, round_no: int, files: Iterable[Path], *, copy_only: Iterable[Path] = ()) -> list[str]:
    """Move ``files`` into ``<run>/_superseded/round<n>/`` (keeping their run-relative path; a name
    clash gets a numeric suffix). Files outside the run directory, shared ledger files and
    anything already archived are left alone; ``data/`` files and ``copy_only`` entries are copied
    (they are re-created by their step). Returns the run-relative posix paths archived."""
    root = Path(run_dir).resolve()
    dest_root = root / SUPERSEDED_DIR / f"round{round_no}"
    copies = {Path(p).resolve() for p in copy_only}
    done: list[str] = []
    seen: set[Path] = set()
    for f in files:
        f = Path(f)
        try:
            rel = f.resolve().relative_to(root)
        except ValueError:
            continue
        if (not f.is_file() or rel in seen or (rel.parts and rel.parts[0] == SUPERSEDED_DIR)
                or f.name in KEEP_IN_PLACE_NAMES):
            continue
        seen.add(rel)
        dest = dest_root / rel
        n = 1
        while dest.exists():
            n += 1
            dest = dest_root / rel.parent / f"{rel.name}.{n}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f.resolve() in copies or (len(rel.parts) > 1 and rel.parts[-2] == "data"):
            shutil.copy2(f, dest)
        else:
            os.replace(f, dest)
        done.append(rel.as_posix())
    return done


def _reopen_record(rec: dict, *, round_no: int, now: str, lines: list[str], max_attempts: int) -> dict:
    """The new order record of a chair-reopened order: attempt + 1, output ``<stem>.r<attempt>.json``."""
    attempt = int(rec["attempt"]) + 1
    cap = max_attempts + CHAIR_ORDER_EXTRA_ATTEMPTS
    new = copy.deepcopy(rec)
    new.update(attempt=attempt, output=_variant_path(rec["base_output"], f"r{attempt}"), status="dispatched",
               resolved_output=None, reopened_by="chair", reopen_round=round_no,
               attempt_limit=min(attempt + max_attempts - 1, cap), chair_prior=list(lines))
    new["reopen_history"] = list(rec.get("reopen_history") or []) + [
        {"round": round_no, "at": now, "from_attempt": int(rec["attempt"]),
         "previous_output": rec.get("resolved_output")}]
    return new


def run_reopen(run: str | Path, items: list[dict], *, reason: str | None = None) -> dict:
    """Chair reopen (DESIGN §10): send finished orders back to their agents with written feedback and
    reset everything downstream of them.

    ``items`` = ``[{"order_id", "feedback"}]``. Each named order (status done/failed) becomes a retry
    order exactly like the retry machinery builds one - same order id and order file, attempt + 1,
    output ``<stem>.r<attempt>.json`` (the earlier output stays on disk) - whose ``prior_errors`` are
    the feedback lines prefixed ``CHAIR FEEDBACK:``; the record gets ``reopened_by: chair`` and
    ``reopen_round``. Downstream (``STEP_DEPENDS``): later steps go back to ``pending`` and their outputs
    move to ``_superseded/round<n>/``; python steps simply re-run, agent steps are re-dispatched as
    fresh orders with the ``inputs changed after chair re-run of ...`` ``prior_errors`` text. Per-ticker /
    per-sector agent steps (``SCOPE_KEY``) only redo the orders of the touched tickers / sectors.
    The manifest status becomes ``reopened`` and ``chair_rounds`` gets one entry; ``next`` then
    continues as for any run. Nothing is changed when the request is refused (``ReopenError``)."""
    run_dir = resolve_run_dir(str(run))
    manifest = load_manifest(run_dir)
    apply_run_env(manifest, run_dir)
    steps_mod = _steps_module()
    ctx, manifest = _make_ctx(steps_mod, run_dir, manifest)
    st = _Run(run_dir, manifest, ctx, steps_mod)
    orders = manifest.get("orders") or {}
    steps = manifest.get("steps") or []
    step_map = {s["name"]: s for s in steps}
    rounds = list(manifest.get("chair_rounds") or [])
    round_no = len(rounds) + 1
    now = common.now_iso()
    problems: list[str] = []

    status = manifest.get("status")
    if status != "done":
        problems.append(f"run {manifest['run_id']} is {status!r}: only a finished run (status 'done') can be reopened"
                        + (" - finish the current reopen round with `next` first" if status == "reopened" else ""))
    max_rounds = _max_chair_rounds(st.cfg)
    if len(rounds) >= max_rounds:
        problems.append(f"chair round limit reached: {len(rounds)} of {max_rounds} rounds used "
                        f"(orchestration.max_chair_rounds); record what is left in open_issues instead")
    order_cap = st.max_attempts + CHAIR_ORDER_EXTRA_ATTEMPTS

    named: "OrderedDict[str, str]" = OrderedDict()
    for it in items or []:
        oid = str((it or {}).get("order_id") or "").strip()
        fb = str((it or {}).get("feedback") or "").strip()
        if not oid:
            problems.append("a reopen item has no order_id")
            continue
        if not fb:
            problems.append(f"{oid}: feedback is empty - say what is wrong, the evidence and what to change")
        named[oid] = (named[oid] + "\n" + fb) if oid in named else fb
    if not named:
        problems.append("no orders to reopen (use --order ORDER_ID ... --feedback TEXT or --spec-file)")
    lines: dict[str, list[str]] = {}
    for oid, fb in named.items():
        rec = orders.get(oid)
        if rec is None:
            problems.append(f"{oid}: no such order in the manifest (list them with `status --run {manifest['run_id']}` "
                            f"or see orders/index.json)")
            continue
        if rec.get("status") not in ("done", "failed"):
            problems.append(f"{oid}: order is {rec.get('status')!r} (still pending an agent) - finish it before reopening")
        if rec.get("step") not in step_map:
            problems.append(f"{oid}: its step {rec.get('step')!r} is not in this run's step sequence")
        if int(rec.get("attempt") or 1) >= order_cap:
            problems.append(f"{oid}: attempt cap reached ({int(rec.get('attempt') or 1)} of {order_cap} = "
                            f"max_attempts + {CHAIR_ORDER_EXTRA_ATTEMPTS}); it cannot be sent back again")
        if len(fb) > CHAIR_FEEDBACK_MAX_CHARS:
            problems.append(f"{oid}: feedback is {len(fb)} characters (limit {CHAIR_FEEDBACK_MAX_CHARS}); be specific and shorter")
        lines[oid] = chair_feedback_lines(fb)
        if len(lines[oid]) > CHAIR_FEEDBACK_MAX_LINES:
            problems.append(f"{oid}: feedback has {len(lines[oid])} lines (limit {CHAIR_FEEDBACK_MAX_LINES})")
    if problems:
        raise ReopenError("reopen refused: " + problems[0] if len(problems) == 1 else
                          f"reopen refused ({len(problems)} problems)", problems)

    # ------------------------------------------------------------------ plan (no side effects)
    names = [s["name"] for s in steps]
    reopen_steps = {orders[o]["step"] for o in named}
    affected = _step_dependents(names, reopen_steps)
    plain = [n for n in names if n in reopen_steps and n not in affected]
    nrecs = [orders[o] for o in named]
    tickers = {r.get("ticker") for r in nrecs if r.get("ticker")}
    sectors = {r.get("sector") for r in nrecs if r.get("sector")}
    scoped_ok = (not (set(affected) & set(SET_CHANGING_STEPS))
                 and all(r.get("ticker") or r.get("sector") for r in nrecs))
    plan: list[dict] = []                      # reset steps, in sequence order
    for name in names:
        if name not in affected:
            continue
        step = step_map[name]
        if step.get("kind") != "agents":
            plan.append({"name": name, "kind": step.get("kind"), "aff": [], "keep": []})
            continue
        ids = [o for o in step.get("orders") or [] if o in orders]
        aff, keep = ids, []
        key = SCOPE_KEY.get(name) if scoped_ok else None
        if key and all(r.get(key) for r in nrecs):
            need = tickers if key == "ticker" else sectors
            aff = [o for o in ids if o in named or orders[o].get(key) in need]
            keep = [o for o in ids if o not in aff]
            if not aff:
                continue                       # nothing of this step depends on the reopened orders
        plan.append({"name": name, "kind": "agents", "aff": aff, "keep": keep})
    reset_names = [p["name"] for p in plan]
    text = inputs_changed_text(list(named))
    index = _load_order_index(run_dir)

    new_recs: "OrderedDict[str, dict]" = OrderedDict()   # reopened orders whose step is NOT reset
    order_files: list[tuple[str, str]] = []
    carry_by_step: dict[str, dict] = {}
    for oid in named:
        rec = orders[oid]
        if rec["step"] in reset_names:
            c = _reopen_record(rec, round_no=round_no, now=now, lines=lines[oid], max_attempts=st.max_attempts)
            carry_by_step.setdefault(rec["step"], {})[oid] = {
                "attempt": c["attempt"], "feedback": lines[oid], "round": round_no,
                "attempt_limit": c["attempt_limit"], "history": c["reopen_history"]}
            continue
        new = _reopen_record(rec, round_no=round_no, now=now, lines=lines[oid], max_attempts=st.max_attempts)
        _fm, otext = build_order_file(_spec_for(run_dir, oid, index), new, run_id=st.run_id, as_of=st.as_of,
                                      prior_errors=lines[oid], cfg=st.cfg)
        order_files.append((new["order_path"], otext))
        new_recs[oid] = new

    fb_doc = {"schema": "chair_feedback/1", "run_id": st.run_id, "round": round_no, "at": now,
              "reason": reason, "items": [{"order_id": o, "feedback": named[o]} for o in named]}
    feedback_sha = hashlib.sha256(json.dumps(fb_doc["items"], sort_keys=True, ensure_ascii=False)
                                  .encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ apply
    fb_path = run_dir / "orders" / f"chair_feedback_r{round_no}.json"
    common.write_json(fb_path, dict(fb_doc, feedback_sha=feedback_sha))
    archive_files: list[Path] = []
    copy_only: list[Path] = []
    superseded: dict[str, dict] = {}
    for p in plan:
        step = step_map[p["name"]]
        if p["kind"] == "agents":
            for oid in p["aff"]:
                rec = orders[oid]
                archive_files += _order_output_files(rec)
                if Path(rec["order_path"]).is_file():
                    copy_only.append(Path(rec["order_path"]))
                    archive_files.append(Path(rec["order_path"]))
                superseded[oid] = {"step": p["name"], "attempt": rec.get("attempt"), "status": rec.get("status"),
                                   "output": rec.get("resolved_output")}
        else:
            for out in step.get("outputs") or []:
                path = Path(str(out))
                archive_files.append(path if path.is_absolute() else run_dir / path)
    for path, otext in order_files:
        common.write_text(path, otext)
    archived = _archive_files(run_dir, round_no, archive_files, copy_only=copy_only)
    if archived:
        common.write_json(run_dir / SUPERSEDED_DIR / f"round{round_no}" / "index.json",
                          {"round": round_no, "at": now, "orders": list(named), "files": archived})

    for oid in superseded:
        orders.pop(oid, None)
    for p in plan:
        step = step_map[p["name"]]
        step.update(status="pending", started=None, ended=None, summary=None, outputs=[], reset_round=round_no)
        for key in ("error", "result", "circuit_breaker"):
            step.pop(key, None)
        if p["kind"] == "agents":
            step["orders"] = list(p["keep"])
            step["invalidated"] = {"round": round_no, "text": text, "keep": list(p["keep"]),
                                   "carry": carry_by_step.get(p["name"], {})}
        else:
            prefix = f"{p['name']}: "
            manifest["warnings"] = [w for w in manifest.get("warnings") or [] if not str(w).startswith(prefix)]
    orders.update(new_recs)
    for name in plain:
        step = step_map[name]
        step.update(status="dispatched", ended=None, summary=None, reopen_round=round_no)
        step.pop("error", None)
        step["summary"] = f"chair reopen round {round_no}: " + ", ".join(o for o in new_recs if orders[o]["step"] == name)
    previous_finished = manifest.pop("finished_at", None)
    manifest["status"] = "reopened"
    entry = {
        "round": round_no, "at": now,
        "reason": _clip(reason or lines[next(iter(named))][0].replace(CHAIR_PREFIX, "").strip(), 300),
        "orders": list(named), "feedback_sha": feedback_sha,
        "feedback_file": fb_path.relative_to(run_dir).as_posix(),
        "invalidated_steps": reset_names, "reopened_steps": plain,
        "kept_orders": {p["name"]: p["keep"] for p in plan if p["keep"]},
        "superseded_orders": superseded, "archive_dir": f"{SUPERSEDED_DIR}/round{round_no}",
        "archived_files": len(archived), "previous_finished_at": previous_finished,
    }
    manifest.setdefault("chair_rounds", []).append(entry)
    st.save()
    st.log(f"CHAIR REOPEN round {round_no}/{max_rounds}: orders {', '.join(named)}; "
           f"re-run steps {', '.join(reset_names) or '-'}; {len(archived)} file(s) archived to "
           f"{SUPERSEDED_DIR}/round{round_no}; feedback_sha {feedback_sha[:12]}",
           block="\n\n".join(f"[{o}]\n{named[o]}" for o in named))

    recs = list(new_recs.items())
    payload = _agents_payload(manifest, plain[0], recs, st.concurrency, retry=True)
    payload.update(reopened=True, round=round_no, rounds_left=max_rounds - round_no, steps=plain,
                   invalidated_steps=reset_names, kept_orders=entry["kept_orders"],
                   archive_dir=entry["archive_dir"], feedback_sha=feedback_sha,
                   summary=f"chair reopen round {round_no}: " + _batch_summary(plain[0], recs, retry=True)
                   + (f"; {len(reset_names)} downstream step(s) reset" if reset_names else ""))
    return payload


def run_audit(run: str | Path) -> dict:
    """``audit`` subcommand: ``scripts.audit.run(run_dir)`` (imported lazily)."""
    run_dir = resolve_run_dir(str(run))
    try:
        mod = importlib.import_module("scripts.audit")
    except ImportError as exc:
        raise ReopenError("scripts/audit.py is not available (or failed to import): " + _clip(exc, 300),
                          ["the audit module is delivered separately; nothing was changed"]) from exc
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise ReopenError("scripts/audit.py has no run(run_dir) function")
    return fn(run_dir)


def _words(text: Any) -> int:
    return len(str(text).split())


def chair_review_errors(doc: Any, *, run_id: str | None = None) -> list[str]:
    """Schema errors (``schemas/chair_review.schema.json``) plus the word limits and consistency
    rules of a chair review document; [] = valid."""
    from jsonschema import Draft202012Validator, FormatChecker

    schema = common.read_json(common.schemas_dir() / CHAIR_REVIEW_SCHEMA)
    validator = Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())
    errs = sorted(validator.iter_errors(doc), key=lambda e: (common.format_error_path(e.absolute_path), e.message))
    out = [f"{common.format_error_path(e.absolute_path)}: {_clip(e.message, 300)}" for e in errs]
    if out or not isinstance(doc, dict):
        return out or ["$: chair review must be a JSON object"]
    if run_id and doc.get("run_id") != run_id:
        out.append(f"$.run_id: expected {run_id!r} (got {doc.get('run_id')!r})")

    def over(path: str, text: Any, key: str) -> None:
        n = _words(text)
        if n > CHAIR_WORD_LIMITS[key]:
            out.append(f"{path}: {n} words (limit {CHAIR_WORD_LIMITS[key]})")

    over("$.headline", doc["headline"], "headline")
    for i, item in enumerate(doc["executive_summary"]):
        over(f"$.executive_summary[{i}]", item, "executive_summary")
    seen: set[str] = set()
    for i, d in enumerate(doc["decisions"]):
        over(f"$.decisions[{i}].chair_view", d["chair_view"], "chair_view")
        over(f"$.decisions[{i}].what_would_change_it", d["what_would_change_it"], "what_would_change_it")
        if d["ticker"] in seen:
            out.append(f"$.decisions[{i}].ticker: duplicate decision for {d['ticker']}")
        seen.add(d["ticker"])
    return out


def run_chair_save(run: str | Path, file: str | Path) -> dict:
    """Validate a chair review document and store it atomically as ``phase4/chair_review.json``; the
    previous review is kept as ``chair_review.r<round>.json`` (``.r<round>.vN.json`` for a re-save of
    the same round). Rounds may not go backwards."""
    run_dir = resolve_run_dir(str(run))
    manifest = load_manifest(run_dir)
    src = Path(file)
    if not src.is_file():
        raise ReopenError(f"chair review file not found: {src.as_posix()}")
    try:
        doc = common.read_json(src)
    except (ValueError, OSError) as exc:
        raise ReopenError(f"chair review is not valid JSON: {_clip(exc, 300)}") from exc
    errors = chair_review_errors(doc, run_id=manifest["run_id"])
    target = run_dir / "phase4" / "chair_review.json"
    previous = None
    if not errors and target.is_file():
        try:
            previous = common.read_json(target)
        except (ValueError, OSError):
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("round"), int) and doc["round"] < previous["round"]:
            errors.append(f"$.round: {doc['round']} is older than the saved review (round {previous['round']})")
    if errors:
        raise ReopenError(f"chair review invalid ({len(errors)} error(s))", errors)
    kept = None
    if target.is_file():
        rnd = previous.get("round") if isinstance(previous, dict) and isinstance(previous.get("round"), int) else 0
        kept = target.with_name(f"chair_review.r{rnd}.json")
        n = 1
        while kept.exists():
            n += 1
            kept = target.with_name(f"chair_review.r{rnd}.v{n}.json")
        os.replace(target, kept)
    common.write_json(target, doc)
    log(run_dir, f"chair review saved: round {doc['round']} verdict {doc['verdict']}"
                 + (f" (previous kept as {kept.name})" if kept else ""))
    return {"type": "chair_saved", "run_id": manifest["run_id"], "round": doc["round"], "verdict": doc["verdict"],
            "path": _posix(target), "previous": _posix(kept) if kept else None,
            "decisions": len(doc["decisions"])}


# =====================================================================================
# dry-run
# =====================================================================================


def _find_agent_fixture(agent_dir: Path, oid: str, rec: dict) -> Path | None:
    names: list[str] = []
    attempt = int(rec.get("attempt") or 1)
    if rec.get("reopen_round"):                       # a chair-reopened order may have its own fixture
        names.append(f"{oid}.chair{rec['reopen_round']}.json")
    if attempt > 1:
        names.append(f"{oid}.r{attempt}.json")
    names += [f"{oid}.json", Path(rec["output"]).name, Path(rec.get("base_output") or rec["output"]).name]
    for nm in names:
        cand = agent_dir / nm
        if cand.is_file():
            return cand
    return None


def simulate_agents(run_dir: Path, payload: dict, fixtures_root: Path) -> dict:
    """Play the agents of one batch from ``<fixtures>/agent_outputs``: copy ``<order_id>.json``
    (fallback: the output file name) to the order's current output path after replacing the
    tokens ``${RUN_DIR}`` (run dir, posix), ``${REPO_ROOT}``, ``${RUN_ID}``, ``${AS_OF}`` and
    setting ``run_id`` / ``order_id`` / ``as_of``. A missing fixture writes nothing (the order
    then goes through the retry / failed path)."""
    manifest = load_manifest(run_dir)
    agent_dir = Path(fixtures_root) / "agent_outputs"
    written: list[str] = []
    missing: list[str] = []
    replacements = {"${RUN_DIR}": _posix(run_dir), "${REPO_ROOT}": common.REPO_ROOT.as_posix(),
                    "${RUN_ID}": manifest["run_id"], "${AS_OF}": str(manifest.get("as_of"))}
    for order in payload.get("orders") or []:
        oid = order["order_id"]
        rec = manifest["orders"][oid]
        src = _find_agent_fixture(agent_dir, oid, rec)
        if src is None:
            missing.append(oid)
            continue
        text = common.read_text(src)
        for token, value in replacements.items():
            text = text.replace(token, value)
        out = Path(rec["output"])
        try:
            doc = json.loads(text)
        except ValueError:
            common.write_text(out, text)  # deliberately broken fixture: exercise the retry path
        else:
            if isinstance(doc, dict):
                doc["run_id"] = manifest["run_id"]
                doc["order_id"] = oid
                doc["as_of"] = manifest.get("as_of")
            common.write_json(out, doc)
        written.append(oid)
    log(run_dir, f"[dry-run] simulated {len(written)} agent output(s)"
                 + (f"; no fixture for: {', '.join(missing)}" if missing else ""))
    return {"written": written, "missing": missing}


def run_dry_run(*, fixtures_dir: str | Path | None = None, as_of: str | None = None,
                config_dir: str | Path | None = None, quick: bool = False,
                sectors: Iterable[str] | str | None = None, holding: str | None = None,
                max_iterations: int = DRY_RUN_MAX_ITERATIONS) -> tuple[int, dict]:
    """Whole pipeline offline. Returns ``(exit_code, final_json)``.

    The fixtures root must contain ``data/`` (provider fixtures, AGINV_FIXTURES_DIR),
    ``agent_outputs/`` (one JSON per order) and ``config/`` (unless ``config_dir`` is given
    explicitly) — otherwise exit 2 ``{"type":"error","step":"init",...}`` before anything is
    created; the real config dir is never used implicitly. ``quick`` / ``sectors`` / ``holding`` are
    not supported by the fixtures: exit 2 with an error JSON, nothing is created. The final JSON
    lists the orders that had no agent fixture (``fixtures_missing``); when ANY agent order failed
    the exit code is 1 and ``ok`` is false."""
    unsupported = [flag for flag, on in (("--quick", quick), ("--sectors", _parse_sectors(sectors)),
                                         ("--holding", holding)) if on]
    if unsupported:
        return 2, {"type": "error", "step": "init",
                   "error": f"dry-run does not support {', '.join(unsupported)}: the fixtures cover the full "
                            f"pipeline only (a quick / sector-limited / holding run needs different agent outputs)",
                   "details": ["run `committee.py dry-run` without those flags, or start a real run with "
                               "`committee.py init` (no --dry-run) for a quick, sector or holding run"]}
    fx = Path(fixtures_dir) if fixtures_dir else DRY_RUN_DEFAULT_FIXTURES
    if not fx.is_absolute() and not fx.exists() and (common.REPO_ROOT / fx).exists():
        fx = common.REPO_ROOT / fx
    fx = fx.resolve()
    layout = [f"{sub}/ — {what}" for sub, what in DRY_RUN_LAYOUT
              if not (sub == "config" and config_dir)]
    if not fx.is_dir():
        return 2, {"type": "error", "step": "init", "error": f"fixtures dir not found: {fx.as_posix()}",
                   "details": [f"expected layout under the fixtures dir: {e}" for e in layout]}
    missing_dirs = [sub for sub, _what in DRY_RUN_LAYOUT
                    if not (sub == "config" and config_dir) and not (fx / sub).is_dir()]
    if missing_dirs:
        return 2, {"type": "error", "step": "init",
                   "error": f"fixtures dir incomplete: {fx.as_posix()} lacks {', '.join(d + '/' for d in missing_dirs)}",
                   "details": [f"expected layout under the fixtures dir: {e}" for e in layout]
                   + ([] if config_dir or "config" not in missing_dirs else
                      ["or pass --config-dir explicitly (the real config dir is never used implicitly)"])}
    as_of = as_of or DRY_RUN_DEFAULT_AS_OF
    as_of_d = common.parse_date(as_of)
    if as_of_d is None:
        raise common.ConfigError(f"--as-of: not a date: {as_of!r}")
    as_of = as_of_d.isoformat()
    if config_dir:
        os.environ[common.ENV_CONFIG_DIR] = str(Path(config_dir).resolve())
    else:
        os.environ[common.ENV_CONFIG_DIR] = str(fx / "config")
    os.environ[common.ENV_FIXTURES_DIR] = str(fx / "data")
    os.environ[common.ENV_OFFLINE] = "1"
    os.environ[common.ENV_AS_OF] = as_of
    os.environ[common.ENV_NOW] = f"{as_of}T12:00:00+00:00"
    common.clear_caches()
    try:
        info = init_run(quick=quick, sectors=sectors, holding=holding, dry_run=True,
                        fixtures_dir=fx / "data")
    except InitError as exc:
        return 2, {"type": "error", "step": "init", "error": exc.message, "details": exc.details}
    run_dir = Path(info["run_dir"])
    os.environ[common.ENV_CACHE_DIR] = str(run_dir / ".cache")
    common.clear_caches()
    batches = 0
    fixtures_missing: list[str] = []
    for _ in range(max_iterations):
        res = run_next(info["run_id"], ignore_circuit_breaker=True)   # agents are simulated: no outage
        kind = res.get("type")
        if kind == "continue":
            continue
        if kind == "agents":
            batches += 1
            sim = simulate_agents(run_dir, res, fx)
            for oid in sim["missing"]:
                if oid not in fixtures_missing:
                    fixtures_missing.append(oid)
            continue
        if kind == "done":
            counts = res.get("orders") or {}
            n_done, n_failed = int(counts.get("done") or 0), int(counts.get("failed") or 0)
            res = dict(res, dry_run=True, ok=n_failed == 0, agent_batches=batches,
                       fixtures_dir=fx.as_posix(), fixtures_missing=fixtures_missing)
            if n_failed:                       # ANY failed order makes the dry run fail
                what = ("every agent order failed" if n_done == 0
                        else f"{n_failed} of {n_done + n_failed} agent orders failed")
                res["message"] = (f"{what} ({n_failed}); {len(fixtures_missing)} had no fixture in "
                                  f"{(fx / 'agent_outputs').as_posix()}")
                return 1, res
            return 0, res
        return 1, res
    err = RuntimeError(f"dry-run did not finish within {max_iterations} next() calls")
    return 1, _record_error(run_dir, None, None, err)


# =====================================================================================
# scorecard
# =====================================================================================


def _split_role_action(key: str) -> tuple[str, str]:
    for sep in ("|", "×", " x ", ":", "/", ","):
        if sep in key:
            a, b = key.split(sep, 1)
            return a.strip(), b.strip()
    return key, ""


def scorecard_rows(result: dict) -> list[dict]:
    """Flatten ``ledger.score`` summaries (dict role->action->stats, dict "role|action"->stats, or
    a list of {role, action, ...}) into rows."""
    summ = None
    for key in ("summary", "by_role_action", "by_role"):
        if result.get(key):
            summ = result[key]
            break
    rows: list[dict] = []
    if isinstance(summ, list):
        rows = [dict(r) for r in summ if isinstance(r, dict)]
    elif isinstance(summ, dict):
        for k, v in summ.items():
            if not isinstance(v, dict):
                continue
            nested = [x for x in v.values() if isinstance(x, dict)]
            if nested and "n" not in v:
                for action, stats in v.items():
                    if isinstance(stats, dict):
                        rows.append({"role": k, "action": action, **stats})
            else:
                role, action = _split_role_action(str(k))
                rows.append({"role": v.get("role", role), "action": v.get("action", action), **v})
    return rows


def _fmt_num(x: Any, dp: int = 2) -> str:
    f = common.safe_float(x)
    return "–" if f is None else (f"{f:.{dp}f}".rstrip("0").rstrip(".") if dp else f"{f:.0f}")


def render_scorecard(result: dict, *, ledger: Path, as_of: str) -> str:
    rows = scorecard_rows(result)
    lines = [f"# Recommendation scorecard — {as_of}", "", f"> {common.DISCLAIMER}", "",
             f"Ledger: `{Path(ledger).as_posix()}`", ""]
    if not rows:
        lines += ["No scored recommendations yet (rows younger than the minimum age are not scored).", ""]
    else:
        lines += ["| Role | Action | N | Hit rate | Mean excess vs SPY (pp) | Mean excess vs sector (pp) |",
                  "|---|---|---:|---:|---:|---:|"]
        for r in sorted(rows, key=lambda r: (str(r.get("role")), str(r.get("action")))):
            hit = common.safe_float(r.get("hit_rate"))  # ledger.score: fraction 0-1
            cells = [str(r.get("role", "")), str(r.get("action", "")), _fmt_num(r.get("n"), 0),
                     "–" if hit is None else f"{hit * 100:.0f}%", _fmt_num(r.get("mean_excess_spy_pp")),
                     _fmt_num(r.get("mean_excess_sector_pp"))]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        lines.append("")
    lines += ["Hit: ADD/BUY or bullish calls beat SPY; SELL/TRIM or bearish calls lagged SPY; "
              "HOLD/PASS/neutral are not scored.", ""]
    return "\n".join(lines)


# =====================================================================================
# CLI
# =====================================================================================


def _add_mode_args(p: argparse.ArgumentParser, *, holding: bool = True) -> None:
    p.add_argument("--quick", action="store_true", help="Phase 2 with the quick roles; skip Phase 3")
    p.add_argument("--sectors", default=None, help="comma-separated sector keys limiting the scope")
    if holding:
        p.add_argument("--holding", default=None, help="single-ticker trial (holding mode)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="committee.py",
                                     description="Agent Investors committee orchestration (DESIGN §2).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="validate configs and create a run")
    _add_mode_args(p)
    p.add_argument("--dry-run", action="store_true", help="mark the run as a dry run (fixtures)")
    p.add_argument("--run-id", default=None)
    common.add_common_args(p)

    for name, help_text in (("next", "advance the run state machine"),
                            ("redispatch", "re-emit the dispatched orders that still need an agent "
                                           "(valid outputs are skipped); no attempt is counted"),
                            ("status", "compact progress JSON")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--run", required=True, help="run id, run directory, or 'latest'")
        if name == "next":
            p.add_argument("--ignore-circuit-breaker", action="store_true",
                           help="treat orders without any output file as ordinary failed attempts instead "
                                "of stopping on a suspected agent outage")
        common.add_common_args(p)

    p = sub.add_parser("reopen", help="chair: send finished orders back to their agents with feedback and "
                                      "reset everything downstream")
    p.add_argument("--run", required=True, help="run id, run directory, or 'latest'")
    p.add_argument("--order", action="append", default=[], metavar="ORDER_ID",
                   help="order to redo (repeatable); gets --feedback / --feedback-file")
    p.add_argument("--feedback", default=None, help="feedback text for every --order")
    p.add_argument("--feedback-file", dest="feedback_file", default=None, help="UTF-8 file with the feedback text")
    p.add_argument("--spec-file", dest="spec_file", default=None,
                   help="JSON list of {order_id, feedback} (different feedback per order)")
    p.add_argument("--reason", default=None, help="one line for chair_rounds[].reason (default: first feedback line)")
    common.add_common_args(p)

    p = sub.add_parser("audit", help="run scripts/audit.py on a finished run (chair review input)")
    p.add_argument("--run", required=True, help="run id, run directory, or 'latest'")
    common.add_common_args(p)

    p = sub.add_parser("chair-save", help="validate a chair review (schemas/chair_review.schema.json) and "
                                          "store it as phase4/chair_review.json")
    p.add_argument("--run", required=True, help="run id, run directory, or 'latest'")
    p.add_argument("--file", required=True, help="chair review JSON document")
    common.add_common_args(p)

    p = sub.add_parser("holding", help="init a single-holding trial run")
    p.add_argument("ticker")
    _add_mode_args(p, holding=False)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-id", default=None)
    common.add_common_args(p)

    p = sub.add_parser("dry-run", help="whole pipeline offline with fixtures")
    p.add_argument("--fixtures-dir", default=None,
                   help=f"fixtures root (default {DRY_RUN_DEFAULT_FIXTURES.as_posix()})")
    _add_mode_args(p)
    p.add_argument("--max-iterations", type=int, default=DRY_RUN_MAX_ITERATIONS)
    common.add_common_args(p, exclude=("as_of", "config_dir"))
    p.add_argument("--as-of", dest="as_of", default=None, help=f"default {DRY_RUN_DEFAULT_AS_OF}")
    p.add_argument("--config-dir", dest="config_dir", default=None,
                   help="config dir (default <fixtures>/config)")

    p = sub.add_parser("scorecard", help="score the ledger -> ledger/scorecard_<date>.md")
    p.add_argument("--out", default=None, help="markdown output path")
    p.add_argument("--min-age-days", type=int, default=None)
    common.add_common_args(p)

    p = sub.add_parser("validate-config", help="print config errors or 'ok'")
    p.add_argument("--allow-template", action="store_true",
                   help="accept null shares/weights (the prefilled template)")
    p.add_argument("--json", action="store_true", help="print JSON {ok, errors}")
    common.add_common_args(p)
    return parser


def _set_runs_dir(args: argparse.Namespace) -> None:
    if getattr(args, "runs_dir", None):
        os.environ[common.ENV_RUNS_DIR] = str(Path(args.runs_dir).expanduser().resolve())


def cmd_init(args: argparse.Namespace, *, holding: str | None = None) -> int:
    common.apply_common_args(args)
    try:
        info = init_run(quick=args.quick, sectors=args.sectors,
                        holding=holding if holding is not None else getattr(args, "holding", None),
                        dry_run=args.dry_run, run_id=args.run_id)
    except InitError as exc:
        _emit({"type": "error", "step": "init", "error": exc.message, "details": exc.details})
        return 2
    _emit(info)
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    res = run_next(args.run, cli_args=args,
                   ignore_circuit_breaker=bool(getattr(args, "ignore_circuit_breaker", False)))
    _emit(res)
    return 1 if res.get("type") == "error" else 0


def cmd_redispatch(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    _emit(run_redispatch(args.run))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    _emit(run_status(args.run))
    return 0


def _read_arg_file(path: str, what: str, *, as_json: bool = False) -> Any:
    try:
        return common.read_json(path) if as_json else common.read_text(path)
    except (OSError, ValueError) as exc:
        raise ReopenError(f"cannot read {what} {path!r}: {_clip(exc, 200)}") from exc


def _reopen_items(args: argparse.Namespace) -> list[dict]:
    feedback_file = _read_arg_file(args.feedback_file, "--feedback-file") if args.feedback_file else ""
    parts = [t.strip() for t in ((args.feedback or ""), feedback_file) if t.strip()]
    shared = "\n".join(parts)
    items = [{"order_id": oid, "feedback": shared} for oid in args.order or []]
    if args.spec_file:
        spec = _read_arg_file(args.spec_file, "--spec-file", as_json=True)
        if isinstance(spec, dict) and isinstance(spec.get("orders"), list):
            spec = spec["orders"]
        if not isinstance(spec, list) or not all(isinstance(e, dict) and e.get("order_id") for e in spec):
            raise ReopenError("--spec-file must hold a JSON list of {\"order_id\", \"feedback\"} objects")
        items += [{"order_id": e["order_id"], "feedback": str(e.get("feedback") or shared)} for e in spec]
    return items


def cmd_reopen(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    common.apply_common_args(args)
    _emit(run_reopen(args.run, _reopen_items(args), reason=args.reason))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    common.apply_common_args(args)
    try:
        res = run_audit(args.run)
    except ReopenError as exc:
        _emit({"type": "error", "step": "audit", "message": exc.message, "details": exc.details})
        return 1
    _emit(res)
    return 0


def cmd_chair_save(args: argparse.Namespace) -> int:
    _set_runs_dir(args)
    common.apply_common_args(args)
    _emit(run_chair_save(args.run, args.file))
    return 0


def cmd_holding(args: argparse.Namespace) -> int:
    return cmd_init(args, holding=args.ticker)


def cmd_dry_run(args: argparse.Namespace) -> int:
    for dest, env in (("runs_dir", common.ENV_RUNS_DIR), ("cache_dir", common.ENV_CACHE_DIR),
                      ("ledger", common.ENV_LEDGER)):
        value = getattr(args, dest, None)
        if value:
            os.environ[env] = str(Path(value).expanduser().resolve())
    code, res = run_dry_run(fixtures_dir=args.fixtures_dir, as_of=args.as_of, config_dir=args.config_dir,
                            quick=args.quick, sectors=args.sectors, holding=args.holding,
                            max_iterations=args.max_iterations)
    _emit(res)
    return code


def cmd_scorecard(args: argparse.Namespace) -> int:
    common.apply_common_args(args)
    ledger_mod = _ledger_module()
    lp = common.ledger_path()
    kwargs: dict[str, Any] = {}
    if args.as_of:
        kwargs["as_of"] = common.today().isoformat()
    if args.min_age_days is not None:
        kwargs["min_age_days"] = args.min_age_days
    result = ledger_mod.score(lp, **kwargs) or {}
    as_of = common.today().isoformat()
    out = Path(args.out) if args.out else lp.parent / f"scorecard_{as_of}.md"
    renderer = getattr(ledger_mod, "render_scorecard_md", None)
    text = renderer(result) if callable(renderer) else render_scorecard(result, ledger=lp, as_of=as_of)
    common.write_text(out, text)
    payload: dict[str, Any] = {"type": "scorecard", "scorecard_md": _posix(out), "ledger": _posix(lp),
                               "as_of": as_of}
    for k, v in result.items():
        if k == "rows" and isinstance(v, list):
            payload["n_rows"] = len(v)
        else:
            payload[k] = v
    _emit(payload)
    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    common.apply_common_args(args)
    errors = common.config_errors(common.validate_configs(require_portfolio_values=not args.allow_template))
    if args.json:
        _emit({"ok": not errors, "config_dir": _posix(common.config_dir()), "errors": errors})
    elif errors:
        print("\n".join(errors))
    else:
        print("ok")
    return 1 if errors else 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init, "next": cmd_next, "redispatch": cmd_redispatch, "status": cmd_status,
    "holding": cmd_holding, "dry-run": cmd_dry_run, "scorecard": cmd_scorecard,
    "reopen": cmd_reopen, "audit": cmd_audit, "chair-save": cmd_chair_save,
    "validate-config": cmd_validate_config,
}


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except common.ConfigError as exc:
        _emit({"type": "error", "step": args.cmd, "message": str(exc)})
        return 2
    except ReopenError as exc:
        _emit({"type": "error", "step": args.cmd, "message": exc.message, "details": exc.details})
        return 2
    except Exception as exc:  # every failure is one JSON line, never a bare traceback
        _emit({"type": "error", "step": args.cmd, "message": _clip(f"{type(exc).__name__}: {exc}", 2000)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
