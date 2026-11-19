"""Shared foundation for every script in ``scripts/`` (DESIGN.md §0, §1, §3).

Contents
--------
* paths (env/flag aware) and the common CLI flags
* config loading (YAML) + config validation (JSON Schema + semantic checks)
* atomic UTF-8 IO, ``to_jsonable`` / ``safe_float`` / ``norm_ticker`` / ``safe_name``
* provenance helpers: ``fact`` / ``source`` / ``envelope`` (+ dataset source ids)
* time helpers: ``today`` (honours ``AGINV_AS_OF``), ``trading_days_between`` (NYSE)
* GICS sector constants, Yahoo sector maps, ``sic_to_sector``
* ``DiskCache`` (JSON entries + CSV frames, never caches errors)
* the ONLY network seam: ``http_get`` (+ text/json) and ``yf_history`` / ``yf_info`` /
  ``yf_funds``; offline mode (``AGINV_OFFLINE=1``) and fixture mode (``AGINV_FIXTURES_DIR``)

The as-built API reference (exact signatures + semantics) is CONTRACTS.md at the repo root.
Nothing in this module may call ``requests`` or ``yfinance`` outside the network seam.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Callable, Iterable, Mapping, TypeVar
from urllib.parse import quote, urlencode

import numpy as np
import pandas as pd
import requests
import yaml

T = TypeVar("T")

# =====================================================================================
# Constants
# =====================================================================================

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

ENV_CONFIG_DIR = "AGINV_CONFIG_DIR"
ENV_CACHE_DIR = "AGINV_CACHE_DIR"
ENV_RUNS_DIR = "AGINV_RUNS_DIR"
ENV_LEDGER = "AGINV_LEDGER"
ENV_AS_OF = "AGINV_AS_OF"
ENV_OFFLINE = "AGINV_OFFLINE"
ENV_FIXTURES_DIR = "AGINV_FIXTURES_DIR"
ENV_NOW = "AGINV_NOW"
ENV_SCHEMAS_DIR = "AGINV_SCHEMAS_DIR"
ENV_KNOWLEDGE_DIR = "AGINV_KNOWLEDGE_DIR"
ENV_TEMPLATES_DIR = "AGINV_TEMPLATES_DIR"
AGINV_ENV_VARS = (
    ENV_CONFIG_DIR, ENV_CACHE_DIR, ENV_RUNS_DIR, ENV_LEDGER, ENV_AS_OF, ENV_OFFLINE,
    ENV_FIXTURES_DIR, ENV_NOW, ENV_SCHEMAS_DIR, ENV_KNOWLEDGE_DIR, ENV_TEMPLATES_DIR,
)

DISCLAIMER = (
    "Research output for decision support only — not personalized financial advice. "
    "Figures come from free public sources that may be delayed or incomplete. "
    "The investor makes all decisions."
)

#: Role keys (schemas ``agent_role`` enum; report filename prefix).
ROLE_KEYS: tuple[str, ...] = (
    "fundamental", "technical", "operator_ceo", "industry_ops", "osint",
    "devils_advocate", "etf", "macro", "cio", "sector_head",
)
#: Roles that write ``specialist_report`` documents.
SPECIALIST_ROLES: tuple[str, ...] = ("fundamental", "technical", "operator_ceo", "industry_ops", "osint")
#: Roles that are scored by evidence_score / listed in ``evidence.required_fields``.
SCORED_ROLES: tuple[str, ...] = SPECIALIST_ROLES + ("devils_advocate", "etf")

#: role key -> agent type (``.claude/agents/<agent type>.md``).
AGENT_TYPES: dict[str, str] = {
    "fundamental": "fundamental-analyst",
    "technical": "technical-analyst",
    "operator_ceo": "operator-ceo",
    "industry_ops": "industry-ops",
    "osint": "osint-sentiment",
    "devils_advocate": "devils-advocate",
    "etf": "etf-analyst",
    "macro": "macro-geopolitics",
    "cio": "cio",
    "sector_head": "sector-head",
}

#: role key -> agent-output schema name (cio depends on mode: see ``role_schema``).
ROLE_SCHEMAS: dict[str, str] = {
    "fundamental": "specialist_report",
    "technical": "specialist_report",
    "operator_ceo": "specialist_report",
    "industry_ops": "specialist_report",
    "osint": "specialist_report",
    "devils_advocate": "devils_advocate_report",
    "etf": "etf_review",
    "macro": "macro_view",
    "cio": "cio_allocation",
    "sector_head": "sector_verdict",
}

AGENT_SCHEMA_NAMES: tuple[str, ...] = (
    "common_defs", "specialist_report", "devils_advocate_report", "etf_review",
    "sector_verdict", "macro_view", "cio_allocation", "cio_final", "failed_report",
)
DATA_SCHEMA_NAMES: tuple[str, ...] = (
    "prices", "technicals", "fundamentals", "valuation", "macro", "lookthrough", "screen",
    "evidence", "aggregate", "portfolio_plan", "manifest", "work_order",
)
CONFIG_NAMES: tuple[str, ...] = ("investor_profile", "portfolio", "universe", "committee", "etf_overrides")

#: schema name -> path relative to ``schemas_dir()``. The schema's ``$id`` is
#: ``schema_id(name)``: ``urn:agent-investors:<name>`` (agent + config schemas) or
#: ``urn:agent-investors:data/<name>`` (data contracts).
SCHEMA_FILES: dict[str, str] = {
    **{n: f"{n}.json" for n in AGENT_SCHEMA_NAMES},
    **{n: f"data/{n}.schema.json" for n in DATA_SCHEMA_NAMES},
    **{f"config_{n}": f"config_{n}.schema.json" for n in CONFIG_NAMES},
}
SCHEMA_ID_PREFIX = "urn:agent-investors:"

SECTOR_CLASSES: tuple[str, ...] = ("default", "financials", "fund")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high")
OHLCV_COLUMNS: list[str] = ["Open", "High", "Low", "Close", "Volume"]


class FactKind(str, Enum):
    """``Fact.kind`` values (drive freshness rules in evidence_score)."""

    PRICE = "price"
    FUNDAMENTAL = "fundamental"
    MACRO = "macro"
    OTHER = "other"

    def __str__(self) -> str:  # f-strings / str() give the bare value on 3.11+
        return self.value


FACT_KINDS: tuple[str, ...] = tuple(k.value for k in FactKind)
NA_REASONS: tuple[str, ...] = (
    "missing", "not_meaningful", "negative_denominator", "insufficient_history", "not_applicable",
)
#: ``na_reason`` values that are NOT penalized by the evidence score.
NA_UNPENALIZED: tuple[str, ...] = ("not_meaningful", "not_applicable")

# =====================================================================================
# Exceptions
# =====================================================================================


class AginvError(Exception):
    """Base class of every error raised on purpose by scripts/common.py."""


class ConfigError(AginvError):
    """A config file is missing/invalid or a required config value is absent."""


class DataUnavailable(AginvError):
    """Data could not be obtained (HTTP error, empty response, provider failure...)."""

    def __init__(self, message: str = "", *, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class ProviderBlocked(DataUnavailable):
    """Provider refuses service (bot wall / HTTP 403). Never try to bypass it."""


class OfflineError(DataUnavailable):
    """``AGINV_OFFLINE=1`` and a network call was attempted."""


class FixtureMissing(DataUnavailable):
    """``AGINV_FIXTURES_DIR`` is set but has no fixture for the requested resource."""


# =====================================================================================
# Paths
# =====================================================================================


def _env_path(var: str, default: Path) -> Path:
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def config_dir() -> Path:
    """Active config directory: ``$AGINV_CONFIG_DIR`` or ``<repo>/config``."""
    return _env_path(ENV_CONFIG_DIR, REPO_ROOT / "config")


def default_cache_dir() -> Path:
    """The repo's own cache root ``<repo>/data/cache`` (what ``cache_dir()`` falls back to)."""
    return (REPO_ROOT / "data" / "cache").resolve()


def cache_dir() -> Path:
    """Active cache root: ``$AGINV_CACHE_DIR`` or ``<repo>/data/cache``."""
    return _env_path(ENV_CACHE_DIR, REPO_ROOT / "data" / "cache")


def _is_within(path: Path, parent: Path) -> bool:
    """True when ``path`` is ``parent`` or lies below it (case-insensitive on Windows)."""
    a = os.path.normcase(str(Path(path).resolve()))
    b = os.path.normcase(str(Path(parent).resolve()))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def runs_dir() -> Path:
    """Active runs root: ``$AGINV_RUNS_DIR`` or ``<repo>/runs``."""
    return _env_path(ENV_RUNS_DIR, REPO_ROOT / "runs")


def ledger_path() -> Path:
    """Active ledger CSV: ``$AGINV_LEDGER`` or ``<repo>/ledger/recommendations.csv``."""
    return _env_path(ENV_LEDGER, REPO_ROOT / "ledger" / "recommendations.csv")


def schemas_dir() -> Path:
    """``$AGINV_SCHEMAS_DIR`` or ``<repo>/schemas``."""
    return _env_path(ENV_SCHEMAS_DIR, REPO_ROOT / "schemas")


def knowledge_dir() -> Path:
    """``$AGINV_KNOWLEDGE_DIR`` or ``<repo>/knowledge``."""
    return _env_path(ENV_KNOWLEDGE_DIR, REPO_ROOT / "knowledge")


def templates_dir() -> Path:
    """``$AGINV_TEMPLATES_DIR`` or ``<repo>/templates``."""
    return _env_path(ENV_TEMPLATES_DIR, REPO_ROOT / "templates")


def schema_path(name: str) -> Path:
    """Absolute path of schema ``name`` (a key of ``SCHEMA_FILES``)."""
    if name not in SCHEMA_FILES:
        raise KeyError(f"unknown schema name {name!r}; known: {sorted(SCHEMA_FILES)}")
    return schemas_dir() / SCHEMA_FILES[name]


def schema_id(name: str) -> str:
    """``$id`` of schema ``name``: ``urn:agent-investors:data/<name>`` for data contracts,
    ``urn:agent-investors:<name>`` otherwise (KeyError if unknown)."""
    if name not in SCHEMA_FILES:
        raise KeyError(f"unknown schema name {name!r}; known: {sorted(SCHEMA_FILES)}")
    return f"{SCHEMA_ID_PREFIX}data/{name}" if name in DATA_SCHEMA_NAMES else f"{SCHEMA_ID_PREFIX}{name}"


def role_schema(role: str, mode: str | None = None) -> str:
    """Schema name for an agent role (cio: mode 'allocation' -> cio_allocation, 'reconciliation' -> cio_final)."""
    if role == "cio":
        return "cio_final" if mode == "reconciliation" else "cio_allocation"
    if role not in ROLE_SCHEMAS:
        raise KeyError(f"unknown role {role!r}")
    return ROLE_SCHEMAS[role]


def is_offline() -> bool:
    """True when ``AGINV_OFFLINE`` is 1/true/yes/on."""
    return os.environ.get(ENV_OFFLINE, "").strip().lower() in ("1", "true", "yes", "on")


