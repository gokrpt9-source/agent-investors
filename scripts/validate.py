"""validate.py — JSON-Schema + semantic validation of agent outputs (DESIGN §4.13).

Every agent writes exactly one JSON file; ``committee.py`` validates it with
``validate_order_output(order, path)`` and, on failure, retries the agent once with the error
messages as ``prior_errors``. Messages are therefore written for the agent:
``"<json path>: <problem>; expected <x>, got <y>"``.

Layers
------
1. JSON Schema (draft 2020-12) through a ``referencing.Registry`` built from every
   ``schemas/*.json`` and ``schemas/data/*.json`` (``$id: urn:agent-investors:[data/]<name>``),
   ``Draft202012Validator`` + ``FormatChecker``, extended with the schema annotations
   ``x-max-words`` / ``x-min-words`` (word limits; ``x-min-words`` applies to non-nullable string
   schemas only — nullable fields get their minimum from the conditional ``then`` branch).
2. Semantic checks (agent-output schemas only; thresholds from ``committee.yaml: validation`` and
   ``phase3``): configured word limits, quoted-span copyright guard, source-id resolution and
   uniqueness, ``data_ref`` value match and Fact provenance match (a key figure must carry the
   ``as_of`` / ``kind`` / ``unit`` / ``filed`` of the Fact it points at, because the evidence score
   reads the agent-supplied fields), null key figure ⇒ listed in ``missing_data``,
   fundamental cell means / ordering / method families, technical regime / levels /
   invalidation, DA ``bear_case_ref``, macro probabilities and bounds, weight-map sums, CIO
   macro-bound overrides / expand cap / look-through match, sector verdict vs aggregate and BUY
   cap, CIO final actions vs sector verdicts, work-order identity and seed sources.

Context (``context=`` dict; values are file paths or already-loaded dicts):
``technicals, fundamentals, valuation, aggregate, macro, macro_view, lookthrough,
cio_allocation, prices`` (one each), ``verdicts`` (list), ``committee_cfg`` (dict or YAML path;
default ``common.committee_cfg()``), ``run_dir`` (current run; data_refs that name another
run's ``phaseN/...`` file are re-anchored onto it), ``strict_refs`` (missing data_ref files are
errors, not notes), ``expect`` (identity fields the document must carry: order_id, run_id,
agent_role, ticker, sector, mode), ``word_limits`` (work-order overrides), ``seed_sources``,
``sole_context`` (kinds among technicals / fundamentals / valuation for which the work order lists
exactly ONE file; derived from ``<run_dir>/orders/<order_id>.md`` when absent). A context file whose
ticker differs from the order's is skipped with a note, EXCEPT when it is the order's only file of
its kind: a proxy-priced fund (order ticker 1186063, data under the proxy QQQ) is then validated
against that file.
A missing context file never crashes: the dependent checks are skipped and a note is recorded
(``Report.notes``; the CLI prints notes on stderr).

CLI: ``python scripts/validate.py --file X [--schema NAME] [--order ORDER_MD]`` → exit 0 when
valid, 1 when invalid; errors printed one per line on stdout.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import contextvars  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any, Iterable, Iterator, Mapping  # noqa: E402

import yaml  # noqa: E402
from jsonschema import Draft202012Validator, FormatChecker, validators  # noqa: E402
from jsonschema.exceptions import ValidationError, best_match  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

__all__ = [
    "Report", "validate_doc", "validate_file", "validate_order_output", "validate_doc_report",
    "validate_file_report", "validate_order_output_report", "parse_order_md", "build_order_context",
    "infer_schema", "resolve_schema_name", "schema_errors", "word_count", "main",
]

# =====================================================================================
# Defaults (used only when committee.yaml lacks a key; the repo config carries all but
# `current_weight_tolerance_pp`)
# =====================================================================================

VALIDATION_DEFAULTS: dict[str, float] = {
    "data_ref_tolerance_pct": 0.5,
    "data_ref_abs_tolerance": 1e-6,
    "level_match_tolerance_pct": 0.5,
    "cell_mean_tolerance_pct": 0.5,
    "min_base_method_families": 2,
    "probability_sum_tolerance": 0.01,
    "sector_weight_sum_tolerance_pct": 0.5,
    "min_justification_words": 15,
    "max_quote_words": 25,
    "max_executive_summary_items": 5,
    "max_top_risks": 5,
    # CIO current_sector_weights must match lookthrough.json within this many pp (DESIGN §4.13
    # "within 0.5 pp"); not (yet) a committee.yaml key.
    "current_weight_tolerance_pp": 0.5,
}
PHASE3_DEFAULTS: dict[str, int] = {"max_expand_sectors": 2, "max_buys_per_sector": 3}

#: Values the agent must copy verbatim (aggregate numbers, override targets) may differ only by
#: float noise.
COPY_ABS_TOL = 1e-6
#: Cap on reported errors (a garbage document should not produce a wall of text).
MAX_ERRORS = 60
#: Character cap per allowed word where a configured word limit replaces the schema's coarse
#: maxLength guard (the schemas use 8 characters per word; this is deliberately looser).
CHARS_PER_WORD_CAP = 10

AGENT_DOC_SCHEMAS: tuple[str, ...] = (
    "specialist_report", "devils_advocate_report", "etf_review", "sector_verdict", "macro_view",
    "cio_allocation", "cio_final",
)
REPORT_SCHEMAS: tuple[str, ...] = ("specialist_report", "devils_advocate_report", "etf_review")
CONTEXT_FILE_KEYS: tuple[str, ...] = (
    "technicals", "fundamentals", "valuation", "aggregate", "macro", "macro_view", "lookthrough",
    "cio_allocation", "prices",
)

#: (schemas, path pattern, committee.yaml validation.word_limits key). "*" matches any list
#: index / object key. These paths are checked against the configured (or work-order) limit,
#: which replaces the schema's x-max-words annotation at the same path.
WORD_LIMIT_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("specialist_report", "etf_review"), ("thesis",), "thesis"),
    (("sector_verdict",), ("verdicts", "*", "thesis"), "thesis"),
    (("sector_verdict",), ("verdicts", "*", "rationale"), "sector_rationale"),
    (("sector_verdict",), ("summary",), "sector_summary"),
    (("macro_view",), ("regime_summary",), "regime_summary"),
    (("macro_view",), ("scenarios", "*", "description"), "scenario_description"),
    (("specialist_report",), ("extensions", "setup"), "technical_setup"),
    (("specialist_report",), ("extensions", "*", "assessment"), "extension_assessment"),
    (REPORT_SCHEMAS, ("flag_responses", "*", "note"), "flag_response_note"),
    (("cio_allocation",), ("rationale",), "cio_rationale"),
    (("cio_final",), ("executive_summary", "*"), "executive_summary_item"),
    (("cio_final",), ("dissent_summary",), "dissent_summary"),
)

METHOD_FAMILY: dict[str, str] = {
    "dcf_nopat": "dcf", "dcf_fcf": "dcf", "pe": "multiple", "ev_ebit": "multiple", "p_b": "multiple",
}
_LEVEL_ID_RE = re.compile(r"^(S|R|STOP)[0-9]+$")
_DATA_REF_RE = re.compile(r"^[^#]+#/.*$")
_PHASE_DIR_RE = re.compile(r"^phase[1-4]$")
_JSON_PATH_PROP_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_QUOTE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r'"([^"]+)"'),
    re.compile("“([^“”]+)”"),        # “ … ”
    re.compile("„([^“”„]+)[“”]"),  # „ … “
    re.compile("«([^«»]+)»"),        # « … »
)
_FORMAT_HINTS = {"date": "a real calendar date YYYY-MM-DD", "uri": "an absolute http(s) URL",
                 "date-time": "an ISO-8601 timestamp with UTC offset"}


# =====================================================================================
# Small helpers
# =====================================================================================


def word_count(text: Any) -> int:
    """Whitespace-separated word count (hyphenated words count once)."""
    return len(str(text).split()) if text is not None else 0


def _num(x: Any) -> float | None:
    """A finite float for real numbers (bools excluded), else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def _fmt(x: Any) -> str:
    f = _num(x)
    if f is None:
        return _short(x)
    return f"{f:.6g}"


def _jtype(x: Any) -> str:
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "boolean"
    if isinstance(x, int):
        return "integer"
    if isinstance(x, float):
        return "number"
    if isinstance(x, str):
        return "string"
    if isinstance(x, Mapping):
        return "object"
    if isinstance(x, (list, tuple)):
        return "array"
    return type(x).__name__


def _short(x: Any, limit: int = 80) -> str:
    """Compact JSON-ish rendering of a value for messages."""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (int, float)):
        f = _num(x)
        s = (str(x) if isinstance(x, int) else f"{f:.6g}") if f is not None else str(x)
    elif isinstance(x, str):
        s = json.dumps(x, ensure_ascii=False)
    elif isinstance(x, Mapping):
        return f"an object with {len(x)} key(s)"
    elif isinstance(x, (list, tuple)):
        if len(x) <= 6 and all(isinstance(i, (str, int, float, bool)) or i is None for i in x):
            s = "[" + ", ".join(_short(i, 30) for i in x) + "]"
        else:
            return f"an array of {len(x)} item(s)"
    else:
        s = str(x)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _list_preview(values: Iterable[Any], limit: int = 12) -> str:
    vals = list(values)
    shown = ", ".join(str(v) for v in vals[:limit])
    return shown + (f", ... (+{len(vals) - limit} more)" if len(vals) > limit else "")


def _jp(parts: Iterable[Any]) -> str:
    """JSON path in jsonschema's ``ValidationError.json_path`` format (``$.a[0].b``)."""
    path = "$"
    for p in parts:
        if isinstance(p, int):
            path += f"[{p}]"
        elif _JSON_PATH_PROP_RE.match(str(p)):
            path += "." + str(p)
        else:
            path += "['" + str(p).replace("\\", "\\\\").replace("'", r"\'") + "']"
    return path


def _close(a: float, b: float, pct: float, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs(b) * pct / 100.0, abs_tol)


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    fa, fb = _num(a), _num(b)
    if fa is not None and fb is not None:
        return abs(fa - fb) <= COPY_ABS_TOL
    return a == b


