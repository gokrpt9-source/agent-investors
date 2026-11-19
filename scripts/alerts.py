"""alerts.py - end-of-day warning signs for the live portfolio (rule engine, pure + testable).

``evaluate(snapshot, ...)`` turns the dashboard's live portfolio snapshot (``dashboard.portfolio_snapshot``)
into a list of alerts. Each alert says WHAT is breached, WHY (one line) and WHAT TO DO (one line).

Rules (thresholds: investor_profile.yaml limits + the ``alerts`` section of committee.yaml):
  position_cap     critical  one stock above max_position_pct
  fund_cap         critical  one fund above max_fund_position_pct
  sector_cap       critical  a sector's look-through weight above max_sector_pct
  stop_breached    critical  price below the invalidation price of the latest committee run
  day_drop         warning / critical  a one-day fall of ``day_drop_stock_pct`` / ``day_drop_fund_pct``
                   (critical at ``critical_multiplier`` times that)
  deep_loss        warning / critical  down ``loss_warn_pct`` / ``loss_crit_pct`` against average cost
  cash_floor       warning   cash below target_cash_pct
  pending_action   warning   the latest committee plan said SELL/TRIM and the position is unchanged
  quote_failed     warning   a holding has no live quote, so it cannot be watched
  stale_holdings   info      portfolio.yaml not updated for ``stale_holdings_days`` days
  fx_move          info      USD/ILS moved ``fx_move_pct`` or more today

"Final" alerts are those computed while the US market is closed: they use the closing prices of the last
session and carry that session's date in their key, so a popup fires once per session. Decision support
only - nothing here is advice and nothing is ever traded.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import math  # noqa: E402
from datetime import date, datetime, time as dtime, timedelta, timezone  # noqa: E402
from typing import Any  # noqa: E402

DEFAULTS: dict[str, float] = {
    "day_drop_stock_pct": 5.0, "day_drop_fund_pct": 3.0, "critical_multiplier": 1.6,
    "loss_warn_pct": 15.0, "loss_crit_pct": 25.0, "stale_holdings_days": 14,
    "run_max_age_days": 7, "fx_move_pct": 1.5,
}
SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
SECTOR_NAMES = {
    "information_technology": "Information Technology", "health_care": "Health Care", "financials": "Financials",
    "energy": "Energy", "industrials": "Industrials", "consumer_discretionary": "Consumer Discretionary",
    "consumer_staples": "Consumer Staples", "utilities": "Utilities", "materials": "Materials",
    "real_estate": "Real Estate", "communication_services": "Communication Services", "unclassified": "Unclassified",
}


# ------------------------------------------------------------------------------------------- market clock
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def et_offset_hours(now_utc: datetime) -> int:
    """US Eastern offset from UTC (-4 during daylight time, -5 otherwise). DST runs from the second Sunday
    of March 02:00 local to the first Sunday of November 02:00 local."""
    y = now_utc.year
    start = datetime.combine(_nth_weekday(y, 3, 6, 2), dtime(7, 0), tzinfo=timezone.utc)   # 02:00 EST = 07:00 UTC
    end = datetime.combine(_nth_weekday(y, 11, 6, 1), dtime(6, 0), tzinfo=timezone.utc)    # 02:00 EDT = 06:00 UTC
    return -4 if start <= now_utc < end else -5


def market_state(now_utc: datetime | None = None, holidays: list[date] | None = None) -> dict:
    """US equity market state: ``{"open": bool, "last_session": date, "closed_at_utc": iso, "et_time": str}``.
    Regular hours 09:30-16:00 ET on weekdays that are not in ``holidays`` (early closes are not modelled)."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hol = set(holidays if holidays is not None else common.market_holidays())
    off = et_offset_hours(now)
    et = now + timedelta(hours=off)

    def trading(d: date) -> bool:
        return d.weekday() < 5 and d not in hol

    d = et.date()
    t = et.time()
    is_open = trading(d) and dtime(9, 30) <= t < dtime(16, 0)
    if trading(d) and t >= dtime(9, 30):
        last = d                       # today's session is open or already over
    else:
        last = d - timedelta(days=1)
        while not trading(last):
            last -= timedelta(days=1)
    off_last = et_offset_hours(datetime.combine(last, dtime(12, 0), tzinfo=timezone.utc))   # noon UTC is never near a DST switch
    close_utc = datetime.combine(last, dtime(16, 0), tzinfo=timezone.utc) - timedelta(hours=off_last)
    return {"open": is_open, "last_session": last, "closed_at_utc": close_utc.isoformat(timespec="minutes"),
            "et_time": et.strftime("%Y-%m-%d %H:%M ET")}