def active_fixtures_dir() -> Path | None:
    """``Path($AGINV_FIXTURES_DIR)`` when fixture mode is on, else None."""
    raw = os.environ.get(ENV_FIXTURES_DIR, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


#: dest -> (flag, env var, help)
_COMMON_ARGS: "OrderedDict[str, tuple[str, str, str]]" = OrderedDict([
    ("config_dir", ("--config-dir", ENV_CONFIG_DIR, "config directory (default: <repo>/config)")),
    ("cache_dir", ("--cache-dir", ENV_CACHE_DIR, "cache directory (default: <repo>/data/cache)")),
    ("runs_dir", ("--runs-dir", ENV_RUNS_DIR, "runs directory (default: <repo>/runs)")),
    ("ledger", ("--ledger", ENV_LEDGER, "ledger CSV (default: <repo>/ledger/recommendations.csv)")),
    ("as_of", ("--as-of", ENV_AS_OF, "as-of date YYYY-MM-DD (default: today)")),
])


def add_common_args(parser: Any, *, exclude: Iterable[str] = ()) -> Any:
    """Add ``--config-dir --cache-dir --runs-dir --ledger --as-of`` (default None) to an
    argparse parser. ``exclude`` may list dests (``"as_of"``) or flags (``"--as-of"``) to skip."""
    skip = set(exclude)
    for dest, (flag, _env, help_text) in _COMMON_ARGS.items():
        if dest in skip or flag in skip:
            continue
        parser.add_argument(flag, dest=dest, default=None, help=help_text)
    return parser


def apply_common_args(args: Any) -> Any:
    """Export the common flags present on ``args`` to their ``AGINV_*`` env vars (dirs are
    resolved to absolute paths, ``--as-of`` validated) and clear in-process caches."""
    for dest, (flag, env, _help) in _COMMON_ARGS.items():
        value = getattr(args, dest, None)
        if value is None or str(value).strip() == "":
            continue
        if dest == "as_of":
            try:
                parsed = parse_date(value)
            except ValueError:
                parsed = None
            if parsed is None:
                raise ConfigError(f"{flag}: not a date (expected YYYY-MM-DD): {value!r}")
            os.environ[env] = parsed.isoformat()
        else:
            os.environ[env] = str(Path(str(value)).expanduser().resolve())
    clear_caches()
    return args


# =====================================================================================
# Small pure helpers (ported from stock_dashboard/utils.py, fixed)
# =====================================================================================


def safe_float(x: Any) -> float | None:
    """Coerce to float; None for None / bool / NaN / inf / pd.NA / non-numeric."""
    if x is None or isinstance(x, (bool, np.bool_)):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


#: Significant digits kept by ``round_price``. yfinance hands back float32-derived prices
#: (769.64 arrives as 769.6400146484375); float32 carries ~7.2 significant digits, so seven digits
#: recover the quoted decimal while 6 decimal places (769.640015) would not.
PRICE_SIG_DIGITS = 7


def round_price(x: Any, sig: int = PRICE_SIG_DIGITS, dp: int = 6) -> float | None:
    """Price value without float32 noise: ``sig`` significant digits, then at most ``dp``
    decimals (``769.6400146484375`` -> ``769.64``). None for None / NaN / inf / non-numeric.
    Use it for prices only (it would truncate large share counts); derived indicator values are
    rounded with plain decimal places by their callers."""
    f = safe_float(x)
    if f is None:
        return None
    if f == 0:
        return 0.0
    return round(float(f"{f:.{int(sig)}g}"), int(dp))


def _json_key(k: Any) -> str:
    if isinstance(k, str):
        return k
    if isinstance(k, Enum):
        return str(k.value)
    if isinstance(k, (pd.Timestamp, datetime, date)):
        return str(to_jsonable(k))
    return str(k)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert to JSON-safe builtins.

    NaN/inf/pd.NA/NaT -> None; numpy scalars -> int/float/bool; Enum -> value; Path -> posix
    string; Decimal -> float; date -> 'YYYY-MM-DD'; datetime -> ISO-8601; pd.Timestamp at
    midnight without tz -> 'YYYY-MM-DD' else ISO; DataFrame -> {"index","columns","data"};
    Series -> {key: value}; dataclass -> dict; set -> sorted list (when sortable); bytes ->
    utf-8 text; anything else -> str(obj).
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return to_jsonable(float(obj))
    if isinstance(obj, Decimal):
        return to_jsonable(float(obj))
    if obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, np.datetime64):
        return None if np.isnat(obj) else to_jsonable(pd.Timestamp(obj))
    if isinstance(obj, pd.Timestamp):
        if obj.tzinfo is None and obj == obj.normalize():
            return obj.date().isoformat()
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, PurePath):
        return obj.as_posix()
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.DataFrame):
        return {
            "index": [to_jsonable(i) for i in obj.index],
            "columns": [_json_key(c) for c in obj.columns],
            "data": [[to_jsonable(v) for v in row] for row in obj.to_numpy(dtype=object).tolist()],
        }
    if isinstance(obj, pd.Series):
        return {_json_key(k): to_jsonable(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {_json_key(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        items = [to_jsonable(v) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return items
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


def retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 1.0, *,
          exceptions: tuple[type[BaseException], ...] = (Exception,),
          no_retry: tuple[type[BaseException], ...] | None = None,
          max_delay: float = 30.0) -> T:
    """Call ``fn()`` up to ``attempts`` times with exponential backoff (base·2^i, capped).

    Re-raises the last exception. Never retries ``no_retry`` exceptions (default: ConfigError,
    OfflineError, FixtureMissing, ProviderBlocked). Sleeps via ``time.sleep`` (patchable).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if no_retry is None:
        no_retry = (ConfigError, OfflineError, FixtureMissing, ProviderBlocked)
    for i in range(attempts):
        try:
            return fn()
        except no_retry:
            raise
        except exceptions:
            if i == attempts - 1:
                raise
            time.sleep(min(max_delay, base_delay * (2 ** i)))
    raise RuntimeError("unreachable")  # pragma: no cover


_EXCHANGE_SUFFIXES = frozenset({
    "TA", "L", "TO", "V", "NE", "DE", "F", "PA", "AS", "BR", "MC", "MI", "LS", "SW", "ST", "OL",
    "CO", "HE", "IR", "VI", "WA", "HK", "T", "KS", "KQ", "SS", "SZ", "TW", "SI", "AX", "NZ",
    "NS", "BO", "SA", "MX", "JO",
})


#: US share-class tickers whose class letter collides with an exchange suffix or looks like one
#: (``MKC.V`` is McCormick's non-voting class, ``MKC-V`` on Yahoo - not a TSX-Venture listing).
#: These always map to the dash form.
US_SHARE_CLASS_TICKERS = frozenset({
    "MKC.V", "BF.B", "BRK.B", "BRK.A", "LEN.B", "HEI.A", "CRD.A", "CRD.B", "GEF.B", "MOG.A", "MOG.B",
    "UHAL.B", "LGF.A", "LGF.B", "RDS.A", "RDS.B",
})


def _keeps_exchange_suffix(s: str, base: str, suffix: str) -> bool:
    """True when the trailing ``.suffix`` of the normalized ticker ``s`` is an exchange suffix.
    Not for the known US share classes; a one-letter base only counts for a multi-letter suffix
    (``T.TO`` Telus is an exchange listing, ``X.V`` is a class letter on a one-letter ticker)."""
    if not base or suffix not in _EXCHANGE_SUFFIXES or s in US_SHARE_CLASS_TICKERS:
        return False
    return len(base) >= 2 or len(suffix) >= 2


def norm_ticker(ticker: Any) -> str:
    """Normalize a ticker to Yahoo style: strip, upper-case, remove spaces, ``/`` and share-class
    dots -> ``-`` (``brk.b`` -> ``BRK-B``, ``MKC.V`` -> ``MKC-V``). Known exchange suffixes are
    kept (``TEVA.TA``, ``AZN.L``, ``SAP.DE``) except for ``US_SHARE_CLASS_TICKERS``.
    Raises ValueError for empty input."""
    if ticker is None:
        raise ValueError("ticker is None")
    s = re.sub(r"\s+", "", str(ticker)).upper().replace("/", "-")
    if not s:
        raise ValueError("empty ticker")
    if "." in s:
        base, _, suffix = s.rpartition(".")
        if _keeps_exchange_suffix(s, base, suffix):
            return f"{base.replace('.', '-')}.{suffix}"
        s = s.replace(".", "-")
    return s


_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]')
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)
_SAFE_NAME_MAX = 120


def safe_name(key: Any) -> str:
    """Filesystem-safe (Windows-safe) file stem for ``key``.

    Characters invalid on Windows (``<>:"/\\|?*``, control chars, whitespace) -> ``_``;
    trailing dots/spaces stripped; reserved device names (CON, PRN, AUX, NUL, COM1-9,
    LPT1-9, any case, with or without extension) get a trailing underscore on the stem
    (``PRN`` -> ``PRN_``, ``con.txt`` -> ``con_.txt``); empty -> ``_``; names longer than 120
    chars are truncated and suffixed with a short sha1 of the original key."""
    raw = str(key)
    s = _WIN_INVALID.sub("_", raw.strip().rstrip(" .")).rstrip(" .")
    if not s:
        s = "_"
    stem, dot, rest = s.partition(".")
    if stem.upper() in _WIN_RESERVED:
        s = f"{stem}_{dot}{rest}"
    if len(s) > _SAFE_NAME_MAX:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        s = f"{s[:_SAFE_NAME_MAX - 13]}_{digest}"
    return s


def data_filename(kind: str, ticker: str, ext: str = "json") -> str:
    """``<kind>_<NORM TICKER>.<ext>`` — the only allowed naming for per-ticker files."""
    stem = safe_name(f"{kind}_{norm_ticker(ticker)}")
    suffix = ext.lstrip(".")
    return f"{stem}.{suffix}"


def get_dotted(doc: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path (``extensions.valuation.fair_value_base``; list indices allowed as
    ``items.0.x``). Missing -> ``default``."""
    cur = doc
    for part in str(path).split("."):
        if isinstance(cur, Mapping):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.lstrip("-").isdigit():
            idx = int(part)
            if not -len(cur) <= idx < len(cur):
                return default
            cur = cur[idx]
        else:
            return default
    return cur


def json_pointer_get(doc: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer (``/ttm/revenue``, ``""`` = whole doc). Raises
    KeyError when the pointer does not resolve."""
    if pointer in ("", "#"):
        return doc
    ptr = pointer[1:] if pointer.startswith("#") else pointer
    if not ptr.startswith("/"):
        raise KeyError(f"JSON pointer must start with '/': {pointer!r}")
    cur = doc
    for raw in ptr.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, Mapping):
            if token not in cur:
                raise KeyError(f"{pointer!r}: key {token!r} not found")
            cur = cur[token]
        elif isinstance(cur, list):
            if not token.isdigit() or int(token) >= len(cur):
                raise KeyError(f"{pointer!r}: bad list index {token!r}")
            cur = cur[int(token)]
        else:
            raise KeyError(f"{pointer!r}: cannot descend into {type(cur).__name__}")
    return cur


def is_fact(obj: Any) -> bool:
    """True when ``obj`` looks like a Fact dict (has value, unit, source_id, as_of, kind)."""
    return isinstance(obj, Mapping) and all(k in obj for k in ("value", "unit", "source_id", "as_of", "kind"))


def fact_value(obj: Any) -> float | None:
    """``obj['value']`` for a Fact, else ``safe_float(obj)``."""
    if is_fact(obj):
        return safe_float(obj.get("value"))
    return safe_float(obj)


# =====================================================================================
# IO (UTF-8, atomic)
# =====================================================================================


def setup_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 with errors='replace' (Windows consoles)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def read_text(path: str | Path) -> str:
    """Read a UTF-8 text file (a leading BOM is stripped)."""
    text = Path(path).read_text(encoding="utf-8")
    return text[1:] if text.startswith("﻿") else text


def read_json(path: str | Path) -> Any:
    """Parse a UTF-8 JSON file (BOM tolerated)."""
    return json.loads(read_text(path))


def write_text(path: str | Path, text: str) -> Path:
    """Atomically write ``text`` as UTF-8 (no newline translation): temp file in the same
    directory + ``os.replace`` (3 retries on PermissionError). Creates parent dirs."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        for attempt in range(4):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.1 * (2 ** attempt))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def dumps_json(data: Any, *, indent: int | None = 2) -> str:
    """``json.dumps(to_jsonable(data), ensure_ascii=False, allow_nan=False, indent=indent)``."""
    return json.dumps(to_jsonable(data), ensure_ascii=False, allow_nan=False, indent=indent)


def write_json(path: str | Path, data: Any, *, indent: int | None = 2) -> Path:
    """Atomically write ``data`` as UTF-8 JSON (``to_jsonable`` first; NaN/inf -> null;
    ``ensure_ascii=False``; trailing newline). Returns the path."""
    return write_text(path, dumps_json(data, indent=indent) + "\n")


# =====================================================================================
# Time
# =====================================================================================

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_date(x: Any) -> date | None:
    """Coerce to ``datetime.date``. Accepts date, datetime, pd.Timestamp, np.datetime64,
    'YYYY-MM-DD' (anything after the first 10 chars is ignored when it starts like a date) and
    ISO timestamps. None / '' / NaN / NaT -> None. Unparseable -> ValueError."""
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    if x is pd.NaT or x is pd.NA:
        return None
    if isinstance(x, pd.Timestamp):
        return None if pd.isna(x) else x.date()
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, np.datetime64):
        return None if np.isnat(x) else pd.Timestamp(x).date()
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        if _DATE_RE.match(s):
            try:
                return date.fromisoformat(s[:10])
            except ValueError as exc:
                raise ValueError(f"invalid date: {x!r}") from exc
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError(f"invalid date: {x!r}") from exc
    raise ValueError(f"cannot interpret {type(x).__name__} as a date: {x!r}")


def date_str(x: Any) -> str | None:
    """``parse_date(x).isoformat()`` or None."""
    d = parse_date(x)
    return d.isoformat() if d else None


def today() -> date:
    """Today's date, or ``$AGINV_AS_OF`` when set (deterministic tests / dry-run)."""
    raw = os.environ.get(ENV_AS_OF, "").strip()
    if raw:
        try:
            d = parse_date(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_AS_OF} is not a date: {raw!r}") from exc
        if d is not None:
            return d
    return date.today()


def now_iso() -> str:
    """Current UTC timestamp ``YYYY-MM-DDTHH:MM:SS+00:00`` (``$AGINV_NOW`` overrides verbatim)."""
    raw = os.environ.get(ENV_NOW, "").strip()
    if raw:
        return raw
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def market_holidays(config_dir: str | Path | None = None) -> list[date]:
    """NYSE holidays from ``committee.yaml: market_holidays`` (empty list if unavailable)."""
    try:
        cfg = committee_cfg(config_dir)
    except ConfigError:
        return []
    out: list[date] = []
    for h in cfg.get("market_holidays") or []:
        try:
            d = parse_date(h)
        except ValueError:
            continue
        if d:
            out.append(d)
    return sorted(set(out))


def _holiday_array(holidays: Iterable[Any] | None) -> np.ndarray:
    hol = market_holidays() if holidays is None else [parse_date(h) for h in holidays]
    return np.array(sorted({h.isoformat() for h in hol if h}), dtype="datetime64[D]")


def trading_days_between(d1: Any, d2: Any, holidays: Iterable[Any] | None = None) -> int:
    """Number of NYSE trading days strictly after ``d1`` up to and including ``d2``
    (``np.busday_count(d1+1, d2+1, holidays=...)``; weekends + ``holidays`` excluded).

    Same day -> 0; Fri -> next Mon -> 1; ``d2 < d1`` -> negative count. ``holidays=None``
    loads ``committee.yaml: market_holidays``; pass ``()`` for none."""
    a, b = parse_date(d1), parse_date(d2)
    if a is None or b is None:
        raise ValueError(f"trading_days_between needs two dates, got {d1!r}, {d2!r}")
    hol = _holiday_array(holidays)
    if b < a:
        return -trading_days_between(b, a, [str(h) for h in hol])
    start = np.datetime64((a + timedelta(days=1)).isoformat(), "D")
    end = np.datetime64((b + timedelta(days=1)).isoformat(), "D")
    return int(np.busday_count(start, end, holidays=hol))


def is_trading_day(d: Any, holidays: Iterable[Any] | None = None) -> bool:
    """True when ``d`` is a weekday and not a holiday."""
    day = parse_date(d)
    if day is None:
        raise ValueError("is_trading_day needs a date")
    return bool(np.is_busday(np.datetime64(day.isoformat(), "D"), holidays=_holiday_array(holidays)))


# =====================================================================================
# Sectors
# =====================================================================================

SECTORS: "OrderedDict[str, dict[str, str]]" = OrderedDict([
    ("information_technology", {"name": "Information Technology", "etf": "XLK"}),
    ("health_care", {"name": "Health Care", "etf": "XLV"}),
    ("financials", {"name": "Financials", "etf": "XLF"}),
    ("energy", {"name": "Energy", "etf": "XLE"}),
    ("industrials", {"name": "Industrials", "etf": "XLI"}),
    ("consumer_discretionary", {"name": "Consumer Discretionary", "etf": "XLY"}),
    ("consumer_staples", {"name": "Consumer Staples", "etf": "XLP"}),
    ("utilities", {"name": "Utilities", "etf": "XLU"}),
    ("materials", {"name": "Materials", "etf": "XLB"}),
    ("real_estate", {"name": "Real Estate", "etf": "XLRE"}),
    ("communication_services", {"name": "Communication Services", "etf": "XLC"}),
])
SECTOR_KEYS: tuple[str, ...] = tuple(SECTORS)
#: Non-GICS buckets: ``funds`` (pseudo-sector for fund holdings in Phase 2), and the
#: look-through buckets ``cash`` and ``unclassified``.
PSEUDO_SECTORS: dict[str, str] = {"funds": "Funds", "cash": "Cash", "unclassified": "Unclassified"}

#: Yahoo ``info['sector']`` names -> sector keys (GICS names accepted too).
YF_SECTOR_MAP: dict[str, str] = {
    "Technology": "information_technology",
    "Information Technology": "information_technology",
    "Healthcare": "health_care",
    "Health Care": "health_care",
    "Financial Services": "financials",
    "Financials": "financials",
    "Financial": "financials",
    "Energy": "energy",
    "Industrials": "industrials",
    "Consumer Cyclical": "consumer_discretionary",
    "Consumer Discretionary": "consumer_discretionary",
    "Consumer Defensive": "consumer_staples",
    "Consumer Staples": "consumer_staples",
    "Utilities": "utilities",
    "Basic Materials": "materials",
    "Materials": "materials",
    "Real Estate": "real_estate",
    "Communication Services": "communication_services",
}

#: Yahoo ``funds_data.sector_weightings`` keys -> sector keys.
YF_FUND_SECTOR_MAP: dict[str, str] = {
    "technology": "information_technology",
    "healthcare": "health_care",
    "financial_services": "financials",
    "energy": "energy",
    "industrials": "industrials",
    "consumer_cyclical": "consumer_discretionary",
    "consumer_defensive": "consumer_staples",
    "utilities": "utilities",
    "basic_materials": "materials",
    "realestate": "real_estate",
    "real_estate": "real_estate",
    "communication_services": "communication_services",
}


def sector_display(key: str) -> str:
    """Display name for a sector key (also ``funds``/``cash``/``unclassified``)."""
    if key in SECTORS:
        return SECTORS[key]["name"]
    if key in PSEUDO_SECTORS:
        return PSEUDO_SECTORS[key]
    return str(key).replace("_", " ").title()


def sector_etf(key: str) -> str | None:
    """Benchmark SPDR ETF for a sector key (None for pseudo-sectors/unknown)."""
    entry = SECTORS.get(key)
    return entry["etf"] if entry else None


def sector_class(sector: str | None, asset_type: str = "stock") -> str:
    """``fund`` for funds, ``financials`` for the financials sector, else ``default``."""
    if asset_type == "fund" or sector == "funds":
        return "fund"
    if sector == "financials":
        return "financials"
    return "default"


# First match wins: specific exceptions are listed before the broad range they sit in.
_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "consumer_staples"),
    (1000, 1099, "materials"),
    (1200, 1299, "energy"),
    (1300, 1399, "energy"),
    (1400, 1499, "materials"),
    (1500, 1799, "industrials"),
    (2000, 2199, "consumer_staples"),
    (2200, 2399, "consumer_discretionary"),
    (2400, 2499, "materials"),
    (2500, 2599, "consumer_discretionary"),
    (2600, 2699, "materials"),
    (2700, 2799, "communication_services"),
    (2800, 2829, "materials"),
    (2830, 2836, "health_care"),
    (2840, 2844, "consumer_staples"),
    (2845, 2899, "materials"),
    (2900, 2999, "energy"),
    (3000, 3099, "materials"),
    (3100, 3199, "consumer_discretionary"),
    (3200, 3399, "materials"),
    (3400, 3569, "industrials"),
    (3570, 3579, "information_technology"),
    (3580, 3599, "industrials"),
    (3600, 3629, "industrials"),
    (3630, 3639, "consumer_discretionary"),
    (3640, 3659, "industrials"),
    (3660, 3699, "information_technology"),
    (3711, 3716, "consumer_discretionary"),
    (3700, 3799, "industrials"),
    (3812, 3812, "industrials"),
    (3840, 3851, "health_care"),
    (3800, 3899, "information_technology"),
    (3900, 3999, "consumer_discretionary"),
    (4000, 4799, "industrials"),
    (4800, 4899, "communication_services"),
    (4900, 4999, "utilities"),
    (5122, 5122, "health_care"),
    (5140, 5149, "consumer_staples"),
    (5000, 5199, "industrials"),
    (5400, 5499, "consumer_staples"),
    (5912, 5912, "consumer_staples"),
    (5200, 5999, "consumer_discretionary"),
    (6798, 6798, "real_estate"),
    (6000, 6499, "financials"),
    (6500, 6553, "real_estate"),
    (6700, 6799, "financials"),
    (7000, 7099, "consumer_discretionary"),
    (7200, 7299, "consumer_discretionary"),
    (7370, 7379, "information_technology"),
    (7300, 7399, "industrials"),
    (7800, 7989, "communication_services"),
    (7990, 7999, "consumer_discretionary"),
    (8000, 8099, "health_care"),
    (8700, 8799, "industrials"),
)


def sic_to_sector(sic: Any) -> str | None:
    """Rough SIC -> GICS-sector fallback (see CONTRACTS.md for the range table).
    Accepts int or numeric string; anything unmapped / invalid -> None."""
    code = safe_float(sic)
    if code is None or code != int(code):
        return None
    n = int(code)
    for lo, hi, sector in _SIC_RANGES:
        if lo <= n <= hi:
            return sector
    return None


# =====================================================================================
# Provenance: Fact / Source / Envelope
# =====================================================================================

_FACT_EXTRA_DATE_KEYS = ("filed", "observation_date", "period_end")


def _fact_number(value: Any) -> int | float | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        raise TypeError("Fact value must be numeric, got bool")
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating, Decimal)):
        f = float(value)
        return f if math.isfinite(f) else None
    raise TypeError(f"Fact value must be numeric or None, got {type(value).__name__}: {value!r}")


def fact(value: Any, unit: str, source_id: str | None, as_of: Any, kind: str | FactKind,
         na_reason: str | None = None, **extra: Any) -> dict:
    """Build a Fact dict (DESIGN §1.1).

    * ``value``: int/float/numpy number or None; NaN/inf -> None. Non-numeric -> TypeError.
    * ``kind``: one of ``FACT_KINDS`` (or a FactKind) else ValueError.
    * ``value is None`` -> ``na_reason`` defaults to ``"missing"`` and must be in ``NA_REASONS``;
      a non-null value always carries ``na_reason: None``.
    * ``as_of`` and the date-like extras (``filed``, ``observation_date``, ``period_end``) are
      normalized to 'YYYY-MM-DD'. Extras whose value is None are omitted.
    """
    v = _fact_number(value)
    k = FactKind(kind).value
    if v is None:
        reason = na_reason or "missing"
        if reason not in NA_REASONS:
            raise ValueError(f"na_reason must be one of {NA_REASONS}, got {reason!r}")
    else:
        reason = None
    out: dict[str, Any] = {
        "value": v,
        "unit": unit,
        "source_id": source_id,
        "as_of": date_str(as_of),
        "kind": k,
        "na_reason": reason,
    }
    for key, val in extra.items():
        if val is None:
            continue
        if key in _FACT_EXTRA_DATE_KEYS:
            out[key] = date_str(val)
        else:
            out[key] = to_jsonable(val)
    return out


def na_fact(unit: str, source_id: str | None, as_of: Any, kind: str | FactKind,
            na_reason: str = "missing", **extra: Any) -> dict:
    """Shorthand for ``fact(None, ...)`` with an explicit ``na_reason``."""
    return fact(None, unit, source_id, as_of, kind, na_reason, **extra)


def _check_url(url: str) -> str:
    if not isinstance(url, str) or not re.match(r"^https?://\S+$", url.strip()):
        raise ValueError(f"source url must be an http(s) URL, got {url!r}")
    return url.strip()


def source(source_id: str | None = None, url: str | None = None, title: str | None = None,
           accessed: Any = None, *, kind: str = "dataset", path: str | Path | None = None,
           id: str | None = None) -> dict:  # noqa: A002 - ``id=`` mirrors the Source key
    """Build a Source dict ``{id, url, title, accessed, kind[, path]}`` (DESIGN §1.2).
    The id may be passed positionally, as ``source_id=`` or as ``id=``. ``url`` must be
    http(s) (ValueError otherwise); ``title`` defaults to the url; ``accessed`` defaults to
    ``today()``; ``kind`` is ``dataset`` or ``web``; ``path`` (local file) is stored as a posix
    string."""
    sid = source_id if source_id is not None else id
    if not sid:
        raise ValueError("source id is required")
    if kind not in ("dataset", "web"):
        raise ValueError(f"source kind must be 'dataset' or 'web', got {kind!r}")
    checked_url = _check_url(url)  # type: ignore[arg-type]
    out: dict[str, Any] = {
        "id": str(sid),
        "url": checked_url,
        "title": str(title) if title else checked_url,
        "accessed": date_str(accessed) if accessed is not None else today().isoformat(),
        "kind": kind,
    }
    if path is not None:
        out["path"] = Path(path).as_posix()
    return out


# ---- upstream URLs + dataset source ids (DESIGN §1.2) -------------------------------

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YF_HISTORY_URL = "https://finance.yahoo.com/quote/{ticker}/history/"
YF_QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}/"
YF_HOLDINGS_URL = "https://finance.yahoo.com/quote/{ticker}/holdings/"


