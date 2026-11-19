"""dashboard.py - live, read-only progress page for a committee run.

Serves a single self-contained HTML page on 127.0.0.1 that polls ``/api/state`` every couple of
seconds. The run state is read from the run folder only (manifest.json, run_log.md, the agents' output
files). The Portfolio tab (``/api/portfolio``) values config/portfolio.yaml with live Yahoo quotes - the
only network use. The Portfolio tab's "Edit holdings" form writes config/portfolio.yaml (validated, backup in data/backups/, behind a
per-session token), and the Notifications tab keeps a small log of end-of-day alerts in data/alerts/log.json; the
server refuses non-local binds.

    python scripts/dashboard.py                # latest run, http://127.0.0.1:8765
    python scripts/dashboard.py --run 2026-10-25_1100 --port 9000

What it can show: step progress, every work order (pending / working / done / retry / failed), the
agent's one-line result as soon as its output file lands, the run log, and the memo link when done.
It cannot show an agent's internal tool calls - Claude Code does not expose them to the project.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts import alerts as alerts_mod  # noqa: E402
from scripts import portfolio_edit  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import hmac  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
import webbrowser  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

RUN_ID_RE = re.compile(r"^[0-9A-Za-z_.-]{3,64}$")
LOG_TAIL_LINES = 80
TEXT_LIMIT = 220


def list_runs(runs_dir: Path) -> list[str]:
    """Run ids (newest first) that have a manifest."""
    if not runs_dir.is_dir():
        return []
    ids = [p.name for p in runs_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file()]
    return sorted(ids, reverse=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _clip(text: Any, n: int = TEXT_LIMIT) -> str | None:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _num(x: Any) -> float | None:
    if isinstance(x, dict):
        x = x.get("value")
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def digest(role: str | None, doc: Any) -> dict:
    """One-line result of a finished agent output, by role. Never raises."""
    if not isinstance(doc, dict):
        return {}
    out: dict[str, Any] = {}
    try:
        if doc.get("status") == "failed":
            return {"headline": "failed - no report", "tone": "bad"}
        if role == "devils_advocate":
            out = {"headline": f"verdict: {doc.get('verdict_on_thesis')}", "text": _clip(doc.get("verdict_reason")),
                   "tone": {"survives": "good", "damaged": "warn", "broken": "bad"}.get(doc.get("verdict_on_thesis"), "")}
        elif role == "sector_head":
            vs = [v for v in doc.get("verdicts", []) if isinstance(v, dict)]
            acts = ", ".join(f"{v.get('ticker')} {v.get('action')}" for v in vs[:12])
            dev = sum(1 for v in vs if v.get("rule_deviation"))
            out = {"headline": acts or "no verdicts", "text": _clip(doc.get("summary")),
                   "tone": "warn" if dev else "", "badge": f"{dev} deviation(s)" if dev else None}
        elif role == "macro":
            out = {"headline": f"regime: {doc.get('regime')}", "text": _clip(doc.get("regime_summary")),
                   "conviction": doc.get("conviction")}
        elif role == "cio":
            if "final_actions" in doc:
                acts = [a for a in doc.get("final_actions", []) if isinstance(a, dict)]
                ov = sum(1 for a in acts if a.get("override"))
                es = doc.get("executive_summary") or [None]
                out = {"headline": f"{len(acts)} final actions", "text": _clip(es[0]), "conviction": doc.get("conviction"),
                       "badge": f"{ov} override(s)" if ov else None}
            else:
                out = {"headline": "expand: " + (", ".join(doc.get("expand_sectors") or []) or "none"),
                       "text": _clip(doc.get("rationale")), "conviction": doc.get("conviction")}
        elif role == "etf":
            out = {"headline": f"{doc.get('stance')} / {doc.get('action')}", "text": _clip(doc.get("thesis")),
                   "conviction": doc.get("conviction"),
                   "tone": {"bullish": "good", "bearish": "bad"}.get(doc.get("stance"), "")}
        else:  # fundamental / technical / operator_ceo / industry_ops / osint
            ext = doc.get("extensions") if isinstance(doc.get("extensions"), dict) else {}
            val = ext.get("valuation") if isinstance(ext.get("valuation"), dict) else {}
            fv = _num(val.get("fair_value_base"))
            out = {"headline": f"{doc.get('stance')}", "text": _clip(doc.get("thesis")), "conviction": doc.get("conviction"),
                   "tone": {"bullish": "good", "bearish": "bad"}.get(doc.get("stance"), ""),
                   "badge": f"fair value {fv:,.2f}" if fv is not None else None}
        flags = doc.get("red_flags")
        if isinstance(flags, list):
            hi = sum(1 for f in flags if isinstance(f, dict) and f.get("severity") == "high")
            if hi:
                out["flags"] = hi
    except Exception:  # noqa: BLE001 - a malformed report must never break the page
        return {}
    return out


def _output_path(order: dict) -> Path | None:
    for key in ("resolved_output", "output"):
        v = order.get(key)
        if v:
            return Path(v)
    return None


def order_state(oid: str, o: dict) -> dict:
    """UI state of one work order: pending | working | done | retry | failed."""
    status = o.get("status")
    out = _output_path(o)
    exists = bool(out and out.is_file())
    mtime = None
    if exists:
        try:
            mtime = datetime.fromtimestamp(out.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            pass
    attempt = int(o.get("attempt") or 1)
    if status == "done":
        ui = "done"
    elif status == "failed":
        ui = "failed"
    elif status == "dispatched":
        ui = "retry" if attempt > 1 and not exists else "working"
        if exists and attempt == 1:
            ui = "writing"  # output landed, the orchestrator has not validated it yet
    else:
        ui = "pending"
    d: dict[str, Any] = {}
    if exists and ui in ("done", "writing", "failed") or (exists and ui == "retry"):
        d = digest(o.get("agent_role"), _read_json(out))
    return {"id": oid, "step": o.get("step"), "agent_type": o.get("agent_type"), "role": o.get("agent_role"),
            "ticker": o.get("ticker"), "sector": o.get("sector"), "phase": o.get("phase"), "attempt": attempt,
            "ui": ui, "output_mtime": mtime, "errors": [_clip(e, 300) for e in (o.get("errors") or [])][:3],
            "result": d}


def tail_log(run_dir: Path, n: int = LOG_TAIL_LINES) -> list[str]:
    p = run_dir / "run_log.md"
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    return [ln[:400] for ln in lines[-n:]]


def build_state(runs_dir: Path, run_id: str | None) -> dict:
    """Everything the page needs, from the run folder (read-only)."""
    runs = list_runs(runs_dir)
    rid = run_id if run_id and run_id in runs else (runs[0] if runs else None)
    state: dict[str, Any] = {"now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "runs": runs[:15], "run_id": rid}
    if rid is None:
        return {**state, "empty": True}
    run_dir = runs_dir / rid
    m = _read_json(run_dir / "manifest.json") or {}
    steps = [{"name": s.get("name"), "kind": s.get("kind"), "status": s.get("status"), "started": s.get("started"),
              "ended": s.get("ended"), "summary": _clip(s.get("summary"), 300)} for s in m.get("steps", []) if isinstance(s, dict)]
    orders = [order_state(k, v) for k, v in (m.get("orders") or {}).items() if isinstance(v, dict)]
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["ui"]] = counts.get(o["ui"], 0) + 1
    memo = run_dir / "phase4" / "memo.html"
    trial = sorted((run_dir / "phase4").glob("trial_*.html")) if (run_dir / "phase4").is_dir() else []
    memo_path = memo if memo.is_file() else (trial[0] if trial else None)
    summary = _read_json(run_dir / "phase4" / "summary.json") or {}
    return {**state, "empty": False, "status": m.get("status"), "mode": m.get("mode_label") or m.get("mode"),
            "as_of": m.get("as_of"), "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
            "steps": steps, "orders": orders, "counts": counts, "warnings": [_clip(w, 300) for w in m.get("warnings", [])][:8],
            "log": tail_log(run_dir), "memo": f"/memo?run={rid}" if memo_path else None,
            "headline": _clip(summary.get("summary") or summary.get("top_line"), 300),
            "config_drift": bool(m.get("config_drift"))}



# =====================================================================================
# Live portfolio (read-only): config/portfolio.yaml + live Yahoo quotes
# =====================================================================================
QUOTE_TTL_S = 8.0
_quote_lock = threading.Lock()
_snapshot_cache: dict[str, Any] = {"t": 0.0, "key": None, "data": None}


def _usd_price(price: float, currency: str | None, usdils: float | None) -> tuple[float | None, float | None]:
    """(price in USD, price in ILS major units) for a Yahoo quote. USD quotes have no ILS price."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return price, (price * usdils if usdils else None)
    if cur in ("ILS", "ILA"):
        ils = price / 100.0 if cur == "ILA" else price
        return (ils / usdils if usdils else None), ils
    return None, None


