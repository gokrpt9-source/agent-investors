"""ledger.py — every recommendation ever made, and how it performed (DESIGN §4.12).

``ledger/recommendations.csv`` (UTF-8, ``newline=""``) holds one row per (run_id, ticker, role):
each specialist report (fundamental, technical, operator_ceo, industry_ops, osint, etf), the
devil's advocate, the sector head, and a ``committee`` row carrying the CIO's final action.
Columns (exact order) are ``LEDGER_COLUMNS``.

API:
    rows_from_run(run_dir) -> list[dict]
    append(rows, ledger_path=None) -> int                 # dedupes on (run_id, ticker, role),
                                                          # ticker/role normalised (brk.b == BRK-B)
    load_rows(ledger_path=None) -> list[dict]
    score(ledger_path=None, *, price_fn=None, as_of=None, min_age_days=None) -> dict
    render_scorecard_md(result) -> str
CLI:
    python scripts/ledger.py append --run-dir runs/<id>   (or --run <id>; a dry run writes
                                                           <run>/phase4/ledger_preview.csv)
    python scripts/ledger.py score [--out ledger/scorecard_<date>.md] [--min-age-days N]

Column notes: ``target_weight_pct`` (committee rows) is the VERDICT-INTENDED target weight, in % of
the portfolio: the weight the sizing rules called for (``weight_intent_pct`` of the final
``phase4/portfolio_plan.json``, else the preview; older plans without that field fall back to
``weight_tgt_pct``). It can differ from the weight actually reached when the proposed trade was
dropped (rounds to zero shares / below the minimum trade / no price) — a TRIM whose trade was dropped
still records the reduced target, never the unchanged current weight.

Scoring: ``ret_pct`` since the recommendation, minus the benchmark (SPY) and the sector ETF over
the same window -> ``excess_spy_pp`` / ``excess_sector_pp``. Hit: ADD/BUY (or a bullish stance
when the row has no action) -> excess_spy > 0; SELL/TRIM (or bearish) -> excess_spy < 0;
HOLD/PASS/neutral rows are scored for returns but excluded from hit rates. Rows younger than
``min_age_days`` (committee.yaml ``ledger.min_age_days``) are not scored. Prices come from an
injectable ``price_fn(ticker, "YYYY-MM-DD") -> float|None``; the default reads the cached price
series (``fetch_prices.load_series``) at or before the date and never touches the network.

Stale prices are never scored silently: when ``price_fn`` has a ``last_bar(ticker, "YYYY-MM-DD") ->
"YYYY-MM-DD"|None`` attribute (the default one does), the last bars of the row's price ticker, the
benchmark and the sector ETF must each be within ``evidence.price_max_trading_days`` NYSE trading
days of ``as_of`` — otherwise the row gets ``status = "stale_price"`` and a ``status_reason`` and is
not scored. Rows that pass are measured to the COMMON last date (the earliest of the three last
bars, ``price_end_date``) so the ticker, SPY and the sector ETF always end on the same day. An
injected ``price_fn`` without ``last_bar`` is trusted as is.

Proxy-priced holdings (a TASE fund with ``proxy_ticker`` / look-through ``price_ticker``) record
``price_at_rec`` from the proxy and are scored against the same proxy: ``score`` maps
ticker -> price ticker from the row's own run (``<runs>/<run_id>/phase1/lookthrough.json``), then
from the active ``portfolio.yaml`` ``proxy_ticker`` (an explicit ``price_tickers`` map wins).

CSV safety: text cells beginning with ``= + - @ TAB CR`` (or a single quote) are written with a
leading ``'`` so spreadsheet apps do not evaluate them as formulas; ``load_rows`` strips it again.
Numeric cells (e.g. ``-1.5``) are never prefixed.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts import portfolio  # noqa: E402

LEDGER_COLUMNS: tuple[str, ...] = (
    "run_id", "rec_date", "price_date", "ticker", "asset_type", "sector", "kind", "role", "action",
    "rule_action", "stance", "conviction", "evidence", "rr", "fair_value_base", "invalidation_price",
    "target_weight_pct", "price_at_rec", "spy_at_rec", "sector_etf", "sector_etf_at_rec", "thesis",
    "run_mode", "report_path",
)
DEDUPE_KEY = ("run_id", "ticker", "role")
#: report roles that produce one ledger row per ticker (plus sector_head and committee rows)
REPORT_ROLES: tuple[str, ...] = common.SPECIALIST_ROLES + ("devils_advocate", "etf")
LEDGER_ROLES: tuple[str, ...] = REPORT_ROLES + ("sector_head", "committee")
LONG_ACTIONS = ("ADD", "BUY")
SHORT_ACTIONS = ("SELL", "TRIM")
#: Devil's Advocate verdict -> implied stance for hit scoring (a "survives" verdict makes no call)
DA_STANCE = {"broken": "bearish", "damaged": "bearish", "survives": "neutral"}
PREVIEW_LEDGER = "ledger_preview.csv"
#: used when committee.yaml has no ``evidence.price_max_trading_days``
DEFAULT_PRICE_MAX_TRADING_DAYS = 3
PCT_DP = 4
#: leading characters that make a spreadsheet evaluate a cell as a formula (CSV injection)
FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")
CSV_ESCAPE = "'"
_NUMERIC_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

PriceFn = Callable[[str, str], "float | None"]


# =====================================================================================
# helpers
# =====================================================================================


def _cell(v: Any) -> str:
    """CSV cell text: None -> '', floats without trailing noise, everything else str()."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        f = common.safe_float(v)
        if f is None:
            return ""
        return repr(f)
    if isinstance(v, int):
        return str(v)
    return str(v)


