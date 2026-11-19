"""render_memo.py — the Investment Committee memo as Markdown + self-contained HTML (DESIGN §4.14).

Pipeline:
* ``build_context(run_dir)`` reads every run file *optionally* (manifest, phase 1–4 JSON, agent
  reports located through ``manifest["orders"]`` with a directory scan as fallback, evidence,
  aggregates, verdicts, data envelopes). Missing or unreadable inputs never crash — the part of
  the memo that needs them renders "Not available in this run".
* ``build_memo(ctx, mode=..., ticker=...)`` turns the context into the 11 memo sections (exact
  headings in ``SECTION_TITLES``), each a list of simple blocks: ``para``, ``subheading``,
  ``bullets``, ``kv``, ``table`` and ``card``. All number formatting (fixed decimals, as-of
  dates) happens here, so both formats show identical figures.
* ``render(run_dir, ...)`` renders ``templates/memo.md.j2`` (autoescape off; table cells escaped
  for ``|`` and newlines) and ``templates/memo.html.j2`` (``autoescape=True``, inline CSS, light
  and dark via ``prefers-color-scheme``, print styles, no scripts, no external assets).

Trial mode (``/committee-holding``) renders sections 1, 5, 8, 9, 10, 11 for one ticker and
writes ``phase4/trial_<T>.md/.html``; full mode writes ``phase4/memo.md/.html``.

CLI: ``python scripts/render_memo.py --run-dir runs/<id> [--mode full|trial] [--ticker T]
[--out-md P] [--out-html P]`` (or ``--run <id>`` under the runs dir). Prints
``{"memo_md": ..., "memo_html": ...}``. Decision support only — never advice.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from collections import OrderedDict  # noqa: E402
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation  # noqa: E402
from typing import Any, Callable, Iterable, Mapping  # noqa: E402

import jinja2  # noqa: E402
from markupsafe import Markup, escape  # noqa: E402

# =====================================================================================
# Constants
# =====================================================================================

NA_TEXT = "Not available in this run"

#: Exact section headings (both formats, DESIGN §4.14).
SECTION_TITLES: "OrderedDict[int, str]" = OrderedDict([
    (1, "1. Disclaimer"),
    (2, "2. Executive Summary"),
    (3, "3. Macro Regime, Scenarios & Sector Constraints"),
    (4, "4. Current vs Target Portfolio"),
    (5, "5. Holding Verdicts"),
    (6, "6. New Proposals"),
    (7, "7. Proposed Rebalancing Plan"),
    (8, "8. Devil's Advocate Highlights & Dissents"),
    (9, "9. Overrides & Rule Deviations"),
    (10, "10. Data Gaps & Low-Evidence Warnings"),
    (11, "11. Appendix: Sources"),
])
FULL_SECTIONS: tuple[int, ...] = tuple(SECTION_TITLES)
TRIAL_SECTIONS: tuple[int, ...] = (1, 5, 8, 9, 10, 11)
MODES = ("full", "trial")

MD_TEMPLATE = "memo.md.j2"
HTML_TEMPLATE = "memo.html.j2"

# Fallbacks used ONLY when committee.yaml / investor_profile.yaml cannot be read (the live
# values always come from config: actions.min_evidence, aggregation.round_dp,
# risk_reward.round_dp, investor_profile.capital_gains_tax_pct / base_currency).
DEFAULT_MIN_EVIDENCE = 5.0
DEFAULT_SCORE_DP = 1
DEFAULT_RR_DP = 2
DEFAULT_CAPITAL_GAINS_TAX_PCT = 25.0
DEFAULT_BASE_CURRENCY = "ILS"

#: Presentation-only limit for the "one-line why" column.
WHY_MAX_WORDS = 28

#: Cell text of a negative risk/reward (base fair value below the price): the ratio is not
#: meaningful, so the raw number moves to a tooltip (HTML) and a footnote (both formats).
RR_NOT_MEANINGFUL = "n/m (FV below price)"
#: What each ``rr.null_reason`` code means (shown in a footnote when the code occurs).
RR_NULL_LEGEND: dict[str, str] = {
    "not_applicable": "no R/R test for this asset type (funds)",
    "missing_input": "the price, base fair value or invalidation price was missing",
    "price_below_invalidation": "the price is at or below the invalidation (stop) price",
    "stop_too_tight": "the stop is too close to the price for a meaningful ratio",
}
#: Position flags (look-through classification, price conversion) and what they mean.
FLAG_LEGEND: dict[str, str] = {
    "conglomerate": "diversified holding company: judged as an insurance/holding company (float, "
                    "look-through earnings, capital allocation), so plain DCF and single-sector multiples "
                    "are less reliable",
    "proxy_priced": "priced and decomposed through a US proxy ticker, not through the listed unit itself",
    "converted_to_usd": "quote converted from its native currency to USD at spot USD/ILS "
                        "(native price in prices.json)",
    "unclassified": "sector could not be classified",
}
#: Envelope flag codes of the valuation files that Section 10 always surfaces (even when low severity).
VALUATION_CAVEAT_CODES = ("multiple_bands_are_assumptions", "dcf_financials_caution")
FULL_EXIT_SUFFIX = " (full exit)"
#: Line break inside a table cell (rendered as ``<br>``). The labelled parts of a Why cell
#: ("Call: ...", "Why: ...") are joined with it; everything else in a cell stays one line.
CELL_BREAK = "\u2028"
CELL_LABELS = ("Call", "Why", "Watch", "Model caveat", "Note")
_CELL_LABEL_RE = re.compile(r"^(" + "|".join(CELL_LABELS) + r"): (.*)$", re.DOTALL)

SECTOR_ORDER: tuple[str, ...] = tuple(common.SECTOR_KEYS) + ("funds", "cash", "unclassified")
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

#: HTML badge classes (case-sensitive: action PASS vs constraint status "pass").
BADGE_CLASSES: dict[str, str] = {
    "BUY": "b-pos", "ADD": "b-pos", "HOLD": "b-neutral", "TRIM": "b-warn", "SELL": "b-neg",
    "PASS": "b-mute", "NONE": "b-mute",
    "pass": "b-pos", "warn": "b-warn", "fail": "b-neg",
    "survives": "b-pos", "damaged": "b-warn", "broken": "b-neg",
    "ok": "b-pos", "failed": "b-neg", "missing": "b-neg",
    "high": "b-neg", "medium": "b-warn", "low": "b-mute",
    "bullish": "b-pos", "neutral": "b-neutral", "bearish": "b-neg",
    "yes": "b-neg", "no": "b-mute",
}

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
_PHASE_RE = re.compile(r"(?:^|/)(phase[1-4]/)")
_VARIANT_RE = re.compile(r"(\.r\d+)?(\.failed)?\.json$", re.IGNORECASE)
_DATA_NAME_RE = re.compile(r"^([a-z]+)_(.+)\.json$")
_TAG_LIKE_RE = re.compile(r"<(?=[A-Za-z/!?])")
_NEWLINES_RE = re.compile(r"\s*(?:\r\n|\r|\n)\s*")
#: Markdown block-level markers at the start of a line (ATX heading, blockquote, list item,
#: ordered list item, thematic break / setext underline, code fence). Agent text starting with
#: one is backslash-escaped so it can never create memo structure (e.g. a fake "## 5." heading).
_BLOCK_START_RE = re.compile(
    r"^(#{1,6}(?=\s|$)|>|[-+*](?=\s|$)|\d{1,9}[.)](?=\s|$)|(?:[-*_=]\s*){3,}$|=+\s*$|`{3,}|~{3,})")

# =====================================================================================
# Formatting helpers (also registered as Jinja filters)
# =====================================================================================


def _num(x: Any) -> float | None:
    return common.fact_value(x) if isinstance(x, Mapping) else common.safe_float(x)


def _quantize(v: float, dp: int) -> Decimal:
    """ROUND_HALF_UP to ``dp`` decimals (same rounding rule as aggregate.py); never ``-0``."""
    try:
        d = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-int(dp)), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        d = Decimal(str(round(float(v), int(dp))))
    return abs(d) if d == 0 else d


def fmt_num(x: Any, dp: int = 2, *, sign: bool = False, na: str = "n/a") -> str:
    """Fixed-decimal number with thousands separators (ROUND_HALF_UP); ``n/a`` for None/NaN."""
    v = _num(x)
    if v is None:
        return na
    d = _quantize(v, dp)
    return f"{d:+,.{dp}f}" if sign else f"{d:,.{dp}f}"


def fmt_threshold(x: Any) -> str:
    """A config threshold as written (``5``, ``9.5``, ``25``)."""
    v = _num(x)
    return "n/a" if v is None else f"{v:g}"


def fmt_pct(x: Any, dp: int = 1, *, sign: bool = False) -> str:
    s = fmt_num(x, dp, sign=sign)
    return s if s == "n/a" else s + "%"


def fmt_pp(x: Any, dp: int = 1) -> str:
    s = fmt_num(x, dp, sign=True)
    return s if s == "n/a" else s + " pp"


def fmt_money(x: Any, prefix: str = "$", dp: int = 0) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    d = _quantize(v, dp)
    return ("-" if d < 0 else "") + prefix + f"{abs(d):,.{dp}f}"


def fmt_usd(x: Any, dp: int = 0) -> str:
    return fmt_money(x, "$", dp)


def fmt_ils(x: Any, dp: int = 0) -> str:
    return fmt_money(x, "ILS ", dp)


def fmt_shares(x: Any) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _fmt_unit(v: float, unit: str, dp: int | None) -> str:
    u = unit.strip().lower()
    if u == "pct":
        return fmt_pct(v, 2 if dp is None else dp)
    if u == "pp":
        return fmt_pp(v, 2 if dp is None else dp)
    if u == "usd":
        return fmt_usd(v, 0 if dp is None else dp)
    if u == "usd/share":
        return fmt_usd(v, 2 if dp is None else dp)
    if u == "x":
        return fmt_num(v, 1 if dp is None else dp) + "x"
    if u == "shares":
        return fmt_shares(v)
    if u in ("", "index", "number"):
        return fmt_num(v, 2 if dp is None else dp)
    return f"{fmt_num(v, 2 if dp is None else dp)} {unit}"


def fmt_fact(f: Any, dp: int | None = None, *, asof: bool = True) -> str:
    """Fact → value with unit + ``(as of YYYY-MM-DD)``; null Fact → ``n/a (<na_reason>)``."""
    if not isinstance(f, Mapping):
        v = common.safe_float(f)
        return "n/a" if v is None else fmt_num(v, 2 if dp is None else dp)
    v = common.safe_float(f.get("value"))
    if v is None:
        s = f"n/a ({f.get('na_reason') or 'missing'})"
    else:
        s = _fmt_unit(v, str(f.get("unit") or ""), dp)
    as_of = f.get("as_of")
    if asof and as_of:
        s += f" (as of {as_of})"
    return s


def _escape_block_start(m: "re.Match[str]") -> str:
    tok = m.group(1)
    if tok[0].isdigit():  # "1." / "1)": only the punctuation is escapable in CommonMark
        return tok[:-1] + "\\" + tok[-1]
    return "\\" + tok


def md_text(s: Any) -> str:
    """Markdown-safe inline text: newlines collapsed, tag-like ``<`` neutralized, and a leading
    block marker (``#``, ``>``, ``-``/``+``/``*`` list, ``1.``, ``---``, code fence)
    backslash-escaped so agent text cannot inject headings or other structure."""
    if s is None:
        return ""
    t = _NEWLINES_RE.sub(" ", str(s)).strip()
    t = _BLOCK_START_RE.sub(_escape_block_start, t, count=1)
    return _TAG_LIKE_RE.sub("&lt;", t)


def md_cell(s: Any) -> str:
    """Markdown table cell: ``md_text`` + escaped pipes. ``CELL_BREAK`` splits a cell into lines
    joined with ``<br>``; a leading ``Call:`` / ``Why:`` / ... label of a line is bolded."""
    if s is None or CELL_BREAK not in str(s):
        return md_text(s).replace("|", "\\|")
    lines = []
    for part in str(s).split(CELL_BREAK):
        m = _CELL_LABEL_RE.match(part)
        body = md_text(m.group(2) if m else part).replace("|", "\\|")
        lines.append(f"**{m.group(1)}:** {body}" if m else body)
    return "<br>".join(lines)


def md_row(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(md_cell(c) for c in cells) + " |"


def md_sep(align: Any, n: int) -> str:
    al = list(align or [])
    parts = []
    for i in range(int(n)):
        a = al[i] if i < len(al) else "l"
        parts.append("---:" if a == "r" else ("---" if a != "c" else ":---:"))
    return "| " + " | ".join(parts) + " |"


def safe_url(u: Any) -> str:
    """The URL if it is a plain http(s) URL, else ``""`` (never linkify anything else)."""
    s = str(u or "").strip()
    return s if _URL_RE.match(s) else ""


def badge_class(cell: Any) -> str:
    if cell is None:
        return ""
    text = str(cell).strip()
    if text.endswith(FULL_EXIT_SUFFIX):
        text = text[: -len(FULL_EXIT_SUFFIX)].strip()
    if "->" in text:                      # "TRIM -> HOLD (trim < 1 share)": coloured by the effective action
        text = text.split("->")[-1].strip()
    if " (" in text:
        text = text.split(" (")[0].strip()
    return BADGE_CLASSES.get(text, "")


def align_class(a: Any) -> str:
    return "num" if a == "r" else ("wide" if a == "w" else "")


def cell_html(c: Any) -> Markup:
    """HTML of a table cell: escaped text; ``CELL_BREAK`` becomes ``<br>`` and a leading
    ``Call:`` / ``Why:`` / ... label is bold."""
    parts = _s(c).split(CELL_BREAK)
    if len(parts) == 1:
        return escape(_s(c))
    out = []
    for part in parts:
        m = _CELL_LABEL_RE.match(part)
        out.append(Markup("<strong>{}:</strong> {}").format(m.group(1), m.group(2)) if m else escape(part))
    return Markup("<br>").join(out)


def _words(s: Any) -> list[str]:
    return str(s or "").split()


#: Tokens ending in "." that do not end a sentence (compared lower-case, trailing "." removed).
_ABBREVIATIONS = frozenset({
    "vs", "v", "etc", "e.g", "i.e", "cf", "approx", "est", "inc", "corp", "co", "ltd", "plc", "llc",
    "no", "nos", "mr", "mrs", "ms", "dr", "st", "jr", "sr", "fig", "vol", "yr", "yrs", "mo", "mos",
    "q", "fy", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "u.s", "u.k", "e.u", "n.a", "a.k.a", "incl", "excl", "avg", "min", "max", "bn", "mn", "mln", "bln",
})
_DOTTED_ABBREV_RE = re.compile(r"^\(?(?:[A-Za-z]\.){2,}$")


def _is_abbrev(word: str) -> bool:
    w = word.strip("\"'()[]")
    return (w[:-1].lower() in _ABBREVIATIONS or bool(_DOTTED_ABBREV_RE.match(w))
            or (len(w) == 2 and w[0].isupper()))  # an initial ("J. Smith")


def one_line(text: Any, max_words: int = WHY_MAX_WORDS) -> str:
    """First sentence of ``text`` (≤ ``max_words`` words, ellipsis when cut)."""
    words = _words(text)
    if not words:
        return ""
    cut = len(words)
    for i, w in enumerate(words):
        if w[-1] not in ".!?":
            continue
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None:
            break
        if w[-1] == "." and (_is_abbrev(w) or not (nxt[0].isupper() or nxt[0].isdigit() or nxt[0] in "\"'(")):
            continue  # "vs.", "U.S.", "Inc." or a lowercase continuation: not a sentence end
        if w[-1] in "!?" and nxt[0].islower():
            continue
        cut = i + 1
        break
    s = " ".join(words[:cut])
    w = words[:cut]
    if len(w) > max_words:
        s = " ".join(w[:max_words]) + "…"
    return s


def _sector_name(key: Any) -> str:
    return common.sector_display(str(key)) if key else "—"


def _norm_t(t: Any) -> str:
    try:
        return common.norm_ticker(t)
    except (ValueError, TypeError, AttributeError):
        return str(t or "").strip().upper()


def _d(x: Any) -> dict | None:
    return x if isinstance(x, dict) else None


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _m(doc: Any, key: str, where: str | None = None, errors: list[str] | None = None) -> Mapping:
    """``doc[key]`` when it is an object, else ``{}``; a present-but-wrong-typed value is recorded
    in ``errors`` (as ``"<where>: '<key>' is not an object (<type>)"``) instead of crashing."""
    if not isinstance(doc, Mapping):
        return {}
    v = doc.get(key)
    if isinstance(v, Mapping):
        return v
    if v is not None and errors is not None and where:
        msg = f"{where}: '{key}' is not an object ({type(v).__name__}); ignored"
        if msg not in errors:
            errors.append(msg)
    return {}


def _s(x: Any) -> str:
    return "" if x is None else str(x)


# =====================================================================================
# Blocks (the view model both templates render)
# =====================================================================================


def _p(text: Any, style: str = "normal") -> dict:
    return {"type": "para", "text": _s(text), "style": style}


def _na(text: str = NA_TEXT) -> dict:
    return _p(text, "muted")


def _sub(text: Any) -> dict:
    return {"type": "subheading", "text": _s(text)}


def _bul(items: Iterable[Any]) -> dict:
    return {"type": "bullets", "entries": [_s(i) for i in items]}


def _kv(pairs: Iterable[tuple[Any, Any]]) -> dict:
    return {"type": "kv", "entries": [(_s(k), _s(v) if v not in (None, "") else "—") for k, v in pairs]}


def _tbl(headers: list[str], rows: list[list[Any]], *, align: str = "", caption: str | None = None,
         badges: Iterable[int] = (), links: Iterable[int] = (),
         titles: Mapping[tuple[int, int], str] | None = None) -> dict:
    """Table block. ``align`` is a string of ``l``/``r`` per column (``r`` = numeric). ``titles``
    maps (row index, column index) to a tooltip (``title=`` in HTML; Markdown ignores it, so
    anything a reader must see is also put in a footnote)."""
    al = list(align) + ["l"] * max(0, len(headers) - len(align))
    clean = [["—" if c is None or c == "" else _s(c) for c in r] for r in rows]
    return {"type": "table", "headers": [_s(h) for h in headers], "rows": clean, "align": al[:len(headers)],
            "caption": caption, "badges": list(badges), "links": list(links),
            "titles": {f"{r}:{c}": _s(t) for (r, c), t in (titles or {}).items() if t}}


def _card(title: Any, blocks: list[dict]) -> dict:
    return {"type": "card", "title": _s(title), "blocks": blocks}


# =====================================================================================
# Reading the run directory
# =====================================================================================


def _phase_tail(path: Any) -> str | None:
    """``.../runs/<id>/phase2/reports/x.json`` → ``phase2/reports/x.json`` (rightmost phase dir)."""
    s = str(path or "").replace("\\", "/")
    ms = list(_PHASE_RE.finditer(s))
    return s[ms[-1].start(1):] if ms else None


def _norm_key(path: Any) -> str:
    s = str(path or "").replace("\\", "/")
    return _phase_tail(s) or s.rsplit("/", 1)[-1]


class _Loader:
    """Cached, never-raising JSON reader; unreadable files are recorded in ``errors``."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.errors: list[str] = []
        self._cache: dict[str, Any] = {}

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.run_dir.resolve()).as_posix()
        except (ValueError, OSError):
            return _phase_tail(p) or p.as_posix()

    def load(self, path: Path | None) -> Any:
        if path is None:
            return None
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        doc = None
        try:
            if path.is_file():
                doc = common.read_json(path)
        except (OSError, ValueError) as exc:
            self.errors.append(f"{self.rel(path)}: unreadable ({exc.__class__.__name__})")
            doc = None
        self._cache[key] = doc
        return doc


