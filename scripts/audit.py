"""audit.py - deterministic quality audit of a finished committee run (used by the chair).

``run(run_dir, *, now=None, portfolio_path=None, write=True) -> dict`` reads the run's artifacts
(manifest, phase1 look-through, phase2/3 aggregates, sector-head verdicts, agent reports, valuation and
fundamentals data files, phase4 ``cio_final.json`` / ``portfolio_plan.json`` / ``memo.md``) plus the LIVE
``config/portfolio.yaml`` and returns - and, with ``write=True``, atomically writes to
``phase4/audit.json`` - a list of findings. Every finding says what is wrong in plain English, carries the
numbers that prove it, and names the fix the chair should apply (re-run agents with a ready-made
feedback text, re-render, re-plan, start a new run, or just note it). Nothing here calls the network or an
agent; the same run always gives the same findings (ids are assigned after a stable sort).

Checks (each ``check_*`` takes a :class:`Ctx` and returns a list of findings; thresholds live in
``config/committee.yaml`` under ``audit:``, with defaults in :data:`DEFAULTS`):

    stale_portfolio            high    live portfolio.yaml / prices differ from what the run analysed
    action_vs_trade            medium  final action TRIM/SELL/ADD/BUY but the plan has no executable trade
    trim_became_full_exit      high    a TRIM verdict that the plan sizes as a full exit
    text_vs_plan_contradiction high    CIO / sector-head / memo text contradicts the plan for a ticker
    valuation_outlier          med/high fair value base far from the price (needs a sanity check)
    systematic_valuation_bias  high    nearly every stock valued below its price (method likely too harsh)
    captive_or_leverage_flag   medium  captive-finance / leverage flag on the valuation inputs
    plan_does_not_cure_breach  medium  a look-through sector is still above the profile cap after the plan
    limit_inconsistency        medium  different sector limits quoted in different places
    weak_why                   low     a holding's "Why" is too short or still shows rule codes
    unresolved_high_flags / failed_reports / low_evidence   medium
    thin_review                medium  no Devil's Advocate, or lenses disagree with the action
    pending_event_risk         low     results / ruling due within days of the run
    cash_drag                  low     lots of idle cash after the plan and no buys

CLI: ``python scripts/audit.py --run-dir runs/<id> [--json] [--no-write]``. Decision support only.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from typing import Any, Callable, Iterable, Mapping  # noqa: E402

SCHEMA = "audit/1"
SEVERITIES = ("high", "medium", "low")
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
FIX_TYPES = ("rerun_agents", "rerender", "replan", "new_run", "note_only")
SECTIONS = ("2", "4", "5", "6", "7", "10", "run")
AUDIT_FILE = "audit.json"

#: Defaults used when ``committee.yaml`` has no ``audit:`` block (or a key is missing).
DEFAULTS: dict[str, Any] = {
    "stale_price_trading_days": 3,      # prices older than this many trading days at audit time
    "stale_cash_change_usd": 100.0,     # cash in portfolio.yaml moved by more than this since the run
    "share_tolerance": 1e-6,            # shares equal within this (relative)
    "cost_tolerance": 0.005,            # cost basis equal within this (absolute)
    "outlier_medium_low": 0.6,          # fair value base / price below this -> medium
    "outlier_medium_high": 1.6,         # ... above this -> medium
    "outlier_high_low": 0.35,           # ... below this -> high
    "outlier_high_high": 2.5,           # ... above this -> high
    "bias_share": 0.75,                 # share of stocks valued below price that suggests a harsh method
    "bias_min_n": 6,                    # ... only judged with at least this many stock valuations
    "weak_why_min_words": 18,
    "event_window_days": 3,             # a dated event within this many days of as_of
    "cash_drag_margin_pp": 5.0,         # post-trade cash above floor + this, with no buys
    "min_evidence": None,               # evidence below this is "low"; None -> actions.min_evidence (5)
    "text_share_tolerance": 0.5,        # shares quoted in text vs the plan
    "text_pct_tolerance_pp": 1.5,       # percent quoted in text vs the plan
    "text_ticker_window_chars": 50,     # a claim refers to a ticker at most this far away
}

DEFAULT_MIN_EVIDENCE = 5.0

# =====================================================================================
# Small helpers
# =====================================================================================


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _num(x: Any) -> float | None:
    return common.safe_float(common.fact_value(x) if isinstance(x, dict) else x)


def _nt(t: Any) -> str:
    try:
        return common.norm_ticker(t)
    except (ValueError, TypeError, AttributeError):
        return str(t or "").strip().upper()


def _f2(x: Any, dp: int = 2) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    s = f"{v:,.{dp}f}"
    return s


def _pct(x: Any, dp: int = 2) -> str:
    v = _num(x)
    return "n/a" if v is None else f"{v:.{dp}f}%"


def _usd(x: Any, dp: int = 2) -> str:
    v = _num(x)
    return "n/a" if v is None else f"${v:,.{dp}f}"


def _clip(s: Any, n: int = 220) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


def _read(path: Path) -> Any:
    try:
        return common.read_json(path)
    except (OSError, ValueError):
        return None


def _as_date(x: Any) -> date | None:
    try:
        return common.parse_date(x)
    except (ValueError, TypeError):
        return None


def _join(items: Iterable[str], last: str = "and") -> str:
    xs = [str(i) for i in items if str(i)]
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + f" {last} " + xs[-1]


def audit_cfg(full_cfg: Mapping | None) -> dict[str, Any]:
    """:data:`DEFAULTS` overlaid with the numeric keys of ``committee.yaml: audit``."""
    out = dict(DEFAULTS)
    block = _d((full_cfg or {}).get("audit")) if isinstance(full_cfg, Mapping) else {}
    for k, default in DEFAULTS.items():
        if k not in block:
            continue
        v = common.safe_float(block[k])
        if v is None:
            continue
        out[k] = int(v) if isinstance(default, int) and not isinstance(default, bool) else v
    if out.get("min_evidence") is None and isinstance(full_cfg, Mapping):
        out["min_evidence"] = common.safe_float(common.get_dotted(full_cfg, "actions.min_evidence"))
    if out.get("min_evidence") is None:
        out["min_evidence"] = DEFAULT_MIN_EVIDENCE
    return out


def _fix(type_: str, roles: Iterable[str] = (), tickers: Iterable[str] = (), feedback: str = "") -> dict:
    return {"type": type_, "roles": list(dict.fromkeys(roles)), "tickers": list(dict.fromkeys(tickers)),
            "feedback": feedback}


def _finding(code: str, severity: str, ticker: str | None, section: str, message: str, evidence: str,
             fix: dict) -> dict:
    return {"code": code, "severity": severity, "ticker": ticker, "section": section,
            "message": message, "evidence": evidence, "fix": fix}


# =====================================================================================
# Context (everything the checks read), built from disk by ``load_ctx``
# =====================================================================================


class Ctx:
    """Run artifacts the checks read. Tests can build one directly with keyword arguments."""

    def __init__(self, **kw: Any) -> None:
        self.run_dir: Path | None = None
        self.run_id: str | None = None
        self.as_of: date | None = None
        self.now: date | None = None
        self.cfg: dict[str, Any] = dict(DEFAULTS)
        self.full_cfg: dict = {}
        self.profile: dict = {}
        self.manifest: dict = {}
        self.plan: dict | None = None
        self.cio: dict | None = None
        self.memo_text: str = ""
        self.lookthrough: dict | None = None
        self.holdings: list[str] = []
        self.aggregates: dict[tuple[int, str], dict] = {}
        self.verdict_docs: list[dict] = []        # {phase, sector, path, doc}
        self.verdicts: dict[tuple[int, str], dict] = {}
        self.reports: dict[tuple[int, str, str], dict] = {}   # (phase, role, ticker) -> report
        self.valuation: dict[str, dict] = {}
        self.fundamentals: dict[str, dict] = {}
        self.snapshot_portfolio: dict | None = None
        self.live_portfolio: dict | None = None
        self.live_portfolio_sha: str | None = None
        self.holidays: list = []
        for k, v in kw.items():
            setattr(self, k, v)

    # ---- derived views -------------------------------------------------------------
    @property
    def plan_positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in _l(_d(self.plan).get("positions")):
            if isinstance(p, dict) and p.get("ticker"):
                out.setdefault(_nt(p["ticker"]), p)
        return out

    @property
    def final_actions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for a in _l(_d(self.cio).get("final_actions")):
            if isinstance(a, dict) and a.get("ticker"):
                out.setdefault(_nt(a["ticker"]), a)
        return out

    def action_of(self, t: str) -> str | None:
        fa = self.final_actions.get(t)
        if fa and fa.get("action"):
            return str(fa["action"]).upper()
        pos = self.plan_positions.get(t)
        if pos and pos.get("action"):
            return str(pos["action"]).upper()
        for (_, tk), v in sorted(self.verdicts.items()):
            if tk == t and v.get("action"):
                return str(v["action"]).upper()
        return None

    def sector_of(self, t: str) -> str | None:
        for (_, tk), a in sorted(self.aggregates.items()):
            if tk == t and a.get("sector"):
                return a["sector"]
        for (_, tk), v in sorted(self.verdicts.items()):
            if tk == t and v.get("_sector"):
                return v["_sector"]
        pos = self.plan_positions.get(t)
        return pos.get("sector") if pos else None

    def aggregate(self, t: str) -> dict | None:
        """The ticker's aggregate entry (full beats light, phase 2 beats 3)."""
        best = None
        for (phase, tk), a in sorted(self.aggregates.items(), key=lambda kv: (bool(kv[1].get("_light")), kv[0])):
            if tk == t:
                best = a
                break
        return best

    def is_holding(self, t: str) -> bool:
        if t in self.holdings:
            return True
        a = self.aggregate(t)
        return bool(a and a.get("kind") == "holding")

    def asset_type(self, t: str) -> str | None:
        a = self.aggregate(t)
        if a and a.get("asset_type"):
            return a["asset_type"]
        pos = self.plan_positions.get(t)
        return pos.get("asset_type") if pos else None

    def memo_section(self, number: int) -> str:
        return memo_section(self.memo_text, number)


def memo_section(memo_text: str, number: int) -> str:
    """Text of ``## <number>. ...`` up to the next ``## `` heading (empty when absent)."""
    m = re.search(rf"^## {number}\. .*$", memo_text or "", re.M)
    if not m:
        return ""
    rest = memo_text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _split_cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip().replace("\\|", "|") for p in parts]