def _yq(ticker: str) -> str:
    return quote(str(ticker), safe="-.=")


def sec_companyfacts_url(cik: int | str) -> str:
    return SEC_COMPANYFACTS_URL.format(cik=int(cik))


def sec_submissions_url(cik: int | str) -> str:
    return SEC_SUBMISSIONS_URL.format(cik=int(cik))


def fred_csv_url(series: str) -> str:
    return FRED_CSV_URL.format(series=quote(str(series), safe=""))


def stooq_csv_url(symbol: str) -> str:
    """Stooq daily CSV URL for an already-mapped Stooq symbol (e.g. ``ko.us``, ``usdils``)."""
    return STOOQ_CSV_URL.format(symbol=quote(str(symbol).lower(), safe="."))


def yf_history_url(ticker: str) -> str:
    return YF_HISTORY_URL.format(ticker=_yq(ticker))


def yf_quote_url(ticker: str) -> str:
    return YF_QUOTE_URL.format(ticker=_yq(ticker))


def yf_holdings_url(ticker: str) -> str:
    return YF_HOLDINGS_URL.format(ticker=_yq(ticker))


def dataset_source(source_id: str, *, title: str | None = None, accessed: Any = None,
                   path: str | Path | None = None, cik: int | str | None = None,
                   provider: str = "yfinance", url: str | None = None) -> dict:
    """Build the Source for a deterministic dataset id (``D:PX:KO``, ``D:YF:QQQ:FUND``,
    ``D:SEC:KO:CF`` (needs ``cik``), ``D:SEC:KO:SUB`` (``cik``), ``D:SEC:MAP``,
    ``D:FRED:DGS10``, ``D:VAL:KO`` (``cik``), ``D:CFG:<name>`` (needs ``url``)).
    ``provider='stooq'`` with ``D:PX`` requires ``url`` (the Stooq CSV URL actually used).
    An explicit ``url`` always wins."""
    parts = str(source_id).split(":")
    if len(parts) < 2 or parts[0] != "D":
        raise ValueError(f"not a dataset source id: {source_id!r}")
    family = parts[1]
    auto_url: str | None = None
    auto_title: str
    if family == "PX" and len(parts) == 3:
        t = parts[2]
        if provider == "stooq":
            auto_title = f"Stooq daily price history — {t}"
        else:
            auto_url = yf_history_url(t)
            auto_title = f"Yahoo Finance price history (yfinance) — {t}"
    elif family == "YF" and len(parts) == 4 and parts[3] == "INFO":
        auto_url, auto_title = yf_quote_url(parts[2]), f"Yahoo Finance quote/profile (yfinance) — {parts[2]}"
    elif family == "YF" and len(parts) == 4 and parts[3] == "FUND":
        auto_url, auto_title = yf_holdings_url(parts[2]), f"Yahoo Finance fund holdings (yfinance) — {parts[2]}"
    elif family == "SEC" and len(parts) == 3 and parts[2] == "MAP":
        auto_url, auto_title = SEC_TICKERS_URL, "SEC EDGAR company tickers map"
    elif family == "SEC" and len(parts) == 4 and parts[3] in ("CF", "SUB"):
        if cik is not None:
            auto_url = sec_companyfacts_url(cik) if parts[3] == "CF" else sec_submissions_url(cik)
        what = "XBRL companyfacts" if parts[3] == "CF" else "submissions"
        auto_title = f"SEC EDGAR {what} — {parts[2]}" + (f" (CIK {int(cik):010d})" if cik is not None else "")
    elif family == "FRED" and len(parts) == 3:
        auto_url, auto_title = fred_csv_url(parts[2]), f"FRED series {parts[2]} (CSV)"
    elif family == "VAL" and len(parts) == 3:
        if cik is not None:
            auto_url = sec_companyfacts_url(cik)
        auto_title = f"Valuation grid derived from SEC companyfacts — {parts[2]}"
    elif family == "CFG" and len(parts) >= 3:
        auto_title = f"Configured assumption — {':'.join(parts[2:])}"
    else:
        raise ValueError(f"unrecognized dataset source id: {source_id!r}")
    final_url = url or auto_url
    if not final_url:
        raise ValueError(f"{source_id}: url (or cik) required to build this source")
    return source(source_id, final_url, title or auto_title, accessed, kind="dataset", path=path)