def portfolio_snapshot(cfg: dict | None = None, quote_fn: Any = None) -> dict:
    """Live valuation of the holdings in portfolio.yaml. Never raises for a missing quote (that row
    carries ``error`` and is left out of the totals). ``quote_fn(ticker) -> {price, previous_close,
    currency}`` defaults to ``common.yf_quote``."""
    quote_fn = quote_fn or common.yf_quote
    cfg = cfg if cfg is not None else common.load_config("portfolio")
    fx_t = (common.committee_cfg().get("fx_ticker") if cfg is not None else None) or "ILS=X"
    holdings = [h for h in cfg.get("holdings") or [] if isinstance(h, dict) and h.get("ticker")]
    need = {fx_t}
    for h in holdings:
        need.add(str(h.get("proxy_ticker") or h["ticker"]))
    quotes: dict[str, Any] = {}

    def get(t: str) -> None:
        try:
            quotes[t] = quote_fn(t)
        except Exception as exc:  # noqa: BLE001 - one bad quote must not break the page
            quotes[t] = exc

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(get, sorted(need)))

    fxq = quotes.get(fx_t)
    usdils = fxq["price"] if isinstance(fxq, dict) else None
    fx_prev = fxq.get("previous_close") if isinstance(fxq, dict) else None
    warnings: list[str] = []
    if usdils is None:
        warnings.append("No live USD/ILS rate - ILS values are unavailable.")
    rows: list[dict] = []
    for h in holdings:
        t = str(h["ticker"])
        qt = str(h.get("proxy_ticker") or t)
        q = quotes.get(qt)
        shares = common.safe_float(h.get("shares"))
        units = common.safe_float(h.get("units"))
        row: dict[str, Any] = {"ticker": t, "name": h.get("name"), "asset_type": h.get("asset_type"),
                               "currency": (h.get("currency") or "USD"), "quote_ticker": qt,
                               "proxy": bool(h.get("proxy_ticker")), "shares": shares, "units": units,
                               "error": None}
        if not isinstance(q, dict) or shares is None:
            row["error"] = ("no live quote from Yahoo - check the symbol" if isinstance(q, Exception)
                            else "no shares in portfolio.yaml")
            warnings.append(f"{t}: {row['error']}")
            rows.append(row)
            continue
        price_usd, price_ils = _usd_price(q["price"], q.get("currency"), usdils)
        prev = q.get("previous_close")
        day_pct = (q["price"] / prev - 1.0) * 100.0 if prev else None
        value_usd = shares * price_usd if price_usd is not None else None
        value_ils = value_usd * usdils if (value_usd is not None and usdils) else None
        cb = common.safe_float(h.get("cost_basis"))
        cb_cur = str(h.get("cost_basis_currency") or "USD").upper()
        cost_native = pl_native = pl_pct = pl_usd = cost_usd = None
        native_ccy = "USD"
        avg_cost = None
        last = price_usd
        if row["proxy"] and units and common.safe_float(h.get("unit_cost_ils")) is not None and value_ils is not None:
            # TASE fund priced through its US proxy: P/L in ILS against the units' ILS cost
            native_ccy, avg_cost = "ILS", common.safe_float(h.get("unit_cost_ils"))
            cost_native = units * avg_cost
            pl_native = value_ils - cost_native
            last = value_ils / units
        elif cb is not None and cb_cur == "ILS" and price_ils is not None:
            native_ccy, avg_cost, last = "ILS", cb, price_ils
            cost_native = shares * cb
            pl_native = shares * price_ils - cost_native
        elif cb is not None and cb_cur == "USD" and price_usd is not None:
            avg_cost, cost_native = cb, shares * cb
            pl_native = value_usd - cost_native
        if cost_native:
            pl_pct = pl_native / cost_native * 100.0
            if native_ccy == "ILS" and usdils:
                cost_usd, pl_usd = cost_native / usdils, pl_native / usdils
            elif native_ccy == "USD":
                cost_usd, pl_usd = cost_native, pl_native
        day_usd = (value_usd - value_usd / (1 + day_pct / 100.0)) if (value_usd is not None and day_pct is not None) else None
        editable_units = bool(row["proxy"] and units)
        row.update({"qty_key": "units" if editable_units else "shares",
                    "cost_key": "unit_cost_ils" if editable_units else "cost_basis",
                    "native_currency": native_ccy, "avg_cost": avg_cost, "last": last, "day_pct": day_pct,
                    "value_usd": value_usd, "value_ils": value_ils, "cost_usd": cost_usd, "pl_native": pl_native,
                    "pl_pct": pl_pct, "pl_usd": pl_usd, "day_usd": day_usd})
        if cost_native is None:
            row["note"] = "no cost basis in portfolio.yaml"
        elif row["proxy"]:
            row["note"] = f"valued through {qt}"
        rows.append(row)

    cash_usd = common.safe_float(cfg.get("cash_usd")) or 0.0
    ok = [r for r in rows if r.get("value_usd") is not None]
    value = sum(r["value_usd"] for r in ok) + cash_usd
    with_cost = [r for r in ok if r.get("cost_usd")]
    cost = sum(r["cost_usd"] for r in with_cost)
    pl = sum(r["pl_usd"] for r in with_cost)
    day = sum(r["day_usd"] for r in ok if r.get("day_usd") is not None)
    for r in ok:
        r["weight_pct"] = r["value_usd"] / value * 100.0 if value else None
    missing_cost = [r["ticker"] for r in ok if not r.get("cost_usd")]
    if missing_cost:
        warnings.append("No cost basis for: " + ", ".join(missing_cost) + " (left out of the P/L total).")
    return {
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of_config": cfg.get("as_of"),
        "fx": {"usdils": usdils, "previous": fx_prev,
               "day_pct": (usdils / fx_prev - 1.0) * 100.0 if usdils and fx_prev else None},
        "rows": rows,
        "cash": {"usd": cash_usd, "weight_pct": cash_usd / value * 100.0 if value else None},
        "totals": {"value_usd": value, "value_ils": value * usdils if usdils else None,
                   "invested_cost_usd": cost, "pl_usd": pl, "pl_pct": pl / cost * 100.0 if cost else None,
                   "pl_ils": pl * usdils if usdils else None,
                   "day_usd": day, "day_pct": day / (value - day) * 100.0 if value - day else None},
        "warnings": warnings,
    }