def _resolve(run_dir: Path, raw: Any) -> Path | None:
    """A manifest path (absolute, run-relative or from another machine) → local Path."""
    if not raw:
        return None
    s = str(raw).strip().replace("\\", "/")
    p = Path(s)
    cands: list[Path] = [p] if p.is_absolute() else [run_dir / s]
    tail = _phase_tail(s)
    if tail:
        cands.append(run_dir / tail)
    if not p.is_absolute():
        cands.append(common.REPO_ROOT / s)
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return run_dir / tail if tail else cands[0]


def _stem(name: str) -> str:
    return _VARIANT_RE.sub("", name)


def _pick_variant(path: Path) -> Path:
    """Resolved report among ``X.failed.json`` > ``X.r2.json`` > ``X.json`` (first existing)."""
    stem = _stem(path.name)
    for cand in (path.with_name(stem + ".failed.json"), path.with_name(stem + ".r2.json"),
                 path.with_name(stem + ".json")):
        if cand.is_file():
            return cand
    return path


def _split_role_ticker(stem: str) -> tuple[str | None, str | None]:
    for role in sorted(common.ROLE_KEYS, key=len, reverse=True):
        if stem.startswith(role + "_"):
            return role, stem[len(role) + 1:]
    return None, None


def _phase_of(path: Any, default: int | None = None) -> int | None:
    tail = _phase_tail(path)
    if tail:
        return int(tail[5])
    return default


def _report_key(phase: int | None, role: str | None, ticker: str | None, sector: str | None,
                mode: str | None) -> tuple:
    if role == "sector_head":
        subject = sector or "-"
    elif role == "cio":
        subject = mode or ("reconciliation" if phase == 4 else "allocation")
    elif role == "macro":
        subject = "-"
    else:
        subject = ticker or "-"
    return (phase, role, subject)


def _make_rec(loader: _Loader, path: Path | None, *, role: str | None, ticker: str | None,
              sector: str | None, phase: int | None, mode: str | None = None,
              order_id: str | None = None, attempt: Any = None, order_status: str | None = None,
              order_errors: Any = None) -> dict:
    doc = loader.load(path) if path is not None else None
    doc = doc if isinstance(doc, dict) else None
    failed = bool(doc and str(doc.get("status") or "") == "failed") or (
        path is not None and path.name.lower().endswith(".failed.json") and doc is not None)
    if doc:
        role = doc.get("agent_role") or role
        ticker = doc.get("ticker") or ticker
        sector = doc.get("sector") or sector
        mode = doc.get("mode") or mode
    if failed:
        status = "failed"
    elif doc is not None:
        status = "ok"
    elif order_status == "failed":
        status = "failed"
    else:
        status = "missing"
    ticker = _norm_t(ticker) if ticker else None
    if phase is None and path is not None:
        phase = _phase_of(loader.rel(path))
    rec = {
        "path": path, "rel": loader.rel(path) if path is not None else None,
        "role": role, "ticker": ticker, "sector": sector, "phase": phase, "mode": mode,
        "order_id": order_id or (doc or {}).get("order_id"), "attempt": attempt,
        "status": status, "doc": doc if status == "ok" else None,
        "stub": doc if status == "failed" else None,
        "order_errors": [str(e) for e in _l(order_errors)],
        "order_status": order_status,
    }
    rec["key"] = _report_key(phase, role, ticker, sector, mode)
    return rec


def _rec_rank(rec: dict) -> tuple:
    exists = rec["status"] in ("ok", "failed")
    try:
        attempt = int(rec.get("attempt") or 0)
    except (TypeError, ValueError):
        attempt = 0
    return (exists, rec["status"] == "ok", attempt)


def _locate_reports(run_dir: Path, loader: _Loader, manifest: Mapping) -> list[dict]:
    """Every agent output of the run: manifest orders first (``resolved_output`` → ``output``
    variants), then a directory scan for anything the manifest does not list."""
    index: "OrderedDict[tuple, dict]" = OrderedDict()

    def add(rec: dict) -> None:
        old = index.get(rec["key"])
        if old is None or _rec_rank(rec) > _rec_rank(old):
            index[rec["key"]] = rec

    orders = manifest.get("orders") if isinstance(manifest, Mapping) else None
    if isinstance(orders, Mapping):
        for oid, o in orders.items():
            if not isinstance(o, Mapping):
                continue
            role = o.get("agent_role")
            if role not in common.ROLE_KEYS:
                continue
            path = _resolve(run_dir, o.get("resolved_output"))
            if path is None or not path.is_file():
                out = _resolve(run_dir, o.get("output"))
                path = _pick_variant(out) if out is not None else path
            phase = o.get("phase")
            try:
                phase = int(phase) if phase is not None else None
            except (TypeError, ValueError):
                phase = None
            if phase is None:
                m = re.match(r"^p([1-4])", str(oid))
                phase = int(m.group(1)) if m else _phase_of(path)
            add(_make_rec(loader, path, role=role, ticker=o.get("ticker"), sector=o.get("sector"),
                          phase=phase, mode=o.get("mode"), order_id=str(oid), attempt=o.get("attempt"),
                          order_status=o.get("status"), order_errors=o.get("errors")))

    def scan_add(path: Path, **kw: Any) -> None:
        rec = _make_rec(loader, _pick_variant(path), **kw)
        if rec["key"] not in index:
            index[rec["key"]] = rec

    fixed = ((1, "macro", None, "phase1", "macro_view"), (1, "cio", "allocation", "phase1", "cio_allocation"),
             (4, "cio", "reconciliation", "phase4", "cio_final"))
    for phase, role, mode, folder, stem in fixed:
        d = run_dir / folder
        if any(d.glob(f"{stem}*.json")):
            scan_add(d / f"{stem}.json", role=role, ticker=None, sector=None, phase=phase, mode=mode)
    for phase in (2, 3):
        d = run_dir / f"phase{phase}"
        if d.is_dir():
            for stem in sorted({_stem(f.name) for f in d.glob("*_verdict*.json")}):
                sector = stem[: -len("_verdict")] if stem.endswith("_verdict") else stem
                scan_add(d / f"{stem}.json", role="sector_head", ticker=None, sector=sector, phase=phase)
        rd = d / "reports"
        if rd.is_dir():
            for stem in sorted({_stem(f.name) for f in rd.glob("*.json")}):
                role, ticker = _split_role_ticker(stem)
                if role is None:
                    continue
                scan_add(rd / f"{stem}.json", role=role, ticker=ticker, sector=None, phase=phase)
    return list(index.values())


def _load_cfg(run_dir: Path) -> dict:
    snap = run_dir / "inputs" / "committee.yaml"
    try:
        if snap.is_file():
            data = common.load_yaml(snap)
            if isinstance(data, dict):
                return data
    except common.AginvError:
        pass
    try:
        return common.committee_cfg()
    except (common.AginvError, OSError, ValueError):
        return {}


def _load_profile(run_dir: Path) -> dict:
    snap = run_dir / "inputs" / "investor_profile.yaml"
    try:
        if snap.is_file():
            data = common.load_yaml(snap)
            if isinstance(data, dict):
                return data
    except common.AginvError:
        pass
    try:
        data = common.load_config("investor_profile")
        return data if isinstance(data, dict) else {}
    except (common.AginvError, OSError, ValueError):
        return {}


def _load_portfolio(run_dir: Path) -> "OrderedDict[str, dict]":
    """Holdings of the run's ``inputs/portfolio.yaml`` snapshot keyed by normalized ticker (empty
    when the snapshot is absent or unreadable — never the live config, which may have changed)."""
    out: "OrderedDict[str, dict]" = OrderedDict()
    snap = run_dir / "inputs" / "portfolio.yaml"
    try:
        data = common.load_yaml(snap) if snap.is_file() else None
    except common.AginvError:
        data = None
    for h in _l(data.get("holdings") if isinstance(data, dict) else None):
        if isinstance(h, Mapping) and h.get("ticker"):
            out.setdefault(_norm_t(h["ticker"]), dict(h))
    return out


def _position_flags(manifest: Mapping, lookthrough: dict | None, plan: dict | None,
                    prices: dict | None) -> "OrderedDict[str, list[str]]":
    """ticker -> classification / price flags (manifest holdings, look-through, plan positions) plus
    ``converted_to_usd`` for tickers whose prices.json entry carries a ``native`` block."""
    out: "OrderedDict[str, list[str]]" = OrderedDict()

    def add(t: Any, flags: Any) -> None:
        if not t:
            return
        key = _norm_t(t)
        for f in _l(flags):
            if isinstance(f, str) and f and f not in out.setdefault(key, []):
                out[key].append(f)

    for h in _l(manifest.get("holdings") if isinstance(manifest, Mapping) else None):
        if isinstance(h, Mapping):
            add(h.get("ticker"), h.get("flags"))
    price_of: dict[str, str] = {}
    for h in _l((lookthrough or {}).get("holdings")):
        if isinstance(h, Mapping):
            add(h.get("ticker"), h.get("flags"))
            if h.get("ticker"):
                price_of[_norm_t(h["ticker"])] = _norm_t(h.get("price_ticker") or h["ticker"])
    for pos in _l((plan or {}).get("positions")):
        if isinstance(pos, Mapping):
            add(pos.get("ticker"), pos.get("flags"))
    table = (prices or {}).get("tickers") if isinstance((prices or {}).get("tickers"), Mapping) else {}
    for t in list(out) + [k for k in price_of if k not in out]:
        entry = table.get(price_of.get(t, t)) or table.get(t)
        if isinstance(entry, Mapping) and isinstance(entry.get("native"), Mapping):
            add(t, ["converted_to_usd"])
    return OrderedDict((k, v) for k, v in out.items() if v)


def _cfg_get(cfg: Mapping, dotted: str, default: Any) -> Any:
    v = common.get_dotted(cfg, dotted, None) if isinstance(cfg, Mapping) else None
    return default if v is None else v


def _cfg_num(cfg: Mapping, dotted: str, default: float | None, cast: Callable[[float], Any] = float) -> Any:
    """Numeric config value; the default when missing or not a number (never raises)."""
    v = common.safe_float(_cfg_get(cfg, dotted, None))
    if v is None:
        return default
    return cast(v)


def _mode_info(manifest: Mapping) -> dict:
    m = manifest.get("mode") if isinstance(manifest, Mapping) else None
    if isinstance(m, Mapping):
        info = {"dry_run": bool(m.get("dry_run")), "quick": bool(m.get("quick")),
                "sectors": m.get("sectors"), "holding": m.get("holding")}
    else:
        s = str(m or "").lower()
        info = {"dry_run": "dry" in s, "quick": "quick" in s,
                "sectors": manifest.get("sectors") if isinstance(manifest, Mapping) else None,
                "holding": manifest.get("holding") if isinstance(manifest, Mapping) else None}
        if s.startswith("holding") and not info["holding"]:
            info["holding"] = True
    parts = []
    if info["holding"]:
        parts.append(f"holding trial ({info['holding']})" if isinstance(info["holding"], str) else "holding trial")
    elif info["quick"]:
        parts.append("quick run")
    else:
        parts.append("full run" if m is not None else "unknown")
    if info["sectors"]:
        parts.append("sectors: " + ", ".join(_sector_name(x) for x in _l(info["sectors"])))
    if info["dry_run"]:
        parts.append("dry run (fixtures)")
    info["label"] = " · ".join(parts)
    return info


def _data_files(run_dir: Path, loader: _Loader, plan_path: Path | None) -> list[dict]:
    """Script-output envelopes (sources, missing_data, flags) with the ticker they cover."""
    paths: list[tuple[Path, str | None]] = []
    for name in ("prices", "macro", "lookthrough"):
        paths.append((run_dir / "phase1" / f"{name}.json", None))
    for phase in (2, 3):
        d = run_dir / f"phase{phase}" / "data"
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                m = _DATA_NAME_RE.match(p.name)
                paths.append((p, _norm_t(m.group(2)) if m else None))
    p3 = run_dir / "phase3"
    if p3.is_dir():
        paths.extend((p, None) for p in sorted(p3.glob("screen_*.json")))
    for rel in ("phase2/aggregate.json", "phase2/evidence.json", "phase3/aggregate_light.json",
                "phase3/evidence_light.json", "phase3/aggregate.json", "phase3/evidence.json"):
        paths.append((run_dir / rel, None))
    if plan_path is not None:
        paths.append((plan_path, None))
    out = []
    for p, ticker in paths:
        doc = _d(loader.load(p))
        if doc is None:
            continue
        out.append({"rel": loader.rel(p), "ticker": ticker or (_norm_t(doc["ticker"]) if doc.get("ticker") else None),
                    "doc": doc})
    return out


def _holdings(manifest: Mapping, lookthrough: dict | None, agg2: dict | None,
              verdicts2: Mapping[str, dict], plan: dict | None,
              errors: list[str] | None = None) -> "OrderedDict[str, dict]":
    out: "OrderedDict[str, dict]" = OrderedDict()

    def put(t: Any, **kw: Any) -> None:
        if not t:
            return
        key = _norm_t(t)
        entry = out.setdefault(key, {"ticker": key, "asset_type": None, "sector": None, "position_pct": None})
        for k, v in kw.items():
            if entry.get(k) is None and v is not None:
                entry[k] = v

    for h in _l(manifest.get("holdings") if isinstance(manifest, Mapping) else None):
        if isinstance(h, Mapping):
            put(h.get("ticker"), asset_type=h.get("asset_type"), sector=h.get("sector"),
                position_pct=common.safe_float(h.get("position_pct")))
    for h in _l((lookthrough or {}).get("holdings")):
        if isinstance(h, Mapping):
            put(h.get("ticker"), asset_type=h.get("asset_type"),
                sector=h.get("sector") if h.get("asset_type") != "fund" else "funds",
                position_pct=common.fact_value(h.get("weight_pct")))
    for t, a in _m(agg2, "tickers", "phase2/aggregate.json", errors).items():
        if isinstance(a, Mapping) and a.get("kind") == "holding":
            put(t, asset_type=a.get("asset_type"), sector=a.get("sector"),
                position_pct=common.safe_float((a.get("inputs") or {}).get("position_pct")))
    for sector, doc in verdicts2.items():
        for v in _l(doc.get("verdicts")):
            if isinstance(v, Mapping) and v.get("kind") == "holding":
                put(v.get("ticker"), sector=sector)
    for pos in _l((plan or {}).get("positions")):
        if isinstance(pos, Mapping) and (common.safe_float(pos.get("shares_cur"))
                                         or common.safe_float(pos.get("weight_cur_pct"))):
            put(pos.get("ticker"), asset_type=pos.get("asset_type"), sector=pos.get("sector"),
                position_pct=common.safe_float(pos.get("weight_cur_pct")))
    return out


def build_context(run_dir: str | Path, *, cfg: Mapping | None = None, profile: Mapping | None = None) -> dict:
    """Read everything the memo can use from ``run_dir`` (all optional; never raises for missing
    or malformed inputs) and return a plain dict consumed by ``build_memo``. Main keys:
    ``manifest, mode, settings`` (config-derived thresholds), ``phase1`` (macro_view,
    cio_allocation, lookthrough, macro, prices), ``verdicts`` {phase: {sector: doc}},
    ``verdict_index``/``aggregates`` keyed (phase, ticker), ``evidence`` keyed by
    ``phaseN/...`` path, ``cio_final, final_actions, plan, plan_kind, plan_positions``,
    ``reports`` (every located agent output: role, ticker, phase, status ok|failed|missing,
    doc/stub) + ``report_index`` keyed (phase, role, ticker|sector|mode), ``data_files``
    (script envelopes), ``holdings`` and ``load_errors`` (unreadable files).
    ``cfg``/``profile`` override committee.yaml / investor_profile.yaml (default: the run's
    ``inputs/`` snapshot, else the active config dir)."""
    run_dir = Path(run_dir)
    loader = _Loader(run_dir)
    manifest = _d(loader.load(run_dir / "manifest.json")) or {}
    cfg = dict(cfg) if cfg is not None else _load_cfg(run_dir)
    profile = dict(profile) if profile is not None else _load_profile(run_dir)

    reports = _locate_reports(run_dir, loader, manifest)
    by_key = {r["key"]: r for r in reports}

    def doc_of(key: tuple) -> dict | None:
        rec = by_key.get(key)
        return rec["doc"] if rec and rec["status"] == "ok" else None

    verdicts: dict[int, "OrderedDict[str, dict]"] = {2: OrderedDict(), 3: OrderedDict()}
    for rec in reports:
        if rec["role"] == "sector_head" and rec["status"] == "ok" and rec["phase"] in (2, 3):
            verdicts[rec["phase"]][str(rec["sector"] or rec["doc"].get("sector") or "-")] = rec["doc"]

    plan_path = None
    plan_kind = None
    for name, kind in (("portfolio_plan.json", "final"), ("portfolio_preview.json", "preview")):
        p = run_dir / "phase4" / name
        if _d(loader.load(p)) is not None:
            plan_path, plan_kind = p, kind
            break
    plan = _d(loader.load(plan_path)) if plan_path else None

    phase1 = {
        "macro_view": doc_of((1, "macro", "-")),
        "cio_allocation": doc_of((1, "cio", "allocation")),
        "lookthrough": _d(loader.load(run_dir / "phase1" / "lookthrough.json")),
        "macro": _d(loader.load(run_dir / "phase1" / "macro.json")),
        "prices": _d(loader.load(run_dir / "phase1" / "prices.json")),
    }
    agg2 = _d(loader.load(run_dir / "phase2" / "aggregate.json"))
    agg3 = _d(loader.load(run_dir / "phase3" / "aggregate.json"))
    agg3l = _d(loader.load(run_dir / "phase3" / "aggregate_light.json"))
    evidence_docs = {rel: _d(loader.load(run_dir / rel)) for rel in
                     ("phase2/evidence.json", "phase3/evidence_light.json", "phase3/evidence.json")}

    aggregates: dict[tuple[int, str], dict] = {}
    for phase, doc, light, rel in ((2, agg2, False, "phase2/aggregate.json"), (3, agg3, False, "phase3/aggregate.json"),
                                   (3, agg3l, True, "phase3/aggregate_light.json")):
        for t, a in _m(doc, "tickers", rel, loader.errors).items():
            if isinstance(a, Mapping):
                aggregates.setdefault((phase, _norm_t(t)), {**a, "_light": light})

    evidence: dict[str, dict] = {}
    for rel, doc in evidence_docs.items():
        for k, v in _m(doc, "reports", rel, loader.errors).items():
            if isinstance(v, Mapping):
                evidence[_norm_key(k)] = {**v, "_key": k, "_file": rel}

    verdict_index: dict[tuple[int, str], dict] = {}
    for phase, docs in verdicts.items():
        for sector, doc in docs.items():
            for v in _l(doc.get("verdicts")):
                if isinstance(v, Mapping) and v.get("ticker"):
                    verdict_index.setdefault((phase, _norm_t(v["ticker"])),
                                             {**v, "_sector": sector, "_phase": phase, "_as_of": doc.get("as_of")})

    chair_review = _d(loader.load(run_dir / "phase4" / "chair_review.json"))
    audit = _d(loader.load(run_dir / "phase4" / "audit.json"))
    cio_final = doc_of((4, "cio", "reconciliation"))
    final_actions = OrderedDict()
    for fa in _l((cio_final or {}).get("final_actions")):
        if isinstance(fa, Mapping) and fa.get("ticker"):
            final_actions.setdefault(_norm_t(fa["ticker"]), fa)
    plan_positions = OrderedDict()
    for pos in _l((plan or {}).get("positions")):
        if isinstance(pos, Mapping) and pos.get("ticker"):
            plan_positions.setdefault(_norm_t(pos["ticker"]), pos)

    return {
        "run_dir": run_dir,
        "run_id": manifest.get("run_id") or run_dir.name,
        "as_of": manifest.get("as_of") or (phase1["prices"] or {}).get("as_of"),
        "created_at": manifest.get("created_at"),
        "generated_at": common.now_iso(),
        "manifest": manifest,
        "mode": _mode_info(manifest),
        "cfg": cfg,
        "profile": profile,
        "settings": {
            "min_evidence": _cfg_num(cfg, "actions.min_evidence", DEFAULT_MIN_EVIDENCE),
            "score_dp": _cfg_num(cfg, "aggregation.round_dp", DEFAULT_SCORE_DP, int),
            "rr_dp": _cfg_num(cfg, "risk_reward.round_dp", DEFAULT_RR_DP, int),
            "tax_pct": _cfg_num(profile, "capital_gains_tax_pct", DEFAULT_CAPITAL_GAINS_TAX_PCT),
            "base_currency": str(_cfg_get(profile, "base_currency", DEFAULT_BASE_CURRENCY)),
            "max_sector_pct": _cfg_num(profile, "max_sector_pct", None),
            "max_position_pct": _cfg_num(profile, "max_position_pct", None),
        },
        "phase1": phase1,
        "portfolio_cfg": _load_portfolio(run_dir),
        "config_drift": [e for e in _l(manifest.get("config_drift")) if isinstance(e, Mapping)],
        "position_flags": _position_flags(manifest, phase1["lookthrough"], plan, phase1["prices"]),
        "verdicts": verdicts,
        "verdict_index": verdict_index,
        "aggregates": aggregates,
        "evidence": evidence,
        "cio_final": cio_final,
        "chair_review": chair_review,
        "audit": audit,
        "final_actions": final_actions,
        "plan": plan,
        "plan_kind": plan_kind,
        "plan_rel": loader.rel(plan_path) if plan_path else None,
        "plan_positions": plan_positions,
        "screens": {p.name[len("screen_"):-len(".json")]: _d(loader.load(p))
                    for p in sorted((run_dir / "phase3").glob("screen_*.json"))} if (run_dir / "phase3").is_dir() else {},
        "reports": reports,
        "report_index": by_key,
        "data_files": _data_files(run_dir, loader, plan_path),
        "holdings": _holdings(manifest, phase1["lookthrough"], agg2, verdicts[2], plan, loader.errors),
        "load_errors": loader.errors,
        "consistency_warnings": _copy_mismatches(
            verdict_index, aggregates, [ph for ph, doc in ((2, agg2), (3, agg3)) if doc is not None]),
    }