# ------------------------------------------------------------------------------------------- helpers
def _f(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _alert(rule: str, sev: str, ticker: str | None, title: str, why: str, action: str, final: bool, session: str,
           value: float | None = None, limit: float | None = None) -> dict:
    scope = ticker or "portfolio"
    return {"key": f"{rule}|{scope}|{session if final else 'live'}", "rule": rule, "severity": sev, "ticker": ticker,
            "title": title, "why": why, "action": action, "final": final, "session": session,
            "value": None if value is None else round(value, 4), "limit": limit}


def _sell_qty(excess_usd: float, unit_usd: float, held: float, whole: bool = True) -> float:
    n = excess_usd / unit_usd if unit_usd > 0 else held
    n = math.ceil(n - 1e-9) if whole else round(n, 4)
    return float(min(max(n, 1), held))


def sector_exposure(rows: list[dict], total: float, splits: dict[str, dict[str, float]]) -> dict[str, dict]:
    """Look-through weight per sector: sum(holding weight x its sector split). ``splits[ticker]`` maps a
    sector key to the percent of that holding in the sector; holdings without a split count as unclassified.
    Returns {sector: {"pct": float, "top": [(ticker, contribution_pct)...]}}."""
    out: dict[str, dict] = {}
    for r in rows:
        w = _f(r.get("weight_pct"))
        if w is None:
            continue
        split = splits.get(r["ticker"]) or {"unclassified": 100.0}
        for sec, pct in split.items():
            c = w * float(pct) / 100.0
            e = out.setdefault(sec, {"pct": 0.0, "top": []})
            e["pct"] += c
            e["top"].append((r["ticker"], c))
    for e in out.values():
        e["top"].sort(key=lambda x: -x[1])
    return out


# ------------------------------------------------------------------------------------------- the engine
def evaluate(snapshot: dict, profile: dict, cfg: dict | None = None, *, market: dict | None = None,
             splits: dict[str, dict[str, float]] | None = None, run: dict | None = None,
             today: date | None = None) -> list[dict]:
    """Return the active alerts for ``snapshot``, most severe first. ``run`` = {"id","as_of","invalidations":
    {ticker: price}, "plan": {ticker: {"side","shares","shares_cur","action"}}} from the latest committee run."""
    c = {**DEFAULTS, **(cfg or {})}
    mkt = market or market_state()
    final = not mkt["open"]
    session = mkt["last_session"].isoformat() if hasattr(mkt["last_session"], "isoformat") else str(mkt["last_session"])
    tot = _f((snapshot.get("totals") or {}).get("value_usd")) or 0.0
    rows = [r for r in snapshot.get("rows") or [] if not r.get("error") and r.get("value_usd") is not None]
    out: list[dict] = []
    A = lambda *a, **k: out.append(_alert(*a, final=final, session=session, **k))   # noqa: E731

    # -- limits ---------------------------------------------------------------------------------
    max_pos, max_fund = _f(profile.get("max_position_pct")), _f(profile.get("max_fund_position_pct"))
    max_sec, tgt_cash = _f(profile.get("max_sector_pct")), _f(profile.get("target_cash_pct"))
    for r in rows:
        w = _f(r.get("weight_pct"))
        if w is None or not tot:
            continue
        is_fund = r.get("asset_type") == "fund"
        cap = max_fund if is_fund else max_pos
        if cap is None or w <= cap:
            continue
        held = r.get("units") if r.get("proxy") and r.get("units") else r.get("shares")
        unit_usd = r["value_usd"] / held if held else 0.0
        excess = r["value_usd"] - cap / 100.0 * tot
        n = _sell_qty(excess, unit_usd, held, whole=True)
        unit = "units" if r.get("proxy") and r.get("units") else "shares"
        A("fund_cap" if is_fund else "position_cap", "critical", r["ticker"],
          f"{r['ticker']} is over your {'fund' if is_fund else 'single-stock'} limit",
          f"{r['ticker']} is {w:.1f}% of the portfolio; your limit is {cap:g}%.",
          f"Sell about {n:g} {unit} (~{_usd(n * unit_usd)}) to get under {cap:g}%, or raise the limit on purpose.",
          value=w, limit=cap)
    if max_sec is not None and tot and splits is not None:
        for sec, e in sorted(sector_exposure(rows, tot, splits).items(), key=lambda kv: -kv[1]["pct"]):
            if sec == "unclassified" or e["pct"] <= max_sec:
                continue
            top = ", ".join(f"{t} {x:.1f}%" for t, x in e["top"][:3])
            A("sector_cap", "critical", None,
              f"{SECTOR_NAMES.get(sec, sec)} is over your sector limit",
              f"{SECTOR_NAMES.get(sec, sec)} is {e['pct']:.1f}% of the portfolio looking through funds; limit {max_sec:g}%. Biggest: {top}.",
              f"Stop adding to {SECTOR_NAMES.get(sec, sec)}; trim {e['top'][0][0]} or point new money to other sectors.",
              value=e["pct"], limit=max_sec)
    cash_w = _f((snapshot.get("cash") or {}).get("weight_pct"))
    if tgt_cash is not None and cash_w is not None and cash_w < tgt_cash:
        gap = (tgt_cash - cash_w) / 100.0 * tot
        A("cash_floor", "warning", None, "Cash is below your buffer",
          f"Cash is {cash_w:.1f}% of the portfolio; your target is {tgt_cash:g}%.",
          f"Hold off new buys or raise about {_usd(gap)} (sell or deposit).", value=cash_w, limit=tgt_cash)

    # -- price-based ----------------------------------------------------------------------------
    for r in rows:
        t = r["ticker"]
        is_fund = r.get("asset_type") == "fund"
        d = _f(r.get("day_pct"))
        thr = c["day_drop_fund_pct"] if is_fund else c["day_drop_stock_pct"]
        if d is not None and d <= -thr:
            crit = d <= -thr * c["critical_multiplier"]
            A("day_drop", "critical" if crit else "warning", t, f"{t} fell sharply today",
              f"{t} is down {abs(d):.1f}% on the day (alert level {thr:g}%).",
              "Check the news; no action unless the reason changes your thesis.", value=d, limit=-thr)
        pl = _f(r.get("pl_pct"))
        if pl is not None and pl <= -c["loss_warn_pct"]:
            crit = pl <= -c["loss_crit_pct"]
            A("deep_loss", "critical" if crit else "warning", t, f"{t} is well below your cost",
              f"{t} is down {abs(pl):.1f}% against your average cost.",
              "Re-check the thesis: hold on purpose, or sell and note the loss for tax.", value=pl, limit=-c["loss_warn_pct"])
    if run and run.get("invalidations"):
        for r in rows:
            inv = _f(run["invalidations"].get(r["ticker"]))
            px = r["value_usd"] / r["shares"] if r.get("shares") and not r.get("proxy") else None
            if inv and px and px < inv:
                A("stop_breached", "critical", r["ticker"], f"{r['ticker']} broke the committee's stop",
                  f"Price ${px:,.2f} is below the stop ${inv:,.2f} from run {run['id']}.",
                  "Review now: sell, or write down why the thesis still holds.", value=px, limit=inv)

    # -- hygiene --------------------------------------------------------------------------------
    for r in snapshot.get("rows") or []:
        if r.get("error"):
            A("quote_failed", "warning", r["ticker"], f"No live price for {r['ticker']}",
              "Yahoo returned no quote, so this holding cannot be watched.", "Check the symbol in portfolio.yaml.")
    if run and run.get("plan"):
        cur = {r["ticker"]: r for r in rows}
        for t, p in run["plan"].items():
            r = cur.get(t)
            held = (r.get("units") if r and r.get("proxy") and r.get("units") else (r or {}).get("shares")) if r else None
            if r and p.get("side") == "SELL" and held is not None and held >= (p.get("shares_cur") or 0) - 1e-9:
                A("pending_action", "warning", t, f"Committee advice on {t} is still open",
                  f"Run {run['id']} said {p.get('action')} ({p.get('shares'):g} to sell) and your position has not changed.",
                  "Do it, or dismiss it with a reason; re-run the committee if things moved.")
    asof = snapshot.get("as_of_config")
    try:
        age = ((today or date.today()) - date.fromisoformat(str(asof))).days
        if age >= c["stale_holdings_days"]:
            A("stale_holdings", "info", None, "Holdings list is out of date",
              f"portfolio.yaml was last updated {age} days ago ({asof}).", "Update it after your trades (Edit holdings).")
    except (TypeError, ValueError):
        pass
    fx = _f((snapshot.get("fx") or {}).get("day_pct"))
    if fx is not None and abs(fx) >= c["fx_move_pct"]:
        A("fx_move", "info", None, "USD/ILS moved a lot today",
          f"USD/ILS changed {fx:+.1f}% today, shifting your shekel value.", "No action; note it before converting currency.", value=fx)
    out.sort(key=lambda a: (SEV_ORDER[a["severity"]], a["rule"], a["ticker"] or ""))
    return out


def rules_catalog(profile: dict, cfg: dict | None = None) -> list[dict]:
    """Human-readable list of the checks and thresholds in force (shown in the tab)."""
    c = {**DEFAULTS, **(cfg or {})}
    return [
        {"rule": "position_cap", "severity": "critical", "text": f"One stock above {profile.get('max_position_pct')}% of the portfolio"},
        {"rule": "fund_cap", "severity": "critical", "text": f"One fund above {profile.get('max_fund_position_pct')}%"},
        {"rule": "sector_cap", "severity": "critical", "text": f"A sector above {profile.get('max_sector_pct')}% looking through funds"},
        {"rule": "stop_breached", "severity": "critical", "text": "Price below the latest committee run's stop (invalidation) price"},
        {"rule": "day_drop", "severity": "warning/critical", "text": f"Day drop of {c['day_drop_stock_pct']:g}% (stock) or {c['day_drop_fund_pct']:g}% (fund); critical at {c['critical_multiplier']:g}x"},
        {"rule": "deep_loss", "severity": "warning/critical", "text": f"Down {c['loss_warn_pct']:g}% / {c['loss_crit_pct']:g}% against average cost"},
        {"rule": "cash_floor", "severity": "warning", "text": f"Cash below {profile.get('target_cash_pct')}%"},
        {"rule": "pending_action", "severity": "warning", "text": "Latest committee plan said SELL/TRIM and the position is unchanged"},
        {"rule": "quote_failed", "severity": "warning", "text": "A holding has no live quote"},
        {"rule": "stale_holdings", "severity": "info", "text": f"portfolio.yaml older than {int(c['stale_holdings_days'])} days"},
        {"rule": "fx_move", "severity": "info", "text": f"USD/ILS moves {c['fx_move_pct']:g}% or more in a day"},
    ]