def dedupe_sources(sources: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Keep the first Source per ``id`` (order preserved)."""
    seen: set[str] = set()
    out: list[dict] = []
    for s in sources or []:
        sid = str(s.get("id"))
        if sid in seen:
            continue
        seen.add(sid)
        out.append(dict(s))
    return out


def flag(code: str, severity: str, detail: str = "") -> dict:
    """Envelope flag ``{"code","severity","detail"}`` (severity low|medium|high)."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")
    return {"code": str(code), "severity": severity, "detail": str(detail)}


def envelope(schema_name: str, as_of: Any, **payload: Any) -> dict:
    """Script-output envelope (DESIGN §1.3)::

        {"schema": "<name>/v1", "generated_at": now_iso(), "as_of": "YYYY-MM-DD",
         "sources": [...], "missing_data": [...], "flags": [...], **payload}

    ``schema_name`` without a ``/`` gets ``/v1`` appended. ``as_of`` None -> ``today()``.
    ``sources`` are de-duplicated by id, ``missing_data`` de-duplicated (order kept)."""
    schema = schema_name if "/" in schema_name else f"{schema_name}/v1"
    sources = dedupe_sources(payload.pop("sources", None) or [])
    missing: list[Any] = []
    for m in payload.pop("missing_data", None) or []:
        if m not in missing:
            missing.append(m)
    flags = list(payload.pop("flags", None) or [])
    out: dict[str, Any] = {
        "schema": schema,
        "generated_at": now_iso(),
        "as_of": date_str(as_of) or today().isoformat(),
        "sources": sources,
        "missing_data": missing,
        "flags": flags,
    }
    out.update(payload)
    return out


# =====================================================================================
# Config
# =====================================================================================


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML-1.2-style scalars: only true/false are booleans (tickers like
    ``ON``/``NO`` stay strings), dates stay 'YYYY-MM-DD' strings, and ``1e9`` / ``10e9`` are
    floats (PyYAML's default needs a dot and a signed exponent)."""


_BOOL_TAG = "tag:yaml.org,2002:bool"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_TS_TAG = "tag:yaml.org,2002:timestamp"
_YamlLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL_TAG, _FLOAT_TAG, _TS_TAG)]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_YamlLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
_YamlLoader.add_implicit_resolver(
    _FLOAT_TAG,
    re.compile(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
                  |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
                  |[-+]?\.[0-9_]+(?:[eE][-+]?[0-9]+)?
                  |[-+]?\.(?:inf|Inf|INF)
                  |\.(?:nan|NaN|NAN))$""", re.X),
    list("-+0123456789."))


def load_yaml(path: str | Path) -> Any:
    """Parse a UTF-8 YAML file with the project loader (see ``_YamlLoader``). Empty file ->
    ``{}``. Missing file or parse error -> ConfigError."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"{p.as_posix()}: file not found")
    try:
        data = yaml.load(read_text(p), Loader=_YamlLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p.as_posix()}: YAML parse error: {exc}") from exc
    return {} if data is None else data


def _resolve_config_dir(cfg_dir: str | Path | None) -> Path:
    return Path(cfg_dir).expanduser().resolve() if cfg_dir else config_dir()


def load_config(name: str, config_dir: str | Path | None = None) -> dict:
    """Load ``<config dir>/<name>.yaml`` (name in ``CONFIG_NAMES``). Not validated — call
    ``validate_configs`` for that. Raises ConfigError if missing/unparseable/not a mapping."""
    if name not in CONFIG_NAMES:
        raise ValueError(f"unknown config {name!r}; expected one of {CONFIG_NAMES}")
    path = _resolve_config_dir(config_dir) / f"{name}.yaml"
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path.as_posix()}: top level must be a mapping")
    return data


_CFG_CACHE: dict[tuple[str, int], dict] = {}
_CFG_LOCK = threading.Lock()