def cached_snapshot(quote_fn: Any = None) -> dict:
    """``portfolio_snapshot`` with a few seconds of caching so several open tabs do not hammer Yahoo."""
    try:
        key = (str(common.config_dir()), (common.config_dir() / "portfolio.yaml").stat().st_mtime)
    except OSError:
        key = None
    with _quote_lock:
        if quote_fn is None and _snapshot_cache["data"] is not None and _snapshot_cache["key"] == key \
                and time.time() - _snapshot_cache["t"] < QUOTE_TTL_S:
            return _snapshot_cache["data"]
        data = portfolio_snapshot(None, quote_fn)
        if quote_fn is None:
            _snapshot_cache.update(t=time.time(), key=key, data=data)
        return data



# =====================================================================================
# Notifications: end-of-day warning signs (rules in scripts/alerts.py)
# =====================================================================================
_sector_cache: dict[str, str | None] = {}
_alerts_lock = threading.Lock()


def latest_run_context(runs_dir: Path, today: Any = None, max_age_days: float = 7.0) -> tuple[dict | None, dict]:
    """(run context for stop/plan rules, {ticker: sector split} from the newest look-through).

    The run context comes from the newest run that has a Phase-2 aggregate and is at most
    ``max_age_days`` old: ``{"id","as_of","invalidations","plan"}``. Splits come from the newest run that
    has a look-through, however old (fund compositions change slowly)."""
    today = today or datetime.now().date()
    ctx = None
    splits: dict[str, dict[str, float]] = {}
    for rid in list_runs(runs_dir):
        rd = runs_dir / rid
        if not splits:
            lt = _read_json(rd / "phase1" / "lookthrough.json")
            if isinstance(lt, dict):
                for h in lt.get("holdings") or []:
                    if isinstance(h, dict) and isinstance(h.get("sector_split"), dict):
                        splits[str(h["ticker"])] = {k: float(v) for k, v in h["sector_split"].items()
                                                    if isinstance(v, (int, float))}
        if ctx is None:
            man = _read_json(rd / "manifest.json") or {}
            agg = _read_json(rd / "phase2" / "aggregate.json")
            if isinstance(agg, dict) and not (man.get("mode") or {}).get("dry_run"):
                try:
                    age = (today - datetime.fromisoformat(str(man.get("as_of"))).date()).days
                except (TypeError, ValueError):
                    age = 10 ** 6
                if age <= max_age_days:
                    inv = {}
                    for t, e in (agg.get("tickers") or {}).items():
                        v = ((e or {}).get("rr") or {}).get("invalidation")
                        if isinstance(v, (int, float)) and (e or {}).get("kind") == "holding":
                            inv[str(t)] = float(v)
                    plan = {}
                    pp = _read_json(rd / "phase4" / "portfolio_plan.json") or {}
                    for pos in pp.get("positions") or []:
                        tr = pos.get("trade") or {}
                        if tr.get("side") == "SELL" and tr.get("shares"):
                            plan[str(pos["ticker"])] = {"side": "SELL", "shares": float(tr["shares"]),
                                                        "shares_cur": float(pos.get("shares_cur") or 0),
                                                        "action": pos.get("action")}
                    ctx = {"id": rid, "as_of": man.get("as_of"), "invalidations": inv, "plan": plan}
        if ctx is not None and splits:
            break
    return ctx, splits