def _escape_cell(text: str) -> str:
    """Neutralize spreadsheet formulas: prefix ``'`` to text starting with a formula lead (or a
    quote, so the escape stays reversible). Plain numbers such as ``-1.5`` are left alone."""
    if not text or _NUMERIC_RE.fullmatch(text):
        return text
    if text.startswith(FORMULA_LEADS) or text.startswith(CSV_ESCAPE):
        return CSV_ESCAPE + text
    return text


def _unescape_cell(text: Any) -> Any:
    if isinstance(text, str) and len(text) > 1 and text.startswith(CSV_ESCAPE) \
            and text[1:].startswith(FORMULA_LEADS + (CSV_ESCAPE,)):
        return text[1:]
    return text


def _f(x: Any) -> float | None:
    if isinstance(x, str):
        x = x.strip()
        if not x:
            return None
        try:
            return common.safe_float(float(x))
        except ValueError:
            return None
    return common.fact_value(x)


def _norm(t: Any) -> str:
    return common.norm_ticker(t)


def _path_keys(p: Any) -> list[str]:
    """Lookup keys for a report path: the posix path itself and its ``phaseN/...`` tail."""
    if not p:
        return []
    path = Path(str(p))
    keys = [path.as_posix(), str(p)]
    parts = path.parts
    for i, part in enumerate(parts):
        if re.fullmatch(r"phase\d+", part):
            keys.append("/".join(parts[i:]))
            break
    return keys