# =====================================================================================
# Shared derivations
# =====================================================================================


def _ticker_sector(ctx: Mapping, t: str) -> str | None:
    h = ctx["holdings"].get(t)
    if h and h.get("sector"):
        return h["sector"]
    for phase in (2, 3):
        v = ctx["verdict_index"].get((phase, t))
        if v:
            return v.get("_sector")
        a = ctx["aggregates"].get((phase, t))
        if a and a.get("sector"):
            return a["sector"]
    pos = ctx["plan_positions"].get(t)
    return pos.get("sector") if pos else None


def _report(ctx: Mapping, phase: int, role: str, ticker: str) -> dict | None:
    return ctx["report_index"].get((phase, role, ticker))


def _evidence_of(ctx: Mapping, rec: Mapping) -> dict | None:
    if not rec.get("rel"):
        return None
    return ctx["evidence"].get(_norm_key(rec["rel"]))


#: Deterministic per-ticker figures: aggregate.json is authoritative; the sector verdict's copy
#: is only a fallback (an agent mis-copy must never change a displayed number).
DETERMINISTIC_KEYS = ("rule_action", "agg_conviction", "agg_evidence", "rr")


def _agg_rr(a: Mapping | None) -> tuple[bool, Any]:
    """``(present, rr)`` from an aggregate entry (present even when rr is legitimately null)."""
    if not a or "rr" not in a:
        return False, None
    rr = a.get("rr")
    if isinstance(rr, Mapping):
        return True, rr.get("rr")
    return (True, None) if rr is None else (False, None)


def _det(v: Mapping | None, a: Mapping | None, key: str) -> Any:
    """Deterministic figure ``key``: aggregate.json first, the sector verdict's copy as fallback."""
    if key == "rr":
        present, val = _agg_rr(a)
        if present:
            return val
        rv = (v or {}).get("rr")
        return None if isinstance(rv, Mapping) else rv
    if a and a.get(key) is not None:
        return a.get(key)
    return (v or {}).get(key)


def _rr_value(v: Mapping | None, a: Mapping | None) -> Any:
    return _det(v, a, "rr")


def _rr_obj(a: Mapping | None) -> Mapping:
    rr = (a or {}).get("rr")
    return rr if isinstance(rr, Mapping) else {}


class _RrNotes:
    """Collects what the R/R, fair-value and stop cells of ONE table need explained underneath it
    (raw negative ratios, null reasons), so the footnote lists exactly what that table shows."""

    def __init__(self, dp: int) -> None:
        self.dp = dp
        self.negative: list[tuple[str, float]] = []
        self.reasons: list[str] = []

    def paragraph(self) -> dict | None:
        parts = []
        if self.negative:
            raw = ", ".join(f"{t} {fmt_num(v, self.dp)}" for t, v in self.negative)
            parts.append(f"{RR_NOT_MEANINGFUL}: the base fair value is below the current price, so the ratio is "
                         f"negative and not meaningful (raw R/R: {raw}).")
        for code in self.reasons:
            parts.append(f"n/a ({code}): {RR_NULL_LEGEND.get(code, 'R/R could not be computed')}.")
        return _p(" ".join(parts), "muted") if parts else None


def _rr_cells(ctx: Mapping, tk: str, v: Mapping | None, a: Mapping | None,
              notes: _RrNotes | None = None) -> dict:
    """R/R, base fair value and stop cells of one ticker.

    * rr > 0 → the number; rr < 0 (fair value below price) → ``n/m (FV below price)`` with the raw
      number in a tooltip/footnote; rr null → ``n/a (<null_reason>)`` (bare ``n/a`` only when the
      aggregate carries no reason). Fair value / stop come from the aggregate's rr object (the
      sector verdict's ``fair_value_base`` / ``invalidation_price`` are the fallback)."""
    dp = ctx["settings"]["rr_dp"]
    obj = _rr_obj(a)
    raw = _num(_rr_value(v, a))
    title: str | None = None
    if raw is None:
        reason = str(obj.get("null_reason") or "").strip()
        text = f"n/a ({reason})" if reason else "n/a"
        if reason and notes is not None and reason not in notes.reasons:
            notes.reasons.append(reason)
        if reason:
            title = RR_NULL_LEGEND.get(reason)
    elif raw < 0:
        text = RR_NOT_MEANINGFUL
        title = f"raw R/R {fmt_num(raw, dp)}: the base fair value is below the current price"
        if notes is not None:
            notes.negative.append((tk, raw))
    else:
        text = fmt_num(raw, dp)
    fv = _num(obj.get("fv_base"))
    if fv is None and v:
        fv = _num(v.get("fair_value_base"))
    stop = _num(obj.get("invalidation"))
    if stop is None and v:
        stop = _num(v.get("invalidation_price"))
    src = obj.get("invalidation_source")
    return {"rr": text, "rr_title": title, "rr_raw": raw,
            "fv": fmt_usd(fv, 2) if fv is not None else "n/a",
            "stop": fmt_usd(stop, 2) if stop is not None else "n/a",
            "stop_title": f"stop source: {src}" if src and stop is not None else None}


def _rr_suffix(rr: Mapping) -> str:
    """Explanation appended to a raw R/R in a detail line: null reason, or n/m for a negative ratio."""
    raw = _num(rr.get("rr"))
    if raw is None:
        return f" — {rr['null_reason']}" if rr.get("null_reason") else ""
    return f" — {RR_NOT_MEANINGFUL}: not a meaningful ratio" if raw < 0 else ""


def _flag_legend(flags_by_ticker: Mapping[str, list[str]], tickers: Iterable[str]) -> dict | None:
    """Muted legend for the position flags shown in a table (None when no row has a flag)."""
    seen: list[str] = []
    for t in tickers:
        for f in flags_by_ticker.get(t, []):
            if f not in seen:
                seen.append(f)
    if not seen:
        return None
    items = "; ".join(f"{f} — {FLAG_LEGEND.get(f, 'classification flag from the look-through')}" for f in seen)
    return _p(f"Flags: {items}.", "muted")