def _glob(node: Any, pattern: tuple[str, ...], path: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """Yield ``(path, value)`` for every location matching ``pattern`` ("*" = any key/index)."""
    if not pattern:
        yield path, node
        return
    head, rest = pattern[0], pattern[1:]
    if isinstance(node, Mapping):
        keys = list(node) if head == "*" else ([head] if head in node else [])
        for k in keys:
            yield from _glob(node[k], rest, path + (k,))
    elif isinstance(node, list) and head == "*":
        for i, item in enumerate(node):
            yield from _glob(item, rest, path + (i,))


def _walk_strings(node: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for k, v in node.items():
            if k == "_comment":
                continue
            yield from _walk_strings(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, path + (i,))


def _cited_ids(node: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
    """Every cited source id (``evidence_refs[]``, ``source_ids[]``, ``source_id``) outside
    the top-level ``sources`` list."""
    if isinstance(node, Mapping):
        for k, v in node.items():
            if not path and k in ("sources", "_comment"):
                continue
            if k in ("evidence_refs", "source_ids") and isinstance(v, list):
                for i, ref in enumerate(v):
                    if isinstance(ref, str):
                        yield path + (k, i), ref
            elif k == "source_id" and isinstance(v, str):
                yield path + (k,), v
            else:
                yield from _cited_ids(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _cited_ids(v, path + (i,))


def _run_dir_of(path: Any) -> Path | None:
    """The run directory containing ``path`` (parent of its ``phaseN`` ancestor)."""
    if not path:
        return None
    p = Path(str(path))
    for parent in p.parents:
        if _PHASE_DIR_RE.match(parent.name):
            return parent.parent
    return None


def _norm_t(t: Any) -> str | None:
    try:
        return common.norm_ticker(t) if t not in (None, "") else None
    except (ValueError, TypeError):
        return None


# =====================================================================================
# Schema registry + extended validator
# =====================================================================================

#: Active ``validation.min_justification_words`` while a document is being validated.
_MIN_WORDS: contextvars.ContextVar[int | None] = contextvars.ContextVar("aginv_min_words", default=None)


def _kw_max_words(validator: Any, value: Any, instance: Any, schema: Mapping) -> Iterator[ValidationError]:
    if isinstance(instance, str) and isinstance(value, int):
        n = word_count(instance)
        if n > value:
            yield ValidationError(f"too long; expected <= {value} words, got {n}")


def _kw_min_words(validator: Any, value: Any, instance: Any, schema: Mapping) -> Iterator[ValidationError]:
    if not isinstance(instance, str) or not isinstance(value, int):
        return
    typ = schema.get("type")
    if typ == "null" or (isinstance(typ, list) and "null" in typ):
        return  # optional prose: the minimum is enforced by the conditional `then` branch
    limit = _MIN_WORDS.get() or value
    n = word_count(instance)
    if n < limit:
        yield ValidationError(f"too short; expected >= {limit} words, got {n}")


_ExtendedValidator = validators.extend(
    Draft202012Validator, {"x-max-words": _kw_max_words, "x-min-words": _kw_min_words})

_REG_LOCK = threading.Lock()
_REG_STATE: dict[str, Any] = {}


def _schema_files() -> list[Path]:
    root = common.schemas_dir()
    return sorted(root.glob("*.json")) + sorted((root / "data").glob("*.json"))


def _registry_state() -> dict[str, Any]:
    files = _schema_files()
    sig = (str(common.schemas_dir()),
           tuple((p.as_posix(), p.stat().st_mtime_ns) for p in files))
    with _REG_LOCK:
        state = _REG_STATE.get("state")
        if state is not None and state["sig"] == sig:
            return state
        by_id: dict[str, dict] = {}
        for p in files:
            schema = common.read_json(p)
            sid = schema.get("$id") if isinstance(schema, dict) else None
            if sid:
                by_id[sid] = schema
        registry = Registry().with_resources(
            [(sid, Resource.from_contents(s, default_specification=DRAFT202012)) for sid, s in by_id.items()])
        state = {"sig": sig, "by_id": by_id, "registry": registry, "validators": {}}
        _REG_STATE["state"] = state
        return state


def resolve_schema_name(name: Any) -> str:
    """Map ``specialist_report``, ``data/prices``, ``prices/v1``, a schema file path or a
    ``urn:agent-investors:...`` id to a ``common.SCHEMA_FILES`` key. KeyError if unknown."""
    s = str(name).strip()
    if s in common.SCHEMA_FILES:
        return s
    if s.startswith(common.SCHEMA_ID_PREFIX):
        s = s[len(common.SCHEMA_ID_PREFIX):]
    s = s.replace("\\", "/")
    m = re.fullmatch(r"([a-z_]+)/v[0-9]+", s)
    if m and m.group(1) in common.SCHEMA_FILES:
        return m.group(1)
    base = s.rsplit("/", 1)[-1]
    for suffix in (".schema.json", ".json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base in common.SCHEMA_FILES:
        return base
    raise KeyError(f"unknown schema {name!r}")


def get_validator(name: str) -> Any:
    """Cached extended Draft 2020-12 validator for schema ``name`` (registry-backed)."""
    key = resolve_schema_name(name)
    state = _registry_state()
    v = state["validators"].get(key)
    if v is None:
        sid = common.schema_id(key)
        schema = state["by_id"].get(sid)
        if schema is None:
            raise KeyError(f"schema {key!r} ($id {sid}) not found under {common.schemas_dir().as_posix()}")
        v = _ExtendedValidator(schema, registry=state["registry"], format_checker=FormatChecker())
        state["validators"][key] = v
    return v


# ---- schema error formatting ---------------------------------------------------------


def _pick_branch_error(e: ValidationError) -> ValidationError | None:
    """For anyOf/oneOf failures: the most specific error of the branch that 'almost' matched
    (type mismatches of the other branches are ignored)."""
    ctx = list(e.context or [])
    non_type = [c for c in ctx if c.validator != "type"]
    return best_match(non_type) if non_type else None


def _hint(path: str, e: ValidationError, min_words: int) -> str:
    v = e.validator
    if v == "required":
        missing = _missing_prop(e)
        if missing == "data_ref" and "key_figures" in path:
            return (" (a figure citing a D:* dataset needs data_ref \"<data file path>#<JSON pointer>\" "
                    "copied from the work order inputs)")
        if missing == "bear_case_ref":
            return (" (bear_case_price needs bear_case_ref: a valuation cell id from valuation_<T>.json "
                    "or a level id S#/R#/STOP# from technicals_<T>.json)")
        if missing in ("deviation_justification", "override_justification"):
            return f" (add {missing}: at least {min_words} words)"
    if v == "type" and re.search(r"key_figures\[\d+\]\.(source_id|as_of)$", path):
        return (" (a numeric figure needs source_id and as_of; if it cannot be sourced set value null "
                "and list its name in missing_data)")
    if v == "type" and path.endswith(".extensions.invalidation_ref"):
        return " (invalidation_price needs invalidation_ref: the S#/R#/STOP# id whose computed price equals it)"
    if v == "const" and re.search(r"verdicts\[\d+\]\.action$", path):
        return (" (action differs from rule_action: use the rule action, or set rule_deviation true "
                f"with a deviation_justification of at least {min_words} words)")
    if v == "const" and re.search(r"final_actions\[\d+\]\.action$", path):
        return (" (action differs from source_verdict_action: use the sector verdict action, or set "
                f"override true with an override_justification of at least {min_words} words)")
    if v in ("pattern", "format") and re.search(r"sources\[\d+\]\.url$", path):
        return " (source urls must be upstream http(s) URLs; put local file paths in 'path')"
    return ""


def _missing_prop(e: ValidationError) -> str:
    m = re.match(r"^'(.+)' is a required property$", e.message)
    return m.group(1) if m else e.message


def _got(x: Any) -> str:
    """``null`` / ``string "abc"`` / ``integer 5`` for type-mismatch messages."""
    t = _jtype(x)
    return t if x is None or isinstance(x, (Mapping, list, tuple)) else f"{t} {_short(x)}"


def _format_schema_error(e: ValidationError, min_words: int = 15) -> tuple[str, str, str]:
    """``(json_path, validator, message)`` for one schema error."""
    if e.validator in ("anyOf", "oneOf") and e.context:
        sub = _pick_branch_error(e)
        if sub is not None:
            return _format_schema_error(sub, min_words)
        types = []
        for c in e.context:
            vv = c.validator_value
            types.extend(vv if isinstance(vv, list) else [vv])
        path = e.json_path
        exp = " or ".join(dict.fromkeys(str(t) for t in types)) or "one of the allowed forms"
        return path, e.validator, f"{path}: wrong type; expected {exp}, got {_got(e.instance)}"
    path = e.json_path
    v = e.validator
    vv = e.validator_value
    inst = e.instance
    if v == "type":
        exp = " or ".join(vv) if isinstance(vv, list) else str(vv)
        msg = f"wrong type; expected {exp}, got {_got(inst)}"
    elif v == "enum":
        msg = f"not an allowed value; expected one of {_list_preview(_short(x, 40) for x in vv)}, got {_short(inst)}"
    elif v == "const":
        msg = f"wrong value; expected {_short(vv)}, got {_short(inst)}"
    elif v == "required":
        msg = f"missing required property '{_missing_prop(e)}'"
    elif v == "additionalProperties" and isinstance(inst, Mapping):
        props = list((e.schema or {}).get("properties", {}))
        extras = [k for k in inst if k not in props]
        msg = (f"unexpected property {', '.join(repr(k) for k in extras) or e.message}; "
               f"expected only: {_list_preview(props, 30)}")
    elif v in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        op = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}[v]
        msg = f"out of range; expected {op} {_fmt(vv)}, got {_short(inst)}"
    elif v in ("minItems", "maxItems") and isinstance(inst, list):
        word, op = ("too few", ">=") if v == "minItems" else ("too many", "<=")
        msg = f"{word} items; expected {op} {vv}, got {len(inst)}"
    elif v in ("minLength", "maxLength") and isinstance(inst, str):
        word, op = ("too short", ">=") if v == "minLength" else ("too long", "<=")
        msg = f"{word}; expected {op} {vv} characters, got {len(inst)}"
    elif v in ("minProperties", "maxProperties") and isinstance(inst, Mapping):
        op = ">=" if v == "minProperties" else "<="
        msg = f"wrong number of keys; expected {op} {vv}, got {len(inst)}"
    elif v == "pattern":
        msg = f"invalid format; expected a string matching {vv}, got {_short(inst)}"
    elif v == "format":
        msg = f"invalid {vv}; expected {_FORMAT_HINTS.get(str(vv), str(vv))}, got {_short(inst)}"
    elif v == "uniqueItems" and isinstance(inst, list):
        seen: list = []
        dups = []
        for item in inst:
            if item in seen and item not in dups:
                dups.append(item)
            seen.append(item)
        msg = f"duplicate items; expected unique values, got duplicates {_short(dups)}"
    else:
        msg = e.message if len(e.message) <= 300 else e.message[:297] + "..."
    if "propertyNames" in list(e.absolute_schema_path):
        msg = f"invalid key {_short(inst)}: {msg}"
    msg += _hint(path, e, min_words)
    return path, v, f"{path}: {msg}"


def schema_errors(doc: Any, schema_name: str) -> list[str]:
    """JSON-Schema errors only (no semantic checks), formatted and sorted by path."""
    validator = get_validator(schema_name)
    out = [_format_schema_error(e) for e in validator.iter_errors(doc)]
    return _dedupe([m for _p, _v, m in sorted(out, key=lambda t: (t[0], t[2]))])


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


# =====================================================================================
# Schema inference
# =====================================================================================


def infer_schema(doc: Any, path: Any = None) -> str | None:
    """Best guess of the schema for ``doc`` (data envelope ``schema``, failed stub, agent_role
    (+ cio mode), work order, manifest, file name). None when unknown."""
    if isinstance(doc, Mapping):
        s = doc.get("schema")
        if isinstance(s, str) and "/" in s:
            name = s.split("/", 1)[0]
            if name == "portfolio_preview":  # same contract as the final plan
                name = "portfolio_plan"
            if name in common.DATA_SCHEMA_NAMES:
                return name
        if doc.get("status") == "failed":
            return "failed_report"
        if "agent_type" in doc and "inputs" in doc and "order_id" in doc:
            return "work_order"
        role = doc.get("agent_role")
        if not isinstance(role, str):
            role = None  # malformed (list / object): fall through to "cannot infer", never a TypeError
        if role == "cio":
            mode = doc.get("mode")
            if mode is None and "final_actions" in doc:
                mode = "reconciliation"
            if mode is None or isinstance(mode, str):
                try:
                    return common.role_schema("cio", mode)
                except (KeyError, ValueError):
                    return None
            return None
        if role is not None and role in common.ROLE_SCHEMAS:
            return common.role_schema(role)
        if "run_id" in doc and "config_sha256" in doc:
            return "manifest"
    if path:
        name = Path(str(path)).name
        if name.endswith(".failed.json"):
            return "failed_report"
        if name == "manifest.json":
            return "manifest"
    return None


# =====================================================================================
# Report + per-document run state
# =====================================================================================


@dataclass
class Report:
    """Result of one validation: ``errors`` (empty = valid), ``notes`` (skipped checks, context
    gaps — informational) and the schema name used."""
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    schema: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class _Run:
    """Per-document state of the semantic pass."""

    def __init__(self, doc: Any, schema: str, context: Mapping[str, Any] | None):
        self.doc = doc
        self.schema = schema
        self.ctx: dict[str, Any] = dict(context or {})
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.suppress: set[tuple[str, str]] = set()
        self._json_cache: dict[str, Any] = {}
        self._ctx_cache: dict[str, Any] = {}
        self._sole: set[str] | None = None
        self.strict = bool(self.ctx.get("strict_refs"))
        rd = self.ctx.get("run_dir") or _run_dir_of(self.ctx.get("_doc_path"))
        self.run_dir: Path | None = Path(str(rd)) if rd else None
        self.cfg = self._load_cfg()

    # ---- messages ---------------------------------------------------------------------
    def err(self, path: Iterable[Any], problem: str, expected: Any, got: Any) -> None:
        self.errors.append(f"{_jp(path)}: {problem}; expected {expected}, got {got}")

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    # ---- config ----------------------------------------------------------------------
    def _load_cfg(self) -> dict:
        c = self.ctx.get("committee_cfg")
        if isinstance(c, Mapping):
            return dict(c)
        if c:
            try:
                data = common.load_yaml(c)
                return data if isinstance(data, dict) else {}
            except common.ConfigError as exc:
                self.note(f"committee config {c} unreadable ({exc}); using built-in validation defaults")
                return {}
        try:
            return common.committee_cfg()
        except common.ConfigError as exc:
            self.note(f"committee.yaml not available ({exc}); using built-in validation defaults")
            return {}

    def vnum(self, key: str) -> float:
        section = self.cfg.get("validation") if isinstance(self.cfg.get("validation"), Mapping) else {}
        val = _num(section.get(key)) if key in section else None
        return float(VALIDATION_DEFAULTS[key]) if val is None else val

    def p3(self, key: str) -> int:
        section = self.cfg.get("phase3") if isinstance(self.cfg.get("phase3"), Mapping) else {}
        val = _num(section.get(key))
        return int(PHASE3_DEFAULTS[key] if val is None else val)

    def word_limits(self) -> dict[str, int]:
        section = self.cfg.get("validation") if isinstance(self.cfg.get("validation"), Mapping) else {}
        limits: dict[str, int] = {}
        for src in (section.get("word_limits"), self.ctx.get("word_limits")):
            if isinstance(src, Mapping):
                for k, v in src.items():
                    n = _num(v)
                    if n is not None and n >= 1:
                        limits[str(k)] = int(n)
        return limits

    # ---- files -----------------------------------------------------------------------
    def load_json(self, path: Path) -> Any:
        key = str(path)
        if key not in self._json_cache:
            self._json_cache[key] = common.read_json(path)
        return self._json_cache[key]

    def resolve_file(self, file_part: str) -> Path | None:
        raw = file_part.strip().replace("\\", "/")
        p = Path(raw)
        cands: list[Path] = []
        if self.run_dir is not None:
            parts = p.parts
            idx = next((i for i, part in enumerate(parts) if _PHASE_DIR_RE.match(part)), None)
            if idx is not None:
                cands.append(self.run_dir.joinpath(*parts[idx:]))
        if p.is_absolute():
            cands.append(p)
        else:
            cands += [common.REPO_ROOT / p, Path.cwd() / p]
            if self.run_dir is not None:
                cands.append(self.run_dir / p)
            if len(p.parts) > 1 and p.parts[0] == "runs":
                cands.append(common.runs_dir().joinpath(*p.parts[1:]))
        for c in cands:
            try:
                if c.is_file():
                    return c
            except OSError:
                continue
        return None

    def ctx_doc(self, key: str) -> dict | None:
        """Context document ``key`` (path or dict) or None (missing → note)."""
        if key in self._ctx_cache:
            return self._ctx_cache[key]
        val = self.ctx.get(key)
        doc = None
        if isinstance(val, Mapping):
            doc = dict(val)
        elif val:
            doc = self._read_ctx_file(key, val)
        self._ctx_cache[key] = doc
        return doc

    def _read_ctx_file(self, key: str, val: Any) -> dict | None:
        p = Path(str(val))
        if not p.is_file():
            self.note(f"{key} context file not found: {p.as_posix()}; dependent checks skipped")
            return None
        try:
            data = self.load_json(p)
        except (ValueError, OSError) as exc:
            self.note(f"{key} context file unreadable ({p.as_posix()}: {exc}); dependent checks skipped")
            return None
        if not isinstance(data, dict):
            self.note(f"{key} context file {p.as_posix()} is not a JSON object; dependent checks skipped")
            return None
        return data

    def discover(self, key: str) -> dict | None:
        """Context ``key`` (technicals|valuation|...) from the context, else from a data file the
        document itself references (``<key>_<T>.json`` in a data_ref / input_ref)."""
        doc = self.ctx_doc(key)
        if doc is not None or self.ctx.get(key):
            return doc
        ticker = _norm_t(self.doc.get("ticker")) if isinstance(self.doc, Mapping) else None
        wanted = f"{key}_{ticker}.json" if ticker else None
        for _path, ref, _cmp in _iter_refs(self.doc):
            file_part = ref.partition("#")[0]
            name = Path(file_part.replace("\\", "/")).name
            if (wanted and name == wanted) or (not wanted and name.startswith(f"{key}_")):
                fp = self.resolve_file(file_part)
                if fp is not None:
                    data = self._read_ctx_file(key, fp)
                    if data is not None:
                        self.note(f"{key} context taken from the document's own data_ref: {fp.as_posix()}")
                        self._ctx_cache[key] = data
                        return data
        return None

    def sole_context_kinds(self) -> set[str]:
        """Kinds (technicals / fundamentals / valuation) for which the work order lists exactly one
        file: ``context['sole_context']`` when given, else read from the order's own markdown
        (``<run_dir>/orders/<order_id>.md``), else none."""
        if self._sole is not None:
            return self._sole
        kinds: set[str] = set()
        explicit = self.ctx.get("sole_context")
        if isinstance(explicit, (list, tuple, set, frozenset)):
            kinds = {str(k) for k in explicit}
        else:
            expect = self.ctx.get("expect")
            oid = expect.get("order_id") if isinstance(expect, Mapping) else None
            if self.run_dir is not None and oid:
                order_md = self.run_dir / "orders" / f"{common.safe_name(str(oid))}.md"
                try:
                    order = parse_order_md(order_md) if order_md.is_file() else None
                except (ValueError, OSError):
                    order = None
                if order is not None:
                    kinds = {k for k, items in _order_input_files(order).items() if len(items) == 1}
        self._sole = kinds
        return kinds

    def verdict_docs(self) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        vals = self.ctx.get("verdicts") or []
        if isinstance(vals, (str, Path, Mapping)):
            vals = [vals]
        for i, v in enumerate(vals):
            if isinstance(v, Mapping):
                out.append((str(v.get("sector") or f"verdict[{i}]"), dict(v)))
                continue
            data = self._read_ctx_file("verdicts", v)
            if data is not None:
                out.append((Path(str(v)).name, data))
        return out


# =====================================================================================
# data_ref iteration
# =====================================================================================


def _iter_refs(doc: Any, path: tuple = ()) -> Iterator[tuple[tuple, str, tuple | None]]:
    """``(path, ref, compare)`` for every data reference in ``doc``. ``compare`` is
    ``(agent_value, value_path, owner, owner_path)`` when the reference backs a number the agent
    wrote (key_figures[].value, key_levels[].price), else None (existence check only). ``owner``
    is the key figure itself (its as_of / kind / unit / filed are checked against the Fact) and
    None for a key level."""
    if isinstance(doc, Mapping):
        for k, v in doc.items():
            if k == "_comment":
                continue
            if k == "data_ref" and isinstance(v, str):
                cmp = None
                if "value" in doc and "name" in doc:
                    cmp = (doc.get("value"), path + ("value",), doc, path)
                elif "price" in doc and "ref_id" in doc:
                    cmp = (doc.get("price"), path + ("price",), None, path)
                yield path + (k,), v, cmp
            elif k in ("data_refs", "indicator_refs", "input_refs") and isinstance(v, list):
                for i, r in enumerate(v):
                    if isinstance(r, str) and _DATA_REF_RE.match(r):
                        yield path + (k, i), r, None
            else:
                yield from _iter_refs(v, path + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from _iter_refs(v, path + (i,))


# =====================================================================================
# Semantic checks
# =====================================================================================


def _check_identity(run: _Run) -> None:
    expect = run.ctx.get("expect")
    doc = run.doc
    if not isinstance(expect, Mapping):
        return
    for key in ("order_id", "run_id", "agent_role"):
        want = expect.get(key)
        if want and key in doc and doc.get(key) != want:
            run.err((key,), "does not match the work order", _short(want), _short(doc.get(key)))
    want_t = _norm_t(expect.get("ticker"))
    if want_t and "ticker" in doc and _norm_t(doc.get("ticker")) != want_t:
        run.err(("ticker",), "does not match the work order", _short(want_t), _short(doc.get("ticker")))
    want_s = expect.get("sector")
    if want_s and "sector" in doc and doc.get("sector") != want_s:
        run.err(("sector",), "does not match the work order", _short(want_s), _short(doc.get("sector")))
    mode = expect.get("mode")
    if mode and run.schema in ("cio_allocation", "cio_final") and doc.get("mode") != mode:
        run.err(("mode",), "does not match the work order", _short(mode), _short(doc.get("mode")))
    if mode in ("phase2", "phase3") and run.schema == "sector_verdict":
        want_phase = int(mode[-1])
        if doc.get("phase") != want_phase:
            run.err(("phase",), "does not match the work order mode", want_phase, _short(doc.get("phase")))


def _check_sources(run: _Run) -> None:
    doc = run.doc
    sources = doc.get("sources")
    ids: list[str] = []
    if isinstance(sources, list):
        for i, s in enumerate(sources):
            if isinstance(s, Mapping) and isinstance(s.get("id"), str):
                if s["id"] in ids:
                    run.err(("sources", i, "id"), "duplicate source id", "unique ids in sources[]", _short(s["id"]))
                ids.append(s["id"])
    known = set(ids)
    for path, ref in _cited_ids(doc):
        if ref not in known:
            run.err(path, "unknown source id", f"an id from sources[] ({_list_preview(ids) or 'sources[] is empty'})",
                    _short(ref))
    seeds = run.ctx.get("seed_sources")
    if isinstance(seeds, list) and isinstance(sources, list):
        seed_map = {s.get("id"): s for s in seeds if isinstance(s, Mapping) and s.get("id")}
        for i, s in enumerate(sources):
            if not isinstance(s, Mapping):
                continue
            sid = s.get("id")
            seed = seed_map.get(sid)
            if seed is not None and seed.get("url") and s.get("url") != seed.get("url"):
                run.err(("sources", i, "url"), f"dataset source {sid} must be copied verbatim from the work "
                        "order seed_sources", _short(seed.get("url"), 200), _short(s.get("url"), 200))
            elif seed is None and isinstance(sid, str) and sid.startswith("D:") and seed_map:
                run.note(f"{_jp(('sources', i, 'id'))}: dataset source {sid} is not among the work order seed_sources")


def _check_red_flags(run: _Run) -> None:
    flags = run.doc.get("red_flags")
    if not isinstance(flags, list):
        return
    seen: set[str] = set()
    for i, f in enumerate(flags):
        fid = f.get("id") if isinstance(f, Mapping) else None
        if isinstance(fid, str):
            if fid in seen:
                run.err(("red_flags", i, "id"), "duplicate red flag id", "unique ids F1, F2, ...", _short(fid))
            seen.add(fid)


def _check_quotes(run: _Run) -> None:
    limit = int(run.vnum("max_quote_words"))
    for path, text in _walk_strings(run.doc):
        if path and path[0] == "sources":
            continue
        for rx in _QUOTE_RES:
            for m in rx.finditer(text):
                n = word_count(m.group(1))
                if n > limit:
                    preview = " ".join(m.group(1).split()[:8])
                    run.err(path, f"quoted span of {n} words breaks the copyright guard (paraphrase instead)",
                            f"<= {limit} words inside quotation marks", f"\"{preview} ...\"")


def _check_word_limits(run: _Run) -> None:
    limits = run.word_limits()
    for schemas, pattern, key in WORD_LIMIT_RULES:
        if run.schema not in schemas or key not in limits:
            continue
        lim = limits[key]
        for path, val in _glob(run.doc, pattern):
            # the configured limit replaces the schema's x-max-words annotation and its coarse
            # maxLength guard (re-applied here as a proportional character cap)
            run.suppress.add((_jp(path), "x-max-words"))
            run.suppress.add((_jp(path), "maxLength"))
            if isinstance(val, str):
                n = word_count(val)
                if n > lim:
                    run.err(path, "too long", f"<= {lim} words", n)
                elif len(val) > lim * CHARS_PER_WORD_CAP:
                    run.err(path, "too long", f"<= {lim * CHARS_PER_WORD_CAP} characters", len(val))


def _check_counts(run: _Run) -> None:
    if run.schema != "cio_final":
        return
    for key, cfg_key in (("executive_summary", "max_executive_summary_items"), ("top_risks", "max_top_risks")):
        items = run.doc.get(key)
        lim = int(run.vnum(cfg_key))
        run.suppress.add((_jp((key,)), "maxItems"))
        if isinstance(items, list) and len(items) > lim:
            run.err((key,), "too many items", f"<= {lim}", len(items))


def _check_null_figures(run: _Run) -> None:
    figures = run.doc.get("key_figures")
    if not isinstance(figures, list):
        return
    md = [str(x).lower() for x in (run.doc.get("missing_data") or []) if isinstance(x, str)]
    for i, kf in enumerate(figures):
        if not isinstance(kf, Mapping) or "value" not in kf or kf.get("value") is not None:
            continue
        name = str(kf.get("name") or "").strip()
        if name and not any(name.lower() in item for item in md):
            run.err(("key_figures", i, "name"), "value is null but the figure is not listed in missing_data",
                    f"a missing_data entry containing {json.dumps(name, ensure_ascii=False)}",
                    f"missing_data with {len(md)} entr{'y' if len(md) == 1 else 'ies'}")


def _same_date_text(a: Any, b: Any) -> bool:
    """Equal dates (``2026-10-02`` == ``2026-10-02T00:00:00+00:00``); None only equals None."""
    if a is None or b is None:
        return a is None and b is None
    try:
        da, db = common.parse_date(a), common.parse_date(b)
    except (ValueError, TypeError):
        return str(a).strip() == str(b).strip()
    if da is None or db is None:
        return str(a).strip() == str(b).strip()
    return da == db


def _show(x: Any) -> str:
    return "null" if x is None else str(x)


def _check_provenance(run: _Run, fig_path: tuple, fig: Mapping[str, Any], fact: Mapping[str, Any],
                      ref: str) -> None:
    """A key figure that cites a Fact through ``data_ref`` must carry that Fact's ``as_of``,
    ``kind``, ``unit`` and ``filed``. The evidence score reads the agent-supplied copies (stale
    test, kind-dependent freshness rule), so a free-hand ``as_of`` or ``kind`` would change it.
    ``as_of`` / ``kind`` / ``unit`` are compared when the Fact has them; ``filed`` is compared
    always (a ``filed`` missing from the figure, or invented, is a mismatch)."""
    problem = f"does not match the Fact at {ref}"
    for name in ("as_of", "kind", "unit"):
        dv = fact.get(name)
        if dv is None or (isinstance(dv, str) and not dv.strip()):
            continue
        av = fig.get(name)
        same = _same_date_text(av, dv) if name == "as_of" else av == dv
        if not same:
            run.err(fig_path + (name,), problem, f"{_show(dv)} (from data)", _show(av))
    dv, av = fact.get("filed"), fig.get("filed")
    if not _same_date_text(av, dv):
        expected = f"{_show(dv)} (from data)" if dv is not None else "no filed (the data has none; omit it)"
        run.err(fig_path + ("filed",), problem, expected, _show(av))


def _check_data_refs(run: _Run) -> None:
    pct = run.vnum("data_ref_tolerance_pct")
    abs_tol = run.vnum("data_ref_abs_tolerance")
    unresolved: dict[str, list[str]] = {}
    for path, ref, cmp in _iter_refs(run.doc):
        file_part, _, ptr = ref.partition("#")
        fp = run.resolve_file(file_part)
        if fp is None:
            if run.strict:
                run.err(path, "data_ref file not found",
                        "the path of a data file listed in the work order inputs (absolute or repo-relative)",
                        _short(file_part, 200))
            else:
                unresolved.setdefault(file_part, []).append(_jp(path))
            continue
        try:
            data = run.load_json(fp)
        except (ValueError, OSError) as exc:
            run.err(path, f"data_ref file is not readable JSON ({exc})", "a JSON data file", _short(file_part, 200))
            continue
        try:
            target = common.json_pointer_get(data, "#" + ptr)
        except KeyError:
            run.err(path, f"JSON pointer does not resolve in {fp.name}",
                    "a pointer to an existing key, e.g. #/ttm/revenue", _short("#" + ptr, 200))
            continue
        if cmp is None:
            continue
        agent_val, val_path, owner, owner_path = cmp
        a = _num(agent_val)
        if a is None:
            continue
        fact = target if common.is_fact(target) else None
        if fact is not None:
            d = _num(target.get("value"))
            na = target.get("na_reason")
        elif _num(target) is not None:
            d, na = _num(target), None
        else:
            run.err(path, "data_ref must point at a Fact or a number", "a Fact (value/unit/source_id) or a number",
                    _jtype(target))
            continue
        if d is None:
            run.err(val_path, f"the referenced data value is null (na_reason {na or 'missing'})",
                    "value null with the figure name listed in missing_data", _fmt(a))
        elif not _close(a, d, pct, abs_tol):
            run.err(val_path, f"does not match the data at {ref}", f"{_fmt(d)} (+/-{pct:g}%)", _fmt(a))
        if fact is not None and owner is not None:  # a bare-number pointer has no provenance to compare
            _check_provenance(run, owner_path, owner, fact, ref)
    for file_part, paths in unresolved.items():
        run.note(f"data_ref file not found: {file_part} ({len(paths)} reference(s), first {paths[0]}); "
                 "value checks skipped")


def _cell_map(vdoc: Mapping) -> dict[str, Mapping]:
    cells = vdoc.get("cells") if isinstance(vdoc, Mapping) else None
    return {c["id"]: c for c in (cells or []) if isinstance(c, Mapping) and isinstance(c.get("id"), str)}


def _tech_levels(tdoc: Mapping) -> dict[str, tuple[float | None, str]]:
    out: dict[str, tuple[float | None, str]] = {}
    levels = tdoc.get("levels") if isinstance(tdoc.get("levels"), Mapping) else {}
    for key, typ in (("supports", "support"), ("resistances", "resistance")):
        for lvl in levels.get(key) or []:
            if isinstance(lvl, Mapping) and isinstance(lvl.get("id"), str):
                out[lvl["id"]] = (common.fact_value(lvl.get("price")), typ)
    for stop in tdoc.get("stop_candidates") or []:
        if isinstance(stop, Mapping) and isinstance(stop.get("id"), str):
            out[stop["id"]] = (common.fact_value(stop.get("price")), "stop")
    return out


def _ticker_mismatch(run: _Run, key: str, cdoc: Mapping) -> bool:
    """True when the context file belongs to another ticker (checks are then skipped). A file for
    another ticker still counts as matching when it is the order's ONLY file of its kind: a
    proxy-priced fund (order ticker 1186063) is priced from, and so validated against, its proxy
    (technicals_QQQ.json). With several files of the kind and none for the order ticker the
    mismatch stands."""
    want = _norm_t(run.doc.get("ticker"))
    have = _norm_t(cdoc.get("ticker"))
    if want and have and want != have:
        if key in run.sole_context_kinds():
            run.note(f"{key} context is for {have}, the order's only {key} file; used for {want} (proxy-priced)")
            return False
        run.note(f"{key} context is for {have}, not {want}; dependent checks skipped")
        return True
    return False


def _check_fundamental(run: _Run) -> None:
    ext = run.doc.get("extensions")
    val = ext.get("valuation") if isinstance(ext, Mapping) else None
    if not isinstance(val, Mapping):
        return
    vp = ("extensions", "valuation")
    lo, base, hi = (_num(val.get(k)) for k in ("fair_value_low", "fair_value_base", "fair_value_high"))
    if lo is not None and base is not None and hi is not None and not (lo <= base <= hi):
        run.err(vp, "fair values out of order", "fair_value_low <= fair_value_base <= fair_value_high",
                f"{_fmt(lo)} / {_fmt(base)} / {_fmt(hi)}")
    vdoc = run.discover("valuation")
    if vdoc is None:
        run.note("valuation context not provided: cell-mean and method-family checks skipped")
        return
    if _ticker_mismatch(run, "valuation", vdoc):
        return
    cells = _cell_map(vdoc)
    pct = run.vnum("cell_mean_tolerance_pct")
    abs_tol = run.vnum("data_ref_abs_tolerance")
    for fv_key, cells_key in (("fair_value_low", "cells_low"), ("fair_value_base", "cells_base"),
                              ("fair_value_high", "cells_high")):
        ids = val.get(cells_key)
        if not isinstance(ids, list):
            continue
        fvs: list[tuple[str, float]] = []
        ok = True
        for j, cid in enumerate(ids):
            cell = cells.get(cid) if isinstance(cid, str) else None
            if cell is None:
                run.err(vp + (cells_key, j), "unknown valuation cell",
                        f"an id from valuation_<T>.json cells[].id (e.g. {_list_preview(list(cells)[:4], 4)})",
                        _short(cid))
                ok = False
                continue
            fv = _num(cell.get("fv_per_share"))
            if fv is None:
                run.err(vp + (cells_key, j), f"cell {cid} has no fair value (na_reason {cell.get('na_reason')})",
                        "a cell with a numeric fv_per_share", "null")
                ok = False
                continue
            fvs.append((cid, fv))
        fvx = _num(val.get(fv_key))
        if ok and fvs and fvx is not None:
            mean = sum(f for _c, f in fvs) / len(fvs)
            if not _close(fvx, mean, pct, abs_tol):
                detail = ", ".join(f"{c} {_fmt(f)}" for c, f in fvs)
                run.err(vp + (fv_key,), f"does not equal the mean fair value of {cells_key} ({detail})",
                        f"{_fmt(mean)} (+/-{pct:g}%)", _fmt(fvx))
    # method families of cells_base
    base_ids = val.get("cells_base")
    if base is not None and isinstance(base_ids, list) and base_ids:
        avail = {METHOD_FAMILY[c.get("method")] for c in cells.values()
                 if c.get("method") in METHOD_FAMILY and _num(c.get("fv_per_share")) is not None}
        have = {METHOD_FAMILY[cells[c].get("method")] for c in base_ids
                if isinstance(c, str) and c in cells and cells[c].get("method") in METHOD_FAMILY}
        need = min(int(run.vnum("min_base_method_families")), len(avail))
        if len(have) < need:
            run.err(vp + ("cells_base",), "cells_base must span the available valuation method families",
                    f">= {need} families ({', '.join(sorted(avail))})",
                    f"{len(have)} ({', '.join(sorted(have)) or 'none'})")


def _check_technical(run: _Run) -> None:
    doc = run.doc
    ext = doc.get("extensions")
    if not isinstance(ext, Mapping):
        return
    pct = run.vnum("level_match_tolerance_pct")
    abs_tol = run.vnum("data_ref_abs_tolerance")
    tdoc = run.discover("technicals")
    if tdoc is not None and _ticker_mismatch(run, "technicals", tdoc):
        tdoc = None
    inv = _num(doc.get("invalidation_price"))
    inv_ref = ext.get("invalidation_ref")
    key_levels = [lv for lv in (ext.get("key_levels") or []) if isinstance(lv, Mapping)]
    if tdoc is None:
        run.note("technicals context not provided: regime and level checks skipped")
        if inv is not None and isinstance(inv_ref, str):
            kl = {lv.get("ref_id"): _num(lv.get("price")) for lv in key_levels}
            p = kl.get(inv_ref)
            if p is not None and not _close(inv, p, pct, abs_tol):
                run.err(("invalidation_price",), f"does not equal the price of key level {inv_ref}",
                        f"{_fmt(p)} (+/-{pct:g}%)", _fmt(inv))
        return
    trend = tdoc.get("trend") if isinstance(tdoc.get("trend"), Mapping) else {}
    regime = trend.get("regime")
    if regime and ext.get("trend_regime") != regime:
        run.err(("extensions", "trend_regime"), "does not match the computed regime in technicals_<T>.json",
                _short(regime), _short(ext.get("trend_regime")))
    levels = _tech_levels(tdoc)
    ids_preview = _list_preview(levels)
    for j, lv in enumerate(ext.get("key_levels") or []):
        if not isinstance(lv, Mapping):
            continue
        rid = lv.get("ref_id")
        if rid not in levels:
            run.err(("extensions", "key_levels", j, "ref_id"), "unknown level id",
                    f"one of the computed ids ({ids_preview})", _short(rid))
            continue
        cp, ctype = levels[rid]
        if lv.get("type") != ctype:
            run.err(("extensions", "key_levels", j, "type"), f"wrong type for {rid}", _short(ctype),
                    _short(lv.get("type")))
        lp = _num(lv.get("price"))
        if cp is None:
            run.err(("extensions", "key_levels", j, "price"), f"the computed price of {rid} is null",
                    "a level with a computed price", _fmt(lp))
        elif lp is not None and not _close(lp, cp, pct, abs_tol):
            run.err(("extensions", "key_levels", j, "price"), f"does not equal the computed price of {rid}",
                    f"{_fmt(cp)} (+/-{pct:g}%)", _fmt(lp))
    if inv is not None and isinstance(inv_ref, str):
        if inv_ref not in levels:
            run.err(("extensions", "invalidation_ref"), "unknown level id",
                    f"one of the computed ids ({ids_preview})", _short(inv_ref))
        else:
            cp = levels[inv_ref][0]
            if cp is None or not _close(inv, cp, pct, abs_tol):
                run.err(("invalidation_price",), f"does not equal the computed price of {inv_ref}",
                        f"{_fmt(cp)} (+/-{pct:g}%)", _fmt(inv))


def _check_devils_advocate(run: _Run) -> None:
    doc = run.doc
    price = _num(doc.get("bear_case_price"))
    ref = doc.get("bear_case_ref")
    if price is None or not isinstance(ref, str) or not ref:
        return
    if _LEVEL_ID_RE.match(ref):
        tdoc = run.discover("technicals")
        if tdoc is None or _ticker_mismatch(run, "technicals", tdoc):
            run.note("technicals context not provided: bear_case_ref level check skipped")
            return
        levels = _tech_levels(tdoc)
        if ref not in levels:
            run.err(("bear_case_ref",), "unknown technical level id",
                    f"one of the computed ids ({_list_preview(levels)}) or a valuation cell id", _short(ref))
            return
        lp = levels[ref][0]
        pct = run.vnum("level_match_tolerance_pct")
        if lp is None or not _close(price, lp, pct, run.vnum("data_ref_abs_tolerance")):
            run.err(("bear_case_price",), f"does not equal the computed price of {ref}",
                    f"{_fmt(lp)} (+/-{pct:g}%)", _fmt(price))
        return
    vdoc = run.discover("valuation")
    if vdoc is None or _ticker_mismatch(run, "valuation", vdoc):
        run.note("valuation context not provided: bear_case_ref cell check skipped")
        return
    cells = _cell_map(vdoc)
    cell = cells.get(ref)
    if cell is None:
        run.err(("bear_case_ref",), "unknown reference",
                "a valuation cell id from valuation_<T>.json (e.g. "
                f"{_list_preview(list(cells)[:3], 3)}) or a level id S#/R#/STOP# from technicals_<T>.json",
                _short(ref))
        return
    fv = _num(cell.get("fv_per_share"))
    pct = run.vnum("cell_mean_tolerance_pct")
    if fv is None:
        run.err(("bear_case_ref",), f"cell {ref} has no fair value (na_reason {cell.get('na_reason')})",
                "a cell with a numeric fv_per_share", "null")
    elif not _close(price, fv, pct, run.vnum("data_ref_abs_tolerance")):
        run.err(("bear_case_price",), f"does not equal the fair value of cell {ref}",
                f"{_fmt(fv)} (+/-{pct:g}%)", _fmt(price))


def _check_etf(run: _Run) -> None:
    doc = run.doc
    ticker = _norm_t(doc.get("ticker"))
    pct = run.vnum("data_ref_tolerance_pct")
    abs_tol = run.vnum("data_ref_abs_tolerance")
    lt = run.ctx_doc("lookthrough")
    if lt is None:
        run.note("lookthrough context not provided: expense ratio / concentration checks skipped")
    else:
        fund = (lt.get("funds") or {}).get(ticker) if isinstance(lt.get("funds"), Mapping) else None
        if not isinstance(fund, Mapping):
            run.note(f"lookthrough.json has no fund entry for {ticker}; expense ratio / concentration checks skipped")
        else:
            for key in ("expense_ratio_pct", "top10_concentration_pct"):
                a = _num(doc.get(key))
                if a is None:
                    continue
                d = common.fact_value(fund.get(key))
                if d is None:
                    run.err((key,), "lookthrough.json has no value for this figure",
                            "null (and list it in missing_data)", _fmt(a))
                elif not _close(a, d, pct, abs_tol):
                    run.err((key,), "does not match lookthrough.json", f"{_fmt(d)} (+/-{pct:g}%)", _fmt(a))
    tdoc = run.discover("technicals")
    mom = doc.get("momentum_vs_benchmark")
    if tdoc is None or not isinstance(mom, Mapping) or _ticker_mismatch(run, "technicals", tdoc):
        return
    rs = tdoc.get("relative_strength") if isinstance(tdoc.get("relative_strength"), Mapping) else {}
    bench = _norm_t(mom.get("benchmark"))
    t_bench = _norm_t(tdoc.get("benchmark"))
    t_sector = _norm_t(tdoc.get("sector_etf"))
    if bench is not None and bench == t_bench:
        key = "vs_spy"
    elif bench is not None and bench == t_sector:
        key = "vs_sector"
    else:
        allowed = " or ".join(x for x in (t_bench, t_sector) if x) or "the technicals benchmark"
        run.err(("momentum_vs_benchmark", "benchmark"),
                "is not a benchmark technicals_<T>.json computed relative strength against, so rs_3m/6m/12m "
                "cannot be checked", allowed, _fmt(mom.get("benchmark")))
        return
    series = rs.get(key)
    if not isinstance(series, Mapping):
        run.note(f"technicals_<T>.json has no relative_strength.{key}; momentum_vs_benchmark check skipped")
        return
    for w in ("3m", "6m", "12m"):
        a = _num(mom.get(f"rs_{w}"))
        if a is None:
            continue
        d = common.fact_value(series.get(w))
        if d is None:
            run.err(("momentum_vs_benchmark", f"rs_{w}"), "technicals_<T>.json has no value for this window",
                    "null (and list it in missing_data)", _fmt(a))
        elif not _close(a, d, pct, abs_tol):
            run.err(("momentum_vs_benchmark", f"rs_{w}"), "does not match technicals_<T>.json relative_strength",
                    f"{_fmt(d)} (+/-{pct:g}%)", _fmt(a))


def _check_macro_view(run: _Run) -> None:
    doc = run.doc
    scen = [s for s in (doc.get("scenarios") or []) if isinstance(s, Mapping)]
    probs = [_num(s.get("probability")) for s in scen]
    if probs and all(p is not None for p in probs):
        total = sum(p for p in probs if p is not None)
        tol = run.vnum("probability_sum_tolerance")
        if abs(total - 1.0) > tol + 1e-12:
            run.err(("scenarios",), "scenario probabilities must sum to 1", f"1 +/- {tol:g}", f"{total:.4g}")
    seen: set[str] = set()
    mins: list[float] = []
    for i, c in enumerate(doc.get("sector_constraints") or []):
        if not isinstance(c, Mapping):
            continue
        mn, mx = _num(c.get("min_weight")), _num(c.get("max_weight"))
        if mn is not None:
            mins.append(mn)
        if mn is not None and mx is not None and mn > mx:
            run.err(("sector_constraints", i), "min_weight is greater than max_weight", "min_weight <= max_weight",
                    f"{_fmt(mn)} > {_fmt(mx)}")
        sec = c.get("sector")
        if isinstance(sec, str):
            if sec in seen:
                run.err(("sector_constraints", i, "sector"), "duplicate constraint for sector",
                        "one constraint per sector", _short(sec))
            seen.add(sec)
    if sum(mins) > 100 + run.vnum("sector_weight_sum_tolerance_pct"):
        run.err(("sector_constraints",), "the sector floors (min_weight) cannot all be met",
                "sum of min_weight <= 100", _fmt(sum(mins)))
    mdoc = run.ctx_doc("macro")
    if mdoc is None:
        run.note("macro context not provided: driver series checks skipped")
        return
    known = set((mdoc.get("series") or {}) if isinstance(mdoc.get("series"), Mapping) else [])
    for i, d in enumerate(doc.get("drivers") or []):
        if not isinstance(d, Mapping):
            continue
        for j, sid in enumerate(d.get("series") or []):
            if isinstance(sid, str) and sid not in known:
                run.err(("drivers", i, "series", j), "series not in macro.json",
                        f"one of the fetched series ({_list_preview(sorted(known))})", _short(sid))


def _check_weights_sum(run: _Run, key: str) -> None:
    w = run.doc.get(key)
    if not isinstance(w, Mapping) or not w:
        return
    vals = [_num(v) for v in w.values()]
    if any(v is None for v in vals):
        return
    total = sum(v for v in vals if v is not None)
    tol = run.vnum("sector_weight_sum_tolerance_pct")
    if abs(total - 100.0) > tol + 1e-9:
        run.err((key,), "weights must sum to 100", f"100 +/- {tol:g}", _fmt(total))


def _check_macro_bounds(run: _Run, targets_key: str) -> None:
    doc = run.doc
    mv = run.ctx_doc("macro_view")
    if mv is None:
        run.note("macro_view context not provided: macro-bound override checks skipped")
        return
    targets = doc.get(targets_key)
    if not isinstance(targets, Mapping):
        return
    cons: dict[str, Mapping] = {}
    for c in mv.get("sector_constraints") or []:
        if isinstance(c, Mapping) and isinstance(c.get("sector"), str):
            cons[c["sector"]] = c
    overrides: dict[tuple[str, str], tuple[int, Mapping]] = {}
    for i, o in enumerate(doc.get("constraint_overrides") or []):
        if isinstance(o, Mapping):
            overrides[(str(o.get("sector")), str(o.get("macro_bound")))] = (i, o)

    def target_of(sector: str) -> float:
        t = _num(targets.get(sector))
        return 0.0 if t is None else t

    eps = COPY_ABS_TOL
    for sector, c in cons.items():
        t = target_of(sector)
        for bound, key in (("min", "min_weight"), ("max", "max_weight")):
            b = _num(c.get(key))
            if b is None:
                continue
            outside = t < b - eps if bound == "min" else t > b + eps
            if outside and (sector, bound) not in overrides:
                op = ">=" if bound == "min" else "<="
                run.err((targets_key, sector), f"target is outside the macro {bound} bound without a "
                        "constraint_overrides entry",
                        f"{op} {_fmt(b)}, or a constraint_overrides entry {{sector: {sector}, macro_bound: {bound}, "
                        f"macro_value: {_fmt(b)}, cio_target: {_fmt(t)}, justification}}", _fmt(t))
    for (sector, bound), (i, o) in overrides.items():
        c = cons.get(sector)
        if c is None:
            run.err(("constraint_overrides", i, "sector"), "no macro_view sector constraint exists for this sector",
                    f"one of {_list_preview(cons) or 'none (macro_view has no constraints)'}", _short(sector))
            continue
        if bound not in ("min", "max"):
            continue
        b = _num(c.get(f"{bound}_weight"))
        if b is None:
            continue
        mvv = _num(o.get("macro_value"))
        if mvv is None or abs(mvv - b) > eps:
            run.err(("constraint_overrides", i, "macro_value"), f"does not match macro_view {bound}_weight for {sector}",
                    _fmt(b), _fmt(o.get("macro_value")))
        t = target_of(sector)
        ct = _num(o.get("cio_target"))
        if ct is None or abs(ct - t) > eps:
            run.err(("constraint_overrides", i, "cio_target"), f"does not match {targets_key}.{sector}", _fmt(t),
                    _fmt(o.get("cio_target")))
        outside = t < b - eps if bound == "min" else t > b + eps
        if not outside:
            side = "below" if bound == "min" else "above"
            run.err(("constraint_overrides", i), f"override not needed: the target is within the macro {bound} bound",
                    f"a target {side} {_fmt(b)} (or remove this entry)", _fmt(t))


def _check_cio_allocation(run: _Run) -> None:
    doc = run.doc
    for key in ("current_sector_weights", "target_sector_weights"):
        _check_weights_sum(run, key)
    expand = [s for s in (doc.get("expand_sectors") or []) if isinstance(s, str)]
    mx = run.p3("max_expand_sectors")
    if len(expand) > mx:
        run.err(("expand_sectors",), "too many expand sectors", f"<= {mx} (phase3.max_expand_sectors)",
                f"{len(expand)} ({', '.join(expand)})")
    both = sorted(set(expand) & {s for s in (doc.get("reduce_sectors") or []) if isinstance(s, str)})
    if both:
        run.err(("reduce_sectors",), "sectors listed in both expand_sectors and reduce_sectors", "disjoint lists",
                ", ".join(both))
    _check_macro_bounds(run, "target_sector_weights")
    lt = run.ctx_doc("lookthrough")
    cur = doc.get("current_sector_weights")
    if lt is None:
        run.note("lookthrough context not provided: current_sector_weights check skipped")
        return
    ltw = lt.get("sector_weights_pct")
    if not isinstance(ltw, Mapping) or not isinstance(cur, Mapping):
        return
    tol = run.vnum("current_weight_tolerance_pp")
    order = list(common.SECTOR_KEYS) + ["cash", "unclassified"]
    keys = sorted(set(ltw) | set(cur), key=lambda k: (order.index(k) if k in order else len(order), str(k)))
    for k in keys:
        a = _num(cur.get(k)) if k in cur else 0.0
        b = _num(ltw.get(k)) if k in ltw else 0.0
        if a is None or b is None:
            continue
        if abs(a - b) > tol + 1e-9:
            run.err(("current_sector_weights", k), "does not match lookthrough.json sector_weights_pct",
                    f"{_fmt(b)} (+/-{tol:g} pp)", _fmt(a) if k in cur else "missing (0)")


def _check_sector_verdict(run: _Run) -> None:
    doc = run.doc
    verdicts = [(i, v) for i, v in enumerate(doc.get("verdicts") or []) if isinstance(v, Mapping)]
    seen: set[str] = set()
    for i, v in verdicts:
        t = v.get("ticker")
        if isinstance(t, str):
            if t in seen:
                run.err(("verdicts", i, "ticker"), "duplicate verdict for ticker", "one verdict per ticker", _short(t))
            seen.add(t)
    buys = [str(v.get("ticker")) for _i, v in verdicts if v.get("action") == "BUY"]
    mx = run.p3("max_buys_per_sector")
    if len(buys) > mx:
        run.err(("verdicts",), "too many BUY verdicts for one sector", f"<= {mx} BUY (phase3.max_buys_per_sector)",
                f"{len(buys)} ({', '.join(buys)})")
    agg = run.ctx_doc("aggregate")
    if agg is None:
        run.note("aggregate context not provided: rule_action / aggregate value checks skipped")
        return
    tickers = agg.get("tickers") if isinstance(agg.get("tickers"), Mapping) else {}
    sector = doc.get("sector")
    in_sector = [t for t, a in tickers.items() if isinstance(a, Mapping) and a.get("sector") == sector]
    for i, v in verdicts:
        t = v.get("ticker")
        a = tickers.get(t) if isinstance(t, str) else None
        if not isinstance(a, Mapping):
            run.err(("verdicts", i, "ticker"), "ticker not found in aggregate.json",
                    f"one of {_list_preview(in_sector) or 'none (aggregate.json has no ' + str(sector) + ' tickers)'}",
                    _short(t))
            continue
        if sector and a.get("sector") and a.get("sector") != sector:
            run.err(("verdicts", i, "ticker"), "ticker belongs to another sector in aggregate.json",
                    _short(sector), _short(a.get("sector")))
        rr_obj = a.get("rr")
        agg_rr = rr_obj.get("rr") if isinstance(rr_obj, Mapping) else None
        for key, agg_val in (("kind", a.get("kind")), ("rule_action", a.get("rule_action")),
                             ("agg_conviction", a.get("agg_conviction")),
                             ("agg_evidence", a.get("agg_evidence")), ("rr", agg_rr)):
            if key in v and not _same(v.get(key), agg_val):
                run.err(("verdicts", i, key), f"does not match aggregate.json for {t}", _short(agg_val),
                        _short(v.get(key)))
        flags = a.get("flags") if isinstance(a.get("flags"), Mapping) else {}
        unresolved = [f.get("flag_id") for f in (flags.get("unresolved") or [])
                      if isinstance(f, Mapping) and f.get("flag_id")]
        known_flags = set(unresolved) | {f.get("flag_id") for f in (flags.get("all") or []) if isinstance(f, Mapping)}
        listed = v.get("unresolved_red_flags")
        if isinstance(listed, list):
            missing = [f for f in unresolved if f not in listed]
            if missing:
                run.err(("verdicts", i, "unresolved_red_flags"), "must list every unresolved flag from aggregate.json",
                        f"a list including {', '.join(missing)}", _short(listed))
            unknown = [f for f in listed if f not in known_flags]
            if unknown:
                run.err(("verdicts", i, "unresolved_red_flags"), "lists flags that are not in aggregate.json",
                        f"flag ids from aggregate.json ({_list_preview(sorted(known_flags)) or 'none'})",
                        _short(unknown))
    for t in in_sector:
        if t not in seen:
            run.err(("verdicts",), f"missing verdict for {t}",
                    f"one verdict for every {sector} ticker in aggregate.json ({_list_preview(in_sector)})",
                    f"no verdict for {t}")


def _check_cio_final(run: _Run) -> None:
    doc = run.doc
    _check_weights_sum(run, "final_target_sector_weights")
    if run.ctx.get("macro_view"):
        _check_macro_bounds(run, "final_target_sector_weights")
    vdocs = run.verdict_docs()
    if not vdocs:
        run.note("no sector verdict files in context: final action vs verdict checks skipped")
        return
    vmap: dict[str, tuple[Any, Any, str]] = {}
    for fname, vd in vdocs:
        for v in vd.get("verdicts") or []:
            if isinstance(v, Mapping) and _norm_t(v.get("ticker")):
                vmap[_norm_t(v.get("ticker")) or ""] = (v.get("action"), v.get("kind"), fname)
    min_words = int(run.vnum("min_justification_words"))
    seen: set[str] = set()
    for i, fa in enumerate(doc.get("final_actions") or []):
        if not isinstance(fa, Mapping):
            continue
        t = _norm_t(fa.get("ticker"))
        if t is None:
            continue
        if t in seen:
            run.err(("final_actions", i, "ticker"), "duplicate final action for ticker", "one entry per ticker",
                    _short(t))
        seen.add(t)
        sva, act, ov = fa.get("source_verdict_action"), fa.get("action"), fa.get("override")
        if t not in vmap:
            if sva is not None:
                run.err(("final_actions", i, "source_verdict_action"), "no sector verdict exists for this ticker",
                        "null", _short(sva))
            continue
        vact, _vkind, fname = vmap[t]
        if sva != vact:
            run.err(("final_actions", i, "source_verdict_action"), "does not match the sector verdict",
                    f"{_short(vact)} (from {fname})", _short(sva))
            if act != vact and ov is not True:
                run.err(("final_actions", i, "override"),
                        f"action {_short(act)} differs from the sector verdict {_short(vact)} ({fname}) without an override",
                        f"override true with an override_justification of >= {min_words} words, or action {_short(vact)}",
                        f"override {_short(ov)}")
        if ov is True and act == vact:
            run.err(("final_actions", i, "override"), "override is true but action equals the sector verdict action",
                    "override false (or a different action)", "override true")
    for t, (vact, vkind, fname) in vmap.items():
        if t not in seen and (vkind == "holding" or vact == "BUY"):
            run.err(("final_actions",), f"missing final action for {t}",
                    f"an entry for every holding and BUY candidate in the sector verdicts ({fname}: {t} {vact})",
                    "none")


_ROLE_CHECKS = {"fundamental": _check_fundamental, "technical": _check_technical}


def _semantic(run: _Run) -> None:
    doc = run.doc
    checks: list = [_check_identity, _check_word_limits, _check_counts, _check_quotes, _check_sources,
                    _check_null_figures, _check_data_refs]
    if run.schema in REPORT_SCHEMAS:
        checks.append(_check_red_flags)
    if run.schema == "specialist_report":
        role_check = _ROLE_CHECKS.get(str(doc.get("agent_role")))
        if role_check is not None:
            checks.append(role_check)
    checks += {
        "devils_advocate_report": [_check_devils_advocate],
        "etf_review": [_check_etf],
        "macro_view": [_check_macro_view],
        "cio_allocation": [_check_cio_allocation],
        "cio_final": [_check_cio_final],
        "sector_verdict": [_check_sector_verdict],
    }.get(run.schema, [])
    for check in checks:
        try:
            check(run)
        except Exception as exc:  # noqa: BLE001 - a semantic check must never crash validation
            run.note(f"semantic check {check.__name__.lstrip('_')} skipped ({type(exc).__name__}: {exc})")


# =====================================================================================
# Public API
# =====================================================================================


def validate_doc_report(doc: Any, schema_name: str | None = None, *,
                        context: Mapping[str, Any] | None = None) -> Report:
    """Validate ``doc`` against ``schema_name`` (inferred when None) plus semantic checks."""
    ctx = dict(context or {})
    if schema_name is None:
        schema_name = infer_schema(doc, ctx.get("_doc_path"))
        if schema_name is None:
            return Report(["$: cannot infer the schema; expected a document with 'schema' (data files) or "
                           "'agent_role' (agent outputs), or pass schema_name / --schema, got "
                           f"{_jtype(doc)} {_short(doc)}"])
    try:
        name = resolve_schema_name(schema_name)
        validator = get_validator(name)
    except KeyError:
        return Report([f"$: unknown schema; expected one of {_list_preview(sorted(common.SCHEMA_FILES), 40)}, "
                       f"got {_short(str(schema_name))}"], schema=str(schema_name))
    doc = common.to_jsonable(doc)
    run = _Run(doc, name, ctx)
    token = _MIN_WORDS.set(int(run.vnum("min_justification_words")))
    try:
        raw = list(validator.iter_errors(doc))
    finally:
        _MIN_WORDS.reset(token)
    if name in AGENT_DOC_SCHEMAS and isinstance(doc, dict):
        _semantic(run)
    min_words = int(run.vnum("min_justification_words"))
    formatted = sorted((_format_schema_error(e, min_words) for e in raw), key=lambda t: (t[0], t[2]))
    schema_msgs = [m for path, v, m in formatted if (path, v) not in run.suppress]
    errors = _dedupe(schema_msgs + run.errors)
    if len(errors) > MAX_ERRORS:
        extra = len(errors) - (MAX_ERRORS - 1)
        errors = errors[: MAX_ERRORS - 1] + [f"$: {extra} more errors not shown; expected fewer problems, "
                                             "got a document that needs a rewrite (fix the errors above first)"]
    return Report(errors, run.notes, name)


def validate_doc(doc: Any, schema_name: str | None, *, context: Mapping[str, Any] | None = None) -> list[str]:
    """Errors for ``doc`` (empty list = valid). See ``validate_doc_report``."""
    return validate_doc_report(doc, schema_name, context=context).errors


def validate_file_report(path: str | Path, schema_name: str | None = None, *,
                         context: Mapping[str, Any] | None = None) -> Report:
    """Validate the JSON file at ``path`` (missing / unparsable files are errors)."""
    p = Path(str(path))
    ctx = dict(context or {})
    ctx.setdefault("_doc_path", str(p))
    if not p.is_file():
        return Report([f"{p.as_posix()}: file not found; expected exactly one JSON file written at this path, "
                       "got no file"], schema=schema_name)
    try:
        doc = common.read_json(p)
    except json.JSONDecodeError as exc:
        return Report([f"{p.as_posix()}: invalid JSON at line {exc.lineno} column {exc.colno} ({exc.msg}); "
                       "expected one complete JSON object, got unparsable text"], schema=schema_name)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Report([f"{p.as_posix()}: unreadable file ({exc}); expected a UTF-8 JSON file, got an error"],
                      schema=schema_name)
    return validate_doc_report(doc, schema_name, context=ctx)


def validate_file(path: str | Path, schema_name: str | None = None, *,
                  context: Mapping[str, Any] | None = None) -> list[str]:
    """Errors for the JSON file at ``path`` (empty list = valid)."""
    return validate_file_report(path, schema_name, context=context).errors


# ---- work orders ---------------------------------------------------------------------


def parse_order_md(path: str | Path) -> dict:
    """YAML front matter (between the leading ``---`` lines) of a work-order markdown file.
    Uses the repo's YAML loader (dates stay strings, tickers like ON stay strings).
    Raises ValueError on a missing/unterminated/non-mapping front matter."""
    text = common.read_text(path)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{Path(str(path)).as_posix()}: no YAML front matter (expected a first line '---')")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        raise ValueError(f"{Path(str(path)).as_posix()}: unterminated YAML front matter (no closing '---')")
    loader = getattr(common, "_YamlLoader", yaml.SafeLoader)
    if not (isinstance(loader, type) and issubclass(loader, yaml.SafeLoader)):
        loader = yaml.SafeLoader  # never construct arbitrary Python objects
    try:
        data = yaml.load("\n".join(lines[1:end]), Loader=loader)  # noqa: S506 - SafeLoader subclass only
    except yaml.YAMLError as exc:
        raise ValueError(f"{Path(str(path)).as_posix()}: invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{Path(str(path)).as_posix()}: front matter is not a mapping")
    return common.to_jsonable(data)


def _input_path(item: Any) -> str | None:
    if isinstance(item, Mapping):
        p = item.get("path")
        return str(p) if p else None
    return str(item) if item else None


_PER_TICKER_INPUT_RE = re.compile(r"(technicals|fundamentals|valuation)_(.+)\.json")


def _order_input_files(order: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """``{kind: [(file ticker, path), ...]}`` of the per-ticker data files in an order's inputs."""
    out: dict[str, list[tuple[str, str]]] = {"technicals": [], "fundamentals": [], "valuation": []}
    for item in order.get("inputs") or []:
        p = _input_path(item)
        if not p:
            continue
        m = _PER_TICKER_INPUT_RE.fullmatch(Path(p.replace("\\", "/")).name)
        if m:
            out[m.group(1)].append((m.group(2), p))
    return out


def build_order_context(order: Mapping[str, Any]) -> dict:
    """Validation context for an order's output: data/context files detected by name among
    ``inputs`` (technicals_/fundamentals_/valuation_<T>.json for the order's ticker,
    aggregate.json, macro.json, lookthrough.json, macro_view.json, cio_allocation.json,
    prices.json, *_verdict.json), committee cfg via ``common.committee_cfg()``, the run dir,
    strict data_ref resolution, identity expectations, word limits and seed sources.

    Per-ticker kinds: the file whose ticker equals the order ticker, else the order's only file
    of that kind (a proxy-priced fund's technicals_QQQ.json for order ticker 1186063; recorded in
    ``sole_context`` so the ticker check accepts it), else nothing (several files, none for the
    order ticker: the checks are skipped with a note)."""
    ctx: dict[str, Any] = {
        "strict_refs": True,
        "verdicts": [],
        "word_limits": dict(order.get("word_limits") or {}),
        "seed_sources": list(order.get("seed_sources") or []),
        "expect": {k: order.get(k) for k in ("order_id", "run_id", "agent_role", "ticker", "sector", "mode")},
    }
    run_dir = _run_dir_of(order.get("output"))
    if run_dir is None and order.get("run_id"):
        cand = common.runs_dir() / str(order["run_id"])
        run_dir = cand if cand.is_dir() else None
    if run_dir is not None:
        ctx["run_dir"] = str(run_dir)
    ticker = _norm_t(order.get("ticker"))
    per_ticker = _order_input_files(order)
    ctx["sole_context"] = [kind for kind, items in per_ticker.items() if len(items) == 1]
    light: str | None = None
    for item in order.get("inputs") or []:
        p = _input_path(item)
        if not p:
            continue
        name = Path(p.replace("\\", "/")).name
        if _PER_TICKER_INPUT_RE.fullmatch(name):
            continue
        if name == "aggregate.json":
            ctx["aggregate"] = p
        elif name == "aggregate_light.json":
            light = p
        elif name in ("macro.json", "lookthrough.json", "macro_view.json", "cio_allocation.json", "prices.json"):
            ctx[name[: -len(".json")]] = p
        elif name.endswith("_verdict.json"):
            ctx["verdicts"].append(p)
    for kind, items in per_ticker.items():
        chosen = [p for t, p in items if ticker and _norm_t(t) == ticker] or (
            [items[0][1]] if len(items) == 1 else [])
        if chosen:
            ctx[kind] = chosen[0]
    if "aggregate" not in ctx and light:
        ctx["aggregate"] = light
    try:
        ctx["committee_cfg"] = common.committee_cfg()
    except common.ConfigError:
        pass
    return ctx


def _order_schema_name(order: Mapping[str, Any]) -> str | None:
    if order.get("schema"):
        try:
            return resolve_schema_name(order["schema"])
        except KeyError:
            pass
    role = order.get("agent_role")
    if role:
        try:
            return common.role_schema(str(role), order.get("mode"))
        except KeyError:
            return None
    return None


def validate_order_output_report(order: Mapping[str, Any] | str | Path, path: str | Path | None = None, *,
                                 schema_name: str | None = None,
                                 context: Mapping[str, Any] | None = None) -> Report:
    """Validate the output of a work order (``order`` = parsed front matter or the .md path).
    ``path`` defaults to ``order['output']`` (pass the ``.r2.json`` path for retries)."""
    if not isinstance(order, Mapping):
        try:
            order = parse_order_md(order)
        except (ValueError, OSError) as exc:
            return Report([f"{Path(str(order)).as_posix()}: cannot read work order ({exc}); expected a work "
                           "order .md with YAML front matter, got an unreadable file"])
    target = path or order.get("output")
    if not target:
        return Report(["$: the work order has no output path; expected an 'output' key, got none"])
    ctx = build_order_context(order)
    ctx.update(dict(context or {}))
    name = schema_name or _order_schema_name(order)
    return validate_file_report(target, name, context=ctx)


def validate_order_output(order: Mapping[str, Any] | str | Path, path: str | Path | None = None, *,
                          schema_name: str | None = None, context: Mapping[str, Any] | None = None) -> list[str]:
    """Errors for an order's output file (empty list = valid). Context is built from the
    order's inputs (``build_order_context``)."""
    return validate_order_output_report(order, path, schema_name=schema_name, context=context).errors


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate an agent output (or data file) against its JSON schema plus semantic checks. "
                    "Exit 0 = valid, 1 = invalid (errors printed one per line).")
    parser.add_argument("--file", help="JSON file to validate (default with --order: the order's output)")
    parser.add_argument("--schema", help="schema name (e.g. specialist_report, sector_verdict, prices); "
                                         "inferred from the document when omitted")
    parser.add_argument("--order", help="work order .md: context (data files, expectations) comes from its inputs")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    common.apply_common_args(args)
    if not args.file and not args.order:
        parser.error("give --file and/or --order")
    if args.order:
        rep = validate_order_output_report(args.order, args.file, schema_name=args.schema)
        target = args.file or "(order output)"
    else:
        rep = validate_file_report(args.file, args.schema)
        target = args.file
    for msg in rep.errors:
        print(msg)
    for note in rep.notes:
        print(f"note: {note}", file=sys.stderr)
    if rep.ok:
        print(f"OK {target} ({rep.schema})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