def _yahoo_sector_split(ticker: str) -> dict[str, float] | None:
    """A single-sector split for a stock the latest run did not know (cached; None when unknown)."""
    if ticker not in _sector_cache:
        try:
            sec = common.YF_SECTOR_MAP.get(str(common.yf_info(ticker).get("sector")))
        except Exception:  # noqa: BLE001 - classification is best effort
            sec = None
        _sector_cache[ticker] = sec
    sec = _sector_cache[ticker]
    return {sec: 100.0} if sec else None


def alert_log_path() -> Path:
    return common.cache_dir().parent / "alerts" / "log.json"


def _record_alerts(active: list[dict]) -> list[dict]:
    """Remember FINAL warning/critical alerts the first time they are seen; return the newest 40 records."""
    path = alert_log_path()
    log = _read_json(path)
    log = log if isinstance(log, dict) else {}
    changed = False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for a in active:
        if a["final"] and a["severity"] in ("critical", "warning") and a["key"] not in log:
            log[a["key"]] = {"first_seen": now, **{k: a[k] for k in ("severity", "ticker", "title", "why", "action", "session", "rule")}}
            changed = True
    if changed:
        try:
            common.write_json(path, log)
        except OSError:
            pass
    return sorted(log.values(), key=lambda r: r.get("first_seen", ""), reverse=True)[:40]