def _copy_mismatches(verdict_index: Mapping, aggregates: Mapping, agg_phases: Iterable[int]) -> list[str]:
    """Warnings for sector-verdict values that differ from aggregate.json (the aggregate value is
    what the memo shows), and for verdicts on tickers absent from a loaded aggregate.json."""
    loaded = set(agg_phases)
    out: list[str] = []
    for (ph, tk), v in sorted(verdict_index.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        a = aggregates.get((ph, tk))
        src = f"phase{ph}/{v.get('_sector')}_verdict.json"
        if a is None or a.get("_light"):
            if ph in loaded:
                out.append(f"{src}: {tk} has a verdict but is not in phase{ph}/aggregate.json "
                           f"(figures shown from the verdict copy)")
            continue
        for key in DETERMINISTIC_KEYS:
            if key not in v:
                continue
            vv = v.get(key)
            if key == "rr":
                present, av = _agg_rr(a)
                if not present:
                    continue
            else:
                av = a.get(key)
            fv, fa = common.safe_float(vv), common.safe_float(av)
            same = (abs(fv - fa) <= 1e-6) if (fv is not None and fa is not None) else (vv == av)
            if not same:
                out.append(f"{src}: {tk} {key} {vv!r} differs from phase{ph}/aggregate.json {av!r}; "
                           f"the aggregate value is shown")
    return out


def _rel_doc_label(rec: Mapping) -> str:
    role = rec.get("role") or "report"
    subj = rec.get("ticker") or (rec.get("sector") and _sector_name(rec["sector"])) or rec.get("mode") or ""
    phase = rec.get("phase")
    label = f"{role} · {subj}" if subj else role
    return f"{label} (phase {phase})" if phase else label


def _ticker_in_text(t: str, text: Any) -> bool:
    return bool(re.search(rf"(?<![A-Z0-9-]){re.escape(t)}(?![A-Z0-9-])", str(text or "")))


def _rec_matches(rec: Mapping, t: str | None, ctx: Mapping) -> bool:
    if t is None:
        return True
    if rec.get("ticker") == t:
        return True
    if rec.get("role") == "sector_head" and rec.get("doc"):
        return any(_norm_t(v.get("ticker")) == t for v in _l(rec["doc"].get("verdicts")) if isinstance(v, Mapping))
    return False


# =====================================================================================
# Sections
# =====================================================================================


def _sec_disclaimer(ctx: Mapping, t: str | None) -> list[dict]:
    pairs: list[tuple[str, Any]] = [("Run", ctx["run_id"]), ("As of", ctx["as_of"] or "n/a"),
                                    ("Mode", ctx["mode"]["label"])]
    if t:
        pairs.append(("Ticker", t))
    pairs.append(("Generated", ctx["generated_at"]))
    blocks = [_p(common.DISCLAIMER, "disclaimer"), _kv(pairs)]
    if ctx["mode"].get("dry_run"):
        blocks.append(_p("Dry run: every figure and report comes from test fixtures, not live data.", "warn"))
    return blocks


def _sec_exec_summary(ctx: Mapping, t: str | None) -> list[dict]:
    cf = ctx["cio_final"]
    if not cf:
        rec = ctx["report_index"].get((4, "cio", "reconciliation"))
        blocks = [_na()]
        if rec and rec["status"] == "failed":
            blocks.append(_p("The CIO reconciliation report failed validation; no executive summary was produced.", "warn"))
        return blocks
    blocks: list[dict] = []
    summary = [s for s in _l(cf.get("executive_summary")) if s]
    blocks.append(_bul(summary) if summary else _na())
    risks = [r for r in _l(cf.get("top_risks")) if r]
    if risks:
        blocks += [_sub("Top risks"), _bul(risks)]
    rec_rows = [[r.get("topic"), r.get("phase1_position"), r.get("findings"), r.get("resolution")]
                for r in _l(cf.get("reconciliation")) if isinstance(r, Mapping)]
    if rec_rows:
        blocks += [_sub("CIO reconciliation"),
                   _tbl(["Topic", "Phase 1 position", "Findings (phases 2–3)", "Resolution"], rec_rows)]
    conv = cf.get("conviction")
    if conv is not None:
        blocks.append(_kv([("CIO conviction", f"{fmt_num(conv, 0)}/10"),
                           ("Why", cf.get("conviction_reason") or "—")]))
    return blocks


def _sec_macro(ctx: Mapping, t: str | None) -> list[dict]:
    mv = ctx["phase1"]["macro_view"]
    ca = ctx["phase1"]["cio_allocation"]
    md = ctx["phase1"]["macro"]
    blocks: list[dict] = []
    if mv:
        blocks.append(_kv([("Regime", mv.get("regime") or "—"),
                           ("Macro conviction", f"{fmt_num(mv.get('conviction'), 0)}/10"
                            if mv.get("conviction") is not None else "n/a"),
                           ("As of", mv.get("as_of") or "n/a")]))
        if mv.get("regime_summary"):
            blocks.append(_p(mv["regime_summary"]))
        drivers = []
        for d in _l(mv.get("drivers")):
            if isinstance(d, Mapping):
                series = ", ".join(str(s) for s in _l(d.get("series")))
                extra = "; ".join(x for x in (d.get("direction"), f"series {series}" if series else "") if x)
                drivers.append(f"{d.get('driver')}" + (f" ({extra})" if extra else ""))
        if drivers:
            blocks += [_sub("Key drivers"), _bul(drivers)]
        rows = []
        for sc in _l(mv.get("scenarios")):
            if isinstance(sc, Mapping):
                impl = "; ".join(
                    f"{_sector_name(i.get('sector'))}: {i.get('impact')}" + (f" ({i['note']})" if i.get("note") else "")
                    for i in _l(sc.get("sector_implications")) if isinstance(i, Mapping))
                prob = common.safe_float(sc.get("probability"))
                rows.append([sc.get("name"), fmt_pct(prob * 100 if prob is not None else None, 0),
                             sc.get("description"), impl or "—"])
        blocks += [_sub("Scenarios"), _tbl(["Scenario", "Probability", "Description", "Sector implications"],
                                           rows, align="lrll")] if rows else []
        crow = [[_sector_name(c.get("sector")), fmt_pct(c.get("min_weight")), fmt_pct(c.get("max_weight")),
                 c.get("rationale")] for c in _l(mv.get("sector_constraints")) if isinstance(c, Mapping)]
        if crow:
            blocks += [_sub("Sector constraints (macro bands)"),
                       _tbl(["Sector", "Min", "Max", "Rationale"], crow, align="lrrl")]
    else:
        blocks += [_sub("Macro view"), _na()]
    series = (md or {}).get("series")
    if isinstance(series, Mapping) and series:
        rows = []
        for sid, s in series.items():
            if not isinstance(s, Mapping):
                continue
            latest = s.get("latest") if isinstance(s.get("latest"), Mapping) else {}
            rows.append([f"{sid} — {s.get('name')}" if s.get("name") else sid,
                         fmt_fact(latest, asof=False), latest.get("as_of") or s.get("observation_date") or "n/a",
                         fmt_fact(s.get("chg_3m"), asof=False), fmt_fact(s.get("chg_12m"), asof=False),
                         fmt_fact(s.get("yoy_pct"), asof=False) if s.get("yoy_pct") else "—"])
        if rows:
            blocks += [_sub("Macro data (FRED)"),
                       _tbl(["Series", "Latest", "As of", "3m change", "12m change", "YoY"], rows, align="lrlrrr")]
    if ca:
        blocks.append(_sub("CIO allocation (Phase 1)"))
        if ca.get("rationale"):
            blocks.append(_p(ca["rationale"]))
        pairs = [("Expand sectors", ", ".join(_sector_name(s) for s in _l(ca.get("expand_sectors"))) or "none"),
                 ("Reduce sectors", ", ".join(_sector_name(s) for s in _l(ca.get("reduce_sectors"))) or "none")]
        dropped = _l(ctx["manifest"].get("expand_dropped"))
        if dropped:
            pairs.append(("Not expanded (cap/scope)", ", ".join(_sector_name(s) for s in dropped)))
        if ca.get("conviction") is not None:
            pairs.append(("CIO conviction", f"{fmt_num(ca.get('conviction'), 0)}/10"))
        blocks.append(_kv(pairs))
    elif mv:
        blocks += [_sub("CIO allocation (Phase 1)"), _na()]
    if not blocks or all(b.get("style") == "muted" or b["type"] == "subheading" for b in blocks):
        return [_na()]
    return blocks


# -------------------------------------------------------------------------------------
# What the PLAN does with a holding (effective action, exit kind, share of the position)
# -------------------------------------------------------------------------------------

_BUY_VERDICTS = ("BUY", "ADD")
_SELL_VERDICTS = ("TRIM", "SELL")
#: Short cell label of a dropped trade, by ``dropped_reason`` (``{verb}`` = trim / sale / purchase).
_DROP_SHORT: dict[str, str] = {
    "rounds_to_zero_shares": "{verb} < 1 share",
    "below_min_trade_usd": "below the minimum trade size",
    "at_position_cap": "already at its position cap",
    "no_sector_headroom": "no room under the sector limit",
    "insufficient_cash": "not enough cash",
    "no_price": "no price available",
    "no_weight": "no target weight",
    "no_total_value": "no portfolio value",
}


def _plan_view(pos: Any, verdict: Any = None) -> dict:
    """What the portfolio PLAN does with one holding, independent of the verdict text. Reads the plan
    position (``trade``, ``dropped_reason``, ``full_exit`` and - when the plan carries them -
    ``effective_action``, ``effective_action_reason``, ``exit_kind``, ``trade_pct_of_position``);
    an old plan without the new fields degrades to values derived from the old ones."""
    p = pos if isinstance(pos, Mapping) else None
    ver = _s(verdict or (p or {}).get("action")).strip().upper()
    out = {"has_plan": p is not None, "verdict": ver, "effective": ver, "differs": False, "reason_short": "",
           "reason_full": "", "exit_kind": "none", "pct": None, "side": "NONE", "shares": 0.0,
           "value_usd": None, "cur_shares": None, "keep": None, "proposed_shares": None,
           "proposed_usd": None, "dropped": None, "weight_cur": None, "weight_tgt": None,
           "sizing_rule": ""}
    if p is None:
        return out
    tr = p.get("trade") if isinstance(p.get("trade"), Mapping) else {}
    side = _s(tr.get("side") or "NONE").upper()
    shares = common.safe_float(tr.get("shares")) or 0.0
    cur = common.safe_float(p.get("shares_cur"))
    dropped = p.get("dropped_reason") or None
    traded = side != "NONE" and shares > 0
    verb = {"TRIM": "trim", "SELL": "sale", "BUY": "purchase", "ADD": "purchase"}.get(ver, "trade")
    eff_raw = p.get("effective_action")
    if isinstance(eff_raw, str) and eff_raw.strip():
        eff = eff_raw.strip().upper()
    elif dropped and not traded and (ver in _SELL_VERDICTS or (ver in _BUY_VERDICTS and (cur or 0) > 0)):
        eff = "HOLD"
    elif dropped and not traded and ver in _BUY_VERDICTS:
        eff = "NONE"
    else:
        eff = ver
    kind = _s(p.get("exit_kind")).strip().lower()
    if kind not in ("none", "partial", "full"):
        kind = "full" if p.get("full_exit") is True and traded else ("partial" if side == "SELL" and traded else "none")
    pct = common.safe_float(p.get("trade_pct_of_position"))
    if pct is None and side == "SELL" and shares > 0 and cur:
        pct = min(1.0, shares / cur)
    prop = common.safe_float(tr.get("proposed_shares"))
    prop_usd = common.safe_float(tr.get("proposed_value_usd"))
    reason_plan = _s(p.get("effective_action_reason")).strip()
    short = full = ""
    if eff != ver or dropped:
        if dropped:
            code = str(dropped)
            detail = ""
            if code not in _DROP_SHORT:        # free-text reasons ("below min_trade_usd (USD 150 < 200)")
                m = re.search(r"\(([^)]*)\)", code)
                detail = f" ({m.group(1)})" if m else ""
                flat = code.lower().replace(" ", "_")
                code = ("below_min_trade_usd" if "min_trade" in flat else
                        "rounds_to_zero_shares" if "zero" in flat and "share" in flat else
                        next((k for k in _DROP_SHORT if k in flat), code))
            short = _DROP_SHORT.get(code, str(dropped).replace("_", " ")).format(verb=verb)
            if code == "rounds_to_zero_shares":
                size = f"{fmt_num(prop, 2)}-share" if prop else "fractional"
                full = f"a {size} {verb} is too small to execute (less than 1 share)"
            elif code == "below_min_trade_usd":
                full = (f"the {verb} (about {fmt_usd(prop_usd)}) is below the minimum trade size" if prop_usd
                        else f"the {verb} is below the minimum trade size{detail}")
            else:
                full = short + detail
        elif reason_plan:
            short = full = reason_plan
    out.update({"effective": eff, "differs": bool(ver) and eff != ver, "reason_short": short,
                "reason_full": full or reason_plan, "exit_kind": kind, "pct": pct, "side": side,
                "shares": shares, "value_usd": common.safe_float(tr.get("value_usd")), "cur_shares": cur,
                "keep": (cur - shares) if cur is not None and side == "SELL" else None,
                "proposed_shares": prop, "proposed_usd": prop_usd, "dropped": dropped,
                "weight_cur": common.safe_float(p.get("weight_cur_pct")),
                "weight_tgt": common.safe_float(p.get("weight_tgt_pct")),
                "sizing_rule": _s(p.get("sizing_rule"))})
    return out


def _action_label(pv: Mapping, *, with_exit: bool = False) -> str:
    """Action cell text: the EFFECTIVE action; ``TRIM -> HOLD (trim < 1 share)`` when the plan does
    not act on the verdict; ``(full exit)`` appended on request when the plan sells everything."""
    ver, eff = pv.get("verdict") or "", pv.get("effective") or ""
    if not eff:
        return ver or "n/a"
    if not pv.get("differs"):
        full = with_exit and pv.get("exit_kind") == "full" and eff in ("SELL", "TRIM")
        return eff + (FULL_EXIT_SUFFIX if full else "")
    return f"{ver} -> {eff}" + (f" ({pv['reason_short']})" if pv.get("reason_short") else "")


def _action_text(pos: Mapping) -> str:
    """Plan action of a position for the portfolio table (effective action; ``(full exit)`` suffix)."""
    return _action_label(_plan_view(pos), with_exit=True)


# -------------------------------------------------------------------------------------
# One answer to "which sector limit is binding?"
# -------------------------------------------------------------------------------------


def _sector_limit_info(ctx: Mapping, sector: Any, inputs: Mapping | None = None) -> dict:
    """The sector limit that actually binds, and where it comes from. Sources: the profile's
    ``max_sector_pct``, the CIO's target for the sector, and the macro band (an input the CIO may
    override). ``value`` = the lowest of profile cap and CIO target; ``trigger`` = the level at
    which the rule acts (``value`` + tolerance). Missing pieces are ``None``."""
    inp = inputs if isinstance(inputs, Mapping) else {}
    key = _s(sector)
    plan = ctx.get("plan") if isinstance(ctx.get("plan"), Mapping) else {}
    psec = next((x for x in _l(plan.get("sectors")) if isinstance(x, Mapping) and x.get("sector") == key), {})
    st = ctx.get("settings") or {}
    mv = (ctx.get("phase1") or {}).get("macro_view") or {}
    band = next((c for c in _l(mv.get("sector_constraints")) if isinstance(c, Mapping) and c.get("sector") == key), {})

    def pick(*vals: Any) -> float | None:
        for v in vals:
            f = common.safe_float(v)
            if f is not None:
                return f
        return None

    profile = pick(inp.get("max_sector_pct"), psec.get("max_pct"), st.get("max_sector_pct"))
    target = pick(inp.get("cio_target_pct"), psec.get("target_pct"))
    tol = pick(inp.get("sector_breach_tolerance_pp"))
    trigger = pick(inp.get("sector_limit_pct"))
    macro = pick(band.get("max_weight"))
    cands = [(v, k) for v, k in ((profile, "profile"), (target, "target")) if v is not None]
    if cands:
        value, source = min(cands, key=lambda c: (c[0], 0 if c[1] == "profile" else 1))
    elif trigger is not None:
        value, source = trigger - (tol or 0.0), "rule"
    else:
        value, source = None, ""
    if trigger is None and value is not None and tol is not None:
        trigger = value + tol
    text = {"profile": "your profile's sector cap", "target": "the committee's target for this sector",
            "rule": "the rule's sector limit"}.get(source, "")
    return {"sector": key, "value": value, "source": source, "source_text": text, "trigger": trigger,
            "tolerance": tol, "profile": profile, "target": target, "macro_max": macro}


def _sector_limit_note(ctx: Mapping, inputs_by_sector: Mapping[str, Mapping]) -> str:
    """The one place that names every sector number: the binding limit first, the others once."""
    parts = []
    for key, inp in inputs_by_sector.items():
        info = _sector_limit_info(ctx, key, inp)
        if info["value"] is None:
            continue
        others = []
        if info["profile"] is not None and info["source"] != "profile":
            others.append(f"your profile cap is {fmt_threshold(info['profile'])}%")
        if info["target"] is not None and info["source"] != "target":
            others.append(f"the committee's target is {fmt_threshold(info['target'])}%")
        if info["macro_max"] is not None and info["macro_max"] < info["value"]:
            others.append(f"the macro model's band tops out at {fmt_threshold(info['macro_max'])}% "
                          f"(the committee chose to go above it)")
        elif info["macro_max"] is not None and info["macro_max"] != info["value"]:
            others.append(f"the macro model's band tops out at {fmt_threshold(info['macro_max'])}%")
        tol = (f"; the rule acts only {fmt_num(info['tolerance'], 1)} pp above it, at {fmt_threshold(info['trigger'])}%"
               if info["tolerance"] and info["trigger"] is not None else "")
        parts.append(f"{_sector_name(key)}: the binding limit is {fmt_threshold(info['value'])}% "
                     f"({info['source_text']}){tol}" + (f" (other numbers: {'; '.join(others)})" if others else ""))
    return ("Sector limits - sector weights count the funds' holdings (look-through). " + ". ".join(parts) + ".") \
        if parts else ""


# -------------------------------------------------------------------------------------
# Residual sector breaches (plan.residual_breaches)
# -------------------------------------------------------------------------------------


def _fund_label(ctx: Mapping, ticker: Any) -> str:
    t = _norm_t(ticker)
    row = (ctx.get("portfolio_cfg") or {}).get(t) or {}
    name = _s(row.get("name")).split(" (")[0].strip()
    return f"{t} ({name})" if name else t


def _residual_items(ctx: Mapping) -> list[dict]:
    """Plan ``residual_breaches`` as dicts. A plan without the field falls back to the sectors the
    plan itself marks ``breach`` (no fund detail then)."""
    plan = ctx.get("plan") if isinstance(ctx.get("plan"), Mapping) else {}
    raw = plan.get("residual_breaches")
    if isinstance(raw, list):
        return [dict(r) for r in raw if isinstance(r, Mapping) and r.get("sector")]
    items: list[dict] = []
    for s in _l(plan.get("sectors")):
        if isinstance(s, Mapping) and s.get("breach") is True and s.get("sector"):
            lim = _sector_limit_info(ctx, s["sector"])["value"]
            items.append({"sector": s["sector"], "post_pct": s.get("post_lookthrough_pct"),
                          "limit_pct": lim if lim is not None else s.get("max_pct"), "_derived": True})
    return items


def _residual_text(ctx: Mapping, r: Mapping) -> str:
    """Plain-English sentences for one residual breach."""
    name = _sector_name(r.get("sector"))
    post, lim = common.safe_float(r.get("post_pct")), common.safe_float(r.get("limit_pct"))
    text = (f"{name} would still be {fmt_pct(post)} vs the {fmt_threshold(lim)}% limit after the planned trades."
            if lim is not None else f"{name} would still be {fmt_pct(post)} after the planned trades.")
    stock = common.safe_float(r.get("stock_trim_pp"))
    funds = [f for f in _l(r.get("fund_contributions")) if isinstance(f, Mapping) and f.get("ticker")]
    if funds:
        bits = []
        for f in funds:
            w, sp = common.safe_float(f.get("weight_pct")), common.safe_float(f.get("sector_pct_of_portfolio"))
            piece = _fund_label(ctx, f["ticker"])
            if w is not None:
                piece += f" {fmt_pct(w)} of the portfolio"
            if sp is not None:
                piece += f" ({fmt_pct(sp)} of it in {name})"
            bits.append(piece)
        text += (" Stocks cannot fix this" + (f" (the planned stock trims already remove {fmt_num(stock, 1)} pp)"
                                              if stock else "") + ": " + "; ".join(bits) + " hold most of it."
                 " Only changing those fund holdings, or adding new money in other sectors, would close the gap.")
    elif stock is not None and stock > 0:
        text += f" The planned stock trims already remove {fmt_num(stock, 1)} pp; the rest sits in funds."
    note = _scrub_jargon(r.get("note"))
    if note and not funds:      # with a fund breakdown the plan's note only repeats the same numbers
        text += " " + note
    return text


def _weights(doc: Mapping | None, key: str) -> dict:
    w = (doc or {}).get(key)
    return {str(k): v for k, v in w.items()} if isinstance(w, Mapping) else {}


def _sec_portfolio(ctx: Mapping, t: str | None) -> list[dict]:
    lt = ctx["phase1"]["lookthrough"]
    mv = ctx["phase1"]["macro_view"]
    ca = ctx["phase1"]["cio_allocation"]
    cf = ctx["cio_final"]
    plan = ctx["plan"]
    st = ctx["settings"]
    blocks: list[dict] = []

    total = (plan or {}).get("total_value_usd") or (lt or {}).get("total_value_usd")
    cash = (plan or {}).get("cash") if isinstance((plan or {}).get("cash"), Mapping) else {}
    prof = ctx.get("profile") if isinstance(ctx.get("profile"), Mapping) else {}
    cur = _weights(lt, "sector_weights_pct") or _weights(ca, "current_sector_weights")
    p1 = _weights(ca, "target_sector_weights")
    fin = _weights(cf, "final_target_sector_weights")
    bands = {str(c.get("sector")): c for c in _l((mv or {}).get("sector_constraints")) if isinstance(c, Mapping)}
    psec = {str(s.get("sector")): s for s in _l((plan or {}).get("sectors")) if isinstance(s, Mapping)}
    if not cur and psec:
        cur = {k: v.get("cur_lookthrough_pct") for k, v in psec.items()}

    pairs: list[tuple[str, Any]] = []
    if total:
        pairs.append(("Total value", fmt_fact(total)))
    if cash:
        pairs.append(("Cash (current / post-trade)",
                      f"{fmt_pct(cash.get('current_pct'))} / {fmt_pct(cash.get('post_pct'))}"))
        # the profile's cash floor (plan cash.target_pct) is NOT the CIO's cash target: show both
        pairs.append(("Cash floor (profile)", fmt_pct(cash.get("target_pct"))))
    cio_cash = common.safe_float(fin.get("cash")) if fin.get("cash") is not None else None
    p1_cash = common.safe_float(p1.get("cash")) if p1.get("cash") is not None else None
    if cio_cash is not None:
        pairs.append(("Cash target (CIO)", fmt_pct(cio_cash) + (f" (Phase 1 allocation {fmt_pct(p1_cash)})"
                                                                if p1_cash is not None else "")))
    elif p1_cash is not None:
        pairs.append(("Cash target (CIO)", f"{fmt_pct(p1_cash)} (Phase 1 allocation; no final CIO reconciliation)"))
    if st.get("max_sector_pct") is not None or st.get("max_position_pct") is not None:
        pairs.append(("Profile limits", f"max sector {fmt_pct(st.get('max_sector_pct'))}, "
                                        f"max single stock {fmt_pct(st.get('max_position_pct'))}"))
    inv = prof.get("investor") if isinstance(prof.get("investor"), Mapping) else {}
    who = []
    if inv.get("age") is not None:
        who.append(f"age {inv['age']}")
    if prof.get("risk_tolerance"):
        who.append(f"risk tolerance {prof['risk_tolerance']}")
    if prof.get("horizon_years") is not None:
        who.append(f"horizon {prof['horizon_years']:g} years")
    if who:
        pairs.append(("Investor profile", ", ".join(str(x) for x in who)))
    # a bucket the CIO targets leave out is neither held down nor allocated: say so
    unclassified = common.safe_float(_num(cur.get("unclassified")))
    has_target = p1.get("unclassified") is not None or fin.get("unclassified") is not None
    if pairs:
        blocks.append(_kv(pairs))
    if unclassified and unclassified > 0 and (p1 or fin) and not has_target:
        blocks.append(_p(f"Unclassified exposure ({fmt_pct(unclassified)}) has no target: the CIO sector targets "
                         f"assign this bucket no weight.", "warn"))

    keys = [k for k in SECTOR_ORDER if k in cur or k in p1 or k in fin or k in psec or k in bands]
    keys += sorted(k for k in set(cur) | set(p1) | set(fin) | set(psec) if k not in keys)
    rows = []
    for k in keys:
        b = bands.get(k)
        band = f"{fmt_pct(b.get('min_weight'))}–{fmt_pct(b.get('max_weight'))}" if b else "—"
        ps = psec.get(k) or {}
        breach = ps.get("breach")
        rows.append([_sector_name(k), fmt_pct(cur.get(k)), band, fmt_pct(p1.get(k)), fmt_pct(fin.get(k)),
                     fmt_pct(ps.get("post_lookthrough_pct")),
                     "yes" if breach is True else ("no" if breach is False else "—")])
    if rows:
        blocks += [_sub("Sector exposure (look-through, % of portfolio)"),
                   _tbl(["Sector", "Current", "Macro band", "CIO target (P1)", "Final target", "Post-trade",
                         "Breach"], rows, align="lrlrrrl", badges=[6])]
        resid = _residual_items(ctx)
        if resid:
            blocks.append(_p("Residual breach after the planned trades: " + "; ".join(
                f"{_sector_name(r['sector'])} ends at {fmt_pct(r.get('post_pct'))}"
                + (f" vs the {fmt_threshold(r.get('limit_pct'))}% limit" if r.get("limit_pct") is not None else "")
                for r in resid) + " (details in section 7).", "warn"))
    else:
        blocks += [_sub("Sector exposure (look-through, % of portfolio)"), _na()]

    prow = []
    flags = ctx.get("position_flags") or {}
    if plan and _l(plan.get("positions")):
        for pos in _l(plan.get("positions")):
            if not isinstance(pos, Mapping):
                continue
            wc, wt = common.safe_float(pos.get("weight_cur_pct")), common.safe_float(pos.get("weight_tgt_pct"))
            chg = (wt - wc) if wc is not None and wt is not None else None
            prow.append([pos.get("ticker"), pos.get("asset_type"), _sector_name(pos.get("sector")),
                         fmt_pct(wc), fmt_pct(wt), fmt_pp(chg) if chg is not None else "n/a",
                         _action_text(pos), fmt_usd(pos.get("value_usd_cur")),
                         ", ".join(flags.get(_norm_t(pos.get("ticker")), [])) or "—"])
        if prow:
            tickers = [_norm_t(r[0]) for r in prow]
            show_flags = any(flags.get(tk) for tk in tickers)
            if not show_flags:
                prow = [r[:-1] for r in prow]
            blocks += [_sub("Positions (current vs target weight)"),
                       _tbl(["Ticker", "Type", "Sector", "Current", "Target", "Change", "Action", "Value (USD)"]
                            + (["Flags"] if show_flags else []), prow,
                            align="lllrrrlr" + ("l" if show_flags else ""), badges=[6])]
            legend = _flag_legend(flags, tickers) if show_flags else None
            if legend:
                blocks.append(legend)
    elif lt and _l(lt.get("holdings")):
        for h in _l(lt.get("holdings")):
            if isinstance(h, Mapping):
                prow.append([h.get("ticker"), h.get("asset_type"), _sector_name(h.get("sector")),
                             fmt_pct(common.fact_value(h.get("weight_pct"))),
                             ", ".join(flags.get(_norm_t(h.get("ticker")), [])) or "—"])
        tickers = [_norm_t(r[0]) for r in prow]
        show_flags = any(flags.get(tk) for tk in tickers)
        if not show_flags:
            prow = [r[:-1] for r in prow]
        blocks += [_sub("Positions (current weight)"),
                   _tbl(["Ticker", "Type", "Sector", "Current"] + (["Flags"] if show_flags else []), prow,
                        align="lllr" + ("l" if show_flags else ""))]
        legend = _flag_legend(flags, tickers) if show_flags else None
        if legend:
            blocks.append(legend)
        blocks.append(_p("Target position weights are not available in this run (no portfolio plan).", "muted"))
    else:
        blocks += [_sub("Positions"), _na()]
    if not lt and not plan and not ca and not cf:
        return [_na()]
    return blocks


# -------------------------------------------------------------------------------------
# The plain-English "Why" cell: Call (what the plan does) / Why (the numbers) / Watch (what would
# change it) / Model caveat (when the valuation is unreliable)
# -------------------------------------------------------------------------------------

_RULE_ID_RE = re.compile(r"\bR\d+(?:\.[A-Za-z_]+)+\b")
_RULE_LEAD_RE = re.compile(r"^\s*R\d+(?:\.[A-Za-z_]+)*\s+(?:fired|triggered|applies|applied)\b")

#: Words per Why cell the composer aims for (Call + Why + Watch + one extra sentence).
WHY_CELL_WORDS = 70
#: Lens names (specialist roles) in plain English.
LENS_NAMES: dict[str, str] = {"fundamental": "fundamental", "technical": "technical", "osint": "news",
                              "operator_ceo": "management", "industry_ops": "industry", "etf": "fund review"}
#: Valuation flag codes (script envelopes / DCF cells) that make the model's value less reliable.
_FINANCIALS_FLAGS = ("dcf_financials_caution", "financials_low_reliability")

_MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
           r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE_RE = re.compile(
    r"\b(?:(?P<what>[A-Za-z][A-Za-z/&' -]{2,40}?)\s+(?:due|on|expected|scheduled for|reported)\s+)?"
    r"(?P<date>" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r")\b",
    re.IGNORECASE)
#: Sentences in sector-head prose that state what the PLAN does with the position. The plan decides,
#: so these are never repeated (they used to contradict it, e.g. "stays in the portfolio" on a full exit).
_PLAN_CLAIM_RE = re.compile(
    r"stays? in the portfolio|stay(?:s|ed)? (?:in|on)\b|kept? as is|keeps? the (?:stock|position|holding)|"
    r"full(?:y)? exit|exits? the (?:whole|entire|position)|sell(?:s|ing)? (?:it )?(?:all|entirely|out)|"
    r"only (?:the )?(?:sector )?excess|(?:position|holding) is (?:kept|sold|exited)|cut(?:s)? back under|"
    r"only trimmed|trim reduces the position|no (?:sale|trade) is", re.IGNORECASE)
_STOPWORDS = frozenset(
    "that this with from have been were their there which while about than then them they also into over "
    "under more most some such only just does doing each both very much well still being would could should "
    "stock position holding sector".split())
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _scrub_jargon(text: Any) -> str:
    """Agent prose without internal rule codes (``R4.trim.limit_breach``) and with DA spelled out."""
    s = _RULE_ID_RE.sub("the rule", str(text or ""))
    s = re.sub(r"\bthe rule fired\b", "the rule applied", s)
    s = re.sub(r"\bDA\b", "Devil's Advocate", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_sentences(text: Any) -> list[str]:
    out: list[str] = []
    for piece in _SENTENCE_END_RE.split(re.sub(r"\s+", " ", str(text or "")).strip()):
        if out and _is_abbrev(out[-1].split()[-1]):
            out[-1] += " " + piece
        elif piece:
            out.append(piece)
    return out


def _strip_plan_claims(text: Any) -> str:
    """Sector-head prose with the sentences that state what the plan does with the position removed."""
    return " ".join(s for s in _split_sentences(text) if not _PLAN_CLAIM_RE.search(s))


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z-]{3,}", text.lower()) if w not in _STOPWORDS}


def _shorten_sentence(sentence: str, max_words: int) -> str:
    """A sentence of at most ``max_words`` words: when longer, its last clause after a contrast
    (', but' / ';'), else a hard cut with an ellipsis."""
    words = sentence.split()
    if len(words) <= max_words:
        return sentence
    m = list(re.finditer(r",? but |; ", sentence))
    if m:
        tail = sentence[m[-1].end():].strip()
        if 4 <= len(tail.split()) <= max_words:
            return tail[0].upper() + tail[1:]
    return " ".join(words[:max_words]).rstrip(",;") + "…"


def _lens_phrase(specialists: Any) -> str:
    """'fundamental bearish; technical, news and management neutral' (ok specialists with a stance)."""
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for s in _l(specialists):
        if isinstance(s, Mapping) and s.get("status") in (None, "ok") and s.get("stance"):
            groups.setdefault(str(s["stance"]), []).append(LENS_NAMES.get(str(s.get("role")), str(s.get("role"))))
    parts = []
    for stance, names in groups.items():
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        parts.append(f"{who} {stance}")
    return "; ".join(parts)


def _valuation_signals(ctx: Mapping, tk: str) -> dict:
    """Flags of the ticker's valuation / fundamentals files (envelope flags, DCF cell flags, median
    terminal-value share of the DCF cells). Empty when the files are absent."""
    codes: set[str] = set()
    tvs: list[float] = []
    for df in _l(ctx.get("data_files")):
        if not isinstance(df, Mapping) or df.get("ticker") != tk or not isinstance(df.get("doc"), Mapping):
            continue
        name = Path(_s(df.get("rel"))).name
        if not (name.startswith("valuation_") or name.startswith("fundamentals_")):
            continue
        doc = df["doc"]
        for f in _l(doc.get("flags")):
            if isinstance(f, Mapping) and f.get("code"):
                codes.add(str(f["code"]))
        for c in _l(doc.get("cells")):
            if not isinstance(c, Mapping):
                continue
            codes.update(str(x) for x in _l(c.get("flags")) if isinstance(x, str))
            tv = common.safe_float(c.get("tv_share"))
            if tv is not None and str(c.get("method") or "").startswith("dcf"):
                tvs.append(tv)
    tvs.sort()
    return {"codes": codes, "tv_median": tvs[len(tvs) // 2] if tvs else None}


def _model_caveat(ctx: Mapping, tk: str, price: float | None, fv: float | None) -> str:
    """Why the model's value should be verified rather than trusted ('' when nothing is off)."""
    sig = _valuation_signals(ctx, tk)
    codes = sig["codes"]
    bits = []
    if any(c.startswith("captive_finance") for c in codes):
        bits.append("the company has a captive finance arm whose debt distorts a debt-based value "
                    "(the model adjusts for it - verify the net-debt treatment)")
    elif "possible_captive_finance" in codes:
        bits.append("the company may have a finance arm that inflates its debt, which would distort the value")
    if any(c == "not_meaningful" or c.startswith("not_meaningful") for c in codes):
        bits.append("part of the valuation is not meaningful")
    if "tv_gt_90" in codes or (sig["tv_median"] is not None and sig["tv_median"] > 0.9):
        bits.append("most of the DCF value is the far-future terminal value")
    if any(c in codes for c in _FINANCIALS_FLAGS):
        bits.append("for banks, insurers and holding companies the DCF is of limited use")
    far = price and fv and price > 0 and fv > 0 and (fv / price < 0.6 or fv / price > 1.6)
    if far:
        bits.append("the model's value is far from the market price - treat it as a warning to verify, not a fact")
    if not bits:
        return ""
    text = "; ".join(bits)
    return text[0].upper() + text[1:] + "."


def _rr_clause(ctx: Mapping, v: Mapping | None, a: Mapping | None) -> str:
    obj = _rr_obj(a)
    raw = _num(_rr_value(v, a))
    dp = ctx["settings"]["rr_dp"]
    if raw is None:
        reason = str(obj.get("null_reason") or "")
        return {"missing_input": "reward-to-risk is unavailable (a price, fair value or stop is missing)",
                "price_below_invalidation": "the price is already below the stop",
                "stop_too_tight": "the stop is too close to the price for a meaningful reward-to-risk"}.get(reason, "")
    if raw < 0:
        return "reward-to-risk not meaningful"
    up, down = _num(obj.get("upside_pct")), _num(obj.get("downside_pct"))
    extra = f" (upside {fmt_pct(up, 0)} vs downside {fmt_pct(down, 0)} to the stop)" if up is not None and down is not None else ""
    return f"reward-to-risk {fmt_num(raw, dp)}{extra}"


def _limit_clauses(ctx: Mapping, tk: str, v: Mapping | None, a: Mapping | None,
                   sectors: dict[str, Mapping]) -> list[str]:
    """Position-cap / sector-limit breaches, each with the BINDING limit (one helper for the sector)."""
    inp = (a or {}).get("inputs") if isinstance((a or {}).get("inputs"), Mapping) else {}
    if not inp.get("limit_breach"):
        return []
    out: list[str] = []
    key = (a or {}).get("sector") or (v or {}).get("_sector") or _ticker_sector(ctx, tk)
    for r in _l(inp.get("limit_breach_reasons")):
        if str(r).startswith("position"):
            out.append(f"the position is {fmt_pct(inp.get('position_pct'))} of the portfolio vs the "
                       f"{fmt_threshold(inp.get('cap_pct'))}% single-stock cap")
        elif str(r).startswith("sector"):
            info = _sector_limit_info(ctx, key, inp)
            sectors[_s(key)] = inp
            lim = info["value"] if info["value"] is not None else inp.get("sector_limit_pct")
            clause = (f"{_sector_name(key)} is {fmt_pct(inp.get('sector_lookthrough_pct'))} of the portfolio "
                      f"vs the {fmt_threshold(lim)}% limit")
            res = next((x for x in _residual_items(ctx) if x.get("sector") == key), None)
            post = common.safe_float((res or {}).get("post_pct"))
            if post is not None:
                clause += f" and still {fmt_pct(post)} after the plan"
            out.append(clause)
    return out or ["a portfolio size limit is breached"]


def _shares_word(n: Any) -> str:
    return "share" if common.safe_float(n) == 1 else "shares"


def _call_text(pv: Mapping) -> str:
    """What the PLAN does with the position (never inferred from the verdict alone)."""
    ver, eff = pv["verdict"], pv["effective"]
    if not pv["has_plan"]:
        if ver in _SELL_VERDICTS + _BUY_VERDICTS:
            return f"{ver} verdict - the portfolio plan will size any trade (no plan in this run)."
        return f"{ver or 'No'} verdict - no change proposed."
    if pv["differs"]:
        return f"{ver} verdict, but no trade: {pv['reason_full'] or 'the plan does not act on it'}; effectively {eff}."
    n, cur = fmt_shares(pv["shares"]), fmt_shares(pv["cur_shares"])
    money = fmt_usd(pv["value_usd"])
    rule = pv["sizing_rule"]
    if pv["side"] == "SELL" and pv["shares"] > 0:
        why = (" to move the sector toward its limit" if "sector" in rule else
               " to get back under the single-stock cap" if "position" in rule else
               " because conviction is weak" if "conviction" in rule else "")
        if pv["exit_kind"] == "full":
            head = f"Sell all {n} {_shares_word(pv['shares'])} (about {money})"
            if ver == "SELL":
                return head + "; the verdict was SELL."
            need = (", because the sector limit needs the whole position" if "sector" in rule else
                    ", because the single-stock cap leaves no room" if "position" in rule else "")
            return head + f"; the verdict was {ver}, but the plan sizes it to a full exit{need}."
        pct = fmt_pct((pv["pct"] or 0) * 100, 0) if pv["pct"] is not None else None
        keep = pv["keep"]
        return (f"Sell {n} of {cur} shares" + (f" ({pct})" if pct else "") + f", about {money}" + ("," + why if why else "")
                + (f"; keep {fmt_shares(keep)}" if keep is not None else "") + ".")
    if pv["side"] == "BUY" and pv["shares"] > 0:
        return f"Buy {n} {_shares_word(pv['shares'])}, about {money}."
    w = pv["weight_cur"]
    if ver in _SELL_VERDICTS + _BUY_VERDICTS:
        return f"{ver} verdict, but the plan makes no trade."
    return (f"No trade: keep the {cur} {_shares_word(pv['cur_shares'])}"
            + (f" ({fmt_pct(w)} of the portfolio)." if w is not None else ".") if pv["cur_shares"] else "No trade.")


def _stop_price(a: Mapping | None, v: Mapping | None) -> tuple[float | None, str]:
    obj = _rr_obj(a)
    stop, src = _num(obj.get("invalidation")), _s(obj.get("invalidation_source"))
    if stop is None and v:
        stop = _num(v.get("invalidation_price"))
    if stop is None:
        for s in _l((a or {}).get("specialists")):
            if isinstance(s, Mapping) and _num(s.get("invalidation_price")) is not None:
                stop, src = _num(s["invalidation_price"]), _s(s.get("role")) + "_report"
                break
    return stop, src


def _key_date(texts: Iterable[Any]) -> str:
    for t in texts:
        m = _DATE_RE.search(_s(t))
        if m:
            what = " ".join((m.group("what") or "").split()[-3:])
            date = m.group("date")
            return f"{what.lower()} on {date}" if what else date
    return ""


def _watch_text(ctx: Mapping, tk: str, phase: int, v: Mapping | None, a: Mapping | None,
                kill_words: int = 18) -> str:
    """What would change the call: the stop price, the Devil's Advocate kill criterion, a key date."""
    items: list[str] = []
    stop, src = _stop_price(a, v)
    stop_txt = fmt_usd(stop, 2) if stop is not None else ""
    if stop is not None:
        items.append(f"a close below {stop_txt}" + (" (technical stop)" if src.startswith("technical") else " (the stop)"))
    da = ((_report(ctx, phase, "devils_advocate", tk) or {}).get("doc")) or {}
    kills = [str(k) for k in _l(da.get("kill_criteria")) if k]
    usable = [k for k in kills if "STOP" not in k and not (stop_txt and stop_txt.replace("$", "") in k.replace(",", ""))]
    kill = next((k for k in usable if len(k.split()) <= kill_words), min(usable, key=lambda k: len(k.split()), default=""))
    if kill:
        crit = _shorten_sentence(_scrub_jargon(kill).rstrip("."), kill_words)
        if crit.split(" ", 1)[0] in ("A", "An", "The"):
            crit = crit[0].lower() + crit[1:]
        items.append("also watch: " + crit)
    date = _key_date(kills + [_strip_plan_claims((v or {}).get("rationale")), da.get("verdict_reason")])
    if date and not (kill and date.split(" on ")[-1].lower() in kill.lower()):
        items.append("key date: " + date)
    return "; ".join(items)


def _extra_sentence(v: Mapping | None, composed: str, have: Mapping[str, bool]) -> str:
    """One sentence of the sector head's own reasoning, only if it adds a fact the composed text does
    not already state (no rule restatement, no plan claim, no repeated numbers / lenses / limits)."""
    text = _scrub_jargon((v or {}).get("rationale"))
    if not text:
        return ""
    known = _content_words(composed)
    for s in _split_sentences(text):
        low = s.lower()
        if _PLAN_CLAIM_RE.search(s) or _RULE_LEAD_RE.match(s) or "no limit is breached" in low:
            continue
        if re.search(r"\bconviction\b.*\b(below|above|balanced|neutral)\b", low):
            continue
        if have.get("value") and re.search(r"\b(price|valuation|fair value|overvalued|expensive|stretched)\b", low):
            continue
        if have.get("lens") and re.search(r"\b(neutral|bearish|bullish)\b", low):
            continue
        if have.get("limit") and re.search(r"\b(limit|cap|sector|over its|above its)\b", low):
            continue
        if _DATE_RE.search(s):
            continue
        words = _content_words(s)
        if len(words) < 3 or len(words - known) / len(words) < 0.5:
            continue
        return _shorten_sentence(s, 24)
    return ""


def compose_why(ctx: Mapping, tk: str, v: Mapping | None, a: Mapping | None, pos: Mapping | None,
                verdict_action: Any, *, phase: int = 2) -> dict:
    """The plain-English explanation of one holding, in labelled parts: ``Call`` (what the PLAN does,
    from plan fields, never from the verdict alone), ``Why`` (price vs fair value, conviction and the
    lens stances, reward-to-risk, the binding limit), ``Watch`` (stop / kill criterion / key date) and
    ``Model caveat`` (when the valuation is unreliable). Degrades gracefully when inputs are absent.
    Returns ``{"parts": [(label, text)], "cell": str, "sectors": {sector: aggregate inputs}, "plan": view}``."""
    cfg = ctx.get("cfg") or {}
    dp = ctx["settings"]["score_dp"]
    pv = _plan_view(pos, verdict_action)
    inp = (a or {}).get("inputs") if isinstance((a or {}).get("inputs"), Mapping) else {}
    obj = _rr_obj(a)
    price = _num(inp.get("price")) or _num(obj.get("price")) or _num((pos or {}).get("price"))
    fv = _num(obj.get("fv_base"))
    if fv is None and v:
        fv = _num(v.get("fair_value_base"))
    conv, ev = _det(v, a, "agg_conviction"), _det(v, a, "agg_evidence")
    label = (a or {}).get("rule_label")
    is_fund = (a or {}).get("asset_type") == "fund" or (ctx.get("holdings", {}).get(tk) or {}).get("asset_type") == "fund"
    sell_max = _cfg_num(cfg, "actions.holding.sell_max", None)
    trim_below = _cfg_num(cfg, "actions.holding.trim_below", None)
    da_verdict = ((a or {}).get("da") or {}).get("verdict") if isinstance((a or {}).get("da"), Mapping) else None

    why: list[str] = []
    if price and fv and price > 0 and fv > 0:
        gap = (price / fv - 1) * 100
        if abs(gap) < 5:
            why.append(f"price {fmt_usd(price, 2)} is close to the model's fair value {fmt_usd(fv, 2)}")
        else:
            why.append(f"price {fmt_usd(price, 2)} is {fmt_num(abs(gap), 0)}% {'above' if gap > 0 else 'below'} "
                       f"the model's fair value {fmt_usd(fv, 2)}")
    elif is_fund:
        why.append("a fund has no fair value in the model")
    elif a or v:
        why.append("no model fair value in this run")
    lens = _lens_phrase((a or {}).get("specialists"))
    if conv is not None:
        c = fmt_num(conv, dp)
        head = f"conviction {c}/10" + (f" ({lens})" if lens else "")
        tail = {"sell_conviction": " - at or below the sell level" + (f" ({fmt_num(sell_max, 1)})" if sell_max is not None else ""),
                "trim_conviction": " - weak" + (f" (below {fmt_num(trim_below, 1)})" if trim_below is not None else ""),
                "add": f" - with evidence {fmt_num(ev, dp)}/10 and reward-to-risk, enough to add",
                "insufficient_evidence": f" - but evidence {fmt_num(ev, dp)}/10 is too thin to act on",
                "sell_da_broken": " - and the Devil's Advocate judged the thesis broken",
                }.get(label, "")
        why.append(head + tail)
    if da_verdict in ("damaged", "broken") and label != "sell_da_broken":
        why.append(f"the Devil's Advocate rated the thesis {da_verdict}")
    rr = _rr_clause(ctx, v, a)
    if rr and not (fv is None and "unavailable" in rr):
        why.append(rr)
    sectors: dict[str, Mapping] = {}
    limits = _limit_clauses(ctx, tk, v, a, sectors)
    why += limits
    rule_action = _det(v, a, "rule_action")
    if v and v.get("rule_deviation") and rule_action and v.get("action") and rule_action != v.get("action"):
        just = _shorten_sentence(_scrub_jargon(v.get("deviation_justification")).rstrip("."), 22)
        why.append(f"the rule said {rule_action} but the sector head chose {v.get('action')}"
                   + (f": {just}" if just else ""))
    call = _call_text(pv)
    watch = _watch_text(ctx, tk, phase, v, a)
    if len(call.split()) + len(" ".join(why).split()) + len(watch.split()) > WHY_CELL_WORDS + 15:
        watch = _watch_text(ctx, tk, phase, v, a, kill_words=11)
    composed = " ".join([call] + why + [watch])
    have = {"value": bool(price and fv) or is_fund, "lens": bool(lens), "limit": bool(limits)}
    extra = _extra_sentence(v, composed, have)
    use_extra = bool(extra) and len(composed.split()) + len(extra.split()) <= WHY_CELL_WORDS + 8
    why_txt = "; ".join(why)
    if why_txt:
        why_txt = why_txt[0].upper() + why_txt[1:]
        if not why_txt.endswith((".", "!", "?", "…")):
            why_txt += "."
    if use_extra:
        why_txt = (why_txt + " " + extra.rstrip() + ("" if extra.rstrip().endswith((".", "!", "?", "…")) else ".")).strip()
    caveat = _model_caveat(ctx, tk, price, fv)
    parts: list[tuple[str, str]] = [("Call", call)]
    if why_txt:
        parts.append(("Why", why_txt))
    elif not (a or v):
        parts.append(("Why", "No verdict in this run."))
    if watch:
        parts.append(("Watch", watch))
    if caveat:
        parts.append(("Model caveat", caveat))
    return {"parts": parts, "plan": pv, "sectors": sectors, "extra": extra if use_extra else "",
            "cell": CELL_BREAK.join(f"{k}: {t}" for k, t in parts)}


def _verdict_row(ctx: Mapping, t: str, phase: int, notes: _RrNotes | None = None) -> dict:
    v = ctx["verdict_index"].get((phase, t))
    a = ctx["aggregates"].get((phase, t))
    fa = ctx["final_actions"].get(t)
    pos = ctx["plan_positions"].get(t)
    st = ctx["settings"]
    verdict_action = (fa or {}).get("action") or (v or {}).get("action") or (a or {}).get("rule_action")
    pv = _plan_view(pos, verdict_action)
    rule = _det(v, a, "rule_action")
    conv = _det(v, a, "agg_conviction")
    ev = _det(v, a, "agg_evidence")
    cells = _rr_cells(ctx, t, v, a, notes)
    sectors: dict[str, Mapping] = {}
    if (v and v.get("rationale")) or a:
        comp = compose_why(ctx, t, v, a, pos, verdict_action, phase=phase)
        why, sectors, parts = comp["cell"], comp["sectors"], comp["parts"]
    else:
        why, parts = "No verdict in this run.", []
    flags_note = []
    if fa and fa.get("override"):
        flags_note.append("CIO override")
    if v and v.get("rule_deviation"):
        flags_note.append("sector-head deviation")
    if flags_note:
        why += CELL_BREAK + f"Note: {'; '.join(flags_note)}"
    action = _action_label(pv, with_exit=True) if verdict_action else "n/a"
    return {"ticker": t, "sector": (v or {}).get("_sector") or (a or {}).get("sector") or _ticker_sector(ctx, t),
            "action": action, "verdict_action": verdict_action or "n/a", "plan": pv, "rule": rule or "n/a",
            "conv": fmt_num(conv, st["score_dp"]),
            "ev": fmt_num(ev, st["score_dp"]), "rr": cells["rr"], "rr_title": cells["rr_title"],
            "rr_raw": cells["rr_raw"], "fv": cells["fv"], "stop": cells["stop"], "stop_title": cells["stop_title"],
            "why": why, "parts": parts, "sector_inputs": sectors, "verdict": v, "agg": a, "final": fa}


def _sec_holdings(ctx: Mapping, t: str | None) -> list[dict]:
    tickers = [t] if t else list(ctx["holdings"])
    flags = ctx.get("position_flags") or {}
    show_flags = any(flags.get(tk) for tk in tickers)
    notes = _RrNotes(ctx["settings"]["rr_dp"])
    rows = []
    titles: dict[tuple[int, int], str] = {}
    details: list[dict] = []
    sector_inputs: "OrderedDict[str, Mapping]" = OrderedDict()
    for tk in tickers:
        phase = 2
        if t and (2, tk) not in ctx["verdict_index"] and (2, tk) not in ctx["aggregates"]:
            phase = 3 if ((3, tk) in ctx["verdict_index"] or (3, tk) in ctx["aggregates"]) else 2
        r = _verdict_row(ctx, tk, phase, notes)
        sector_inputs.update(r["sector_inputs"])
        flag_cell = [", ".join(flags.get(tk, [])) or "—"] if show_flags else []
        if not t and r["verdict"] is None and r["agg"] is None and r["final"] is None:
            rows.append([tk, _sector_name(r["sector"]), "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"]
                        + flag_cell + ["No verdict in this run."])
            continue
        rows.append([tk, _sector_name(r["sector"]), r["action"], r["rule"], r["conv"], r["ev"], r["rr"],
                     r["fv"], r["stop"]] + flag_cell + [r["why"]])
        if r["rr_title"]:
            titles[(len(rows) - 1, 6)] = r["rr_title"]
        if r["stop_title"]:
            titles[(len(rows) - 1, 8)] = r["stop_title"]
        if t:
            details = _trial_details(ctx, tk, phase, r)
    if not rows:
        return [_na()]
    headers = ["Ticker", "Sector", "Action", "Rule", "Conviction", "Evidence", "R/R", "Fair value (base)", "Stop"]
    headers += ["Flags"] if show_flags else []
    headers += ["Why"]
    blocks = [_tbl(headers, rows, align="llllrrrrr" + ("l" if show_flags else "") + "w", badges=[2, 3],
                   titles=titles)]
    foot = notes.paragraph()
    if foot:
        blocks.append(foot)
    limit_note = _sector_limit_note(ctx, sector_inputs)
    if limit_note:
        blocks.append(_p(limit_note, "muted"))
    legend = _flag_legend(flags, tickers) if show_flags else None
    if legend:
        blocks.append(legend)
    if not t:
        action_src = ("Action = what the portfolio plan will do, from the final committee action (CIO); "
                      "'TRIM -> HOLD' means the verdict is TRIM but the plan makes no trade"
                      if ctx["cio_final"] is not None else
                      "Action = sector-head verdict (the CIO's final reconciliation is not available in this run)")
        blocks.append(_p(f"{action_src}; Rule = deterministic rule output from "
                         "aggregate.py. Conviction and evidence are aggregated 1–10 scores; R/R = upside to base "
                         "fair value ÷ downside to the invalidation price (Stop); Fair value (base) is the "
                         "valuation anchor of that ratio. R/R is n/a for funds. Section 9 lists every "
                         "deviation with its justification.", "muted"))
    return blocks + details


def _trial_details(ctx: Mapping, t: str, phase: int, r: Mapping) -> list[dict]:
    st = ctx["settings"]
    blocks: list[dict] = []
    v, a = r["verdict"], r["agg"]
    if r.get("parts"):
        blocks += [_sub("Call, reasoning and what to watch"), _kv(r["parts"])]
    rationale = _strip_plan_claims(_scrub_jargon((v or {}).get("rationale")))
    if rationale:      # the sector head's own words, minus rule codes and any claim about what the plan does
        blocks += [_sub("Sector-head rationale"), _p(rationale)]
    if a:
        inp = a.get("inputs") if isinstance(a.get("inputs"), Mapping) else {}
        rr = a.get("rr") if isinstance(a.get("rr"), Mapping) else {}
        da = a.get("da") if isinstance(a.get("da"), Mapping) else {}
        pairs = [("Price", fmt_fact(inp.get("price"))),
                 ("Position / cap", f"{fmt_pct(inp.get('position_pct'))} / {fmt_pct(inp.get('cap_pct'))}"),
                 ("Sector look-through / CIO target",
                  f"{fmt_pct(inp.get('sector_lookthrough_pct'))} / {fmt_pct(inp.get('cio_target_pct'))}"),
                 ("Limit breach", "yes" if inp.get("limit_breach") else "no"),
                 ("Conviction mean (before DA)", fmt_num(a.get("conviction_mean"), 2)),
                 ("Devil's advocate", f"{da.get('verdict') or 'n/a'} (adjustment {fmt_num(da.get('adj'), 0, sign=True)})"
                  if da else "n/a")]
        if rr:
            pairs.append(("Risk/reward", f"{fmt_num(rr.get('rr'), st['rr_dp'])} (fair value {fmt_usd(rr.get('fv_base'), 2)}, "
                                         f"invalidation {fmt_usd(rr.get('invalidation'), 2)} via "
                                         f"{rr.get('invalidation_source') or 'n/a'}; upside {fmt_pct(rr.get('upside_pct'), 1)}, "
                                         f"downside {fmt_pct(rr.get('downside_pct'), 1)})"
                                         + _rr_suffix(rr)))
        blocks += [_sub("Aggregation inputs"), _kv(pairs)]
        srows = []
        for s in _l(a.get("specialists")):
            if isinstance(s, Mapping):
                srows.append([s.get("role"), s.get("status"), s.get("stance") or "—", fmt_num(s.get("conviction"), 0),
                              fmt_num(s.get("effective_conviction"), st["score_dp"]),
                              fmt_num(s.get("evidence"), st["score_dp"]), "yes" if s.get("in_mean") else "no"])
        if srows:
            blocks += [_sub("Specialist views"),
                       _tbl(["Role", "Status", "Stance", "Conviction", "Effective", "Evidence", "In mean"], srows,
                            align="lllrrrl", badges=[1, 2])]
        trows = [[x.get("rule"), x.get("test"), x.get("result")] for x in _l(a.get("rule_trace"))
                 if isinstance(x, Mapping)]
        if trows:
            blocks += [_sub("Rule trace"), _tbl(["Rule", "Test", "Result"], trows)]
    return blocks


def _sec_proposals(ctx: Mapping, t: str | None) -> list[dict]:
    st = ctx["settings"]
    cands: "OrderedDict[str, tuple[int, dict]]" = OrderedDict()
    for phase in (3, 2):
        for (ph, tk), v in ctx["verdict_index"].items():
            if ph == phase and v.get("kind") == "candidate" and tk not in cands:
                cands[tk] = (phase, v)
    for tk, fa in ctx["final_actions"].items():
        if fa.get("action") == "BUY" and tk not in cands and tk not in ctx["holdings"]:
            cands[tk] = (3, {"ticker": tk, "kind": "candidate", "action": fa.get("source_verdict_action")})
    blocks: list[dict] = []
    buys = [(tk, ph, v) for tk, (ph, v) in cands.items()
            if v.get("action") == "BUY" or (ctx["final_actions"].get(tk) or {}).get("action") == "BUY"]
    for tk, ph, v in buys:
        blocks.append(_thesis_card(ctx, tk, ph, v))
    rows: list[list[Any]] = []
    titles: dict[tuple[int, int], str] = {}
    notes = _RrNotes(st["rr_dp"])

    def add_row(tk: str, v: Mapping | None, a: Mapping | None, head: list[Any], why: str) -> None:
        cells = _rr_cells(ctx, tk, v, a, notes)
        rows.append(head + [cells["rr"], cells["fv"], cells["stop"], why])
        if cells["rr_title"]:
            titles[(len(rows) - 1, 6)] = cells["rr_title"]
        if cells["stop_title"]:
            titles[(len(rows) - 1, 8)] = cells["stop_title"]

    if cands:
        for tk, (ph, v) in cands.items():
            a = ctx["aggregates"].get((ph, tk))
            fa = ctx["final_actions"].get(tk)
            add_row(tk, v, a,
                    [tk, _sector_name(v.get("_sector") or (a or {}).get("sector")),
                     (fa or {}).get("action") or v.get("action") or "n/a", _det(v, a, "rule_action") or "n/a",
                     fmt_num(_det(v, a, "agg_conviction"), st["score_dp"]),
                     fmt_num(_det(v, a, "agg_evidence"), st["score_dp"])],
                    one_line(v.get("rationale")) or "—")
    for (ph, tk), a in ctx["aggregates"].items():
        if ph == 3 and a.get("_light") and tk not in cands and tk not in ctx["holdings"]:
            add_row(tk, None, a,
                    [tk, _sector_name(a.get("sector")), "—", a.get("rule_action") or "n/a",
                     fmt_num(a.get("agg_conviction"), st["score_dp"]),
                     fmt_num(a.get("agg_evidence"), st["score_dp"])],
                    "Light review only — not selected as a finalist.")
    if rows:
        blocks += [_sub("Candidates reviewed"),
                   _tbl(["Ticker", "Sector", "Action", "Rule", "Conviction", "Evidence", "R/R", "Fair value (base)",
                         "Stop", "Why"], rows, align="llllrrrrrl", badges=[2, 3], titles=titles)]
        foot = notes.paragraph()
        if foot:
            blocks.append(foot)
    expand = _l(ctx["manifest"].get("expand_sectors")) or _l((ctx["phase1"]["cio_allocation"] or {}).get("expand_sectors"))
    dropped = set(_l(ctx["manifest"].get("expand_dropped")))
    buy_sectors = {(v.get("_sector") or "") for _, _, v in buys}
    empty = [s for s in expand if s not in dropped and s not in buy_sectors]
    for s in empty:
        reviewed = s in ctx["verdicts"][3]
        blocks.append(_p(f"{_sector_name(s)}: no BUY proposal"
                         + (" — zero picks is a valid outcome." if reviewed else " (no Phase 3 verdict in this run)."),
                         "muted"))
    if not blocks:
        if ctx["mode"].get("quick"):
            return [_p("Phase 3 (stock picking) is skipped in quick mode — no new proposals.", "muted")]
        if ctx["phase1"]["cio_allocation"] and not expand:
            return [_p("The CIO named no sectors to expand, so Phase 3 proposed no new positions.", "muted")]
        return [_na()]
    return blocks


def _thesis_card(ctx: Mapping, tk: str, ph: int, v: Mapping) -> dict:
    st = ctx["settings"]
    a = ctx["aggregates"].get((ph, tk)) or {}
    fa = ctx["final_actions"].get(tk) or {}
    fund = _report(ctx, ph, "fundamental", tk)
    fdoc = (fund or {}).get("doc") or {}
    val = ((fdoc.get("extensions") or {}).get("valuation") or {}) if isinstance(fdoc.get("extensions"), Mapping) else {}
    rr = a.get("rr") if isinstance(a.get("rr"), Mapping) else {}
    inp = a.get("inputs") if isinstance(a.get("inputs"), Mapping) else {}
    da = a.get("da") if isinstance(a.get("da"), Mapping) else {}
    pos = ctx["plan_positions"].get(tk) or {}
    trade = pos.get("trade") if isinstance(pos.get("trade"), Mapping) else {}

    def fv(key: str) -> Any:
        return v.get(key) if v.get(key) is not None else val.get(key)

    thesis = v.get("thesis") or fdoc.get("thesis")
    blocks: list[dict] = [_p(thesis) if thesis else _p("No thesis text recorded.", "muted")]
    final_action = fa.get("action") or v.get("action")
    pairs: list[tuple[str, Any]] = [
        ("Sector", _sector_name(v.get("_sector") or a.get("sector"))),
        ("Actions (rule / sector head / CIO)", f"{_det(v, a, 'rule_action') or 'n/a'} / "
                                               f"{v.get('action') or 'n/a'} / {fa.get('action') or 'n/a'}"
                                               + (f" -> plan: {_action_label(_plan_view(pos, final_action))}"
                                                  if pos and _plan_view(pos, final_action)["differs"] else "")),
        ("Price", fmt_fact(inp.get("price")) if inp.get("price") else fmt_usd(rr.get("price"), 2)),
        ("Fair value (low / base / high)", f"{fmt_usd(fv('fair_value_low'), 2)} / {fmt_usd(fv('fair_value_base'), 2)} / "
                                           f"{fmt_usd(fv('fair_value_high'), 2)}"),
        ("Valuation method", val.get("method") or "n/a"),
        ("Invalidation price", fmt_usd(v.get("invalidation_price") if v.get("invalidation_price") is not None
                                       else rr.get("invalidation"), 2)
         + (f" (stop source: {rr['invalidation_source']})" if rr.get("invalidation_source") else "")),
        ("Risk/reward", f"{fmt_num(_rr_value(v, a), st['rr_dp'])} (upside {fmt_pct(rr.get('upside_pct'))}, "
                        f"downside {fmt_pct(rr.get('downside_pct'))})" + _rr_suffix({**rr, "rr": _rr_value(v, a)})),
        ("Conviction / evidence", f"{fmt_num(_det(v, a, 'agg_conviction'), st['score_dp'])}"
                                  f" / {fmt_num(_det(v, a, 'agg_evidence'), st['score_dp'])}"),
        ("Devil's advocate", da.get("verdict") or "n/a"),
    ]
    if pos:
        pairs.append(("Proposed size", f"target {fmt_pct(pos.get('weight_tgt_pct'))}; "
                                       f"{trade.get('side') or 'NONE'} {fmt_shares(trade.get('shares'))} shares "
                                       f"({fmt_usd(trade.get('value_usd'))})"
                      + (f" — dropped: {pos['dropped_reason']}" if pos.get("dropped_reason") else "")))
    blocks.append(_kv(pairs))
    dis = [f"{d.get('role')}: {d.get('view')}" for d in _l(v.get("dissents")) if isinstance(d, Mapping)]
    if dis:
        blocks += [_sub("Dissents"), _bul(dis)]
    flags = [str(f) for f in _l(v.get("unresolved_red_flags"))]
    if flags:
        blocks += [_sub("Unresolved red flags"), _bul(flags)]
    if fa.get("override") and fa.get("override_justification"):
        blocks.append(_p(f"CIO override: {fa['override_justification']}", "warn"))
    shown = _action_label(_plan_view(pos, final_action)) if pos and final_action else final_action
    return _card(f"{tk} — {_sector_name(v.get('_sector') or a.get('sector'))} · {shown or 'n/a'}", blocks)


_USD_ASSET_SENTENCE_RE = re.compile(r"\s*Every holding is a USD asset[^.]*\.")


def _clean_plan_note(note: str, exposure: Mapping | None) -> str:
    """portfolio.py's currency note claims "Every holding is a USD asset". When the holdings show
    non-USD listings or TASE funds priced through US proxies that sentence is wrong, and the
    Currency breakdown above says it correctly — drop the sentence (nothing else is changed)."""
    if exposure and (exposure.get("ils") or exposure.get("proxy") or exposure.get("other")):
        return _USD_ASSET_SENTENCE_RE.sub("", note).strip()
    return note


def _currency_exposure(ctx: Mapping, positions: list[Mapping]) -> dict:
    """Exposure by LISTING currency, from the run's ``inputs/portfolio.yaml`` snapshot (``currency``
    and ``proxy_ticker`` of each holding; the look-through supplies the proxy when the snapshot is
    missing). Groups: ``usd`` (USD-listed), ``proxy`` (TASE-style funds priced through a US proxy:
    ILS-listed units, economically USD), ``ils`` (ILS-listed) and ``other`` (other currencies);
    each is ``[(ticker, weight_pct, detail)]``. ``source`` says whether the snapshot was available."""
    cfg_rows = ctx.get("portfolio_cfg") or {}
    lk_rows = {_norm_t(h.get("ticker")): h for h in _l((ctx["phase1"]["lookthrough"] or {}).get("holdings"))
               if isinstance(h, Mapping) and h.get("ticker")}
    groups: dict[str, list] = {"usd": [], "proxy": [], "ils": [], "other": []}
    for pos in positions:
        w = common.safe_float(pos.get("weight_cur_pct"))
        if not (common.safe_float(pos.get("shares_cur")) or w):
            continue
        tk = _norm_t(pos.get("ticker"))
        row = cfg_rows.get(tk) or {}
        proxy = row.get("proxy_ticker") or (lk_rows.get(tk) or {}).get("proxy_ticker")
        ccy = str(row.get("currency") or "").strip().upper()
        if not ccy:
            ccy = "ILS" if tk.endswith(".TA") else "USD"
        w = w or 0.0
        if proxy:
            groups["proxy"].append((tk, w, str(proxy)))
        elif ccy == "USD":
            groups["usd"].append((tk, w, ccy))
        elif ccy == "ILS":
            groups["ils"].append((tk, w, ccy))
        else:
            groups["other"].append((tk, w, ccy))
    groups["source"] = "portfolio.yaml snapshot" if cfg_rows else "plan / look-through only"
    return groups


def _currency_blocks(ctx: Mapping, plan: Mapping, positions: list[Mapping], cash: Mapping) -> list[dict]:
    st = ctx["settings"]
    fx = plan.get("fx_usdils") or ((ctx["phase1"]["prices"] or {}).get("fx") or {}).get("usdils")
    base = plan.get("base_currency") or st["base_currency"]
    total = common.fact_value(plan.get("total_value_usd"))
    fxv = common.fact_value(fx) if isinstance(fx, Mapping) else None
    ex = _currency_exposure(ctx, positions)

    def pct(group: str) -> float:
        return sum(w for _t, w, _d in ex[group])

    def names(group: str, detail: bool = False) -> str:
        return ", ".join(f"{t} ({d})" if detail else t for t, _w, d in ex[group])

    pairs: list[tuple[str, Any]] = [
        ("Base currency", base),
        ("USD/ILS", fmt_fact(fx, 4) if isinstance(fx, Mapping) else "n/a — ILS values not available")]
    if total is not None:
        pairs.append(("Total value", fmt_usd(total) + (f" ≈ {fmt_ils(total * fxv)}" if fxv else "")))
    if ex["usd"]:
        pairs.append(("USD-listed holdings", f"{fmt_pct(pct('usd'))} — {names('usd')}"))
    if ex["proxy"]:
        pairs.append(("TASE funds priced via US proxies",
                      f"{fmt_pct(pct('proxy'))} — {names('proxy', True)}: ILS-listed units, economically USD "
                      f"(they track the proxy's US index)"))
    if ex["ils"]:
        pairs.append(("ILS-listed holdings", f"{fmt_pct(pct('ils'))} — {names('ils')}"))
    if ex["other"]:
        pairs.append(("Other-currency holdings", f"{fmt_pct(pct('other'))} — {names('other', True)}"))
    cash_pct = common.safe_float(cash.get("current_pct")) if cash else None
    if cash_pct is not None:
        pairs.append(("Cash", f"{fmt_pct(cash_pct)} — currency split not recorded (portfolio.yaml has one USD "
                              f"figure, cash_usd), so cash is not counted as USD or ILS below"))
    usd_econ = pct("usd") + pct("proxy")
    pairs.append(("Economic USD exposure (securities)", fmt_pct(usd_econ)
                  + " (USD-listed holdings plus TASE funds priced via US proxies)"))
    if ex["ils"] or ex["other"]:
        pairs.append(("ILS / other-currency securities", fmt_pct(pct("ils") + pct("other"))))
    blocks = [_sub("Currency"), _kv(pairs)]
    if ex["source"] != "portfolio.yaml snapshot":
        blocks.append(_p("The run has no portfolio.yaml snapshot, so listing currencies are not known; holdings "
                         "without a proxy are shown as USD-listed unless their ticker ends in .TA.", "muted"))
    text = (f"Prices and trade values are in USD while the base currency is {base}. A move in USD/ILS changes "
            f"the {base} value of the economically-USD exposure ({fmt_pct(usd_econ)} of the portfolio) roughly "
            f"one-for-one, independent of the securities themselves")
    text += ("; ILS-listed holdings are not exposed to it" if ex["ils"] else "")
    text += (", and cash is excluded because its currency split is not recorded" if cash_pct is not None else "")
    blocks.append(_p(f"{text}. {base} amounts use the rate shown.", "muted"))
    return blocks


def _sec_rebalancing(ctx: Mapping, t: str | None) -> list[dict]:
    plan = ctx["plan"]
    st = ctx["settings"]
    if not plan:
        return [_na()]
    blocks: list[dict] = []
    if ctx["plan_kind"] == "preview":
        blocks.append(_p("This is the pre-CIO preview (portfolio_preview.json); the final plan was not produced "
                         "in this run.", "warn"))
    positions = [p for p in _l(plan.get("positions")) if isinstance(p, Mapping)]
    rows = []
    buys = sells = 0.0
    for pos in positions:
        tr = pos.get("trade") if isinstance(pos.get("trade"), Mapping) else {}
        side = tr.get("side") or "NONE"
        if side == "NONE" and not pos.get("dropped_reason"):
            continue
        usd = common.safe_float(tr.get("value_usd"))
        if usd is not None and not pos.get("dropped_reason"):
            if side == "BUY":
                buys += abs(usd)
            elif side == "SELL":
                sells += abs(usd)
        pv = _plan_view(pos)
        rows.append([pos.get("ticker"), side, fmt_shares(tr.get("shares")), fmt_usd(tr.get("value_usd")),
                     fmt_ils(tr.get("value_ils")), _action_label(pv),
                     pv["exit_kind"] if pv["exit_kind"] != "none" else "—",
                     fmt_pct(pv["pct"] * 100, 0) if pv["pct"] is not None and side == "SELL" else "—",
                     pos.get("dropped_reason") or "—"])
    blocks.append(_sub("Trades"))
    cash = plan.get("cash") if isinstance(plan.get("cash"), Mapping) else {}
    cash_pair = [("Cash current / floor (profile) / post-trade",
                  f"{fmt_pct(cash.get('current_pct'))} / {fmt_pct(cash.get('target_pct'))} / "
                  f"{fmt_pct(cash.get('post_pct'))}")] if cash else []
    if rows:
        blocks.append(_tbl(["Ticker", "Side", "Shares", "USD", "ILS", "Action", "Exit", "Of position", "Dropped reason"],
                           rows, align="llrrrllrl", badges=[1, 5]))
        blocks.append(_kv([("Total buys", fmt_usd(buys)), ("Total sells", fmt_usd(sells)),
                           ("Net cash flow (sells − buys)", fmt_usd(sells - buys))] + cash_pair))
    else:
        blocks.append(_p("No trades proposed.", "muted"))
        if cash_pair:
            blocks.append(_kv(cash_pair))
    resid = _residual_items(ctx)
    if resid:
        blocks.append(_sub("Residual breaches"))
        blocks.append(_bul(_residual_text(ctx, r) for r in resid))
    checks = [[c.get("check"), c.get("status"), c.get("detail")] for c in _l(plan.get("constraint_checks"))
              if isinstance(c, Mapping)]
    blocks.append(_sub("Constraint checks"))
    blocks.append(_tbl(["Check", "Status", "Detail"], checks, badges=[1]) if checks else _na())

    blocks.append(_sub("Tax notes"))
    trows = []
    for pos in positions:
        tax = pos.get("tax") if isinstance(pos.get("tax"), Mapping) else {}
        if tax.get("applicable"):
            trows.append([pos.get("ticker"), fmt_usd(tax.get("est_gain_usd")), fmt_usd(tax.get("est_tax_usd")),
                          tax.get("note") or "—"])
    if trows:
        blocks.append(_tbl(["Ticker", "Est. gain (USD)", "Est. tax (USD)", "Note"], trows, align="lrrl"))
    else:
        blocks.append(_p("No taxable sale or trim with a known cost basis in this plan.", "muted"))
    blocks.append(_p(f"Tax figures are rough estimates: a flat {fmt_threshold(st['tax_pct'])}% applied to the nominal "
                     f"USD gain from the cost basis in portfolio.yaml. Israeli capital-gains tax is levied on the "
                     f"real gain (CPI/FX-adjusted), so the actual liability can differ — consult a tax advisor "
                     f"before acting.", "muted"))

    blocks += _currency_blocks(ctx, plan, positions, cash)
    exposure = _currency_exposure(ctx, positions)
    notes = [_clean_plan_note(_s(n), exposure) for n in _l(plan.get("notes")) if n]
    for pos in positions:
        notes += [f"{pos.get('ticker')}: {n}" for n in _l(pos.get("notes")) if n]
        if isinstance(pos.get("note"), str) and pos["note"].strip():
            notes.append(f"{pos.get('ticker')}: {pos['note'].strip()}")
    notes = [n for n in notes if n]
    if notes:
        blocks += [_sub("Plan notes"), _bul(notes)]
    return blocks


def _da_records(ctx: Mapping, t: str | None) -> list[dict]:
    recs = [r for r in ctx["reports"] if r["role"] == "devils_advocate" and (t is None or r["ticker"] == t)]
    order = {tk: i for i, tk in enumerate(ctx["holdings"])}
    return sorted(recs, key=lambda r: (r["phase"] or 9, order.get(r["ticker"], 999), r["ticker"] or ""))


def _sec_da(ctx: Mapping, t: str | None) -> list[dict]:
    blocks: list[dict] = []
    overview = []
    cards = []
    for rec in _da_records(ctx, t):
        tk = rec["ticker"] or "?"
        if rec["status"] != "ok":
            stub = rec.get("stub") or {}
            overview.append([tk, f"phase {rec['phase']}", rec["status"], "n/a", "n/a"])
            cards.append(_p(f"{tk} (phase {rec['phase']}): the devil's advocate report "
                            f"{'failed after ' + str(stub.get('attempts')) + ' attempts' if stub.get('attempts') else 'is missing'}"
                            " — no DA adjustment was applied.", "warn"))
            continue
        doc = rec["doc"]
        attacks = [x for x in _l(doc.get("attacks")) if isinstance(x, Mapping)]
        high = [x for x in attacks if str(x.get("severity")) == "high"]
        overview.append([tk, f"phase {rec['phase']}", doc.get("verdict_on_thesis") or "n/a",
                         fmt_usd(doc.get("bear_case_price"), 2), f"{len(high)} of {len(attacks)}"])
        inner: list[dict] = []
        if doc.get("verdict_reason"):
            inner.append(_p(doc["verdict_reason"]))
        inner.append(_kv([("Verdict on thesis", doc.get("verdict_on_thesis") or "n/a"),
                          ("Bear-case price", fmt_usd(doc.get("bear_case_price"), 2)
                           + (f" (ref {doc['bear_case_ref']})" if doc.get("bear_case_ref") else "")),
                          ("Report as of", doc.get("as_of") or "n/a")]))
        inner.append(_sub("High-severity attacks"))
        if high:
            inner.append(_bul(f"vs {x.get('target_agent')} — claim: {one_line(x.get('target_claim'), 40)} "
                              f"Counter-evidence: {x.get('counter_evidence')}" for x in high))
        else:
            inner.append(_p(f"No high-severity attacks ({len(attacks)} lower-severity).", "muted"))
        kills = [k for k in _l(doc.get("kill_criteria")) if k]
        if kills:
            inner += [_sub("Kill criteria"), _bul(kills)]
        v = ctx["verdict_index"].get((rec["phase"], tk))
        if v and v.get("da_rebuttal"):
            inner.append(_p(f"Sector-head rebuttal: {v['da_rebuttal']}", "warn"))
        cards.append(_card(f"{tk} — {_sector_name(rec.get('sector') or _ticker_sector(ctx, tk))} "
                           f"(phase {rec['phase']}): thesis {doc.get('verdict_on_thesis') or 'n/a'}", inner))
    if overview:
        blocks += [_tbl(["Ticker", "Phase", "Verdict", "Bear case", "High attacks"], overview, align="lllrr",
                        badges=[2])]
        blocks += cards
    else:
        blocks += [_sub("Devil's advocate reports"), _na()]
    drows = []
    for (ph, tk), v in sorted(ctx["verdict_index"].items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if t is not None and tk != t:
            continue
        for d in _l(v.get("dissents")):
            if isinstance(d, Mapping):
                drows.append([tk, f"phase {ph}", d.get("role"), d.get("view")])
    blocks.append(_sub("Dissents recorded by sector heads"))
    blocks.append(_tbl(["Ticker", "Phase", "Role", "View"], drows) if drows else _p("No dissents recorded.", "muted"))
    cf = ctx["cio_final"]
    if t is None:
        blocks.append(_sub("Committee dissent summary (CIO)"))
        blocks.append(_p(cf["dissent_summary"]) if cf and cf.get("dissent_summary") else _na())
    if not overview and not drows and not (cf and cf.get("dissent_summary")):
        return [_na()]
    return blocks


def _sec_overrides(ctx: Mapping, t: str | None) -> list[dict]:
    blocks: list[dict] = []
    dev = []
    for (ph, tk), v in sorted(ctx["verdict_index"].items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if t is not None and tk != t:
            continue
        rule = _det(v, ctx["aggregates"].get((ph, tk)), "rule_action")
        if v.get("rule_deviation") or (rule and v.get("action") and rule != v["action"]):
            dev.append([tk, _sector_name(v.get("_sector")), f"phase {ph}", rule, v.get("action"),
                        v.get("deviation_justification") or "No justification recorded."])
    blocks.append(_sub("Sector-head rule deviations"))
    blocks.append(_tbl(["Ticker", "Sector", "Phase", "Rule action", "Sector-head action", "Justification"], dev,
                       badges=[3, 4]) if dev else _p("None.", "muted"))

    reb = []
    for (ph, tk), v in sorted(ctx["verdict_index"].items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if (t is None or tk == t) and v.get("da_rebuttal"):
            reb.append([tk, f"phase {ph}", v.get("da_rebuttal")])
    if reb:
        blocks += [_sub("Rebuttals of a 'broken' devil's-advocate verdict"), _tbl(["Ticker", "Phase", "Rebuttal"], reb)]

    sector_filter = _ticker_sector(ctx, t) if t else None
    co: "OrderedDict[tuple, list]" = OrderedDict()
    for stage, doc in (("Phase 1 allocation", ctx["phase1"]["cio_allocation"]), ("Phase 4 final", ctx["cio_final"])):
        for c in _l((doc or {}).get("constraint_overrides")):
            if not isinstance(c, Mapping):
                continue
            if t is not None and c.get("sector") != sector_filter:
                continue
            key = (c.get("sector"), c.get("macro_bound"), c.get("cio_target"), c.get("justification"))
            if key in co:
                co[key][0] += f", {stage}"
                continue
            co[key] = [stage, _sector_name(c.get("sector")), c.get("macro_bound"), fmt_pct(c.get("macro_value")),
                       fmt_pct(c.get("cio_target")), c.get("justification") or "No justification recorded."]
    cio_docs = ctx["phase1"]["cio_allocation"] is not None or ctx["cio_final"] is not None
    blocks.append(_sub("CIO macro-bound overrides"))
    blocks.append(_tbl(["Stage", "Sector", "Bound", "Macro value", "CIO target", "Justification"], list(co.values()),
                       align="lllrrl") if co else (_p("None.", "muted") if cio_docs else _na()))

    ao = []
    for tk, fa in ctx["final_actions"].items():
        if t is not None and tk != t:
            continue
        if fa.get("override") or (fa.get("source_verdict_action") and fa.get("action")
                                  and fa["source_verdict_action"] != fa["action"]):
            ao.append([tk, fa.get("source_verdict_action") or "n/a", fa.get("action"),
                       fa.get("override_justification") or "No justification recorded."])
    blocks.append(_sub("CIO action overrides"))
    blocks.append(_tbl(["Ticker", "Sector verdict", "CIO final", "Justification"], ao, badges=[1, 2])
                  if ao else (_p("None.", "muted") if ctx["cio_final"] is not None else _na()))
    if not dev and not reb and not co and not ao:
        if not (ctx["verdict_index"] or ctx["cio_final"] or ctx["phase1"]["cio_allocation"]):
            return [_na()]
        if ctx["cio_final"] is not None:
            msg = (f"No overrides or rule deviations for {t} in this run." if t
                   else "No overrides or rule deviations in this run.")
            return [_p(msg, "muted")]
        # CIO reconciliation missing: keep the subsections so "not available" is not read as "none"
    if not ctx["verdict_index"]:
        blocks[1] = _na()
    return blocks


def _missing_items(ctx: Mapping, t: str | None) -> list[tuple[str, str]]:
    """``(where, item)`` of every ``missing_data`` entry declared by an agent report or a data file
    (de-duplicated, first-seen order)."""
    miss: "OrderedDict[tuple[str, str], None]" = OrderedDict()
    for r in ctx["reports"]:
        if r["status"] == "ok" and _rec_matches(r, t, ctx):
            if r["role"] == "sector_head" and t is not None:
                continue
            for item in _l(r["doc"].get("missing_data")):
                if item:
                    miss.setdefault((_rel_doc_label(r), _s(item)), None)
    for df in ctx["data_files"]:
        if t is not None and df["ticker"] != t:
            continue
        for item in _l(df["doc"].get("missing_data")):
            if item:
                miss.setdefault((f"data · {df['rel']}", _s(item)), None)
    return list(miss)


def _audit_findings(ctx: Mapping, t: str | None) -> list[dict]:
    audit = ctx.get("audit")
    out = []
    for f in _l(audit.get("findings") if isinstance(audit, Mapping) else None):
        if isinstance(f, Mapping) and (t is None or _norm_t(f.get("ticker")) == t):
            out.append(dict(f))
    out.sort(key=lambda f: (SEVERITY_RANK.get(_s(f.get("severity")).lower(), 3), _s(f.get("id"))))
    return out


def _audit_blocks(ctx: Mapping, t: str | None) -> list[dict]:
    """Section 10 'Chair audit': the audit script's findings, each with how the chair handled it
    (``chair_review.changes_made`` by ``finding_id``; an ``open_issues`` mention; else not recorded)."""
    audit = ctx.get("audit")
    if not isinstance(audit, Mapping):
        return []
    chair = ctx.get("chair_review") if isinstance(ctx.get("chair_review"), Mapping) else None
    fixed = {_s(c.get("finding_id")): c for c in _l((chair or {}).get("changes_made")) if isinstance(c, Mapping)}
    opened = [_s(x) for x in _l((chair or {}).get("open_issues"))]
    summ = audit.get("summary") if isinstance(audit.get("summary"), Mapping) else {}
    blocks: list[dict] = [_sub("Chair audit")]
    if summ:
        blocks.append(_kv([("High / medium / low findings",
                            f"{fmt_num(summ.get('high'), 0)} / {fmt_num(summ.get('medium'), 0)} / {fmt_num(summ.get('low'), 0)}"),
                           ("Audit verdict", summ.get("verdict") or "n/a")]))
    rows = []
    for f in _audit_findings(ctx, t):
        fid = _s(f.get("id"))
        ch = fixed.get(fid)
        if ch:
            handled = f"Fixed: {_s(ch.get('what'))}" + (f" ({_how_text(ch.get('how'))})" if ch.get("how") else "")
        elif fid and any(fid in o for o in opened):
            handled = "Open issue: " + next(o for o in opened if fid in o)
        elif chair is None:
            handled = "No chair review in this run"
        else:
            fx = f.get("fix") if isinstance(f.get("fix"), Mapping) else {}
            kind = {"new_run": "needs a new run on the current portfolio",
                    "rerun_agents": "agent re-run suggested, not done in this run",
                    "replan": "plan re-computation suggested", "rerender": "wording only",
                    "note_only": "noted for the reader"}.get(_s(fx.get("type")), "no fix recorded")
            handled = "Not fixed in this run: " + kind
        rows.append([fid or "—", _s(f.get("severity")).lower() or "—", f.get("ticker") or "—",
                     f.get("message") or f.get("code") or "—", handled])
    if rows:
        blocks.append(_tbl(["ID", "Severity", "Ticker", "What was found", "How it was handled"], rows,
                           align="lllww", badges=[1]))
    else:
        blocks.append(_p("No audit findings" + (f" for {t}." if t else "."), "muted"))
    return blocks


def _sec_gaps(ctx: Mapping, t: str | None) -> list[dict]:
    st = ctx["settings"]
    blocks: list[dict] = []
    any_content = False

    failed = [r for r in ctx["reports"] if _rec_matches(r, t, ctx) and (
        r["status"] == "failed" or (r["status"] == "missing" and r.get("order_status") in ("done", "failed")))]
    rows = []
    for r in failed:
        stub = r.get("stub") or {}
        errs = [str(e) for e in _l(stub.get("errors"))] or r.get("order_errors") or []
        rows.append([r.get("role"), r.get("ticker") or (r.get("sector") and _sector_name(r["sector"])) or "—",
                     f"phase {r['phase']}" if r.get("phase") else "—", r["status"],
                     fmt_num(stub.get("attempts"), 0) if stub.get("attempts") is not None else "n/a",
                     one_line(errs[-1], 40) if errs else "—"])
    blocks.append(_sub("Failed or missing agent reports"))
    if rows:
        any_content = True
        blocks.append(_tbl(["Role", "Ticker / sector", "Phase", "Status", "Attempts", "Last error"], rows,
                           align="llllrl", badges=[3]))
        blocks.append(_p("Failed reports count as evidence 1 and are excluded from the conviction mean; nothing "
                         "was fabricated in their place.", "muted"))
    else:
        blocks.append(_p("None.", "muted"))

    low = []
    seen = set()
    for key, e in ctx["evidence"].items():
        tk = _norm_t(e.get("ticker")) if e.get("ticker") else None
        if t is not None and tk != t:
            continue
        score = common.safe_float(e.get("score"))
        if score is None or score >= st["min_evidence"] or key in seen:
            continue
        seen.add(key)
        deds = "; ".join(f"{d.get('rule')}" + (f": {d['detail']}" if d.get("detail") else "")
                         + f" ({fmt_num(d.get('points'), 0, sign=True)})"
                         for d in _l(e.get("deductions")) if isinstance(d, Mapping)) or "—"
        low.append([e.get("agent_role"), tk or "—", fmt_num(score, st["score_dp"]), e.get("status") or "ok", deds])
    blocks.append(_sub(f"Low-evidence reports (score below {fmt_threshold(st['min_evidence'])})"))
    if low:
        any_content = True
        blocks.append(_tbl(["Role", "Ticker", "Score", "Status", "Deductions"], low, align="llrll", badges=[3]))
    else:
        blocks.append(_p("None." if ctx["evidence"] else NA_TEXT, "muted"))

    ins = []
    for (ph, tk), a in sorted(ctx["aggregates"].items()):
        if t is not None and tk != t:
            continue
        ev = common.safe_float(a.get("agg_evidence"))
        label = str(a.get("rule_label") or "")
        if (ev is not None and ev < st["min_evidence"]) or "insufficient" in label:
            ins.append([tk, f"phase {ph}", fmt_num(ev, st["score_dp"]), a.get("rule_action") or "n/a", label or "—"])
    if ins:
        any_content = True
        blocks += [_sub("Tickers capped by insufficient evidence"),
                   _tbl(["Ticker", "Phase", "Agg. evidence", "Rule action", "Label"], ins, align="llrll", badges=[3])]

    flags = []
    for (ph, tk), a in sorted(ctx["aggregates"].items()):
        if t is not None and tk != t:
            continue
        for f in _l((a.get("flags") or {}).get("unresolved") if isinstance(a.get("flags"), Mapping) else None):
            if isinstance(f, Mapping):
                flags.append([tk, f"phase {ph}", f.get("flag_id"), f.get("severity") or "—",
                              f.get("flag") or f.get("detail") or f.get("text") or "—"])
    if flags:
        any_content = True
        blocks += [_sub("Unresolved red flags"),
                   _tbl(["Ticker", "Phase", "Flag", "Severity", "Detail"], flags, badges=[3])]

    dflags = []
    caveats: "OrderedDict[tuple[str, str], list]" = OrderedDict()
    low_count = 0
    for df in ctx["data_files"]:
        if t is not None and df["ticker"] != t:
            continue
        for f in _l(df["doc"].get("flags")):
            if not isinstance(f, Mapping):
                continue
            if f.get("code") in VALUATION_CAVEAT_CODES:    # always shown, in their own table below
                caveats.setdefault((df["ticker"] or df["rel"], str(f.get("code"))),
                                   [df["ticker"] or df["rel"], f.get("code"), f.get("severity") or "—",
                                    f.get("detail") or "—"])
                continue
            if str(f.get("severity")) == "low":
                low_count += 1
                continue
            dflags.append([df["rel"], f.get("code"), f.get("severity"), f.get("detail") or "—"])
    dflags.sort(key=lambda r: (SEVERITY_RANK.get(str(r[2]), 3), r[0]))
    prices = ctx["phase1"]["prices"] or {}
    for tk, reason in ((prices.get("failed") or {}).items() if isinstance(prices.get("failed"), Mapping) else []):
        if t is None or _norm_t(tk) == t:
            dflags.append(["phase1/prices.json", "price_fetch_failed", "high", f"{tk}: {reason}"])
    for tk, info in ((prices.get("tickers") or {}).items() if isinstance(prices.get("tickers"), Mapping) else []):
        if isinstance(info, Mapping) and info.get("stale") and (t is None or _norm_t(tk) == t):
            dflags.append(["phase1/prices.json", "stale_price", "high",
                           f"{tk}: last close {info.get('last_date')} served from {info.get('provider')}"])
    if dflags:
        any_content = True
        blocks += [_sub("Data-quality flags (scripts)"),
                   _tbl(["File", "Code", "Severity", "Detail"], dflags, badges=[2])]
        if low_count:
            blocks.append(_p(f"{low_count} low-severity data flags not listed.", "muted"))

    if caveats:
        any_content = True
        blocks += [_sub("Valuation caveats (valuation files)"),
                   _tbl(["Ticker", "Code", "Severity", "Detail"], list(caveats.values()), badges=[2]),
                   _p("multiple_bands_are_assumptions: the P/E, EV/EBIT and P/B cells use the bands configured in "
                      "committee.yaml, which are assumptions, not market data. dcf_financials_caution: for "
                      "financials the DCF cells are of limited meaning; prefer the P/E and P/B cells.", "muted")]

    audit_blocks = _audit_blocks(ctx, t)
    if audit_blocks:
        any_content = True
        blocks += audit_blocks

    miss = _missing_items(ctx, t)
    if miss:
        any_content = True
        blocks += [_sub("Missing data (as declared in reports and data files)"),
                   _tbl(["Where", "Missing item"], [[w, i] for (w, i) in miss])]

    drift = [str(e.get("message") or f"config {e.get('file')} changed after this run started — decisions used "
                  f"the snapshot in inputs/") for e in ctx.get("config_drift") or []]
    if drift:
        any_content = True
        blocks += [_sub("Configuration changed during the run"), _bul(drift)]

    other = []
    for w in _l(ctx["manifest"].get("warnings")):
        txt = w if isinstance(w, str) else json.dumps(w, ensure_ascii=False)
        if any(txt.endswith(m) for m in drift):             # already listed above
            continue
        if t is None or _ticker_in_text(t, txt):
            other.append(txt)
    for s in _l(ctx["manifest"].get("steps")):
        if isinstance(s, Mapping) and s.get("status") == "failed" and t is None:
            other.append(f"Step {s.get('name')} failed: {s.get('summary') or 'no summary'}")
    for w in ctx.get("consistency_warnings") or []:
        if t is None or _ticker_in_text(t, w):
            other.append(w)
    other += list(ctx["load_errors"])
    if other:
        any_content = True
        blocks += [_sub("Run warnings"), _bul(other)]
    if not any_content and not ctx["reports"] and not ctx["data_files"]:
        return [_na()]
    if not any_content:
        blocks.append(_p("No data gaps recorded.", "muted"))
    return blocks


def _source_items(ctx: Mapping, t: str | None) -> list[tuple[str, Mapping]]:
    items: list[tuple[str, Mapping]] = []
    for r in ctx["reports"]:
        if r["status"] == "ok" and _rec_matches(r, t, ctx):
            items.append((_rel_doc_label(r), r["doc"]))
    for df in ctx["data_files"]:
        if t is None or df["ticker"] == t:
            items.append((f"data · {df['rel']}", df["doc"]))
    return items


def collect_sources(items: Iterable[tuple[str, Mapping]]) -> dict[str, list[dict]]:
    """Dedupe ``sources[]`` across documents by URL; group by kind (dataset | web)."""
    by_url: "OrderedDict[str, dict]" = OrderedDict()
    for label, doc in items:
        for s in _l(doc.get("sources") if isinstance(doc, Mapping) else None):
            if not isinstance(s, Mapping):
                continue
            url = _s(s.get("url")).strip()
            if not url:
                continue
            sid = _s(s.get("id")).strip()
            kind = _s(s.get("kind")).strip() or ("dataset" if sid.startswith("D:") else "web")
            e = by_url.get(url)
            if e is None:
                e = by_url[url] = {"url": url, "title": _s(s.get("title")).strip() or url, "kind": kind,
                                   "ids": [], "accessed": None, "cited_by": []}
            if kind == "dataset":
                e["kind"] = "dataset"
            if sid and sid not in e["ids"]:
                e["ids"].append(sid)
            acc = _s(s.get("accessed")).strip()
            if acc and (e["accessed"] is None or acc > e["accessed"]):
                e["accessed"] = acc
            if label not in e["cited_by"]:
                e["cited_by"].append(label)
    groups: dict[str, list[dict]] = {"dataset": [], "web": []}
    for e in by_url.values():
        groups["dataset" if e["kind"] == "dataset" else "web"].append(e)
    groups["dataset"].sort(key=lambda e: (e["ids"][0] if e["ids"] else "~", e["url"]))
    groups["web"].sort(key=lambda e: (e["title"].lower(), e["url"]))
    return groups


def _sec_sources(ctx: Mapping, t: str | None) -> list[dict]:
    groups = collect_sources(_source_items(ctx, t))
    blocks: list[dict] = []
    if groups["dataset"]:
        rows = [[", ".join(e["ids"]) or "—", e["title"], e["url"], e["accessed"] or "n/a", str(len(e["cited_by"]))]
                for e in groups["dataset"]]
        blocks += [_sub(f"Datasets fetched by scripts ({len(rows)})"),
                   _tbl(["ID", "Title", "URL", "Accessed", "Cited by"], rows, align="llllr", links=[2])]
    if groups["web"]:
        rows = [[e["title"], e["url"], e["accessed"] or "n/a", str(len(e["cited_by"]))] for e in groups["web"]]
        blocks += [_sub(f"Web sources cited by agents ({len(rows)})"),
                   _tbl(["Title", "URL", "Accessed", "Cited by"], rows, align="lllr", links=[1])]
    if not blocks:
        return [_na()]
    blocks.append(_p("Sources are de-duplicated by URL across every report and data file of the run; "
                     "'Cited by' counts the documents that list each one.", "muted"))
    return blocks


def summary_digest(run_dir: str | Path, *, ticker: str | None = None, max_missing: int = 20,
                   ctx: Mapping | None = None) -> dict:
    """Digest of the memo context for ``phase4/summary.json``: ``sources`` (every source of every
    report / data file, de-duplicated by URL, datasets first), ``missing_data`` (the first
    ``max_missing`` ``"<where>: <item>"`` strings) and ``missing_data_count`` (all of them)."""
    ctx = ctx if ctx is not None else build_context(run_dir)
    t = _norm_t(ticker) if ticker else None
    groups = collect_sources(_source_items(ctx, t))
    sources = [{"ids": e["ids"], "url": e["url"], "title": e["title"], "kind": e["kind"],
                "accessed": e["accessed"], "cited_by": len(e["cited_by"])}
               for e in groups["dataset"] + groups["web"]]
    miss = _missing_items(ctx, t)
    return {"sources": sources, "missing_data_count": len(miss),
            "missing_data": [f"{where}: {item}" for where, item in miss[:max_missing]]}


SECTION_BUILDERS: dict[int, Callable[[Mapping, str | None], list[dict]]] = {
    1: _sec_disclaimer, 2: _sec_exec_summary, 3: _sec_macro, 4: _sec_portfolio, 5: _sec_holdings,
    6: _sec_proposals, 7: _sec_rebalancing, 8: _sec_da, 9: _sec_overrides, 10: _sec_gaps, 11: _sec_sources,
}


def _norm_mode(mode: str | None) -> str:
    m = str(mode or "full").strip().lower()
    if m in ("holding", "trial"):
        return "trial"
    if m != "full":
        raise ValueError(f"mode must be one of {MODES} (got {mode!r})")
    return m


_CHAIR_HOW: dict[str, str] = {"agent_rerun": "agent re-run", "deterministic_fix": "deterministic fix",
                              "chair_edit": "edited by the chair", "accepted_as_is": "accepted as is"}


def _how_text(x: Any) -> str:
    return _CHAIR_HOW.get(_s(x), _s(x))


_CHAIR_VERDICTS: dict[str, tuple[str, str]] = {
    "approved": ("Approved", "b-pos"),
    "approved_with_notes": ("Approved with notes", "b-warn"),
    "needs_rework": ("Needs rework", "b-neg"),
}


def _confidence_text(x: Any) -> str:
    v = common.safe_float(x)
    return f"{v:g}/10" if v is not None else (_s(x) or "—")


def _chair_block(ctx: Mapping) -> dict | None:
    """The unnumbered 'Chair's Final Review' (``phase4/chair_review.json``, schema chair_review/1) or
    ``None`` when the file is absent / not an object."""
    cr = ctx.get("chair_review")
    if not isinstance(cr, Mapping):
        return None
    key = _s(cr.get("verdict")).strip().lower()
    label, cls = _CHAIR_VERDICTS.get(key, (key.replace("_", " ").capitalize() or "No verdict", "b-mute"))
    meta = " · ".join(x for x in (f"round {cr['round']}" if cr.get("round") is not None else "",
                                      f"reviewed {cr['reviewed_at']}" if cr.get("reviewed_at") else "") if x)
    blocks: list[dict] = []
    summary = [x for x in _l(cr.get("executive_summary")) if x]
    if summary:
        blocks.append(_bul(summary))
    decisions = [d for d in _l(cr.get("decisions")) if isinstance(d, Mapping)]
    if decisions:
        blocks += [_sub("The chair's decision per holding"),
                   _tbl(["Ticker", "Chair call", "Confidence", "Why", "What would change it"],
                        [[d.get("ticker"), d.get("action"), _confidence_text(d.get("confidence")),
                          d.get("chair_view"), d.get("what_would_change_it")] for d in decisions],
                        align="lllww", badges=[1])]
    changes = [c for c in _l(cr.get("changes_made")) if isinstance(c, Mapping)]
    if changes:
        blocks += [_sub("What the chair fixed"),
                   _tbl(["Finding", "What", "How"],
                        [[c.get("finding_id"), c.get("what"), _how_text(c.get("how"))] for c in changes],
                        align="lww")]
    issues = [x for x in _l(cr.get("open_issues")) if x]
    if issues:
        blocks += [_sub("Open issues"), _bul(issues)]
    todo = [x for x in _l(cr.get("user_actions")) if x]
    if todo:
        blocks += [_sub("What you need to do"), _bul(todo)]
    if cr.get("disclaimer_confirmed") is False:
        blocks.append(_p("The chair did not confirm that the disclaimer is intact.", "warn"))
    return {"title": "Chair's Final Review", "headline": _s(cr.get("headline")), "verdict": key,
            "verdict_label": label, "verdict_class": cls, "meta": meta, "blocks": blocks}


def build_memo(ctx: Mapping, *, mode: str = "full", ticker: str | None = None) -> dict:
    """View model: ``{"title","subtitle","mode","ticker","disclaimer","chair","chair_status",
    "sections":[{num,title,anchor,blocks}]}`` (``chair`` = the unnumbered Chair's Final Review, full mode only).
    A section builder that raises is reported inside its own section (the memo still renders)."""
    mode = _norm_mode(mode)
    t = _norm_t(ticker) if ticker else None
    if mode == "trial" and not t:
        holding = ctx["mode"].get("holding")
        if isinstance(holding, str) and holding.strip():
            t = _norm_t(holding)
        else:
            raise ValueError("trial mode needs a ticker (--ticker T)")
    nums = TRIAL_SECTIONS if mode == "trial" else FULL_SECTIONS
    sections = []
    for n in nums:
        try:
            blocks = SECTION_BUILDERS[n](ctx, t if mode == "trial" else None)
        except Exception as exc:  # noqa: BLE001 — one malformed input must not sink the memo
            blocks = [_na(), _p(f"Section could not be built from the run files ({exc.__class__.__name__}: {exc}).",
                                "warn")]
        sections.append({"num": n, "title": Markup(SECTION_TITLES[n]), "anchor": f"s{n}", "blocks": blocks})
    chair = _chair_block(ctx) if mode == "full" else None
    title = f"Holding Trial — {t}" if mode == "trial" else "Investment Committee Memo"
    return {"title": title, "subtitle": f"Run {ctx['run_id']} · as of {ctx['as_of'] or 'n/a'} · {ctx['mode']['label']}",
            "mode": mode, "ticker": t, "disclaimer": common.DISCLAIMER, "sections": sections,
            "chair": chair, "chair_status": "Chair review: not performed" if mode == "full" and chair is None else "",
            "generated_at": ctx["generated_at"]}


# =====================================================================================
# Rendering
# =====================================================================================


def _env(autoescape: bool) -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(common.templates_dir()), encoding="utf-8"),
                             autoescape=autoescape, trim_blocks=True, lstrip_blocks=True,
                             keep_trailing_newline=True, undefined=jinja2.ChainableUndefined)
    env.filters.update({"md_text": md_text, "md_cell": md_cell, "md_row": md_row, "md_sep": md_sep,
                        "safe_url": safe_url, "badge_class": badge_class, "align_class": align_class, "cell_html": cell_html,
                        "fmt_num": fmt_num, "fmt_pct": fmt_pct, "fmt_usd": fmt_usd, "fmt_fact": fmt_fact})
    return env


def render_strings(memo: Mapping) -> tuple[str, str]:
    """Render the view model to (markdown, html) strings."""
    md = _env(False).get_template(MD_TEMPLATE).render(memo=memo)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    html = _env(True).get_template(HTML_TEMPLATE).render(memo=memo)
    html = re.sub(r"\n\s*\n+", "\n", html).strip() + "\n"
    return md, html


def default_outputs(run_dir: Path, mode: str, ticker: str | None) -> tuple[Path, Path]:
    p4 = Path(run_dir) / "phase4"
    if mode == "trial":
        stem = f"trial_{common.safe_name(_norm_t(ticker))}"
        return p4 / f"{stem}.md", p4 / f"{stem}.html"
    return p4 / "memo.md", p4 / "memo.html"


def render(run_dir: str | Path, *, mode: str = "full", ticker: str | None = None,
           out_md: str | Path | None = None, out_html: str | Path | None = None) -> tuple[Path, Path]:
    """Build the context, render both formats and write them atomically (UTF-8).
    Defaults: full → ``phase4/memo.md|html``; trial → ``phase4/trial_<T>.md|html``."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")
    ctx = build_context(run_dir)
    memo = build_memo(ctx, mode=mode, ticker=ticker)
    md, html = render_strings(memo)
    d_md, d_html = default_outputs(run_dir, memo["mode"], memo["ticker"])
    p_md = common.write_text(Path(out_md) if out_md else d_md, md)
    p_html = common.write_text(Path(out_html) if out_html else d_html, html)
    return p_md, p_html


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Render the committee memo (Markdown + HTML) for a run.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="run directory (runs/<run_id>)")
    g.add_argument("--run", help="run id under the runs directory")
    parser.add_argument("--mode", default="full", choices=["full", "trial", "holding"],
                        help="full memo (11 sections) or single-ticker trial (sections 1,5,8-11)")
    parser.add_argument("--ticker", default=None, help="ticker for --mode trial")
    parser.add_argument("--out-md", default=None, help="output Markdown path (default under phase4/)")
    parser.add_argument("--out-html", default=None, help="output HTML path (default under phase4/)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
        run_dir = Path(args.run_dir) if args.run_dir else common.runs_dir() / args.run
        p_md, p_html = render(run_dir, mode=args.mode, ticker=args.ticker, out_md=args.out_md,
                              out_html=args.out_html)
    except (FileNotFoundError, ValueError, common.AginvError, jinja2.TemplateError) as exc:
        print(json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"memo_md": p_md.as_posix(), "memo_html": p_html.as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