def committee_cfg(config_dir: str | Path | None = None) -> dict:
    """``committee.yaml`` as a dict — cached per (config dir, file mtime); returns a deep copy."""
    path = _resolve_config_dir(config_dir) / "committee.yaml"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise ConfigError(f"{path.as_posix()}: file not found") from exc
    key = (str(path), mtime)
    with _CFG_LOCK:
        cached = _CFG_CACHE.get(key)
    if cached is None:
        cached = load_config("committee", path.parent)
        with _CFG_LOCK:
            _CFG_CACHE.clear()  # one config dir/version at a time is plenty
            _CFG_CACHE[key] = cached
    return copy.deepcopy(cached)


# ---- config validation ---------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_SCHEMA_CACHE: dict[tuple[str, int], dict] = {}
_SETUP_HINT = "run /committee-setup (or edit config/portfolio.yaml)"


def email_problem(email: Any) -> str | None:
    """None when ``email`` is a usable contact email; else a short description of the problem
    (missing, malformed, or a placeholder containing 'example')."""
    if email is None or str(email).strip() == "":
        return "missing"
    e = str(email).strip()
    if not _EMAIL_RE.match(e):
        return f"{e!r} does not look like an email address"
    if "example" in e.lower():
        return f"{e!r} is a placeholder (contains 'example')"
    return None


def load_schema(name: str) -> dict:
    """JSON schema ``name`` (a key of ``SCHEMA_FILES``); cached by mtime, returns a copy."""
    path = schema_path(name)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise ConfigError(f"schema file not found: {path.as_posix()}") from exc
    key = (str(path), mtime)
    if key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[key] = read_json(path)
    return copy.deepcopy(_SCHEMA_CACHE[key])


def format_error_path(parts: Iterable[Any]) -> str:
    """``["holdings", 0, "shares"]`` -> ``holdings[0].shares``; empty -> ``(root)``."""
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out or "(root)"


def _num(x: Any) -> float | None:
    return safe_float(x)


def _fmt(x: Any) -> str:
    """Compact number formatting for messages (50.0 -> '50', 0.25 -> '0.25')."""
    f = safe_float(x)
    return f"{f:g}" if f is not None else str(x)


def _sem_investor_profile(d: dict, err: Callable[[str, str], None], **_kw: Any) -> None:
    problem = email_problem(d.get("sec_contact_email"))
    if problem:
        err("sec_contact_email", f"{problem} - SEC EDGAR requires a real contact email in the "
            "User-Agent; run /committee-setup or edit config/investor_profile.yaml")
    mp, ms = _num(d.get("max_position_pct")), _num(d.get("max_sector_pct"))
    if mp is not None and ms is not None and mp > ms:
        err("max_position_pct", f"max_position_pct ({_fmt(mp)}) must be <= max_sector_pct ({_fmt(ms)})")
    tc = _num(d.get("target_cash_pct"))
    if tc is not None and tc >= 100:
        err("target_cash_pct", "target_cash_pct must be < 100")
    if d.get("allow_short") is True:
        err("allow_short", "short positions are not supported (allow_short must be false)")


def _sem_portfolio(d: dict, err: Callable[[str, str], None], *, require_portfolio_values: bool = True,
                   **_kw: Any) -> None:
    holdings = d.get("holdings") or []
    if not isinstance(holdings, list):
        return
    seen: dict[str, int] = {}
    for i, h in enumerate(holdings):
        if not isinstance(h, dict):
            continue
        t = h.get("ticker")
        if isinstance(t, str) and t.strip():
            nt = norm_ticker(t)
            if nt in seen:
                err(f"holdings[{i}].ticker", f"duplicate holding {nt} (also holdings[{seen[nt]}])")
            seen.setdefault(nt, i)
    if d.get("cash_usd") is not None and d.get("cash_pct") is not None:
        err("cash_pct", "set either cash_usd or cash_pct, not both")
    weight_sum = 0.0
    weight_only = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        w = _num(h.get("weight_pct"))
        if w is not None:
            weight_sum += w
            if _num(h.get("shares")) is None:
                weight_only.append(str(h.get("ticker")))
    cash_pct = _num(d.get("cash_pct")) or 0.0
    if weight_sum + cash_pct > 100.5:
        err("holdings", f"weight_pct values plus cash_pct sum to {weight_sum + cash_pct:.2f}% (> 100.5%)")
    if weight_only and _num(d.get("total_value_usd")) is None:
        err("total_value_usd", f"required when holdings are given as weight_pct ({', '.join(weight_only)}) - "
            f"{_SETUP_HINT}")
    if require_portfolio_values:
        if not holdings:
            err("holdings", f"no holdings defined - {_SETUP_HINT}")
        missing = [str(h.get("ticker")) for h in holdings if isinstance(h, dict)
                   and _num(h.get("shares")) is None and _num(h.get("weight_pct")) is None]
        if missing:
            err("holdings", f"{', '.join(missing)}: neither shares nor weight_pct is set - {_SETUP_HINT} "
                "to enter your positions")


def _sem_universe(d: dict, err: Callable[[str, str], None], **_kw: Any) -> None:
    excl = d.get("exclusions") or []
    if isinstance(excl, list):
        for i, t in enumerate(excl):
            if isinstance(t, str) and t.strip() and norm_ticker(t) != t:
                err(f"exclusions[{i}]", f"use the normalized ticker {norm_ticker(t)!r}")
    adds = d.get("additions") or {}
    if isinstance(adds, dict):
        for sector, tickers in adds.items():
            for i, t in enumerate(tickers or []):
                if isinstance(t, str) and t.strip() and norm_ticker(t) != t:
                    err(f"additions.{sector}[{i}]", f"use the normalized ticker {norm_ticker(t)!r}")


def _sem_committee(d: dict, err: Callable[[str, str], None], **_kw: Any) -> None:
    acts = d.get("actions") or {}
    hold = acts.get("holding") or {}
    cand = acts.get("candidate") or {}
    agg = d.get("aggregation") or {}
    bounds = agg.get("conviction_bounds") or [1, 10]
    lo, hi = (_num(bounds[0]), _num(bounds[1])) if len(bounds) == 2 else (1.0, 10.0)
    sell, trim, add = _num(hold.get("sell_max")), _num(hold.get("trim_below")), _num(hold.get("add_conviction"))
    if None not in (sell, trim, add) and not (sell < trim < add):
        err("actions.holding", f"thresholds must satisfy sell_max < trim_below < add_conviction "
            f"(got {_fmt(sell)} / {_fmt(trim)} / {_fmt(add)})")
    if lo is not None and hi is not None:
        if lo >= hi:
            err("aggregation.conviction_bounds", f"lower bound {_fmt(lo)} must be < upper bound {_fmt(hi)}")
        for path, v in (("actions.holding.sell_max", sell), ("actions.holding.trim_below", trim),
                        ("actions.holding.add_conviction", add),
                        ("actions.candidate.buy_conviction", _num(cand.get("buy_conviction")))):
            if v is not None and not (lo <= v <= hi):
                err(path, f"{_fmt(v)} is outside conviction bounds [{_fmt(lo)}, {_fmt(hi)}]")
    min_ev = _num(acts.get("min_evidence"))
    for path, v in (("actions.holding.add_evidence", _num(hold.get("add_evidence"))),
                    ("actions.candidate.buy_evidence", _num(cand.get("buy_evidence")))):
        if min_ev is not None and v is not None and v < min_ev:
            err(path, f"{_fmt(v)} must be >= actions.min_evidence ({_fmt(min_ev)})")
    ev = d.get("evidence") or {}
    if _num(ev.get("floor")) is not None and _num(ev.get("start")) is not None and ev["floor"] >= ev["start"]:
        err("evidence.floor", f"floor ({_fmt(ev['floor'])}) must be < start ({_fmt(ev['start'])})")
    if min_ev is not None and _num(ev.get("start")) is not None and min_ev > ev["start"]:
        err("actions.min_evidence", f"{_fmt(min_ev)} can never be reached (evidence.start is {_fmt(ev['start'])})")
    rf = ev.get("required_fields") or {}
    if isinstance(rf, dict):
        for role, classes in rf.items():
            if role not in SCORED_ROLES:
                err(f"evidence.required_fields.{role}", f"unknown role (expected one of {list(SCORED_ROLES)})")
            if isinstance(classes, dict) and "default" not in classes:
                err(f"evidence.required_fields.{role}", "a 'default' list is required")
        for role in SCORED_ROLES:
            if role not in rf:
                err("evidence.required_fields", f"missing role {role!r}")
    da = agg.get("da_adjust") or {}
    s, dm, br = _num(da.get("survives")), _num(da.get("damaged")), _num(da.get("broken"))
    if None not in (s, dm, br) and not (br <= dm <= s <= 0):
        err("aggregation.da_adjust", f"must satisfy broken <= damaged <= survives <= 0 "
            f"(got {_fmt(br)} / {_fmt(dm)} / {_fmt(s)})")
    p3 = d.get("phase3") or {}
    sl, fin, mb = _num(p3.get("shortlist_size")), _num(p3.get("finalists")), _num(p3.get("max_buys_per_sector"))
    if sl is not None and fin is not None and fin > sl:
        err("phase3.finalists", f"finalists ({_fmt(fin)}) must be <= shortlist_size ({_fmt(sl)})")
    if fin is not None and mb is not None and mb > fin:
        err("phase3.max_buys_per_sector", f"max_buys_per_sector ({_fmt(mb)}) must be <= finalists ({_fmt(fin)})")
    weights = ((d.get("screen") or {}).get("weights") or {})
    if weights:
        total = sum(_num(v) or 0.0 for v in weights.values())
        if abs(total - 1.0) > 1e-6:
            err("screen.weights", f"weights must sum to 1.0 (got {total:.4f})")
    val = d.get("valuation") or {}
    tiers = [_num(x) for x in val.get("growth_tiers") or []]
    if tiers and (None in tiers or tiers != sorted(tiers)):
        err("valuation.growth_tiers", "must be numbers in ascending order")
    emg = val.get("exit_multiple_by_growth") or []
    gs = [_num(p[0]) for p in emg if isinstance(p, list) and len(p) == 2]
    if gs and (None in gs or gs != sorted(gs) or len(set(gs)) != len(gs)):
        err("valuation.exit_multiple_by_growth", "growth thresholds must be strictly ascending")
    bands = val.get("multiple_bands") or {}
    if isinstance(bands, dict):
        for sector in SECTOR_KEYS:
            if sector not in bands:
                err("valuation.multiple_bands", f"missing sector {sector!r}")
                continue
            b = bands[sector] or {}
            if "pe" not in b:
                err(f"valuation.multiple_bands.{sector}", "a 'pe' band is required")
            second = "p_b" if sector == "financials" else "ev_ebit"
            if second not in b:
                err(f"valuation.multiple_bands.{sector}", f"a {second!r} band is required")
            for m, trio in b.items():
                vals = [_num(x) for x in trio] if isinstance(trio, list) else []
                if len(vals) == 3 and None not in vals and not (0 < vals[0] <= vals[1] <= vals[2]):
                    err(f"valuation.multiple_bands.{sector}.{m}", f"must satisfy 0 < low <= mid <= high (got {trio})")
    ft, vt = _num((d.get("fundamentals") or {}).get("default_tax_rate")), _num(val.get("default_tax_rate"))
    if ft is not None and vt is not None and abs(ft - vt) > 1e-12:
        err("valuation.default_tax_rate", f"must equal fundamentals.default_tax_rate ({_fmt(ft)})")
    reg = ((d.get("technicals") or {}).get("regime") or {})
    at, ar = _num(reg.get("adx_trend_min")), _num(reg.get("adx_range_max"))
    if at is not None and ar is not None and ar > at:
        err("technicals.regime", f"adx_range_max ({_fmt(ar)}) must be <= adx_trend_min ({_fmt(at)})")
    series = ((d.get("macro") or {}).get("series") or [])
    ids = [s.get("id") for s in series if isinstance(s, dict)]
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if dupes:
        err("macro.series", f"duplicate series ids: {', '.join(map(str, dupes))}")
    hols = d.get("market_holidays") or []
    if len(hols) != len(set(map(str, hols))):
        err("market_holidays", "duplicate dates")
    for i, h in enumerate(hols):
        try:
            hd = parse_date(h)
        except ValueError:
            continue
        if hd is not None and hd.weekday() >= 5:
            err(f"market_holidays[{i}]", f"{h} is a weekend day")
    etfs = d.get("sector_etfs") or {}
    if isinstance(etfs, dict):
        for k in SECTOR_KEYS:
            if k in etfs and etfs[k] != SECTORS[k]["etf"]:
                err(f"sector_etfs.{k}", f"expected {SECTORS[k]['etf']} (benchmark SPDR), got {etfs[k]!r}")