def memo_table_rows(section_text: str) -> dict[str, dict[str, str]]:
    """First Markdown table of a section as ``{normalized first-cell: {header: cell}}``."""
    lines = [ln for ln in (section_text or "").splitlines()]
    out: dict[str, dict[str, str]] = {}
    i = 0
    while i < len(lines) and not lines[i].lstrip().startswith("|"):
        i += 1
    if i + 1 >= len(lines):
        return out
    headers = _split_cells(lines[i])
    j = i + 2  # skip the separator row
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        cells = _split_cells(lines[j])
        if cells and len(cells) >= 2:
            out.setdefault(_nt(cells[0]), dict(zip(headers, cells)))
        j += 1
    return out


_ROLE_KEYS_BY_LEN = sorted(common.ROLE_KEYS, key=len, reverse=True)


def _split_role_ticker(stem: str) -> tuple[str | None, str | None]:
    for role in _ROLE_KEYS_BY_LEN:
        if stem == role:
            return role, None
        if stem.startswith(role + "_"):
            return role, _nt(stem[len(role) + 1:])
    return None, None


_VARIANT_RE = re.compile(r"^(?P<stem>.+?)(?:\.r(?P<n>\d+))?$")


def _pick_variants(directory: Path) -> dict[str, Path]:
    """``X.json`` / ``X.r2.json`` ... -> {stem: newest variant}."""
    best: dict[str, tuple[int, Path]] = {}
    if not directory.is_dir():
        return {}
    for p in sorted(directory.glob("*.json")):
        m = _VARIANT_RE.match(p.stem)
        if not m:
            continue
        n = int(m.group("n") or 0)
        stem = m.group("stem")
        if stem not in best or n > best[stem][0]:
            best[stem] = (n, p)
    return {k: v[1] for k, v in best.items()}