def build_alerts(runs_dir: Path, snapshot: dict | None = None, now: datetime | None = None) -> dict:
    """Everything the Notifications tab needs."""
    snap = snapshot if snapshot is not None else cached_snapshot()
    try:
        profile = common.load_config("investor_profile")
        ccfg = common.committee_cfg()
    except common.AginvError:
        profile, ccfg = {}, {}
    acfg = ccfg.get("alerts") or {}
    mkt = alerts_mod.market_state(now)
    ctx, splits = latest_run_context(runs_dir, (now or datetime.now()).date(), float(acfg.get("run_max_age_days", 7)))
    for r in snap.get("rows") or []:
        if r["ticker"] not in splits and r.get("asset_type") != "fund" and not r.get("proxy") and not r.get("error"):
            sp = _yahoo_sector_split(r["ticker"])
            if sp:
                splits[r["ticker"]] = sp
    active = alerts_mod.evaluate(snap, profile, acfg, market=mkt, splits=splits or None, run=ctx,
                                 today=(now or datetime.now()).date())
    with _alerts_lock:
        history = _record_alerts(active)
    return {"evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market": {"open": mkt["open"], "last_session": mkt["last_session"].isoformat(),
                       "closed_at_utc": mkt["closed_at_utc"], "et_time": mkt["et_time"]},
            "final": not mkt["open"], "alerts": active, "history": history,
            "rules": alerts_mod.rules_catalog(profile, acfg),
            "run": {"id": ctx["id"], "as_of": ctx["as_of"]} if ctx else None,
            "sector_data": bool(splits)}