def _sem_etf_overrides(d: dict, err: Callable[[str, str], None], **_kw: Any) -> None:
    for t, entry in (d.get("securities") or {}).items():
        if isinstance(t, str) and norm_ticker(t) != t:
            err(f"securities.{t}", f"use the normalized ticker {norm_ticker(t)!r}")
        if not isinstance(entry, dict):
            continue
        sc = entry.get("share_class") or {}
        if isinstance(sc, dict) and sc.get("class_factor") is not None and not entry.get("source_url"):
            err(f"securities.{t}.source_url", "required when share_class.class_factor is set "
                "(configs carrying numbers must declare a source_url)")
    for t, entry in (d.get("funds") or {}).items():
        if isinstance(t, str) and norm_ticker(t) != t:
            err(f"funds.{t}", f"use the normalized ticker {norm_ticker(t)!r}")
        if not isinstance(entry, dict):
            continue
        sw = entry.get("sector_weights") or {}
        if isinstance(sw, dict) and sw:
            total = sum(_num(v) or 0.0 for v in sw.values())
            if abs(total - 100.0) > 1.0:
                err(f"funds.{t}.sector_weights", f"must sum to 100 ±1 (got {total:.2f})")


_SEMANTIC_CHECKS: dict[str, Callable[..., None]] = {
    "investor_profile": _sem_investor_profile,
    "portfolio": _sem_portfolio,
    "universe": _sem_universe,
    "committee": _sem_committee,
    "etf_overrides": _sem_etf_overrides,
}


def validate_config(name: str, data: Any, *, require_portfolio_values: bool = True) -> list[str]:
    """Validate one config document against ``schemas/config_<name>.schema.json`` plus
    semantic checks. Returns human-readable ``"<name>.yaml: <path>: <message>"`` strings
    (empty list = valid). ``require_portfolio_values=False`` accepts null shares/weights (the
    prefilled template)."""
    from jsonschema import Draft202012Validator, FormatChecker

    if name not in CONFIG_NAMES:
        raise ValueError(f"unknown config {name!r}; expected one of {CONFIG_NAMES}")
    fname = f"{name}.yaml"
    doc = to_jsonable(data)
    errors: list[str] = []
    validator = Draft202012Validator(load_schema(f"config_{name}"), format_checker=FormatChecker())
    schema_errs = sorted(validator.iter_errors(doc),
                         key=lambda e: (format_error_path(e.absolute_path), e.message))
    for e in schema_errs:
        msg = e.message if len(e.message) <= 300 else e.message[:297] + "..."
        errors.append(f"{fname}: {format_error_path(e.absolute_path)}: {msg}")

    def err(path: str, message: str) -> None:
        line = f"{fname}: {path}: {message}"
        if line not in errors:
            errors.append(line)

    if isinstance(doc, dict):
        try:
            _SEMANTIC_CHECKS[name](doc, err, require_portfolio_values=require_portfolio_values)
        except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
            if not schema_errs:
                err("(root)", f"semantic check failed: {type(exc).__name__}: {exc}")
    return errors


def validate_configs(config_dir: str | Path | None = None, *,
                     require_portfolio_values: bool = True) -> dict[str, list[str]]:
    """Validate all five configs in ``config_dir`` (default: active config dir). Returns
    ``{name: [errors]}`` for every name in ``CONFIG_NAMES`` (empty list = valid)."""
    d = _resolve_config_dir(config_dir)
    out: dict[str, list[str]] = {}
    for name in CONFIG_NAMES:
        path = d / f"{name}.yaml"
        if not path.is_file():
            out[name] = [f"{name}.yaml: (file): not found in {d.as_posix()}"]
            continue
        try:
            data = load_yaml(path)
        except ConfigError as exc:
            out[name] = [f"{name}.yaml: (file): {exc}"]
            continue
        out[name] = validate_config(name, data, require_portfolio_values=require_portfolio_values)
    return out


def config_errors(result: Mapping[str, list[str]]) -> list[str]:
    """Flatten a ``validate_configs`` result into one list (CONFIG_NAMES order)."""
    return [e for name in CONFIG_NAMES for e in result.get(name, [])]


# =====================================================================================
# Disk cache
# =====================================================================================