def _load_yaml(path: Path) -> dict:
    try:
        data = common.load_yaml(path)
    except (common.AginvError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_now(now: Any) -> tuple[date, str | None]:
    """(date, iso timestamp or None) of the ``now`` argument."""
    if now is None:
        return common.today(), None
    if isinstance(now, datetime):
        stamp = now.isoformat(timespec="seconds")
        if now.tzinfo is None:
            stamp += "+00:00"
        return now.date(), stamp
    d = _as_date(now)
    if d is None:
        raise ValueError(f"unusable now={now!r}")
    return d, None


def load_ctx(run_dir: str | Path, *, now: Any = None, portfolio_path: str | Path | None = None) -> Ctx:
    """Read every artifact of ``run_dir`` the checks need (missing or unreadable files are skipped)."""
    rd = Path(run_dir)
    if not rd.is_dir():
        raise FileNotFoundError(f"run directory not found: {rd}")
    ctx = Ctx(run_dir=rd)
    ctx.now, ctx.now_stamp = _coerce_now(now)  # type: ignore[attr-defined]
    ctx.manifest = _d(_read(rd / "manifest.json"))
    ctx.run_id = ctx.manifest.get("run_id") or rd.name
    ctx.as_of = _as_date(ctx.manifest.get("as_of"))
    committee_snapshot = _load_yaml(rd / "inputs" / "committee.yaml")
    try:
        live_cfg = common.committee_cfg()
    except (common.AginvError, OSError, ValueError):
        live_cfg = {}
    ctx.full_cfg = live_cfg or committee_snapshot
    # thresholds of the audit come from the live config; run-level numbers (min_evidence) from the run snapshot
    cfg = audit_cfg(live_cfg or committee_snapshot)
    if _d(live_cfg.get("audit")).get("min_evidence") is None and committee_snapshot:
        snap_min = common.safe_float(common.get_dotted(committee_snapshot, "actions.min_evidence"))
        if snap_min is not None:
            cfg["min_evidence"] = snap_min
    ctx.cfg = cfg
    ctx.profile = _load_yaml(rd / "inputs" / "investor_profile.yaml")
    if not ctx.profile:
        try:
            ctx.profile = common.load_config("investor_profile")
        except (common.AginvError, OSError, ValueError):
            ctx.profile = {}
    try:
        ctx.holidays = common.market_holidays()
    except (common.AginvError, OSError, ValueError):
        ctx.holidays = []

    p1, p4 = rd / "phase1", rd / "phase4"
    ctx.lookthrough = _read(p1 / "lookthrough.json")
    ctx.plan = _read(p4 / "portfolio_plan.json")
    ctx.cio = _read(_pick_variants(p4).get("cio_final") or (p4 / "cio_final.json"))   # newest retry/chair re-run wins
    try:
        ctx.memo_text = common.read_text(p4 / "memo.md") if (p4 / "memo.md").is_file() else ""
    except OSError:
        ctx.memo_text = ""

    ctx.holdings = [_nt(h["ticker"]) for h in _l(ctx.manifest.get("holdings"))
                    if isinstance(h, dict) and h.get("ticker")]

    for phase in (2, 3):
        d = rd / f"phase{phase}"
        for fname, light in (("aggregate_light.json", True), ("aggregate.json", False)):
            doc = _read(d / fname)
            for t, a in _d(_d(doc).get("tickers")).items():
                if isinstance(a, dict):
                    key = (phase, _nt(t))
                    if light and key in ctx.aggregates:
                        continue
                    ctx.aggregates[key] = dict(a, _light=light, _phase=phase)
        for stem, path in sorted(_pick_variants(d).items()):
            if stem.endswith("_verdict"):
                doc = _read(path)
                if not isinstance(doc, dict):
                    continue
                sector = stem[: -len("_verdict")]
                ctx.verdict_docs.append({"phase": phase, "sector": sector, "path": path, "doc": doc})
                for v in _l(doc.get("verdicts")):
                    if isinstance(v, dict) and v.get("ticker"):
                        ctx.verdicts[(phase, _nt(v["ticker"]))] = dict(v, _sector=sector, _file=path.name)
        for stem, path in sorted(_pick_variants(d / "reports").items()):
            role, ticker = _split_role_ticker(stem)
            if role and ticker:
                doc = _read(path)
                if isinstance(doc, dict):
                    ctx.reports[(phase, role, ticker)] = doc
        data = d / "data"
        if data.is_dir():
            for p in sorted(data.glob("valuation_*.json")):
                doc = _read(p)
                if isinstance(doc, dict):
                    ctx.valuation.setdefault(_nt(doc.get("ticker") or p.stem[len("valuation_"):]), doc)
            for p in sorted(data.glob("fundamentals_*.json")):
                doc = _read(p)
                if isinstance(doc, dict):
                    ctx.fundamentals.setdefault(_nt(doc.get("ticker") or p.stem[len("fundamentals_"):]), doc)

    ctx.snapshot_portfolio = _load_yaml(rd / "inputs" / "portfolio.yaml") or None
    live_path = Path(portfolio_path) if portfolio_path else common.config_dir() / "portfolio.yaml"
    if live_path.is_file():
        ctx.live_portfolio = _load_yaml(live_path) or None
        try:
            ctx.live_portfolio_sha = hashlib.sha256(live_path.read_bytes()).hexdigest()
        except OSError:
            ctx.live_portfolio_sha = None
    return ctx


# =====================================================================================
# Plan helpers
# =====================================================================================


def trade_shares(pos: Mapping | None) -> float:
    return _num(_d(_d(pos).get("trade")).get("shares")) or 0.0


def plan_exit_kind(pos: Mapping | None) -> str:
    """``full`` / ``partial`` / ``none`` - from ``exit_kind`` or ``full_exit`` when present, else computed
    from the trade shares against the shares held."""
    if not isinstance(pos, Mapping):
        return "none"
    ek = pos.get("exit_kind")
    if isinstance(ek, str) and ek.strip():
        e = ek.strip().lower()
        if "full" in e:
            return "full"
        if e in ("none", "no_trade", "hold"):
            return "none"
        if e in ("partial", "trim", "reduce", "reduction"):
            return "partial"
    if pos.get("full_exit") is True:
        return "full"
    sh = trade_shares(pos)
    side = str(_d(pos.get("trade")).get("side") or "").upper()
    cur = _num(pos.get("shares_cur")) or 0.0
    if sh > 0 and side in ("SELL", ""):
        return "full" if cur > 0 and sh >= cur - 1e-9 else "partial"
    return "none"


def describe_plan_trade(pos: Mapping | None) -> str:
    """One self-contained line about what the plan does for a position."""
    if not isinstance(pos, Mapping):
        return "the plan has no position for it"
    sh = trade_shares(pos)
    cur = _num(pos.get("shares_cur"))
    side = str(_d(pos.get("trade")).get("side") or "").upper() or "NONE"
    kind = plan_exit_kind(pos)
    tgt, now_w = _num(pos.get("weight_tgt_pct")), _num(pos.get("weight_cur_pct"))
    if sh <= 0:
        dr = pos.get("dropped_reason")
        prop = _num(_d(pos.get("trade")).get("proposed_shares"))
        extra = f"; the proposed trade of {prop:.2f} share(s) was dropped ({dr})" if prop is not None and dr else (
            f" ({dr})" if dr else "")
        return f"no trade{extra}"
    held = f" of {cur:g} held" if cur is not None else ""
    w = f", weight {_pct(now_w)} -> {_pct(tgt)}" if tgt is not None and now_w is not None else ""
    label = "full exit" if kind == "full" else "partial trim" if side == "SELL" else side.lower()
    return f"{side} {sh:g} share(s){held} ({label}{w})"


# =====================================================================================
# 1. stale_portfolio
# =====================================================================================


def _holding_map(doc: Mapping | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in _l(_d(doc).get("holdings")):
        if isinstance(h, dict) and h.get("ticker"):
            out.setdefault(_nt(h["ticker"]), h)
    return out


def portfolio_diffs(snapshot: Mapping | None, live: Mapping | None, *, share_tol: float = 1e-6,
                    cost_tol: float = 0.005) -> list[str]:
    """Human-readable differences between the run's portfolio snapshot and the live one."""
    snap, cur = _holding_map(snapshot), _holding_map(live)
    diffs: list[str] = []
    for t in snap:
        if t not in cur:
            diffs.append(f"{t}: in the run ({_num(snap[t].get('shares'))} shares) but no longer in portfolio.yaml")
            continue
        a, b = _num(snap[t].get("shares")), _num(cur[t].get("shares"))
        if (a is None) != (b is None) or (a is not None and b is not None
                                           and not math.isclose(a, b, rel_tol=share_tol, abs_tol=1e-9)):
            diffs.append(f"{t}: shares {a:g} in the run, {b:g} now" if a is not None and b is not None
                         else f"{t}: shares {a} in the run, {b} now")
        ca, cb = _num(snap[t].get("cost_basis")), _num(cur[t].get("cost_basis"))
        if ca is not None and cb is not None and abs(ca - cb) > cost_tol:
            diffs.append(f"{t}: average cost {ca:g} in the run, {cb:g} now")
        ua, ub = _num(snap[t].get("units")), _num(cur[t].get("units"))
        if ua is not None and ub is not None and abs(ua - ub) > 1e-9:
            diffs.append(f"{t}: units {ua:g} in the run, {ub:g} now")
    for t in cur:
        if t not in snap:
            diffs.append(f"{t}: new in portfolio.yaml ({_num(cur[t].get('shares'))} shares), not in the run")
    return diffs


def newest_price_date(plan: Mapping | None) -> date | None:
    dates: list[date] = []
    for p in _l(_d(plan).get("positions")):
        d = _as_date(_d(_d(p).get("price")).get("as_of"))
        if d:
            dates.append(d)
    d = _as_date(_d(_d(plan).get("total_value_usd")).get("as_of"))
    if d and not dates:
        dates.append(d)
    return max(dates) if dates else None


def check_stale_portfolio(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    cfg = ctx.cfg
    snap, live = ctx.snapshot_portfolio, ctx.live_portfolio
    manifest_sha = _d(ctx.manifest.get("config_sha256")).get("portfolio.yaml")
    diffs: list[str] = []
    if live is not None:
        if snap is not None:
            diffs = portfolio_diffs(snap, live, share_tol=cfg["share_tolerance"], cost_tol=cfg["cost_tolerance"])
            ca, cb = _num(snap.get("cash_usd")), _num(live.get("cash_usd"))
            if ca is not None and cb is not None and abs(ca - cb) > cfg["stale_cash_change_usd"]:
                diffs.append(f"cash_usd: {_usd(ca)} in the run, {_usd(cb)} now")
        else:  # no snapshot: compare with the shares the plan used
            plan_snap = {"holdings": [{"ticker": t, "shares": p.get("shares_cur")}
                                      for t, p in ctx.plan_positions.items() if p.get("kind") == "holding"]}
            diffs = portfolio_diffs(plan_snap, live, share_tol=cfg["share_tolerance"])
    sha_differs = bool(manifest_sha and ctx.live_portfolio_sha and manifest_sha != ctx.live_portfolio_sha)
    if diffs:
        changed = sorted({d.split(":")[0] for d in diffs if not d.startswith("cash_usd")})
        out.append(_finding(
            "stale_portfolio", "high", None, "run",
            "The portfolio file has changed since this run started (shares, cost or cash differ), so the memo "
            "analyses holdings you no longer have in that form and its trades may not match your account.",
            "; ".join(diffs[:8]) + (f" (+{len(diffs) - 8} more)" if len(diffs) > 8 else "")
            + (f". sha256 of portfolio.yaml now {ctx.live_portfolio_sha[:12]} vs {str(manifest_sha)[:12]} in the "
               f"manifest" if sha_differs else ""),
            _fix("new_run", tickers=[c for c in changed if c],
                 feedback="Start a new committee run on the current portfolio.yaml. The run analysed an older "
                          "portfolio (" + "; ".join(diffs[:5]) + "). Do not patch the numbers of this run by hand.")))
    elif sha_differs:
        if snap is None:
            out.append(_finding(
                "stale_portfolio", "high", None, "run",
                "The portfolio file changed since this run started and the run has no saved copy to compare "
                "with, so it cannot be shown that the memo still matches your holdings.",
                f"sha256 of portfolio.yaml now {ctx.live_portfolio_sha[:12]} vs {str(manifest_sha)[:12]} in the "
                f"manifest; inputs/portfolio.yaml missing",
                _fix("new_run", feedback="Start a new committee run on the current portfolio.yaml.")))
        else:
            out.append(_finding(
                "stale_portfolio", "low", None, "run",
                "The portfolio file was edited after the run (for example its date or notes) but no holding, "
                "cost or cash figure changed, so the memo still matches your holdings.",
                f"sha256 of portfolio.yaml now {ctx.live_portfolio_sha[:12]} vs {str(manifest_sha)[:12]} in the "
                f"manifest; holdings, costs and cash identical",
                _fix("note_only", feedback="No action needed; mention that only the file's metadata changed.")))
    # prices older than N trading days at audit time
    px = newest_price_date(ctx.plan)
    if px is not None and ctx.now is not None:
        age = common.trading_days_between(px, ctx.now, ctx.holidays)
        limit = int(cfg["stale_price_trading_days"])
        if age > limit:
            out.append(_finding(
                "stale_portfolio", "high", None, "run",
                f"The prices behind this memo are {age} trading days old, so trade sizes and valuations no longer "
                "reflect the market.",
                f"newest price in portfolio_plan.json is {px.isoformat()}; audit date {ctx.now.isoformat()}; "
                f"limit {limit} trading days",
                _fix("new_run", feedback="Start a new committee run; prices are older than "
                                         f"{limit} trading days ({px.isoformat()}).")))
    return out


# =====================================================================================
# 2. action_vs_trade, 3. trim_became_full_exit
# =====================================================================================

_DROP_WORDS = {
    "rounds_to_zero_shares": "the trade rounds down to zero whole shares",
    "below_min_trade_usd": "the trade is below the minimum trade size",
    "below_min_trade": "the trade is below the minimum trade size",
}


def check_action_vs_trade(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    for t, fa in sorted(ctx.final_actions.items()):
        action = str(fa.get("action") or "").upper()
        if action not in ("TRIM", "SELL", "ADD", "BUY"):
            continue
        pos = ctx.plan_positions.get(t)
        if pos is not None and trade_shares(pos) > 0:
            continue
        reason = _d(pos).get("dropped_reason")
        prop = _num(_d(_d(pos).get("trade")).get("proposed_shares"))
        why = _DROP_WORDS.get(str(reason), f"it was dropped ({reason})") if reason else "the plan has no trade for it"
        has_eff = pos is not None and bool(pos.get("effective_action"))
        eff = str(pos.get("effective_action")) if has_eff else None
        held = _num(_d(pos).get("shares_cur"))
        detail = ""
        if prop is not None:
            detail = f" The proposed trade was {prop:.2f} share(s)" + (f" of {held:g} held" if held is not None else "") + "."
        out.append(_finding(
            "action_vs_trade", "medium", t, "7",
            f"The committee's final action for {t} is {action}, but the plan contains no executable trade for it "
            f"({why}), so in practice {t} is "
            f"{'held' if action in ('TRIM', 'SELL') else 'not bought'} while the memo's verdict table still "
            f"shows {action}.{detail}",
            f"final action {action}; plan position: {describe_plan_trade(pos)}"
            + (f"; dropped_reason={reason}" if reason else "") + (f"; effective_action={eff}" if eff else ""),
            _fix("rerender" if has_eff else "note_only", tickers=[t],
                 feedback=(f"Re-render the memo so section 5 shows the effective action ({eff}) next to the committee "
                           f"action {action} for {t}." if has_eff else
                           f"In the final report, state that {action} for {t} has no executable trade "
                           f"({why}) and is effectively a HOLD; do not present it as a trade to execute."))))
    return out


def check_trim_became_full_exit(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    for t, pos in sorted(ctx.plan_positions.items()):
        action = str(ctx.final_actions.get(t, {}).get("action") or pos.get("action") or "").upper()
        if action != "TRIM" or plan_exit_kind(pos) != "full":
            continue
        sh, cur = trade_shares(pos), _num(pos.get("shares_cur"))
        rule = pos.get("sizing_rule")
        frac = _num(_d(ctx.plan).get("sizing", {}).get("trim_fraction"))
        keep = f"keep part of the {cur:g} shares" if cur is not None else "keep part of the position"
        out.append(_finding(
            "trim_became_full_exit", "high", t, "7",
            f"The committee voted to TRIM {t} (reduce it), but the plan sells the whole position. A trim and a "
            "complete exit are different decisions with different tax and conviction implications, and nothing "
            "in the verdicts says the stock should be sold entirely.",
            f"plan: {describe_plan_trade(pos)}; sizing_rule={rule}; verdict action TRIM"
            + (f"; configured trim fraction {frac:g}" if frac else ""),
            _fix("replan", tickers=[t],
                 feedback=f"Re-plan {t}: the verdict is TRIM, so size it as a partial trim ({keep}) or have the "
                          "sector head issue an explicit SELL verdict with its reasons if the sector limit really "
                          "requires leaving the stock. Do not let sizing turn a TRIM into a full exit silently. "
                          f"Current plan: {describe_plan_trade(pos)}.")))
    return out


# =====================================================================================
# 4. text_vs_plan_contradiction
# =====================================================================================

_EXIT_RE = re.compile(
    r"\b(?:full(?:y)?[- ]exits?|exit(?:s|ed|ing)?(?![\w])|sell(?:s|ing)?\s+(?:all|everything|out|the\s+(?:whole|entire|full)\b)"
    r"|sold\s+out|liquidat\w+|clos(?:e|es|ed|ing)\s+(?:out\s+)?(?:the\s+|its\s+)?(?:whole\s+|entire\s+)?position"
    r"|eliminat\w+)", re.I)
_WITHDRAWN_RE = re.compile(
    r"\b(?:withdrawn|withdraws?|earlier\s+wording|previous(?:ly)?\s+(?:said|stated|wording|summary))\b", re.I)
_EXIT_FALSE_RE = re.compile(r"exit\s+(?:multiples?|price|rate|value|strategy|criteria|multiple's)", re.I)
_STAY_RE = re.compile(
    r"\b(?:stays?|remains?|kept|keeps?|retained|retains?|still\s+held)\b[^.;]{0,25}?\b(?:in|within)\s+the\s+"
    r"(?:portfolio|book)\b"
    r"|\bonly\s+(?:trimmed|reduced|cut(?:\s+back)?|a\s+(?:partial\s+)?trim)\b"
    r"|\b(?:not|never)\s+(?:be\s+)?(?:exited|sold\s+out|fully\s+sold|liquidated|sold\s+entirely)\b"
    r"|\bpartial(?:ly)?[- ](?:trim|sale|sell|reduction|exit)\w*"
    r"|\bnot\s+a\s+full\s+exit\b|\bwithout\s+(?:a\s+)?(?:full\s+)?exit\b", re.I)
_NEG_BEFORE_RE = re.compile(
    r"(?:\bnot?\b|\bnever\b|\bwithout\b|rather\s+than|instead\s+of|n't|\bavoid\w*|\bno\s+longer\b|"
    r"\bexceed\w*|\bbeyond\b|\bmore\s+than\b|\blarger\s+than\b|\bbigger\s+than\b|\bgoes\s+past\b)"
    r"\s*(?:\w+\s+){0,3}$",
    re.I)
_HYPO_BEFORE_RE = re.compile(r"\b(?:if|would|could|might|may|unless|whether|should)\b", re.I)
_SIZE_RE = re.compile(
    r"\b(?P<verb>sell(?:s|ing)?|sold|buy(?:s|ing)?|bought|trim(?:s|med|ming)?|add(?:s|ed|ing)?|reduc(?:e|es|ed|ing))\s+"
    r"(?:about\s+|roughly\s+|approximately\s+|~)?(?P<n>\d[\d,]*(?:\.\d+)?)\s+(?:more\s+)?(?:whole\s+)?shares?\b", re.I)
_PCT_RE = re.compile(
    r"\b(?P<verb>trim(?:s|med|ming)?|cut(?:s|ting)?|reduc(?:e|es|ed|ing)|sell(?:s|ing)?)\b[^.;%]{0,22}?"
    r"\b(?:by|to|of|about|roughly)\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<u>%|pp|percent|percentage\s+points)", re.I)
_CLAUSE_SEP = re.compile(r";|\b(?:but|while|whereas|however)\b")
_BREAK_VERB_RE = re.compile(r"\b(?:trim|trims|trimmed|hold|holds|keep|keeps|buy|buys|add|adds|reduce|reduces)\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")

_MENTION_CACHE: dict[str, re.Pattern] = {}


def _mention_re(ticker: str) -> re.Pattern:
    pat = _MENTION_CACHE.get(ticker)
    if pat is None:
        pat = re.compile(r"(?<![\w.\-])" + re.escape(ticker) + r"(?![\w\-]|\.\w)")
        _MENTION_CACHE[ticker] = pat
    return pat


_ABBREV_END_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|vs|Inc|Corp|Co|Ltd|St|No|approx)\.$")


def split_sentences(text: Any) -> list[str]:
    out: list[str] = []
    for para in re.split(r"\n+", str(text or "")):
        pieces: list[str] = []
        for s in _SENT_SPLIT.split(para):
            if pieces and _ABBREV_END_RE.search(pieces[-1]):
                pieces[-1] = pieces[-1] + " " + s
            else:
                pieces.append(s)
        for s in pieces:
            s = s.strip()
            if s:
                out.append(s)
    return out


def _clause_span(sent: str, pos: int) -> tuple[int, int]:
    start, end = 0, len(sent)
    for m in _CLAUSE_SEP.finditer(sent):
        if m.end() <= pos:
            start = m.end()
        elif m.start() >= pos:
            end = m.start()
            break
    return start, end


_AFTER_OK_RE = re.compile(r"^(?:[\s,&]|\b(?:and|or|of|in|for|from|on|the)\b)*$", re.I)


def attach_tickers(sent: str, span: tuple[int, int], tickers: Iterable[str], default_ticker: str | None,
                   window: int) -> list[str]:
    """Tickers a claim found at ``span`` of ``sent`` is about.

    A ticker counts when it is in the same clause, within ``window`` characters, with no trim/hold/keep verb
    in between. One named *after* the claim (``exit PLTR``, ``sell 2 shares of BRK-B``) must follow it
    directly (only of / in / the / and or other tickers in between). With no ticker named anywhere in the
    sentence the sentence's default ticker is used."""
    tickers = list(tickers)
    s, e = _clause_span(sent, span[0])
    hits: list[str] = []
    for t in tickers:
        for m in _mention_re(t).finditer(sent):
            if m.start() < s or m.end() > e:
                continue
            after = False
            if m.end() <= span[0]:
                between = sent[m.end():span[0]]
            elif m.start() >= span[1]:
                between = sent[span[1]:m.start()]
                after = True
            else:
                continue
            if len(between) > window or _BREAK_VERB_RE.search(between):
                continue
            if after:
                cleaned = between
                for other in tickers:
                    cleaned = _mention_re(other).sub("", cleaned)
                if not _AFTER_OK_RE.match(cleaned):
                    continue
            hits.append(t)
            break
    if hits:
        return hits
    if default_ticker and not any(_mention_re(t).search(sent) for t in tickers):
        return [default_ticker]
    return []


def _is_negated(sent: str, start: int) -> bool:
    before = sent[max(0, start - 36):start]
    return bool(_NEG_BEFORE_RE.search(before))


def _is_hypothetical(sent: str, start: int) -> bool:
    s, _ = _clause_span(sent, start)
    return bool(_HYPO_BEFORE_RE.search(sent[s:start]))


def find_text_claims(text: Any, tickers: Iterable[str], default_ticker: str | None = None, *,
                     window: int = 50) -> list[dict]:
    """Exit / stay / share-count / percent claims in ``text`` with the ticker each refers to.

    ``kind`` is ``exit`` (text says the stock is sold out), ``stay`` (text says it stays / is only trimmed),
    ``shares`` (``value`` = shares quoted) or ``pct`` (``value`` = percent quoted)."""
    tickers = [t for t in dict.fromkeys(tickers) if len(t) >= 2]
    claims: list[dict] = []
    for sent in split_sentences(text):
        for m in _EXIT_RE.finditer(sent):
            if _WITHDRAWN_RE.search(sent) or _EXIT_FALSE_RE.match(sent, m.start()) or _is_negated(sent, m.start()) or _is_hypothetical(sent, m.start()):
                continue
            for t in attach_tickers(sent, m.span(), tickers, default_ticker, window):
                claims.append({"ticker": t, "kind": "exit", "quote": sent, "value": None})
        for m in _STAY_RE.finditer(sent):
            if _is_negated(sent, m.start()) or _is_hypothetical(sent, m.start()):
                continue
            for t in attach_tickers(sent, m.span(), tickers, default_ticker, window):
                claims.append({"ticker": t, "kind": "stay", "quote": sent, "value": None})
        for m in _SIZE_RE.finditer(sent):
            n = common.safe_float(m.group("n").replace(",", ""))
            if n is None:
                continue
            for t in attach_tickers(sent, m.span(), tickers, default_ticker, window):
                claims.append({"ticker": t, "kind": "shares", "quote": sent, "value": n,
                               "verb": m.group("verb").lower()})
        for m in _PCT_RE.finditer(sent):
            n = common.safe_float(m.group("n"))
            if n is None:
                continue
            for t in attach_tickers(sent, m.span(), tickers, default_ticker, window):
                claims.append({"ticker": t, "kind": "pct", "quote": sent, "value": n})
    seen: set[tuple] = set()
    uniq = []
    for c in claims:
        key = (c["ticker"], c["kind"], c["quote"], c["value"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def text_units(ctx: Ctx) -> list[dict]:
    """Texts that describe actions: ``{source, label, role, text, default_ticker}``."""
    units: list[dict] = []
    plan_tickers = list(ctx.plan_positions)

    def add(source: str, label: str, role: str, text: Any, default: str | None = None) -> None:
        if text and str(text).strip():
            units.append({"source": source, "label": label, "role": role, "text": str(text), "default_ticker": default})

    cio = _d(ctx.cio)
    for i, s in enumerate(_l(cio.get("executive_summary"))):
        add("cio_final.json", f"executive_summary[{i}]", "cio", s)
    for i, item in enumerate(_l(cio.get("reconciliation"))):
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "")
        named = [t for t in plan_tickers if len(t) >= 2 and _mention_re(t).search(topic)]
        default = named[0] if len(named) == 1 else None
        for key in ("phase1_position", "findings", "resolution"):
            add("cio_final.json", f"reconciliation[{i}] ({topic[:40]}) {key}", "cio", item.get(key), default)
    for i, s in enumerate(_l(cio.get("top_risks"))):
        add("cio_final.json", f"top_risks[{i}]", "cio", s)
    add("cio_final.json", "dissent_summary", "cio", cio.get("dissent_summary"))
    add("cio_final.json", "conviction_reason", "cio", cio.get("conviction_reason"))
    for i, fa in enumerate(_l(cio.get("final_actions"))):
        if isinstance(fa, dict) and fa.get("note"):
            add("cio_final.json", f"final_actions[{i}].note", "cio", fa["note"], _nt(fa.get("ticker")))
        if isinstance(fa, dict) and fa.get("override_justification"):
            add("cio_final.json", f"final_actions[{i}].override_justification", "cio", fa["override_justification"],
                _nt(fa.get("ticker")))
    for i, co in enumerate(_l(cio.get("constraint_overrides"))):
        if isinstance(co, dict):
            add("cio_final.json", f"constraint_overrides[{i}]", "cio", co.get("justification"))
    for vd in ctx.verdict_docs:
        fname = Path(vd["path"]).name
        add(fname, "summary", "sector_head", _d(vd["doc"]).get("summary"))
        for v in _l(_d(vd["doc"]).get("verdicts")):
            if not isinstance(v, dict):
                continue
            t = _nt(v.get("ticker"))
            for key in ("rationale", "deviation_justification", "da_rebuttal", "thesis"):
                add(fname, f"{t} {key}", "sector_head", v.get(key), t)
    rows = memo_table_rows(ctx.memo_section(5))
    for t, row in rows.items():
        why = row.get("Why")
        if why:
            add("memo.md", f"section 5 Why for {t}", "memo", why, t)
    return units


def check_text_vs_plan(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    positions = {t: p for t, p in ctx.plan_positions.items() if (_num(p.get("shares_cur")) or 0) > 0}
    if not positions:
        return []
    tickers = list(positions)
    per_ticker: dict[str, list[dict]] = {}
    for u in text_units(ctx):
        default = u["default_ticker"] if u["default_ticker"] in positions else None
        for c in find_text_claims(u["text"], tickers, default, window=int(cfg["text_ticker_window_chars"])):
            pos = positions[c["ticker"]]
            kind = plan_exit_kind(pos)
            problem = None
            sev = "high"
            if c["kind"] == "exit" and kind != "full":
                problem = f"says it is sold out / exited, but the plan is: {describe_plan_trade(pos)}"
            elif c["kind"] == "stay" and kind == "full":
                problem = f"says it stays in the portfolio / is only trimmed, but the plan is: {describe_plan_trade(pos)}"
            elif c["kind"] == "shares":
                planned = trade_shares(pos)
                prop = _num(_d(pos.get("trade")).get("proposed_shares"))
                tol = cfg["text_share_tolerance"]
                ok = abs(c["value"] - planned) <= tol or (prop is not None and abs(c["value"] - prop) <= 0.01)
                held = _num(pos.get("shares_cur"))
                if not ok and not (held is not None and abs(c["value"] - held) <= tol and c["verb"].startswith("hold")):
                    problem = f"quotes {c['value']:g} shares, but the plan is: {describe_plan_trade(pos)}"
            elif c["kind"] == "pct" and trade_shares(pos) > 0:
                cur_w, tgt_w = _num(pos.get("weight_cur_pct")), _num(pos.get("weight_tgt_pct"))
                held = _num(pos.get("shares_cur")) or 0.0
                cands = [x for x in (
                    (cur_w - tgt_w) if cur_w is not None and tgt_w is not None else None,
                    trade_shares(pos) / held * 100.0 if held else None,
                    _num(_d(ctx.plan).get("sizing", {}).get("trim_fraction")) and
                    100.0 * _num(_d(ctx.plan).get("sizing", {}).get("trim_fraction")),
                    cur_w, tgt_w, _num(pos.get("weight_intent_pct"))) if x is not None]
                if cands and all(abs(c["value"] - x) > cfg["text_pct_tolerance_pp"] for x in cands):
                    problem = (f"quotes {c['value']:g}%, but the plan is: {describe_plan_trade(pos)}")
                    sev = "medium"
            if problem:
                per_ticker.setdefault(c["ticker"], []).append(
                    {"unit": u, "claim": c, "problem": problem, "severity": sev})
    out: list[dict] = []
    for t, items in sorted(per_ticker.items()):
        pos = positions[t]
        sev = "high" if any(i["severity"] == "high" for i in items) else "medium"
        roles = []
        for i in items:
            r = i["unit"]["role"]
            if r in ("cio", "sector_head") and r not in roles:
                roles.append(r)
        quotes = []
        seen_q: set[str] = set()
        for i in items:
            q = f"{i['unit']['source']} {i['unit']['label']}: \"{_clip(i['claim']['quote'], 200)}\" ({i['problem']})"
            if q not in seen_q:
                seen_q.add(q)
                quotes.append(q)
        sources_agent = [i for i in items if i["unit"]["role"] in ("cio", "sector_head")]
        wrote: list[str] = []
        for i in (sources_agent or items):
            qt = _clip(i["claim"]["quote"], 260)
            if qt not in wrote:
                wrote.append(qt)
        quote0 = '" and "'.join(wrote[:2])
        sector = ctx.sector_of(t)
        feedback = (
            f"Your text about {t}" + (f" (sector {sector})" if sector else "") + f" contradicts the final portfolio "
            f"plan. You wrote: \"{quote0}\". The plan (portfolio_plan.json) does this for {t}: "
            f"{describe_plan_trade(pos)}. Rewrite every sentence about {t} so that it states exactly what the plan "
            f"does (action, number of shares, full or partial) and uses the same sector limit as the plan. If you "
            f"believe the plan is wrong, say so explicitly as an open issue for the chair instead of describing a "
            f"different trade.")
        if sources_agent:
            fix = _fix("rerun_agents", roles, [t], feedback)
        else:
            fix = _fix("rerender", [], [t],
                       f"The memo's generated text for {t} contradicts the plan ({describe_plan_trade(pos)}). "
                       "Fix the template that writes the Why column so it reads from the plan, then re-render.")
        section = "2" if any(i["unit"]["source"] == "cio_final.json" for i in items) else "5"
        out.append(_finding(
            "text_vs_plan_contradiction", sev, t, section,
            f"The written explanation for {t} does not match what the plan actually does: "
            + _problem_summary(items, pos) + " A reader following the text would expect something different "
            "from the trade table.",
            " | ".join(quotes[:4]) + (f" | (+{len(quotes) - 4} more)" if len(quotes) > 4 else ""), fix))
    return out


def _problem_summary(items: list[dict], pos: Mapping) -> str:
    kinds = {i["claim"]["kind"] for i in items}
    plan = describe_plan_trade(pos)
    if "stay" in kinds and plan_exit_kind(pos) == "full":
        return f"the text says the stock stays in the portfolio / is only trimmed, but the plan sells all of it ({plan})."
    if "exit" in kinds:
        return f"the text speaks of an exit, but the plan is {plan}."
    return f"the text quotes a different trade size than the plan ({plan})."


# =====================================================================================
# 5. valuation_outlier, 6. systematic_valuation_bias, 7. captive/leverage
# =====================================================================================


def valuation_items(ctx: Ctx) -> list[dict]:
    """One entry per stock with a price and a base fair value (aggregates first, reports as fallback)."""
    items: dict[str, dict] = {}
    ordered = sorted(ctx.aggregates.items(), key=lambda kv: (bool(kv[1].get("_light")), kv[0]))
    for (phase, t), a in ordered:
        if t in items:
            continue
        asset = a.get("asset_type")
        if asset == "fund" or a.get("sector_class") == "fund":
            continue
        rr = _d(a.get("rr"))
        price = _num(rr.get("price")) or _num(_d(a.get("inputs")).get("price"))
        fv = _num(rr.get("fv_base"))
        if fv is None:
            rep = ctx.reports.get((phase, "fundamental", t))
            fv = _num(common.get_dotted(rep, "extensions.valuation.fair_value_base")) if rep else None
        if _declared_not_meaningful(ctx, phase, t):
            continue
        if price is None or price <= 0 or fv is None or fv <= 0:
            continue
        items[t] = {"ticker": t, "phase": phase, "kind": a.get("kind"), "price": price, "fv": fv, "ratio": fv / price,
                    "rr": _num(rr.get("rr")), "action": ctx.action_of(t)}
    return list(items.values())


def _declared_not_meaningful(ctx: Ctx, phase: int, t: str) -> bool:
    rep = ctx.reports.get((phase, "fundamental", t))
    val = _d(common.get_dotted(rep, "extensions.valuation")) if rep else {}
    if val.get("not_meaningful") is True or str(val.get("status") or "").lower() == "not_meaningful":
        return True
    doc = ctx.valuation.get(t)
    if doc is not None and doc.get("methods_available") == [] and doc.get("cells"):
        return True
    a = ctx.aggregates.get((phase, t))
    return bool(a and str(_d(a.get("rr")).get("null_reason") or "") == "not_meaningful")


def _reverse_dcf_growth(ctx: Ctx, t: str) -> float | None:
    doc = ctx.valuation.get(t)
    return _num(common.get_dotted(doc, "reverse_dcf.implied_growth_pct")) if doc else None


def check_valuation_outliers(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    out: list[dict] = []
    for it in sorted(valuation_items(ctx), key=lambda x: x["ticker"]):
        r = it["ratio"]
        sev = None
        if r < cfg["outlier_high_low"] or r > cfg["outlier_high_high"]:
            sev = "high"
        elif r < cfg["outlier_medium_low"] or r > cfg["outlier_medium_high"]:
            sev = "medium"
        if sev is None:
            continue
        t = it["ticker"]
        g = _reverse_dcf_growth(ctx, t)
        act = it["action"]
        ev = (f"price {_usd(it['price'])}; fair value base {_usd(it['fv'])}; ratio {r:.2f}"
              + (f"; reverse-DCF implied growth {g:.1f}%/yr" if g is not None else "")
              + (f"; committee action {act}" if act else ""))
        direction = "far below" if r < 1 else "far above"
        out.append(_finding(
            "valuation_outlier", sev, t, "6" if it["kind"] == "candidate" else "5",
            f"The valuation model says {t} is worth {r * 100:.0f}% of its price ({_usd(it['fv'])} vs {_usd(it['price'])}) "
            f"- verify against market multiples (P/E, EV/EBIT, analyst targets) before acting. A result {direction} "
            "the market price is more often a model limitation than a mispricing.",
            ev,
            _fix("rerun_agents", ["fundamental"], [t],
                 f"Your base fair value for {t} is {_usd(it['fv'])} against a price of {_usd(it['price'])} "
                 f"({r * 100:.0f}% of the price). Reconcile the valuation grid with the stock's current market "
                 "multiples and consensus: state today's P/E, EV/EBIT and FCF yield and the analyst price-target "
                 "range with sources. Check the net-debt treatment (is a captive finance arm's debt netted against "
                 "industrial earnings, is cash netted), whether the TTM earnings are cyclical peak or trough, and "
                 "whether the growth and exit-multiple tiers of the grid fit this business. State explicitly, in "
                 "the thesis, whether the gap between the grid and the price is a limitation of the model or a "
                 "real mispricing by the market, and set fair_value_low/base/high and your conviction accordingly "
                 "with sourced reasoning.")))
    return out


def check_systematic_bias(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    items = valuation_items(ctx)
    n = len(items)
    if n < int(cfg["bias_min_n"]):
        return []
    below = [i for i in items if i["fv"] < i["price"]]
    share = len(below) / n
    if share < cfg["bias_share"]:
        return []
    ratios = sorted(i["ratio"] for i in items)
    median = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
    return [_finding(
        "systematic_valuation_bias", "high", None, "run",
        f"{len(below)} of the {n} stocks valued in this run ({share * 100:.0f}%) have a fair value below their market "
        "price. When almost everything looks overpriced the valuation method is probably too harsh for this market "
        "(low multiple bands, a high discount rate, little credit for growth) rather than the whole market being "
        "overpriced. Treat TRIM/SELL/PASS calls that rest only on fair value with caution.",
        f"{len(below)}/{n} below price; median fair value / price = {median:.2f}; lowest "
        + ", ".join(f"{i['ticker']} {i['ratio']:.2f}" for i in sorted(items, key=lambda x: x["ratio"])[:4])
        + f"; threshold {cfg['bias_share'] * 100:.0f}% with n >= {int(cfg['bias_min_n'])}",
        _fix("note_only", feedback="In the final report say that the valuation grid is systematically harsh in this "
                                   "run and that fair value is a cross-check, not a verdict; name the actions that "
                                   "rest on fair value alone."))]


_CAPTIVE_TEXT_RE = re.compile(r"captive[- ]financ|finance subsidiar|financial products segment", re.I)


def check_captive_or_leverage(ctx: Ctx) -> list[dict]:
    found: dict[str, dict] = {}
    for src, docs in (("valuation", ctx.valuation), ("fundamentals", ctx.fundamentals)):
        for t, doc in docs.items():
            for fl in _l(doc.get("flags")):
                code = str(_d(fl).get("code") or "")
                if code.startswith("captive_finance") or code == "possible_captive_finance":
                    e = found.setdefault(t, {"codes": [], "details": [], "text": []})
                    if code not in e["codes"]:
                        e["codes"].append(code)
                        e["details"].append(_clip(_d(fl).get("detail"), 200))
    for (phase, role, t), rep in sorted(ctx.reports.items()):
        if t in found and found[t]["codes"]:
            continue
        for text in _strings(rep, skip=("sources", "key_figures", "input_refs", "data_ref")):
            if _CAPTIVE_TEXT_RE.search(text) and not re.search(r"\b(?:no|not|without)\b[^.]{0,20}captive", text, re.I):
                e = found.setdefault(t, {"codes": [], "details": [], "text": []})
                hit = f"{role} report: {_clip(text, 200)}"
                if hit not in e["text"] and len(e["text"]) < 2:
                    e["text"].append(hit)
    out: list[dict] = []
    for t, e in sorted(found.items()):
        flagged = bool(e["codes"])
        handled = "captive_finance_debt_excluded" in e["codes"]
        how = ", ".join(e["codes"]) or "mentioned in your own report"
        out.append(_finding(
            "captive_or_leverage_flag", "medium", t, "5",
            f"The numbers for {t} may be distorted by a captive finance arm: debt that funds customer loans can be "
            "counted as the company's own debt, which makes the stock look more expensive than it is"
            + (" (the valuation already left that debt out, so fair value may now be slightly high)." if handled else "."),
            ("flags: " + ", ".join(e["codes"]) + "; " + "; ".join(e["details"])) if flagged
            else " | ".join(e["text"]),
            _fix("rerun_agents", ["fundamental"], [t],
                 f"{t} carries a captive-finance / leverage warning ({how}). Separate the industrial business from the finance arm using the latest 10-K/10-Q "
                 "segment data: state industrial net debt, finance-arm debt and receivables, and which net-debt "
                 "figure the valuation grid used. Recompute or adjust the fair value range if the grid is distorted, "
                 "and say in the thesis how much of the gap to the price this explains.")))
    return out


def _strings(node: Any, skip: Iterable[str] = ()) -> Iterable[str]:
    """All string values in a JSON document (keys in ``skip`` are not entered)."""
    skip_set = set(skip)
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in skip_set:
                continue
            yield from _strings(v, skip)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, skip)


# =====================================================================================
# 8. plan_does_not_cure_breach, 9. limit_inconsistency
# =====================================================================================


def residual_breaches(ctx: Ctx) -> list[dict]:
    """[{sector, post, limit}] still above the cap after the plan."""
    plan = _d(ctx.plan)
    limit_default = _num(ctx.profile.get("max_sector_pct"))
    out: list[dict] = []
    given = plan.get("residual_breaches")
    if isinstance(given, list) and given:
        for r in given:
            if isinstance(r, dict):
                sector = r.get("sector")
                post = _num(r.get("post_lookthrough_pct", r.get("post_pct", r.get("post"))))
                lim = _num(r.get("max_pct", r.get("limit_pct", r.get("limit")))) or limit_default
                if sector and post is not None:
                    out.append({"sector": sector, "post": post, "limit": lim})
            elif isinstance(r, str):
                out.append({"sector": r, "post": None, "limit": limit_default})
        if out:
            return out
    for s in _l(plan.get("sectors")):
        if not isinstance(s, dict) or s.get("sector") in ("cash", "unclassified", None):
            continue
        post = _num(s.get("post_lookthrough_pct"))
        lim = _num(s.get("max_pct")) or limit_default
        if post is not None and lim is not None and post > lim + 1e-9:
            out.append({"sector": s["sector"], "post": post, "limit": lim})
    return out


def _sector_contributions(ctx: Ctx, sector: str) -> tuple[list[tuple[str, float]], float]:
    """(fund contributions sorted desc, post-trade stock contribution) to a sector, in portfolio pp."""
    funds: list[tuple[str, float]] = []
    stock_post = 0.0
    plan_pos = ctx.plan_positions
    for h in _l(_d(ctx.lookthrough).get("holdings")):
        if not isinstance(h, dict):
            continue
        c = _num(_d(h.get("contribution_pct")).get(sector))
        if not c:
            continue
        t = _nt(h.get("ticker"))
        if h.get("asset_type") == "fund":
            funds.append((t, c))
        else:
            pos = plan_pos.get(t)
            held = _num(_d(pos).get("shares_cur")) or 0.0
            kept = 1.0 - (trade_shares(pos) / held if held and _d(_d(pos).get("trade")).get("side") == "SELL" else 0.0)
            stock_post += c * max(kept, 0.0)
    funds.sort(key=lambda kv: kv[1], reverse=True)
    return funds, stock_post


def check_plan_does_not_cure_breach(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    for r in residual_breaches(ctx):
        sector = r["sector"]
        name = common.sector_display(sector)
        post, lim = r["post"], r["limit"]
        funds, stock_post = _sector_contributions(ctx, sector)
        top = ", ".join(f"{t} {c:.1f} pp" for t, c in funds[:3])
        excess = (post - lim) if post is not None and lim is not None else None
        lever = (f"Stock sales cannot fix it: the stocks that remain contribute only {stock_post:.1f} pp"
                 + (f" while the excess is {excess:.1f} pp" if excess is not None else "")
                 + "; the exposure sits in overlapping funds" + (f" (largest: {top})" if top else "") + "."
                 if funds else "The remaining exposure comes mainly from funds, which the plan does not sell.")
        out.append(_finding(
            "plan_does_not_cure_breach", "medium", None, "4",
            f"After the planned trades {name} is still {_pct(post)} of the portfolio (look-through), above the "
            f"{_pct(lim, 0)} cap in your profile. " + lever,
            f"plan sectors: {sector} post {_pct(post, 2)} vs cap {_pct(lim, 2)}; stock contribution after trades "
            f"{stock_post:.2f} pp; top fund contributions {top or 'n/a'}",
            _fix("note_only", feedback=(
                f"State plainly in the final report that the plan leaves {name} at {_pct(post)} (cap "
                f"{_pct(lim, 0)}) and that trimming stocks cannot cure it. Name the real lever: the overlap between "
                f"the funds{' (' + top + ')' if top else ''}, and directing new contributions to the under-weight "
                "sectors until the exposure falls under the cap."))))
    return out


_SECTOR_ALIASES: dict[str, tuple[str, ...]] = {
    "information_technology": ("Information Technology", "information_technology", "technology"),
    "health_care": ("Health Care", "health_care", "healthcare"),
    "consumer_discretionary": ("Consumer Discretionary", "consumer_discretionary"),
    "consumer_staples": ("Consumer Staples", "consumer_staples"),
    "communication_services": ("Communication Services", "communication_services"),
    "real_estate": ("Real Estate", "real_estate"),
    "financials": ("Financials", "financials"),
    "energy": ("Energy", "energy"),
    "industrials": ("Industrials", "industrials"),
    "utilities": ("Utilities", "utilities"),
    "materials": ("Materials", "materials"),
}
_LIMIT_NUM_RES = (
    re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%\s*(?:sector\s+|look-?through\s+|allowed\s+)*limit\b", re.I),
    re.compile(r"\blimit\b(?:\s+(?:of|is|at|to|set\s+at))?\s*[:=(]?\s*(\d+(?:\.\d+)?)\s*(?:%|pp)", re.I),
)


def sector_mentions(sentence: str) -> list[str]:
    found = []
    for key, aliases in _SECTOR_ALIASES.items():
        for a in aliases:
            if re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", sentence, re.I if a.islower() or "_" in a else 0):
                found.append(key)
                break
    return found


def quoted_limits(text: Any, default_sector: str | None = None) -> list[tuple[str, float, str]]:
    """[(sector, limit number, sentence)] for every 'limit' figure quoted in ``text``."""
    out: list[tuple[str, float, str]] = []
    for sent in split_sentences(text):
        nums: list[float] = []
        for rx in _LIMIT_NUM_RES:
            for m in rx.finditer(sent):
                if re.search(r"\bposition\s*$", sent[max(0, m.start() - 12):m.start()], re.I) or \
                        re.search(r"\b(?:position|single|holding)\b[^.]{0,12}$", sent[:m.start()][-20:], re.I):
                    continue
                v = common.safe_float(m.group(1))
                if v is not None and v not in nums:
                    nums.append(v)
        if not nums:
            continue
        sectors = sector_mentions(sent) or ([default_sector] if default_sector else [])
        for s in sectors:
            for v in nums:
                out.append((s, v, sent))
    return out


def plan_sector_limits(plan: Mapping | None) -> dict[str, float]:
    """Sector limits the plan declares itself (``sector_limit(s)`` / per-sector ``limit_pct``), if any."""
    out: dict[str, float] = {}
    p = _d(plan)
    for key in ("sector_limits", "sector_limit"):
        v = p.get(key)
        if isinstance(v, dict):
            for s, x in v.items():
                n = _num(x)
                if n is not None:
                    out[s] = n
    for s in _l(p.get("sectors")):
        if isinstance(s, dict) and s.get("sector"):
            n = _num(s.get("sector_limit_pct", s.get("limit_pct")))
            if n is not None:
                out.setdefault(s["sector"], n)
    return out


def check_limit_inconsistency(ctx: Ctx) -> list[dict]:
    per_sector: dict[str, dict[float, list[str]]] = {}

    def note(sector: str, value: float, where: str) -> None:
        bucket = per_sector.setdefault(sector, {})
        for k in bucket:
            if abs(k - value) < 0.05:
                value = k
                break
        if where not in bucket.setdefault(value, []):
            bucket[value].append(where)

    for s, v in plan_sector_limits(ctx.plan).items():
        note(s, v, "plan sector_limit")
    for t, row in memo_table_rows(ctx.memo_section(5)).items():
        for s, v, _ in quoted_limits(row.get("Why"), ctx.sector_of(t)):
            note(s, v, f"memo section 5 Why ({t})")
    for vd in ctx.verdict_docs:
        fname = Path(vd["path"]).name
        for s, v, _ in quoted_limits(_d(vd["doc"]).get("summary"), vd["sector"]):
            note(s, v, f"{fname} summary")
        for v_ in _l(_d(vd["doc"]).get("verdicts")):
            if not isinstance(v_, dict):
                continue
            t = _nt(v_.get("ticker"))
            for key in ("rationale", "deviation_justification"):
                for s, v, _ in quoted_limits(v_.get(key), vd["sector"]):
                    note(s, v, f"{fname} {t} {key}")
    plan = _d(ctx.plan)
    for i, n in enumerate(_l(plan.get("notes"))):
        for s, v, _ in quoted_limits(n, None):
            note(s, v, f"plan note {i + 1}")
    for t, pos in ctx.plan_positions.items():
        for n in _l(pos.get("notes")):
            for s, v, _ in quoted_limits(n, pos.get("sector")):
                note(s, v, f"plan position {t} note")
    out: list[dict] = []
    for sector, bucket in sorted(per_sector.items()):
        if len(bucket) < 2:
            continue
        name = common.sector_display(sector)
        parts = [f"{v:g}% ({_join(sorted(set(w))[:4])})" for v, w in sorted(bucket.items())]
        out.append(_finding(
            "limit_inconsistency", "medium", None, "5",
            f"The memo quotes {len(bucket)} different limits for {name}: "
            + _join([f"{v:g}%" for v in sorted(bucket)]) + ". The reader cannot tell which limit actually triggered "
            "the trims; each place should name its limit (profile cap, macro band, CIO target) or use one number.",
            "; ".join(parts),
            _fix("rerender", [], [], f"Make the {name} limit consistent across the memo: use the number the plan "
                                     "used and name the other figures for what they are (profile cap, macro band, "
                                     "CIO target), then re-render.")))
    return out


# =====================================================================================
# 10. weak_why
# =====================================================================================

_RULE_CODE_RE = re.compile(r"\bR\d+(?:\.[A-Za-z_]+)+\b|\b(?:C|P)\d+\.[A-Za-z_.]+\b")
_FIRED_RE = re.compile(r"\bfired\b", re.I)
_FLAG_TAIL_RE = re.compile(r"\s*\[(?:CIO override|sector-head deviation)(?:; [^\]]+)?\]\s*$")


def weak_why_reason(text: Any, min_words: int = 18) -> str | None:
    """Why a holding's Why cell is weak (too short / shows rule codes), else None."""
    t = _FLAG_TAIL_RE.sub("", str(text or "")).strip()
    reasons = []
    words = [w for w in re.split(r"\s+", t) if re.search(r"[A-Za-z0-9]", w)]
    if len(words) < min_words:
        reasons.append(f"only {len(words)} words (minimum {min_words})")
    m = _RULE_CODE_RE.search(t)
    if m:
        reasons.append(f"shows the rule code '{m.group(0)}'")
    if _FIRED_RE.search(t):
        reasons.append("uses the internal phrase 'fired'")
    return "; ".join(reasons) or None


def check_weak_why(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    rows = memo_table_rows(ctx.memo_section(5))
    out: list[dict] = []
    for t in sorted(set(ctx.holdings) | {tk for (_, tk), a in ctx.aggregates.items() if a.get("kind") == "holding"}):
        why = rows.get(t, {}).get("Why")
        source = "memo section 5"
        if why is None:
            v = next((x for (p, tk), x in sorted(ctx.verdicts.items()) if tk == t), None)
            if not v or not v.get("rationale"):
                continue
            why, source = str(v["rationale"]), "sector verdict rationale"
        reason = weak_why_reason(why, int(cfg["weak_why_min_words"]))
        if not reason:
            continue
        out.append(_finding(
            "weak_why", "low", t, "5",
            f"The explanation shown for {t} in the verdict table is too thin to convince a reader or still shows "
            "internal wording, so the investor cannot see why the committee decided what it did.",
            f"{source}: \"{_clip(why, 160)}\" - {reason}",
            _fix("rerun_agents", ["sector_head"], [t],
                 f"Rewrite the rationale for {t}: at least {int(cfg['weak_why_min_words'])} words of plain English "
                 "that say what the committee decided, the two or three facts behind it (conviction, fair value vs "
                 "price, the limit or event that matters) and what would change the decision. Do not mention rule "
                 "ids (R4.trim...) or words such as 'fired'. The wording must match the portfolio plan "
                 f"({describe_plan_trade(ctx.plan_positions.get(t))}).")))
    return out


# =====================================================================================
# 11. unresolved_high_flags / failed_reports / low_evidence, 12. thin_review
# =====================================================================================


def _holding_aggregates(ctx: Ctx) -> list[tuple[str, dict]]:
    seen: dict[str, dict] = {}
    for (phase, t), a in sorted(ctx.aggregates.items(), key=lambda kv: (bool(kv[1].get("_light")), kv[0])):
        if a.get("kind") == "holding" and t not in seen:
            seen[t] = a
    return sorted(seen.items())


def check_flags_failed_evidence(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    min_ev = float(ctx.cfg["min_evidence"])
    for t, a in _holding_aggregates(ctx):
        unresolved = [f for f in _l(_d(a.get("flags")).get("unresolved")) if _d(f).get("severity") == "high"]
        if unresolved:
            desc = "; ".join(f"{f.get('flag_id') or f.get('id')} ({f.get('role')}): {_clip(f.get('flag'), 160)}"
                             for f in unresolved[:3])
            out.append(_finding(
                "unresolved_high_flags", "medium", t, "10",
                f"A serious warning raised about {t} was never answered by the other reviewers, so the committee "
                "decided without resolving it.", desc,
                _fix("rerun_agents", ["devils_advocate", "sector_head"], [t],
                     f"Answer the open high-severity flag(s) on {t}: {desc}. For each, say whether it is "
                     "confirmed, mitigated or refuted, with the evidence, and reflect the answer in the verdict "
                     "(unresolved_red_flags) and in the Devil's Advocate flag_responses.")))
        specs = _l(a.get("specialists")) + ([dict(_d(a.get("da")), role="devils_advocate")] if _d(a.get("da")) else [])
        failed = [s["role"] for s in specs if isinstance(s, dict) and s.get("status") == "failed"]
        if failed:
            out.append(_finding(
                "failed_reports", "medium", t, "10",
                f"The {_join(failed)} report for {t} failed twice and was replaced by an empty stub, so that "
                "viewpoint is missing from the decision.",
                f"failed roles: {', '.join(failed)} (status failed in aggregate.json)",
                _fix("rerun_agents", failed, [t], f"Your previous attempt for {t} failed validation twice. Produce "
                     "the complete report again, following the work order and the schema exactly.")))
        low = []
        for s in specs:
            if not isinstance(s, dict) or s.get("status") == "failed":
                continue
            ev = _num(s.get("evidence"))
            if ev is not None and ev < min_ev:
                low.append((s["role"], ev))
        if low:
            low_roles = [r for r, _ in low]
            out.append(_finding(
                "low_evidence", "medium", t, "10",
                f"The {_join(low_roles)} report(s) for {t} rest on thin evidence (score below {min_ev:g} of 10), "
                "so their conclusions are weakly supported.",
                ", ".join(f"{r} {e:g}/10" for r, e in low) + f"; minimum {min_ev:g}",
                _fix("rerun_agents", low_roles, [t],
                     f"Your report on {t} scored {', '.join(f'{r} {e:g}/10' for r, e in low)} for evidence. Re-do it "
                     "with more primary sources (filings, transcripts, exchange announcements), fill the required "
                     "fields instead of leaving them null, list what is genuinely unavailable, and cite a source for "
                     "every key figure.")))
    return out


def check_thin_review(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    for t, a in _holding_aggregates(ctx):
        reasons: list[str] = []
        if a.get("asset_type") == "stock" and _d(a.get("da")).get("status") == "not_run":
            reasons.append("the Devil's Advocate (the committee's sceptic) did not review this stock")
        action = ctx.action_of(t)
        stances = {s.get("role"): s.get("stance") for s in _l(a.get("specialists"))
                   if isinstance(s, dict) and s.get("status") == "ok"}
        fs, ts = stances.get("fundamental"), stances.get("technical")
        if action in ("TRIM", "SELL") and fs == "bullish" and ts == "bullish":
            reasons.append(f"both the fundamental and technical reviews are bullish, yet the action is {action}")
        if action in ("ADD", "BUY") and fs == "bearish" and ts == "bearish":
            reasons.append(f"both the fundamental and technical reviews are bearish, yet the action is {action}")
        if not reasons:
            continue
        out.append(_finding(
            "thin_review", "medium", t, "5",
            f"The decision on {t} has a weak safety net: " + _join(reasons, "and") + ". The chair should look at it "
            "personally.",
            f"action {action}; fundamental {fs}; technical {ts}; Devil's Advocate {_d(a.get('da')).get('status')}",
            _fix("rerun_agents", ["sector_head", "devils_advocate"], [t],
                 f"Re-examine {t}: " + _join(reasons, "and") + ". The Devil's Advocate must attack the thesis for the "
                 "final action, and the sector head must either justify the action against the lens stances or "
                 "change it.")))
    return out


# =====================================================================================
# 13. pending_event_risk
# =====================================================================================

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_PAT = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_MD_RE = re.compile(rf"\b{_MONTH_PAT}\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>20\d\d))?\b", re.I)
_DATE_DM_RE = re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_PAT}\.?(?:,?\s+(?P<y>20\d\d))?\b", re.I)
_DATE_ISO_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_EVENT_KW_RE = re.compile(
    r"\b(?:results?|earnings|quarterly|third[- ]quarter|second[- ]quarter|first[- ]quarter|fourth[- ]quarter|q[1-4]|"
    r"rulings?|verdicts?|hearing|decision|trial|fda|pdufa|vote|court|settlement)\b", re.I)
_FORWARD_RE = re.compile(r"\b(?:due|on|scheduled|expected|set\s+for|slated|will|reports?|pending|upcoming|before|ahead\s+of)\b", re.I)
_PAST_RE = re.compile(r"\b(?:began|started|was|were|held|reported|released|announced|issued|delivered)\b", re.I)


def event_dates(sentence: str, as_of: date) -> list[date]:
    """Calendar dates in ``sentence`` (year defaults to as_of's, next year if the date is far in the past)."""
    out: list[date] = []

    def mk(y: int | None, mon: int, d: int) -> None:
        try:
            if y is None:
                cand = date(as_of.year, mon, d)
                if (as_of - cand).days > 180:
                    cand = date(as_of.year + 1, mon, d)
            else:
                cand = date(y, mon, d)
            out.append(cand)
        except ValueError:
            pass

    for m in _DATE_ISO_RE.finditer(sentence):
        mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for rx in (_DATE_MD_RE, _DATE_DM_RE):
        for m in rx.finditer(sentence):
            mk(int(m.group("y")) if m.group("y") else None, _MONTHS[m.group("mon")[:3].lower()], int(m.group("d")))
    low = sentence.lower()
    if re.search(r"\btomorrow\b", low):
        out.append(as_of + timedelta(days=1))
    if re.search(r"\b(?:today|this\s+(?:evening|morning|afternoon))\b", low):
        out.append(as_of)
    return out


def find_pending_events(text: Any, as_of: date, window_days: int) -> list[tuple[date, str]]:
    hits: list[tuple[date, str]] = []
    for sent in split_sentences(text):
        if not _EVENT_KW_RE.search(sent) or not _FORWARD_RE.search(sent):
            continue
        for d in event_dates(sent, as_of):
            if 0 <= (d - as_of).days <= window_days:
                if _PAST_RE.search(sent) and not re.search(r"\b(?:due|scheduled|expected|upcoming|pending|will)\b", sent, re.I):
                    continue
                hits.append((d, sent))
    return hits


def check_pending_events(ctx: Ctx) -> list[dict]:
    if ctx.as_of is None:
        return []
    window = int(ctx.cfg["event_window_days"])
    holdings = [t for t in ctx.plan_positions] or ctx.holdings
    found: dict[str, list[tuple[date, str, str]]] = {}

    def add(t: str, d: date, sent: str, where: str) -> None:
        lst = found.setdefault(t, [])
        if all(s != sent for _, s, _ in lst):
            lst.append((d, sent, where))

    for (phase, role, t), rep in sorted(ctx.reports.items()):
        if t not in holdings and not ctx.is_holding(t):
            continue
        for text in _strings(rep, skip=("sources", "key_figures_unused", "data_ref", "input_refs")):
            for d, sent in find_pending_events(text, ctx.as_of, window):
                add(t, d, sent, f"{role} report")
        for kf in _l(rep.get("key_figures")):
            if isinstance(kf, dict):
                for text in (kf.get("name"), kf.get("note")):
                    for d, sent in find_pending_events(text, ctx.as_of, window):
                        add(t, d, sent, f"{role} key figure")
    for (phase, t), v in sorted(ctx.verdicts.items()):
        for d, sent in find_pending_events(v.get("rationale"), ctx.as_of, window):
            add(t, d, sent, "sector verdict")
    known = list(dict.fromkeys(list(ctx.plan_positions) + list(ctx.holdings)))
    for i, item in enumerate(_l(_d(ctx.cio).get("reconciliation"))):
        if not isinstance(item, dict):
            continue
        topic_text = " ".join(str(item.get(k) or "") for k in ("topic", "findings"))
        named = [t for t in known if len(t) >= 2 and _mention_re(t).search(topic_text)]
        for key in ("findings", "resolution"):
            for d, sent in find_pending_events(item.get(key), ctx.as_of, window):
                for t in named:
                    add(t, d, sent, "cio reconciliation")
    out: list[dict] = []
    for t, lst in sorted(found.items()):
        lst.sort(key=lambda x: x[0])
        d0 = lst[0][0]
        days = (d0 - ctx.as_of).days
        pos = ctx.plan_positions.get(t)
        trade = trade_shares(pos) > 0
        out.append(_finding(
            "pending_event_risk", "low", t, "5",
            f"{t} has a known event {'today' if days == 0 else f'in {days} day(s)'} ({d0.isoformat()}) that could "
            "change the picture right after this memo" + (", and the plan trades it before then." if trade else ".")
            + " Consider waiting for it before acting.",
            " | ".join(f"{where}: \"{_clip(sent, 180)}\"" for _, sent, where in lst[:3]),
            _fix("note_only", tickers=[t], feedback=f"In the final report tell the investor that {t} has an event on "
                 f"{d0.isoformat()} and that waiting until after it is reasonable before executing "
                 f"{'the trade' if trade else 'any change'}.")))
    return out


# =====================================================================================
# 14. cash_drag
# =====================================================================================


def check_cash_drag(ctx: Ctx) -> list[dict]:
    cash = _d(_d(ctx.plan).get("cash"))
    post = _num(cash.get("post_pct"))
    if post is None:
        return []
    floor = _num(cash.get("target_pct"))
    if floor is None:
        floor = _num(ctx.profile.get("target_cash_pct"))
    if floor is None:
        return []
    margin = float(ctx.cfg["cash_drag_margin_pp"])
    buys = [t for t, p in ctx.plan_positions.items()
            if str(_d(p.get("trade")).get("side") or "").upper() == "BUY" and trade_shares(p) > 0]
    if post <= floor + margin or buys:
        return []
    usd = _num(cash.get("post_usd"))
    return [_finding(
        "cash_drag", "low", None, "7",
        f"After the plan {_pct(post, 1)} of the portfolio would sit in cash (floor {_pct(floor, 0)}) and nothing is "
        "bought, so the money earns almost nothing until a candidate qualifies or new contributions arrive.",
        f"post-trade cash {_pct(post, 2)}" + (f" ({_usd(usd, 0)})" if usd is not None else "")
        + f" vs floor {_pct(floor, 2)}; margin {margin:g} pp; no BUY trades in the plan",
        _fix("note_only", feedback="In the final report say where the idle cash is meant to go (waiting list of "
                                   "candidates, new contributions, or fewer sales now) rather than leaving it "
                                   "unexplained."))]


# =====================================================================================
# Orchestration
# =====================================================================================

CHECKS: tuple[tuple[str, Callable[[Ctx], list[dict]]], ...] = (
    ("stale_portfolio", check_stale_portfolio),
    ("action_vs_trade", check_action_vs_trade),
    ("trim_became_full_exit", check_trim_became_full_exit),
    ("text_vs_plan_contradiction", check_text_vs_plan),
    ("valuation_outlier", check_valuation_outliers),
    ("systematic_valuation_bias", check_systematic_bias),
    ("captive_or_leverage_flag", check_captive_or_leverage),
    ("plan_does_not_cure_breach", check_plan_does_not_cure_breach),
    ("limit_inconsistency", check_limit_inconsistency),
    ("weak_why", check_weak_why),
    ("flags_failed_evidence", check_flags_failed_evidence),
    ("thin_review", check_thin_review),
    ("pending_event_risk", check_pending_events),
    ("cash_drag", check_cash_drag),
)


def sort_and_number(findings: list[dict]) -> list[dict]:
    """Severity (high first), then code, then ticker; ids F01, F02, ..."""
    ordered = sorted(findings, key=lambda f: (_SEV_RANK[f["severity"]], f["code"], f.get("ticker") or "", f["message"]))
    out = []
    for i, f in enumerate(ordered, 1):
        g = {"id": f"F{i:02d}"}
        g.update(f)
        out.append(g)
    return out


def audit_ctx(ctx: Ctx) -> dict:
    """Run every check on a loaded :class:`Ctx` and build the audit document."""
    findings: list[dict] = []
    errors: list[dict] = []
    for name, fn in CHECKS:
        try:
            findings.extend(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            errors.append({"check": name, "error": f"{exc.__class__.__name__}: {exc}"})
    numbered = sort_and_number(findings)
    summary = {s: sum(1 for f in numbered if f["severity"] == s) for s in SEVERITIES}
    summary["verdict"] = "needs_rework" if summary["high"] else "pass"
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": ctx.run_id,
        "generated_at": getattr(ctx, "now_stamp", None) or common.now_iso(),
        "as_of": ctx.as_of.isoformat() if ctx.as_of else None,
        "summary": summary,
        "findings": numbered,
    }
    if errors:
        doc["check_errors"] = errors
    return doc


def run(run_dir: str | Path, *, now: Any = None, portfolio_path: str | Path | None = None,
        write: bool = True) -> dict:
    """Audit ``run_dir``. Returns the audit document; with ``write`` also saves ``phase4/audit.json``."""
    ctx = load_ctx(run_dir, now=now, portfolio_path=portfolio_path)
    doc = audit_ctx(ctx)
    if write:
        common.write_json(Path(run_dir) / "phase4" / AUDIT_FILE, doc)
    return doc


def format_summary(doc: Mapping) -> str:
    """Short human summary (one line per finding)."""
    s = doc["summary"]
    lines = [f"Audit of run {doc.get('run_id')} (as of {doc.get('as_of')}): {s['verdict'].upper()} - "
             f"{s['high']} high, {s['medium']} medium, {s['low']} low"]
    for f in doc["findings"]:
        lines.append(f"  {f['id']} {f['severity'].upper():6} {f['code']:27} {(f['ticker'] or '-'):8} "
                     f"[s{f['section']}] {_clip(f['message'], 150)}  -> {f['fix']['type']}"
                     + (f" {','.join(f['fix']['roles'])}" if f["fix"]["roles"] else ""))
    for e in doc.get("check_errors", []):
        lines.append(f"  (check {e['check']} failed: {e['error']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Deterministic quality audit of a finished committee run.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="run directory (runs/<run_id>)")
    g.add_argument("--run", help="run id under the runs directory")
    parser.add_argument("--json", action="store_true", help="print the full audit JSON instead of a summary")
    parser.add_argument("--no-write", action="store_true", help="do not write phase4/audit.json")
    parser.add_argument("--portfolio", default=None, help="portfolio.yaml to compare with (default: config dir)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
        run_dir = Path(args.run_dir) if args.run_dir else common.runs_dir() / args.run
        doc = run(run_dir, portfolio_path=args.portfolio, write=not args.no_write)
    except (FileNotFoundError, ValueError, common.AginvError) as exc:
        print(json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(doc, ensure_ascii=False, indent=2) if args.json else format_summary(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