PAGE_FILE = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"


def load_page(token: str = "") -> bytes:
    """The page is a static file (templates/dashboard.html), re-read on every request so edits show on
    refresh. ``__TOKEN__`` is replaced by this server session's edit token."""
    try:
        return PAGE_FILE.read_bytes().replace(b"__TOKEN__", token.encode("utf-8"))
    except OSError:
        return b"<!doctype html><title>Committee Live</title><p>templates/dashboard.html is missing.</p>"



def edits_from_payload(payload: dict, snapshot: dict) -> dict:
    """Translate the form payload into ``portfolio_edit`` edits. Cash is entered as USD + ILS and stored
    as one USD figure at the live rate."""
    if not isinstance(payload, dict):
        raise portfolio_edit.EditError("bad request")
    edits: dict[str, Any] = {"holdings": {}, "remove": [], "add": []}
    for t, ch in (payload.get("holdings") or {}).items():
        if isinstance(ch, dict) and ch:
            edits["holdings"][str(t)] = ch
    edits["remove"] = [str(x) for x in (payload.get("remove") or [])]
    edits["add"] = [a for a in (payload.get("add") or []) if isinstance(a, dict)]
    cash = payload.get("cash")
    if isinstance(cash, dict):
        usd = portfolio_edit._num(cash.get("usd") or 0, "USD cash")
        ils = portfolio_edit._num(cash.get("ils") or 0, "ILS cash")
        fx = (snapshot.get("fx") or {}).get("usdils")
        if ils and not fx:
            raise portfolio_edit.EditError("no live USD/ILS rate to convert the ILS cash - try again in a moment")
        total = usd + (ils / fx if ils else 0.0)
        edits["cash_usd"] = round(total, 2)
        edits["cash_note"] = f"${usd:,.2f} USD" + (f" + ILS {ils:,.2f} (= ${ils / fx:,.2f} at USD/ILS {fx:.4f})" if ils else "")
    edits["as_of"] = datetime.now().date().isoformat()
    return edits