def _parse_ts(ts: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_ENTRY_ALIASES = ("payload", "fetched_at", "source_url")


class CachedDict(dict):
    """A cached dict payload; metadata on attributes ``fetched_at``, ``source_url``,
    ``age_hours`` and ``stale`` (True when returned by ``get_stale``).

    Compatibility alias: when the payload itself has no such key, ``obj["payload"]`` returns
    the payload (as a plain dict) and ``obj["fetched_at"]`` / ``obj["source_url"]`` (also via
    ``.get``) return the entry metadata — so code written against the raw cache-entry shape
    keeps working. Iteration, ``len``, ``in`` and JSON serialization see only the payload."""

    fetched_at: str | None = None
    source_url: str | None = None
    age_hours: float | None = None
    stale: bool = False

    def __missing__(self, key: Any) -> Any:
        if key == "payload":
            return dict(self)
        if key in ("fetched_at", "source_url"):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        if key in self or key not in _ENTRY_ALIASES:
            return super().get(key, default)
        value = self[key]
        return default if value is None else value


class CachedList(list):
    """A cached list payload (same metadata attributes as ``CachedDict``)."""

    fetched_at: str | None = None
    source_url: str | None = None
    age_hours: float | None = None
    stale: bool = False


def _is_error_payload(payload: Any) -> bool:
    if payload is None or isinstance(payload, BaseException):
        return True
    if isinstance(payload, Mapping) and payload.get("error"):
        return True
    return False


class DiskCache:
    """File cache under ``root`` (default ``cache_dir()`` at construction time).

    Layout: ``<root>/<kind>/<safe_name(key)>.json`` = ``{"fetched_at","source_url","payload"}``;
    frames add ``<root>/<kind>/<safe_name(key)>.csv`` (Date index) and the JSON entry carries
    ``"frame"``/``"rows"``/``"columns"`` with ``payload`` = the caller's metadata dict.
    Errors are never cached: ``put`` ignores None / exceptions / dicts with a truthy
    ``"error"`` key and ``put_frame`` ignores None / empty frames (both return None).
    TTL is measured against wall-clock UTC time (not ``AGINV_AS_OF``).
    Fixture mode never touches the repo's real cache: any disk access under ``<repo>/data/cache``
    while ``AGINV_FIXTURES_DIR`` is set and ``AGINV_CACHE_DIR`` is not raises ConfigError."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else cache_dir()

    # -- paths ---------------------------------------------------------------------
    def _guard_fixture_mode(self) -> None:
        """Fixture mode (``AGINV_FIXTURES_DIR``) must never read or write the repo's real cache
        (``<repo>/data/cache``): fixture data would pollute it and real entries would shadow the
        fixtures. Allowed once ``AGINV_CACHE_DIR`` is set explicitly (tests, the dry run)."""
        if active_fixtures_dir() is None or os.environ.get(ENV_CACHE_DIR, "").strip():
            return
        if _is_within(self.root, default_cache_dir()):
            raise ConfigError(
                f"fixture mode with the real cache dir {default_cache_dir().as_posix()} ({ENV_FIXTURES_DIR} is set): "
                f"set {ENV_CACHE_DIR} (or --cache-dir) to a scratch directory")

    def path_for(self, kind: str, key: str, ext: str = "json") -> Path:
        """Entry path; every disk access goes through here, so it carries the fixture guard."""
        self._guard_fixture_mode()
        return self.root / safe_name(kind) / f"{safe_name(key)}.{ext}"

    # -- entries -------------------------------------------------------------------
    def _read_entry(self, kind: str, key: str) -> dict | None:
        p = self.path_for(kind, key)
        if not p.is_file():
            return None
        try:
            entry = read_json(p)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "fetched_at" not in entry or "payload" not in entry:
            return None
        return entry

    @staticmethod
    def _age_hours(entry: Mapping[str, Any]) -> float | None:
        ts = _parse_ts(entry.get("fetched_at"))
        if ts is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0)

    def get_entry(self, kind: str, key: str, ttl_hours: float | None = None) -> dict | None:
        """Full entry ``{"fetched_at","source_url","payload",...,"age_hours","stale"}`` or None.
        ``ttl_hours=None`` ignores TTL (``stale`` is then always False)."""
        entry = self._read_entry(kind, key)
        if entry is None:
            return None
        age = self._age_hours(entry)
        if age is None:
            return None
        fresh = ttl_hours is None or age <= float(ttl_hours)
        if not fresh:
            return None
        entry["age_hours"] = round(age, 4)
        entry["stale"] = False
        return entry

    @staticmethod
    def _wrap(entry: dict, stale: bool) -> Any:
        payload = entry.get("payload")
        if isinstance(payload, dict):
            out: Any = CachedDict(payload)
        elif isinstance(payload, list):
            out = CachedList(payload)
        else:
            return payload
        out.fetched_at = entry.get("fetched_at")
        out.source_url = entry.get("source_url")
        out.age_hours = entry.get("age_hours")
        out.stale = stale
        return out

    def get(self, kind: str, key: str, ttl_hours: float | None) -> Any:
        """Payload stored by ``put`` if younger than ``ttl_hours`` (None = no TTL), else None.
        dict/list payloads come back as ``CachedDict``/``CachedList`` (metadata attributes)."""
        entry = self.get_entry(kind, key, ttl_hours)
        return None if entry is None else self._wrap(entry, False)

    def get_stale(self, kind: str, key: str) -> Any:
        """Payload regardless of age (for a flagged stale fallback; the caller flags it).
        dict/list payloads carry ``.stale = True`` and ``.age_hours``. None if absent."""
        entry = self._read_entry(kind, key)
        if entry is None:
            return None
        age = self._age_hours(entry)
        entry["age_hours"] = None if age is None else round(age, 4)
        return self._wrap(entry, True)

    def put(self, kind: str, key: str, payload: Any, source_url: str | None) -> Path | None:
        """Store ``payload`` (JSON-able) with ``fetched_at=now`` and ``source_url``. Returns the
        entry path, or None when the payload looks like an error (never cached)."""
        if _is_error_payload(payload):
            return None
        entry = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_url": source_url, "payload": payload}
        return write_json(self.path_for(kind, key), entry)

    def delete(self, kind: str, key: str) -> None:
        for ext in ("json", "csv"):
            p = self.path_for(kind, key, ext)
            if p.exists():
                p.unlink()

    # -- DataFrames ----------------------------------------------------------------
    def put_frame(self, kind: str, key: str, df: pd.DataFrame | None, source_url: str | None,
                  meta: Mapping[str, Any] | None = None) -> Path | None:
        """Store ``df`` as CSV (index written as ``Date``; midnight timestamps as YYYY-MM-DD)
        plus the JSON entry (``payload`` = ``meta`` or {}). Returns the CSV path, or None for
        None/empty frames (never cached)."""
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        frame = df.copy()
        date_fmt = None
        if isinstance(frame.index, pd.DatetimeIndex):
            idx = frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
            frame.index = idx
            if (idx == idx.normalize()).all():
                date_fmt = "%Y-%m-%d"
        csv_path = self.path_for(kind, key, "csv")
        text = frame.to_csv(index_label="Date", lineterminator="\n", date_format=date_fmt)
        write_text(csv_path, text)
        entry = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_url": source_url, "payload": dict(meta or {}),
                 "frame": csv_path.name, "rows": int(len(frame)),
                 "columns": [str(c) for c in frame.columns]}
        write_json(self.path_for(kind, key), entry)
        return csv_path

    def _load_frame(self, kind: str, key: str, entry: dict, stale: bool) -> pd.DataFrame | None:
        csv_path = self.path_for(kind, key, "csv")
        if not csv_path.is_file():
            return None
        try:
            df = pd.read_csv(csv_path, encoding="utf-8", index_col=0)
        except (OSError, ValueError, pd.errors.ParserError):
            return None
        try:
            df.index = pd.to_datetime(df.index)
        except (ValueError, TypeError):
            pass
        df.index.name = "Date"
        df.attrs.update({"fetched_at": entry.get("fetched_at"), "source_url": entry.get("source_url"),
                         "meta": entry.get("payload") or {}, "age_hours": entry.get("age_hours"),
                         "stale": stale})
        return df

    def get_frame(self, kind: str, key: str, ttl_hours: float | None = None) -> pd.DataFrame | None:
        """Frame stored by ``put_frame`` if younger than ``ttl_hours`` (None = no TTL), else
        None. Metadata in ``df.attrs`` (fetched_at, source_url, meta, age_hours, stale)."""
        entry = self.get_entry(kind, key, ttl_hours)
        if entry is None:
            return None
        return self._load_frame(kind, key, entry, False)

    def get_frame_stale(self, kind: str, key: str) -> pd.DataFrame | None:
        """Frame regardless of age (``df.attrs['stale']`` True); None if absent."""
        entry = self._read_entry(kind, key)
        if entry is None:
            return None
        age = self._age_hours(entry)
        entry["age_hours"] = None if age is None else round(age, 4)
        return self._load_frame(kind, key, entry, True)


# =====================================================================================
# Network seam — the ONLY place requests / yfinance are used
# =====================================================================================

HTTP_ATTEMPTS = 4
HTTP_BACKOFF_BASE_S = 1.0
HTTP_MAX_SLEEP_S = 30.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SEC_MIN_INTERVAL_S = 0.11
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; agent-investors/1.0; research)"

_SEC_LOCK = threading.Lock()
_SEC_STATE = {"last": 0.0}
_FIXTURE_INDEX_CACHE: dict[tuple[str, int], dict] = {}


def clear_caches() -> None:
    """Reset in-process caches (committee cfg, schemas, fixture index, SEC limiter clock)."""
    with _CFG_LOCK:
        _CFG_CACHE.clear()
    _SCHEMA_CACHE.clear()
    _FIXTURE_INDEX_CACHE.clear()
    with _SEC_LOCK:
        _SEC_STATE["last"] = 0.0


class FixtureResponse:
    """Minimal ``requests.Response`` stand-in served in fixture mode."""

    def __init__(self, url: str, content: bytes, status_code: int = 200,
                 headers: Mapping[str, str] | None = None):
        self.url = url
        self.content = content
        self.status_code = int(status_code)
        self.headers = dict(headers or {})
        self.encoding = "utf-8"

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        t = self.content.decode("utf-8", errors="replace")
        return t[1:] if t.startswith("﻿") else t

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise DataUnavailable(f"HTTP {self.status_code} for {self.url}", status_code=self.status_code,
                                  url=self.url)


_CONTENT_TYPES = {".json": "application/json", ".csv": "text/csv", ".txt": "text/plain",
                  ".html": "text/html", ".htm": "text/html", ".xml": "application/xml"}


def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode(params, doseq=True)}"


def _fixture_index(fx: Path) -> dict:
    idx_path = fx / "http" / "index.json"
    try:
        mtime = idx_path.stat().st_mtime_ns
    except OSError as exc:
        raise FixtureMissing(f"fixture index not found: {idx_path.as_posix()}") from exc
    key = (str(idx_path), mtime)
    if key not in _FIXTURE_INDEX_CACHE:
        data = read_json(idx_path)
        if not isinstance(data, dict):
            raise FixtureMissing(f"fixture index must be a JSON object: {idx_path.as_posix()}")
        _FIXTURE_INDEX_CACHE[key] = data
    return _FIXTURE_INDEX_CACHE[key]


def _fixture_response(fx: Path, url: str, raise_for_status: bool) -> FixtureResponse:
    index = _fixture_index(fx)
    entry = index.get(url)
    if entry is None and url.endswith("/"):
        entry = index.get(url.rstrip("/"))
    if entry is None:
        raise FixtureMissing(f"no HTTP fixture for {url} in {(fx / 'http' / 'index.json').as_posix()}", url=url)
    status, headers, rel = 200, {}, None
    if isinstance(entry, str):
        rel = entry
    elif isinstance(entry, dict):
        rel = entry.get("file")
        status = int(entry.get("status", 200))
        headers = dict(entry.get("headers") or {})
    else:
        raise FixtureMissing(f"bad fixture index entry for {url}: {entry!r}", url=url)
    content = b""
    if rel:
        candidates = [fx / "http" / rel, fx / rel]
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            raise FixtureMissing(f"fixture file {rel!r} for {url} not found under {fx.as_posix()}", url=url)
        content = path.read_bytes()
        headers.setdefault("Content-Type", _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
    resp = FixtureResponse(url, content, status, headers)
    if raise_for_status and status >= 400:
        cls = ProviderBlocked if status == 403 else DataUnavailable
        raise cls(f"HTTP {status} for {url} (fixture)", status_code=status, url=url)
    return resp


def _parse_retry_after(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    f = safe_float(s)
    if f is not None:
        return max(0.0, f)
    try:
        when = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff_delay(attempt: int, retry_after: Any = None) -> float:
    delay = HTTP_BACKOFF_BASE_S * (2 ** attempt)
    ra = _parse_retry_after(retry_after)
    if ra is not None:
        delay = ra
    return max(0.0, min(HTTP_MAX_SLEEP_S, delay))


def sec_user_agent(config_dir: str | Path | None = None) -> str:
    """``"agent-investors research <sec_contact_email>"`` from investor_profile.yaml in the
    active config dir. ConfigError if the email is missing, malformed or a placeholder."""
    cfg_path = _resolve_config_dir(config_dir) / "investor_profile.yaml"
    try:
        profile = load_config("investor_profile", cfg_path.parent)
    except ConfigError as exc:
        raise ConfigError(f"SEC EDGAR needs sec_contact_email from investor_profile.yaml: {exc} - "
                          "run /committee-setup") from exc
    email = profile.get("sec_contact_email")
    problem = email_problem(email)
    if problem:
        raise ConfigError(f"{cfg_path.as_posix()}: sec_contact_email {problem}. SEC EDGAR requires a real "
                          "contact email in the User-Agent - run /committee-setup or edit the file.")
    return f"agent-investors research {str(email).strip()}"


def _sec_throttle() -> None:
    """Process-wide limiter: at least ``SEC_MIN_INTERVAL_S`` between SEC requests."""
    with _SEC_LOCK:
        wait = _SEC_STATE["last"] + SEC_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _SEC_STATE["last"] = time.monotonic()


def http_get(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 30,
             sec: bool = False, params: Mapping[str, Any] | None = None,
             attempts: int = HTTP_ATTEMPTS, raise_for_status: bool = True) -> Any:
    """GET ``url`` (+ ``params``) and return a ``requests.Response`` (or ``FixtureResponse``).

    Order: fixture mode (``AGINV_FIXTURES_DIR``) -> offline (``AGINV_OFFLINE=1`` raises
    OfflineError) -> network. ``sec=True`` sets the SEC User-Agent (``sec_user_agent()``) and
    applies the process-wide >=0.11 s limiter. Retries (``attempts`` total) on 429/5xx and
    connection errors/timeouts/truncated or undecodable bodies (ChunkedEncodingError,
    ContentDecodingError - the latter is retried asking for ``Accept-Encoding: identity``) with
    exponential backoff 1,2,4 s honouring ``Retry-After`` (every sleep capped at 30 s). Any other
    ``requests`` error (InvalidURL, MissingSchema, TooManyRedirects, ...) is not retried and raised
    as DataUnavailable carrying ``.url``; no ``requests`` exception escapes. Final status >= 400
    raises DataUnavailable (403 -> ProviderBlocked) carrying ``.status_code`` unless
    ``raise_for_status=False``."""
    full_url = _with_params(url, params)
    fx = active_fixtures_dir()
    if fx is not None:
        return _fixture_response(fx, full_url, raise_for_status)
    if is_offline():
        raise OfflineError(f"offline mode ({ENV_OFFLINE}=1): network call refused: {full_url}", url=full_url)
    hdrs = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if sec:
        hdrs["User-Agent"] = sec_user_agent()
    if headers:
        hdrs.update(headers)
    attempts = max(1, int(attempts))
    retryable_exc = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                     requests.exceptions.ContentDecodingError)
    for i in range(attempts):
        if sec:
            _sec_throttle()
        try:
            resp = requests.get(full_url, headers=hdrs, timeout=timeout)
        except retryable_exc as exc:
            if i == attempts - 1:
                raise DataUnavailable(f"network error for {full_url}: {type(exc).__name__}: {exc}",
                                      url=full_url) from exc
            if isinstance(exc, requests.exceptions.ContentDecodingError) and not (
                    headers and any(str(k).lower() == "accept-encoding" for k in headers)):
                hdrs["Accept-Encoding"] = "identity"  # a body that will not decode: ask for it uncompressed
            time.sleep(_backoff_delay(i))
            continue
        except requests.exceptions.RequestException as exc:
            # InvalidURL, MissingSchema, TooManyRedirects, ... : retrying cannot help
            raise DataUnavailable(f"request failed for {full_url}: {type(exc).__name__}: {exc}",
                                  url=full_url) from exc
        status = int(resp.status_code)
        if status in RETRY_STATUSES and i < attempts - 1:
            time.sleep(_backoff_delay(i, resp.headers.get("Retry-After")))
            continue
        if raise_for_status and status >= 400:
            cls = ProviderBlocked if status == 403 else DataUnavailable
            raise cls(f"HTTP {status} for {full_url}", status_code=status, url=full_url)
        return resp
    raise DataUnavailable(f"giving up on {full_url}", url=full_url)  # pragma: no cover


def response_text(resp: Any) -> str:
    """Body of a Response-like as text: UTF-8 (BOM stripped) when it decodes, else ``.text``."""
    content = getattr(resp, "content", None)
    if isinstance(content, (bytes, bytearray)):
        try:
            t = bytes(content).decode("utf-8")
        except UnicodeDecodeError:
            t = resp.text
    else:
        t = resp.text
    return t[1:] if t.startswith("﻿") else t


def http_get_text(url: str, **kwargs: Any) -> str:
    """``http_get(url, **kwargs)`` body as text (see ``response_text``)."""
    return response_text(http_get(url, **kwargs))


def http_get_json(url: str, **kwargs: Any) -> Any:
    """``http_get(url, **kwargs)`` body parsed as JSON; invalid JSON -> DataUnavailable."""
    text = http_get_text(url, **kwargs)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DataUnavailable(f"invalid JSON from {url}: {exc}", url=url) from exc


# ---- yfinance ------------------------------------------------------------------------


def _import_yfinance() -> Any:
    try:
        import yfinance as yf  # noqa: PLC0415 - lazy by design (network seam)
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise DataUnavailable("yfinance is not installed (pip install -r requirements.txt)") from exc
    return yf


def _fixture_file(base: Path, ticker: str, ext: str) -> Path:
    names = [safe_name(ticker)]
    try:
        names.append(safe_name(norm_ticker(ticker)))
    except ValueError:
        pass
    for n in names:
        p = base / f"{n}{ext}"
        if p.is_file():
            return p
    raise FixtureMissing(f"fixture not found: {(base / (names[0] + ext)).as_posix()}")


def _offline_guard(what: str, ticker: str) -> None:
    if is_offline():
        raise OfflineError(f"offline mode ({ENV_OFFLINE}=1): yfinance {what} refused for {ticker}")


def clean_ohlcv(df: pd.DataFrame | None, *, ticker: str = "") -> pd.DataFrame:
    """Normalize daily bars to ``Date``-indexed float columns Open, High, Low, Close, Volume.

    Accepts a DatetimeIndex (tz-aware -> wall time via ``tz_localize(None)``) or a
    Date/Datetime column (first 10 chars parsed as YYYY-MM-DD); timestamps normalized to
    midnight; rows with non-finite Close dropped; missing Open/High/Low filled with Close,
    missing Volume with 0; sorted ascending; duplicate dates keep the last row.
    Raises DataUnavailable when empty or Close is absent."""
    label = f" for {ticker}" if ticker else ""
    if df is None or len(df) == 0:
        raise DataUnavailable(f"no price history{label}")
    d = df.copy()
    lower = {str(c).strip().lower(): c for c in d.columns}
    if not isinstance(d.index, pd.DatetimeIndex):
        date_col = next((lower[k] for k in ("date", "datetime", "timestamp") if k in lower), None)
        if date_col is not None:
            d = d.set_index(date_col)
            lower = {str(c).strip().lower(): c for c in d.columns}
        raw_idx = pd.Index(d.index).astype(str).str.slice(0, 10)
        d.index = pd.to_datetime(raw_idx, format="%Y-%m-%d", errors="coerce")
    elif d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    d = d[~pd.isna(d.index)]
    d.index = pd.DatetimeIndex(d.index).normalize()
    if "close" not in lower:
        raise DataUnavailable(f"price history{label} has no Close column")
    out = pd.DataFrame(index=d.index)
    for col in OHLCV_COLUMNS:
        src = lower.get(col.lower())
        out[col] = pd.to_numeric(d[src], errors="coerce") if src is not None else np.nan
    out = out.astype("float64")
    out = out[np.isfinite(out["Close"].to_numpy())]
    for col in ("Open", "High", "Low"):
        out[col] = out[col].where(np.isfinite(out[col]), out["Close"])
    out["Volume"] = out["Volume"].where(np.isfinite(out["Volume"]), 0.0)
    out = out.sort_index(kind="mergesort")  # stable: duplicate dates keep their original order
    out = out[~out.index.duplicated(keep="last")]
    out.index.name = "Date"
    if out.empty:
        raise DataUnavailable(f"no valid price rows{label}")
    return out


def yf_history(ticker: str, period: str = "3y", *, interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV (``auto_adjust=True``) via yfinance, normalized by ``clean_ohlcv``.

    Fixture mode reads ``<fixtures>/yf/history/<T>.csv`` (returned whole — ``period`` is not
    applied). Offline -> OfflineError. Empty/failed -> DataUnavailable."""
    fx = active_fixtures_dir()
    if fx is not None:
        path = _fixture_file(fx / "yf" / "history", ticker, ".csv")
        return clean_ohlcv(pd.read_csv(path, encoding="utf-8"), ticker=ticker)
    _offline_guard("history", ticker)
    yf = _import_yfinance()
    try:
        raw = retry(lambda: yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True),
                    attempts=3, base_delay=1.0)
    except AginvError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider boundary
        raise DataUnavailable(f"yfinance history failed for {ticker}: {type(exc).__name__}: {exc}") from exc
    if raw is None or getattr(raw, "empty", True):
        raise DataUnavailable(f"yfinance returned no price history for {ticker}")
    return clean_ohlcv(raw, ticker=ticker)