def _display_path(p: Path | None) -> str:
    if p is None:
        return ""
    try:
        return p.resolve().relative_to(common.REPO_ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _run_mode(manifest: Mapping[str, Any]) -> str:
    mode = manifest.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    if isinstance(mode, Mapping):
        if mode.get("dry_run"):
            return "dry_run"
        if mode.get("holding"):
            return "holding"
        if mode.get("quick"):
            return "quick"
    return "full"


def _is_dry_run(manifest: Mapping[str, Any]) -> bool:
    mode = manifest.get("mode")
    if isinstance(mode, Mapping):
        return bool(mode.get("dry_run"))
    return isinstance(mode, str) and mode.replace("-", "_") in ("dry_run", "dryrun")


def _last_close(entry: Any) -> tuple[float | None, str | None]:
    if not isinstance(entry, Mapping):
        return None, None
    lc = entry.get("last_close")
    val = common.fact_value(lc)
    d = entry.get("last_date") or (lc.get("as_of") if isinstance(lc, Mapping) else None)
    return val, common.date_str(d) if d else None


# =====================================================================================
# rows_from_run
# =====================================================================================


def _intended_weight(position: Mapping[str, Any]) -> Any:
    """Verdict-intended target weight of a plan position: ``weight_intent_pct`` (what the sizing
    rules called for), else ``weight_tgt_pct`` (older plans). ``weight_tgt_pct`` alone would record
    the unchanged current weight for a TRIM/ADD whose trade was dropped."""
    v = position.get("weight_intent_pct")
    return v if v is not None else position.get("weight_tgt_pct")


def _report_entries(run_dir: Path, manifest: Mapping[str, Any]) -> list[dict]:
    """One entry per (ticker, role) for report roles: from manifest orders (``resolved_output``)
    or, without orders, from ``phase{2,3}/reports/*.json`` (``.r2.json`` preferred). Failed
    stubs are skipped — no recommendation was made."""
    best: dict[tuple[str, str], dict] = {}
    orders = manifest.get("orders")
    if isinstance(orders, Mapping) and orders:
        for oid, o in orders.items():
            if not isinstance(o, Mapping) or o.get("agent_role") not in REPORT_ROLES or not o.get("ticker"):
                continue
            path = portfolio.order_output(run_dir, o)
            if path is None:
                continue
            entry = {"ticker": _norm(o["ticker"]), "role": o["agent_role"], "sector": o.get("sector"),
                     "phase": common.safe_float(o.get("phase")), "path": path,
                     "attempt": common.safe_float(o.get("attempt")) or 1.0, "order_id": oid}
            key = (entry["ticker"], entry["role"])
            prev = best.get(key)
            if prev is None or (entry["phase"] or 0, entry["attempt"]) >= (prev["phase"] or 0, prev["attempt"]):
                best[key] = entry
        return list(best.values())
    for phase in (2, 3):
        rdir = run_dir / f"phase{phase}" / "reports"
        if not rdir.is_dir():
            continue
        files = sorted(rdir.glob("*.json"))
        for f in files:
            if f.name.endswith(".failed.json"):
                continue
            doc = portfolio.read_json_opt(f)
            if not isinstance(doc, Mapping):
                continue
            role, ticker = doc.get("agent_role"), doc.get("ticker")
            if role not in REPORT_ROLES or not ticker:
                continue
            attempt = 2.0 if f.name.endswith(".r2.json") else 1.0
            entry = {"ticker": _norm(ticker), "role": role, "sector": doc.get("sector"),
                     "phase": float(phase), "path": f, "attempt": attempt, "order_id": None}
            key = (entry["ticker"], role)
            prev = best.get(key)
            if prev is None or (entry["phase"], attempt) >= (prev["phase"], prev["attempt"]):
                best[key] = entry
    return list(best.values())


def _cfg_proxies(portfolio_cfg: Any) -> dict[str, str]:
    """{ticker: proxy_ticker} from a portfolio.yaml mapping (holdings without a proxy omitted)."""
    out: dict[str, str] = {}
    if not isinstance(portfolio_cfg, Mapping):
        return out
    for h in portfolio_cfg.get("holdings") or []:
        if isinstance(h, Mapping) and h.get("ticker") and h.get("proxy_ticker"):
            try:
                t, pt = _norm(h["ticker"]), _norm(h["proxy_ticker"])
            except ValueError:
                continue
            if pt != t:
                out[t] = pt
    return out


def _evidence_index(run_dir: Path) -> dict[str, float]:
    idx: dict[str, float] = {}
    for rel in ("phase2/evidence.json", "phase3/evidence_light.json", "phase3/evidence.json"):
        doc = portfolio.read_json_opt(run_dir / rel)
        if not isinstance(doc, Mapping):
            continue
        for key, e in (doc.get("reports") or {}).items():
            if not isinstance(e, Mapping):
                continue
            score = common.safe_float(e.get("score"))
            for k in _path_keys(key):
                idx[k] = score
    return idx


def _lookup(idx: Mapping[str, Any], path: Any) -> Any:
    for k in _path_keys(path):
        if k in idx:
            return idx[k]
    return None


def _cio_final(run_dir: Path, manifest: Mapping[str, Any]) -> tuple[dict | None, Path | None]:
    """Same resolution as the portfolio plan (manifest resolved_output -> .r2.json -> attempt 1;
    a failed CIO order means no cio_final), so plan and ledger never disagree."""
    return portfolio.resolve_cio(run_dir, "reconciliation", manifest)


def rows_from_run(run_dir: Path) -> list[dict]:
    """Build ledger rows (dicts keyed by ``LEDGER_COLUMNS``) for one run directory from
    manifest.json orders, the reports, evidence/aggregate files, sector verdicts, cio_final and
    the portfolio plan (preview when no final plan exists)."""
    run_dir = Path(run_dir)
    manifest = portfolio.load_manifest(run_dir)
    run_id = str(manifest.get("run_id") or run_dir.name)
    rec_date = (common.date_str(manifest.get("as_of")) or common.date_str(manifest.get("created_at"))
                or common.today().isoformat())
    run_mode = _run_mode(manifest)
    try:
        cfg = portfolio.load_run_config("committee", run_dir)
    except common.ConfigError:
        cfg = {}
    benchmark = _norm(cfg.get("benchmark") or "SPY")
    sector_etfs = {k: v for k, v in (cfg.get("sector_etfs") or {}).items()}

    prices = portfolio.read_json_opt(run_dir / "phase1" / "prices.json") or {}
    px = prices.get("tickers") or {}
    spy_at_rec, _ = _last_close(px.get(benchmark))
    lookthrough = portfolio.read_json_opt(run_dir / "phase1" / "lookthrough.json") or {}

    holdings: dict[str, dict] = {}
    proxies = _cfg_proxies(portfolio.load_run_config("portfolio", run_dir, required=False))
    for h in lookthrough.get("holdings") or []:
        if isinstance(h, Mapping) and h.get("ticker"):
            t = _norm(h["ticker"])
            pt = h.get("price_ticker") or h.get("proxy_ticker") or proxies.get(t)
            holdings[t] = {"asset_type": h.get("asset_type"), "sector": h.get("sector"),
                           "price_ticker": _norm(pt) if pt else None, "price": h.get("price")}
    for h in manifest.get("holdings") or []:
        if isinstance(h, Mapping) and h.get("ticker"):
            holdings.setdefault(_norm(h["ticker"]), {}).update(
                {k: h.get(k) for k in ("asset_type", "sector") if h.get(k)})

    verdicts = portfolio.collect_verdicts(run_dir)
    aggregates = portfolio.collect_aggregates(run_dir)
    evidence = _evidence_index(run_dir)
    cio_final, cio_path = _cio_final(run_dir, manifest)
    final_actions = {}
    for fa in (cio_final or {}).get("final_actions") or []:
        if isinstance(fa, Mapping) and fa.get("ticker"):
            final_actions[_norm(fa["ticker"])] = fa
    plan = None
    for name in ("portfolio_plan.json", "portfolio_preview.json"):
        plan = portfolio.read_json_opt(run_dir / "phase4" / name)
        if isinstance(plan, Mapping):
            break
    tgt_weights = {}
    for p in (plan or {}).get("positions") or []:
        if isinstance(p, Mapping) and p.get("ticker"):
            tgt_weights[_norm(p["ticker"])] = _intended_weight(p)
    reports = _report_entries(run_dir, manifest)
    report_sector = {r["ticker"]: r.get("sector") for r in reports if r.get("sector")}

    def base(t: str) -> dict:
        meta = holdings.get(t, {})
        agg = aggregates.get(t, {})
        sector = meta.get("sector") or (verdicts.get(t) or {}).get("sector") or report_sector.get(t) \
            or agg.get("sector") or ""
        asset_type = meta.get("asset_type") or agg.get("asset_type") or \
            ("fund" if sector == "funds" else "stock")
        price_ticker = meta.get("price_ticker") or proxies.get(t) or t
        price, price_date = _last_close(px.get(price_ticker))
        if price is None and common.fact_value(meta.get("price")) is not None:
            lt_px = meta.get("price")  # look-through's own (proxy) price
            price = common.fact_value(lt_px)
            price_date = common.date_str(lt_px.get("as_of")) if isinstance(lt_px, Mapping) and lt_px.get("as_of") else None
        if price is None:
            for phase in ("phase3", "phase2"):
                tech = portfolio.read_json_opt(run_dir / phase / "data" / common.data_filename("technicals", t))
                if isinstance(tech, Mapping) and common.fact_value(tech.get("close")) is not None:
                    price = common.fact_value(tech["close"])
                    close = tech.get("close")
                    price_date = common.date_str(close.get("as_of")) if isinstance(close, Mapping) else None
                    break
        etf = "" if sector in ("funds", "unclassified", "") else (sector_etfs.get(sector) or common.sector_etf(sector) or "")
        etf_price = _last_close(px.get(_norm(etf)))[0] if etf else None
        return {"run_id": run_id, "rec_date": rec_date, "price_date": price_date or rec_date, "ticker": t,
                "asset_type": asset_type, "sector": sector, "kind": "holding" if t in holdings else "candidate",
                "price_at_rec": price, "spy_at_rec": spy_at_rec, "sector_etf": etf,
                "sector_etf_at_rec": etf_price, "run_mode": run_mode,
                "action": None, "rule_action": None, "stance": None, "conviction": None, "evidence": None,
                "rr": None, "fair_value_base": None, "invalidation_price": None, "target_weight_pct": None,
                "thesis": None, "report_path": None}

    rows: list[dict] = []
    for rep in sorted(reports, key=lambda r: (r["ticker"], LEDGER_ROLES.index(r["role"]))):
        doc = portfolio.read_json_opt(rep["path"])
        if not isinstance(doc, Mapping) or doc.get("status") == "failed":
            continue
        t, role = rep["ticker"], rep["role"]
        row = base(t)
        row["role"] = role
        row["evidence"] = _lookup(evidence, rep["path"])
        row["report_path"] = _display_path(rep["path"])
        if role == "devils_advocate":
            verdict = doc.get("verdict_on_thesis")
            row["stance"] = DA_STANCE.get(str(verdict), None)
            reason = doc.get("verdict_reason") or ""
            row["thesis"] = f"DA verdict {verdict}: {reason}".strip() if verdict else reason or None
        else:
            row["stance"] = doc.get("stance")
            row["conviction"] = doc.get("conviction")
            row["thesis"] = doc.get("thesis")
            row["invalidation_price"] = doc.get("invalidation_price")
            if role == "fundamental":
                row["fair_value_base"] = common.get_dotted(doc, "extensions.valuation.fair_value_base")
            if role == "etf":
                row["action"] = doc.get("action")
        rows.append(row)

    committee_tickers = sorted(set(verdicts) | set(final_actions))
    for t in committee_tickers:
        v = verdicts.get(t)
        agg = aggregates.get(t, {})
        rr_obj = agg.get("rr") if isinstance(agg.get("rr"), Mapping) else {}
        if v is not None:
            row = base(t)
            row.update({"role": "sector_head", "action": v.get("action"), "rule_action": v.get("rule_action"),
                        "conviction": v.get("agg_conviction"), "evidence": v.get("agg_evidence"),
                        "rr": v.get("rr"), "fair_value_base": v.get("fair_value_base"),
                        "invalidation_price": v.get("invalidation_price"),
                        "thesis": v.get("thesis") or v.get("rationale"),
                        "report_path": _display_path(Path(v["path"])) if v.get("path") else None})
            rows.append(row)
        fa = final_actions.get(t)
        action = (fa or {}).get("action") or (v or {}).get("action") or agg.get("rule_action")
        if not action:
            continue
        row = base(t)
        if fa is not None and fa.get("override") and fa.get("override_justification"):
            thesis = fa.get("override_justification")
        else:
            thesis = (v or {}).get("thesis") or (v or {}).get("rationale")
        row.update({
            "role": "committee", "action": action,
            "rule_action": agg.get("rule_action") or (v or {}).get("rule_action"),
            "conviction": agg.get("agg_conviction", (v or {}).get("agg_conviction")),
            "evidence": agg.get("agg_evidence", (v or {}).get("agg_evidence")),
            "rr": rr_obj.get("rr") if rr_obj else (v or {}).get("rr"),
            "fair_value_base": rr_obj.get("fv_base") if rr_obj.get("fv_base") is not None else (v or {}).get("fair_value_base"),
            "invalidation_price": rr_obj.get("invalidation") if rr_obj.get("invalidation") is not None
            else (v or {}).get("invalidation_price"),
            "target_weight_pct": tgt_weights.get(t),
            "thesis": thesis,
            "report_path": _display_path(cio_path) if (fa is not None and cio_path) else
            (_display_path(Path(v["path"])) if v and v.get("path") else None),
        })
        rows.append(row)
    return [{c: r.get(c) for c in LEDGER_COLUMNS} for r in rows]


# =====================================================================================
# append / load
# =====================================================================================


def _read_raw(path: Path) -> str:
    if not path.is_file():
        return ""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    return text[1:] if text.startswith("\ufeff") else text


def load_rows(ledger_path: str | Path | None = None) -> list[dict]:
    """All ledger rows as dicts of strings (missing file -> [])."""
    path = Path(ledger_path) if ledger_path else common.ledger_path()
    text = _read_raw(path)
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [{k: _unescape_cell(v) for k, v in r.items()} for r in reader]


def _dedupe_key(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(run_id, ticker, role) with the ticker normalised (``common.norm_ticker``: ``brk.b`` ==
    ``BRK-B``, ``t0`` == ``T0``) and the role lower-cased; None when a part is missing/invalid."""
    run_id = _cell(row.get("run_id")).strip()
    role = _cell(row.get("role")).strip().lower()
    raw_ticker = _cell(row.get("ticker")).strip()
    if not (run_id and role and raw_ticker):
        return None
    try:
        return run_id, _norm(raw_ticker), role
    except ValueError:
        return None


def append(rows: Iterable[Mapping[str, Any]], ledger_path: str | Path | None = None, *,
           replace_run_ids: Iterable[str] | None = None) -> int:
    """Append ``rows`` to the ledger CSV (header written when the file is new/empty), skipping
    any (run_id, ticker, role) already present or repeated — compared on the normalised ticker and
    role, so ``t0``/``T0`` and ``brk.b``/``BRK-B`` are the same key even on direct calls. The
    normalised ticker and role are what gets written. Atomic rewrite. Returns the number of rows
    appended.

    ``replace_run_ids``: every existing row of these run ids is dropped first (and so replaced by
    ``rows``), used when a run is finalized again after a chair re-run so its rows are neither
    duplicated nor stale. Rows of other runs are rewritten unchanged."""
    path = Path(ledger_path) if ledger_path else common.ledger_path()
    existing = _read_raw(path)
    replace = {str(r).strip() for r in (replace_run_ids or []) if str(r).strip()}
    n_dropped = 0
    seen: set[tuple[str, str, str]] = set()
    if existing.strip():
        reader = csv.DictReader(io.StringIO(existing, newline=""))
        header = tuple(reader.fieldnames or ())
        if header != LEDGER_COLUMNS:
            raise common.ConfigError(f"{path.as_posix()}: unexpected ledger header {list(header)}; "
                                     f"expected {list(LEDGER_COLUMNS)}")
        kept_rows: list[dict] = []
        for r in reader:
            if replace and _cell(_unescape_cell(r.get("run_id"))).strip() in replace:
                n_dropped += 1
                continue
            kept_rows.append(r)
            key = _dedupe_key({k: _unescape_cell(r.get(k)) for k in DEDUPE_KEY})
            if key is not None:
                seen.add(key)
        if n_dropped:
            keep_buf = io.StringIO(newline="")
            keep_writer = csv.DictWriter(keep_buf, fieldnames=list(LEDGER_COLUMNS), extrasaction="ignore",
                                         lineterminator="\n")
            keep_writer.writeheader()
            keep_writer.writerows(kept_rows)
            existing = keep_buf.getvalue()
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(LEDGER_COLUMNS), extrasaction="ignore", lineterminator="\n")
    if not existing.strip():
        writer.writeheader()
        existing = ""
    elif not existing.endswith(("\n", "\r")):
        existing += "\n"
    n = 0
    for r in rows:
        key = _dedupe_key(r)
        if key is None or key in seen:
            continue
        seen.add(key)
        out = {c: _escape_cell(_cell(r.get(c))) for c in LEDGER_COLUMNS}
        out["run_id"], out["ticker"], out["role"] = key
        writer.writerow(out)
        n += 1
    if n or n_dropped or not path.is_file() or not existing:
        common.write_text(path, existing + buf.getvalue())
    return n


# =====================================================================================
# score
# =====================================================================================


def _load_series(ticker: str) -> Any:
    try:
        from scripts import fetch_prices  # lazy: sibling unit, never network
    except ImportError:
        return None
    try:
        return fetch_prices.load_series(ticker)
    except (common.DataUnavailable, OSError, ValueError, KeyError):
        return None


def default_price_fn() -> PriceFn:
    """``price_fn(ticker, date)`` reading the cached series (Close at or before ``date``).

    The returned function also carries ``last_bar(ticker, date) -> "YYYY-MM-DD" | None`` (the date
    of that Close) so ``score`` can refuse to score stale cached series."""
    import pandas as pd

    series: dict[str, Any] = {}

    def closes_upto(ticker: str, date_s: str) -> Any:
        t = _norm(ticker)
        if t not in series:
            series[t] = _load_series(t)
        df = series[t]
        if df is None or getattr(df, "empty", True) or "Close" not in df:
            return None
        d = _safe_date(date_s)
        s = df["Close"].dropna()
        if d is not None:
            s = s[s.index <= pd.Timestamp(d)]
        return None if s.empty else s

    def fn(ticker: str, date_s: str) -> float | None:
        s = closes_upto(ticker, date_s)
        return None if s is None else common.safe_float(s.iloc[-1])

    def last_bar(ticker: str, date_s: str) -> str | None:
        s = closes_upto(ticker, date_s)
        return None if s is None else common.date_str(s.index[-1])

    fn.last_bar = last_bar  # type: ignore[attr-defined]
    return fn


def _safe_date(x: Any) -> Any:
    """``common.parse_date`` that returns None for blank or malformed cells."""
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    try:
        return common.parse_date(x)
    except (ValueError, TypeError):
        return None


def _ret(p0: float | None, p1: float | None) -> float | None:
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def _direction(action: str, stance: str) -> int | None:
    """+1 long call, -1 short call, None = excluded from hit rates."""
    a = (action or "").strip().upper()
    if a in LONG_ACTIONS:
        return 1
    if a in SHORT_ACTIONS:
        return -1
    if a:
        return None  # HOLD / PASS
    s = (stance or "").strip().lower()
    if s == "bullish":
        return 1
    if s == "bearish":
        return -1
    return None


def _rd(x: float | None, nd: int = PCT_DP) -> float | None:
    return None if x is None else round(x, nd)


def _group_stats(items: list[dict]) -> dict:
    eligible = [r for r in items if r["hit"] is not None]
    hits = sum(1 for r in eligible if r["hit"])
    ex_spy = [r["excess_spy_pp"] for r in items if r["excess_spy_pp"] is not None]
    ex_sec = [r["excess_sector_pp"] for r in items if r["excess_sector_pp"] is not None]
    return {"n": len(items), "n_hit_eligible": len(eligible), "hits": hits,
            "hit_rate": _rd(hits / len(eligible)) if eligible else None,
            "mean_excess_spy_pp": _rd(sum(ex_spy) / len(ex_spy)) if ex_spy else None,
            "mean_excess_sector_pp": _rd(sum(ex_sec) / len(ex_sec)) if ex_sec else None}


def _action_key(r: Mapping[str, Any]) -> str:
    a = str(r.get("action") or "").strip().upper()
    if a:
        return a
    s = str(r.get("stance") or "").strip().lower()
    return s or "none"


def _run_price_tickers(run_id: str) -> dict[str, str]:
    """{ticker: price_ticker} for proxy-priced holdings of a past run (its look-through)."""
    if not run_id:
        return {}
    try:
        doc = portfolio.read_json_opt(common.runs_dir() / run_id / "phase1" / "lookthrough.json")
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    holdings = doc.get("holdings") if isinstance(doc, Mapping) else None
    for h in holdings or []:
        if not isinstance(h, Mapping) or not h.get("ticker"):
            continue
        pt = h.get("price_ticker") or h.get("proxy_ticker")
        try:
            t = _norm(h["ticker"])
            pt = _norm(pt) if pt else None
        except ValueError:
            continue
        if pt and pt != t:
            out[t] = pt
    return out


def score(ledger_path: str | Path | None = None, *, price_fn: PriceFn | None = None,
          as_of: Any = None, min_age_days: int | None = None,
          price_tickers: Mapping[str, str] | None = None) -> dict:
    """Score every ledger row older than ``min_age_days`` against SPY and its sector ETF.

    Per row: ``days, ret_pct, spy_ret_pct, sector_ret_pct, excess_spy_pp, excess_sector_pp,
    hit, status`` (``scored | too_recent | no_price | stale_price | no_date``; ``status_reason``
    says why a ``stale_price`` row was not scored, ``price_end_date`` is the common last date the
    ticker / SPY / sector ETF were measured to — see the module doc). ``summary[role][action]`` =
    ``{n, n_hit_eligible, hits, hit_rate, mean_excess_spy_pp, mean_excess_sector_pp}`` where
    ``action`` is the row's action, or its stance when it has none (specialist rows).
    ``price_tickers`` ({ticker: price ticker}) overrides the proxy mapping (see module doc)."""
    path = Path(ledger_path) if ledger_path else common.ledger_path()
    rows = load_rows(path)
    cfg: dict = {}
    try:
        cfg = common.committee_cfg()
    except common.ConfigError:
        cfg = {}
    if min_age_days is None:
        min_age_days = int(common.safe_float(common.get_dotted(cfg, "ledger.min_age_days")) or 0)
    benchmark = _norm(cfg.get("benchmark") or "SPY")
    as_of_d = common.parse_date(as_of) or common.today()
    as_of_s = as_of_d.isoformat()
    fn = price_fn or default_price_fn()
    now_cache: dict[tuple[str, str], float | None] = {}
    bar_cache: dict[str, str | None] = {}
    last_bar_fn = getattr(fn, "last_bar", None)
    lag_cfg = common.safe_float(common.get_dotted(cfg, "evidence.price_max_trading_days"))
    max_lag = int(lag_cfg) if lag_cfg is not None else DEFAULT_PRICE_MAX_TRADING_DAYS
    explicit_pt = {_norm(k): _norm(v) for k, v in (price_tickers or {}).items() if k and v}
    try:
        cfg_pt = _cfg_proxies(common.load_config("portfolio"))
    except (common.ConfigError, OSError, ValueError):
        cfg_pt = {}
    run_pt_cache: dict[str, dict[str, str]] = {}

    def price_ticker(run_id: str, t: str) -> str:
        if t in explicit_pt:
            return explicit_pt[t]
        if run_id not in run_pt_cache:
            run_pt_cache[run_id] = _run_price_tickers(run_id)
        return run_pt_cache[run_id].get(t) or cfg_pt.get(t) or t

    def now_price(t: str, end_s: str) -> float | None:
        if (t, end_s) not in now_cache:
            now_cache[(t, end_s)] = common.safe_float(fn(t, end_s))
        return now_cache[(t, end_s)]

    def last_bar(t: str) -> str | None:
        """Date of the series' last bar at or before ``as_of`` (None: no series / no ``last_bar``)."""
        if last_bar_fn is None:
            return None
        if t not in bar_cache:
            try:
                bar_cache[t] = common.date_str(last_bar_fn(t, as_of_s))
            except (ValueError, TypeError, OSError):
                bar_cache[t] = None
        return bar_cache[t]

    def at_rec(t: str, given: Any, d: str | None) -> float | None:
        v = _f(given)
        if v is None and d:
            v = common.safe_float(fn(t, d))
        return v

    out_rows: list[dict] = []
    counts = {"rows": len(rows), "scored": 0, "too_recent": 0, "no_price": 0, "stale_price": 0, "no_date": 0}
    for r in rows:
        res: dict[str, Any] = dict(r)
        res.update({"days": None, "ret_pct": None, "spy_ret_pct": None, "sector_ret_pct": None,
                    "excess_spy_pp": None, "excess_sector_pp": None, "hit": None,
                    "status_reason": None, "price_end_date": None})
        rec = _safe_date(r.get("rec_date")) or _safe_date(r.get("price_date"))
        if rec is None:
            res["status"] = "no_date"
            counts["no_date"] += 1
            out_rows.append(res)
            continue
        res["days"] = (as_of_d - rec).days
        if res["days"] < min_age_days:
            res["status"] = "too_recent"
            counts["too_recent"] += 1
            out_rows.append(res)
            continue
        pdate = r.get("price_date") or r.get("rec_date")
        t = price_ticker(str(r.get("run_id") or ""), _norm(r.get("ticker") or ""))
        res["price_ticker"] = t
        etf = (r.get("sector_etf") or "").strip()
        etf_t = _norm(etf) if etf else None
        end_s = as_of_s
        if last_bar_fn is not None:
            bars = {x: last_bar(x) for x in dict.fromkeys([t, benchmark] + ([etf_t] if etf_t else []))}
            lagging = []
            for x, b in bars.items():
                if b is None:
                    continue
                lag = common.trading_days_between(b, as_of_d)
                if lag > max_lag:
                    lagging.append(f"{x} last bar {b} is {lag} trading days before {as_of_s}")
            if lagging:
                res["status"] = "stale_price"
                res["status_reason"] = "; ".join(lagging) + f" (max {max_lag})"
                counts["stale_price"] += 1
                out_rows.append(res)
                continue
            present = [b for b in bars.values() if b]
            if present:
                end_s = min(present)   # ticker, SPY and sector ETF are all measured to the same day
        res["price_end_date"] = end_s
        ret = _ret(at_rec(t, r.get("price_at_rec"), pdate), now_price(t, end_s))
        spy_ret = _ret(at_rec(benchmark, r.get("spy_at_rec"), pdate), now_price(benchmark, end_s))
        sec_ret = _ret(at_rec(etf_t, r.get("sector_etf_at_rec"), pdate), now_price(etf_t, end_s)) if etf_t else None
        res["ret_pct"] = _rd(ret)
        res["spy_ret_pct"] = _rd(spy_ret)
        res["sector_ret_pct"] = _rd(sec_ret)
        res["excess_spy_pp"] = _rd(ret - spy_ret) if ret is not None and spy_ret is not None else None
        res["excess_sector_pp"] = _rd(ret - sec_ret) if ret is not None and sec_ret is not None else None
        if ret is None:
            res["status"] = "no_price"
            res["status_reason"] = f"no usable price for {t} at the recommendation or at {end_s}"
            counts["no_price"] += 1
            out_rows.append(res)
            continue
        direction = _direction(r.get("action") or "", r.get("stance") or "")
        if direction is not None and res["excess_spy_pp"] is not None:
            res["hit"] = res["excess_spy_pp"] > 0 if direction > 0 else res["excess_spy_pp"] < 0
        res["status"] = "scored"
        counts["scored"] += 1
        out_rows.append(res)

    scored = [r for r in out_rows if r["status"] == "scored"]
    summary: dict[str, dict[str, dict]] = {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in scored:
        groups.setdefault((str(r.get("role") or ""), _action_key(r)), []).append(r)
    for (role, act), items in sorted(groups.items()):
        summary.setdefault(role, {})[act] = _group_stats(items)
    by_role = {role: _group_stats([r for r in scored if (r.get("role") or "") == role])
               for role in sorted({str(r.get("role") or "") for r in scored})}
    by_action = {a: _group_stats([r for r in scored if _action_key(r) == a])
                 for a in sorted({_action_key(r) for r in scored})}
    return {"as_of": as_of_s, "ledger_path": path.as_posix(), "min_age_days": min_age_days,
            "benchmark": benchmark, "counts": counts, "rows": out_rows, "summary": summary,
            "by_role": by_role, "by_action": by_action}


def _md(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.2f}"
    return str(x).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _pct(x: Any) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def render_scorecard_md(result: Mapping[str, Any]) -> str:
    """Markdown scorecard of ``score()`` output (role x action, by role, by action)."""
    c = result.get("counts") or {}
    lines = [f"# Committee scorecard — {result.get('as_of')}", "", f"> {common.DISCLAIMER}", "",
             f"Ledger: `{result.get('ledger_path')}` · benchmark {result.get('benchmark')} · "
             f"rows {c.get('rows', 0)} · scored {c.get('scored', 0)} · younger than "
             f"{result.get('min_age_days')} days {c.get('too_recent', 0)} · no price {c.get('no_price', 0)} · "
             f"stale price {c.get('stale_price', 0)}", "",
             "Hit = ADD/BUY (or bullish) beat SPY; SELL/TRIM (or bearish) lagged SPY. "
             "HOLD/PASS/neutral rows are not counted in hit rates.", ""]
    if c.get("stale_price"):
        lines += ["> Some rows were NOT scored because a cached price series (the ticker, SPY or the sector "
                  "ETF) ends more than the allowed number of trading days before the as-of date — refresh "
                  "prices and re-run (`status_reason` in the JSON result names each series).", ""]
    head = "| {} | n | hit-eligible | hit rate | mean excess vs SPY (pp) | mean excess vs sector (pp) |"
    sep = "|---|---:|---:|---:|---:|---:|"

    def row(label: str, s: Mapping[str, Any]) -> str:
        return (f"| {label} | {s['n']} | {s['n_hit_eligible']} | {_pct(s['hit_rate'])} | "
                f"{_md(s['mean_excess_spy_pp'])} | {_md(s['mean_excess_sector_pp'])} |")

    lines += ["## By role and action", "", head.format("role · action"), sep]
    summary = result.get("summary") or {}
    if not summary:
        lines.append("| Not available in this run | | | | | |")
    for role, acts in summary.items():
        for act, s in acts.items():
            lines.append(row(f"{_md(role)} · {_md(act)}", s))
    for title, key in (("By role", "by_role"), ("By action", "by_action")):
        lines += ["", f"## {title}", "", head.format(title.split()[-1]), sep]
        data = result.get(key) or {}
        if not data:
            lines.append("| Not available in this run | | | | | |")
        for k, s in data.items():
            lines.append(row(_md(k), s))
    return "\n".join(lines) + "\n"


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(description="Recommendation ledger: append a run, score past calls.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("append", help="append a run's recommendations to the ledger")
    g = pa.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="run directory (runs/<run_id>)")
    g.add_argument("--run", help="run id under the runs dir")
    common.add_common_args(pa)
    ps = sub.add_parser("score", help="score the ledger vs SPY and sector ETFs")
    ps.add_argument("--out", help="write a Markdown scorecard here")
    ps.add_argument("--json-out", help="write the full JSON result here")
    ps.add_argument("--min-age-days", type=int, default=None)
    common.add_common_args(ps)
    args = ap.parse_args(argv)
    explicit_ledger = getattr(args, "ledger", None)
    try:
        common.apply_common_args(args)
        if args.cmd == "append":
            run_dir = Path(args.run_dir) if args.run_dir else common.runs_dir() / args.run
            if not run_dir.is_dir():
                raise common.ConfigError(f"run dir not found: {run_dir.as_posix()}")
            manifest = portfolio.load_manifest(run_dir)
            if explicit_ledger:
                target = common.ledger_path()
            elif _is_dry_run(manifest):
                target = run_dir / "phase4" / PREVIEW_LEDGER
            else:
                target = common.ledger_path()
            rows = rows_from_run(run_dir)
            n = append(rows, target)
            print(common.dumps_json({"ledger": target.as_posix(), "rows": len(rows), "appended": n}))
            return 0
        result = score(min_age_days=args.min_age_days)
        if args.out:
            common.write_text(Path(args.out), render_scorecard_md(result))
        if args.json_out:
            common.write_json(Path(args.json_out), result)
        print(common.dumps_json({"out": args.out, "counts": result["counts"], "by_role": result["by_role"],
                                 "by_action": result["by_action"]}))
        return 0
    except (common.AginvError, OSError, ValueError) as exc:
        print(f"ledger.py: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