class Handler(BaseHTTPRequestHandler):
    runs_dir: Path = Path(".")
    token: str = ""
    port: int = 0

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self) -> bool:
        """Host must be this local server and any Origin must be the same origin (blocks DNS rebinding
        and cross-site form posts)."""
        ok_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if (self.headers.get("Host") or "") not in ok_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in ok_hosts}

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        u = urlparse(self.path)
        if u.path != "/api/portfolio/edit":
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        def reply(code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        if not self._local_request():
            return reply(403, {"error": "forbidden origin"})
        if not self.token or not hmac.compare_digest(self.headers.get("X-Dash-Token") or "", self.token):
            return reply(403, {"error": "missing or wrong session token - reload the page"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 40_000:
            return reply(400, {"error": "bad request size"})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            snap = cached_snapshot()
            result = portfolio_edit.save_edits(edits_from_payload(payload, snap))
        except portfolio_edit.EditError as exc:
            return reply(400, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError):
            return reply(400, {"error": "bad request"})
        _snapshot_cache.update(t=0.0, key=None, data=None)
        running = any((_read_json(self.runs_dir / r / "manifest.json") or {}).get("status") == "in_progress"
                      for r in list_runs(self.runs_dir)[:1])
        reply(200, {"ok": True, **result, "run_in_progress": running, "snapshot": cached_snapshot()})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        u = urlparse(self.path)
        q = parse_qs(u.query)
        run = (q.get("run") or [""])[0]
        if run and not RUN_ID_RE.match(run):
            return self._send(400, b"bad run id", "text/plain; charset=utf-8")
        if u.path == "/":
            return self._send(200, load_page(self.token), "text/html; charset=utf-8")
        if u.path == "/api/state":
            body = json.dumps(build_state(self.runs_dir, run or None), ensure_ascii=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if u.path == "/api/portfolio":
            try:
                body = json.dumps(cached_snapshot(), ensure_ascii=False).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - report, do not crash the server thread
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if u.path == "/api/alerts":
            try:
                body = json.dumps(build_alerts(self.runs_dir), ensure_ascii=False).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - report, do not crash the server thread
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if u.path == "/memo":
            rid = run or (list_runs(self.runs_dir) or [""])[0]
            if not rid:
                return self._send(404, b"no run", "text/plain; charset=utf-8")
            p4 = self.runs_dir / rid / "phase4"
            cands = [p4 / "memo.html"] + sorted(p4.glob("trial_*.html")) if p4.is_dir() else []
            for c in cands:
                if c.is_file():
                    return self._send(200, c.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, b"memo not ready", "text/plain; charset=utf-8")
        self._send(404, b"not found", "text/plain; charset=utf-8")


def make_server(runs_dir: Path, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise common.ConfigError("the dashboard only binds to localhost (it exposes your portfolio runs)")
    handler = type("BoundHandler", (Handler,), {"runs_dir": Path(runs_dir), "token": secrets.token_urlsafe(24)})
    srv = ThreadingHTTPServer((host, port), handler)
    handler.port = srv.server_address[1]
    return srv


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(description="Live read-only dashboard for committee runs.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--run", help="run id to open first (default: latest)")
    ap.add_argument("--no-browser", action="store_true")
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    try:
        common.apply_common_args(args)
        srv = make_server(common.runs_dir(), args.port)
    except (common.AginvError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{args.port}/" + (f"?run={args.run}" if args.run else "")
    print(f"Committee dashboard (read-only): {url}   Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