def yf_info(ticker: str) -> dict:
    """``yfinance.Ticker(t).info`` as a plain dict. Fixture: ``<fixtures>/yf/info/<T>.json``.
    Offline -> OfflineError. Empty -> DataUnavailable."""
    fx = active_fixtures_dir()
    if fx is not None:
        data = read_json(_fixture_file(fx / "yf" / "info", ticker, ".json"))
        if not isinstance(data, dict):
            raise DataUnavailable(f"info fixture for {ticker} is not an object")
        return data
    _offline_guard("info", ticker)
    yf = _import_yfinance()
    try:
        info = retry(lambda: dict(yf.Ticker(ticker).info or {}), attempts=3, base_delay=1.0)
    except AginvError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider boundary
        raise DataUnavailable(f"yfinance info failed for {ticker}: {type(exc).__name__}: {exc}") from exc
    if not info or all(v is None for v in info.values()):
        raise DataUnavailable(f"yfinance returned no info for {ticker}")
    return info


def yf_quote(ticker: str) -> dict:
    """Latest quote ``{"price","previous_close","currency","retrieved_at"}`` via yfinance ``fast_info``
    (intraday during market hours, last close otherwise). Fixture: ``<fixtures>/yf/quote/<T>.json`` with
    the same keys. Offline -> OfflineError; no price -> DataUnavailable. ``currency`` is Yahoo's raw code
    (``ILA`` = agorot); no unit conversion is done here."""
    fx = active_fixtures_dir()
    if fx is not None:
        data = read_json(_fixture_file(fx / "yf" / "quote", ticker, ".json"))
        if not isinstance(data, dict):
            raise DataUnavailable(f"quote fixture for {ticker} is not an object")
        return data
    _offline_guard("quote", ticker)
    yf = _import_yfinance()
    try:
        fi = retry(lambda: yf.Ticker(ticker).fast_info, attempts=2, base_delay=0.5)
        price = safe_float(fi["last_price"])
        prev = safe_float(fi["previous_close"])
        cur = fi["currency"]
    except AginvError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider boundary
        raise DataUnavailable(f"yfinance quote failed for {ticker}: {type(exc).__name__}: {exc}") from exc
    if price is None or price <= 0:
        raise DataUnavailable(f"yfinance returned no price for {ticker}")
    return {"price": price, "previous_close": prev, "currency": cur if isinstance(cur, str) else None,
            "retrieved_at": now_iso()}


def _normalize_funds(d: Mapping[str, Any]) -> dict:
    sw: dict[str, float] = {}
    for k, v in (d.get("sector_weightings") or {}).items():
        f = safe_float(v)
        if f is not None:
            sw[str(k)] = f
    holdings: list[dict] = []
    for h in d.get("top_holdings") or []:
        if not isinstance(h, Mapping):
            continue
        holdings.append({"symbol": str(h.get("symbol") or "").strip(),
                         "name": str(h.get("name") or "").strip(),
                         "holding_pct": safe_float(h.get("holding_pct"))})
    return {"sector_weightings": sw, "top_holdings": holdings,
            "expense_ratio_pct": safe_float(d.get("expense_ratio_pct")),
            "expense_ratio_field": str(d.get("expense_ratio_field") or "unavailable")}


def yf_funds(ticker: str) -> dict:
    """Fund composition, normalized::

        {"sector_weightings": {yahoo_key: fraction},      # e.g. "technology": 0.52
         "top_holdings": [{"symbol","name","holding_pct"}],  # holding_pct in PERCENT
         "expense_ratio_pct": float|None,                 # percent (QQQ -> 0.18)
         "expense_ratio_field": "info.netExpenseRatio" |
             "funds_data.fund_operations.Annual Report Expense Ratio" | "unavailable"}

    ``info['netExpenseRatio']`` is already percent; the fund_operations row is a fraction
    (x100). Fixture: ``<fixtures>/yf/funds/<T>.json`` (already this shape). Offline ->
    OfflineError. No sector weights and no holdings -> DataUnavailable."""
    fx = active_fixtures_dir()
    if fx is not None:
        data = read_json(_fixture_file(fx / "yf" / "funds", ticker, ".json"))
        if not isinstance(data, Mapping):
            raise DataUnavailable(f"funds fixture for {ticker} is not an object")
        return _normalize_funds(data)
    _offline_guard("funds", ticker)
    yf = _import_yfinance()
    try:
        tk = yf.Ticker(ticker)
        fd = retry(lambda: tk.funds_data, attempts=3, base_delay=1.0)
        sector_raw = dict(fd.sector_weightings or {})
        top = fd.top_holdings
    except AginvError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider boundary
        raise DataUnavailable(f"yfinance funds_data failed for {ticker}: {type(exc).__name__}: {exc}") from exc
    holdings: list[dict] = []
    if isinstance(top, pd.DataFrame) and not top.empty:
        for sym, row in top.iterrows():
            frac = safe_float(row.get("Holding Percent"))
            holdings.append({"symbol": str(sym).strip(), "name": str(row.get("Name") or "").strip(),
                             "holding_pct": None if frac is None else round(frac * 100.0, 6)})
    expense, field = None, "unavailable"
    try:
        info = dict(tk.info or {})
    except Exception:  # noqa: BLE001 - expense ratio is optional
        info = {}
    ner = safe_float(info.get("netExpenseRatio"))
    if ner is not None:
        expense, field = ner, "info.netExpenseRatio"
    else:
        try:
            ops = fd.fund_operations
        except Exception:  # noqa: BLE001
            ops = None
        if isinstance(ops, pd.DataFrame) and not ops.empty and "Annual Report Expense Ratio" in ops.index:
            cols = [c for c in ops.columns if str(c) != "Category Average"]
            col = ticker if ticker in ops.columns else (cols[0] if cols else None)
            frac = safe_float(ops.loc["Annual Report Expense Ratio", col]) if col is not None else None
            if frac is not None:
                expense = round(frac * 100.0, 6)
                field = "funds_data.fund_operations.Annual Report Expense Ratio"
    result = _normalize_funds({"sector_weightings": sector_raw, "top_holdings": holdings,
                               "expense_ratio_pct": expense, "expense_ratio_field": field})
    if not result["sector_weightings"] and not result["top_holdings"]:
        raise DataUnavailable(f"yfinance has no fund composition for {ticker}")
    return result


__all__ = [n for n in dir() if not n.startswith("_") and n not in (
    "annotations", "copy", "hashlib", "json", "math", "os", "re", "sys", "threading", "time", "uuid",
    "np", "pd", "requests", "yaml", "T", "Any", "Callable", "Iterable", "Mapping", "TypeVar",
    "OrderedDict", "fields", "is_dataclass", "date", "datetime", "timedelta", "timezone", "Decimal",
    "parsedate_to_datetime", "Enum", "Path", "PurePath", "quote", "urlencode")]
