"""portfolio.py — current vs target weights and the proposed rebalancing plan (DESIGN §4.11).

Deterministic sizing of the committee's actions into a trade list that honours the investor
profile (max position / fund position / sector, min trade, target cash, whole shares) and the
committee ``sizing`` config. Output: ``portfolio_plan/v1`` (``phase4/portfolio_plan.json``) or
``phase4/portfolio_preview.json`` (also ``portfolio_plan/v1`` — DESIGN §4.11; preview vs final
is the file name and the flags), wrapped in the Envelope.

Sizing (all weights are percent of total portfolio value):
* SELL  -> 0 (full exit). A partial sell (TRIM) of a holding without a price is dropped
           (``dropped_reason`` ``no_price``), exactly like an unpriced buy.
* TRIM  -> conviction TRIM: ``min(cap, cur * (1 - trim_fraction))``;
           breach TRIM (position > cap, or stock whose sector look-through exceeds
           ``min(max_sector_pct, target) + sector_breach_tolerance_pp``):
           ``min(cap - breach_buffer_pp, cur)``, then, for breached sectors, TRIM-flagged stocks
           of that sector are reduced lowest-conviction-first until the sector is <= its target -
           but a TRIM verdict never loses more than ``sizing.sector_trim_max_fraction`` (default
           0.5) of its CURRENT weight in total (step-1 trim included), so the sector rule cannot
           turn a TRIM into a full exit. A position-cap breach is still cured fully. What the
           stocks cannot cure (the exposure sits in funds) is reported in ``residual_breaches``.
* ADD   -> ``min(cur + add_step_pp, cap, cur + sector headroom)``.
* BUY   -> ``min(initial_position_pct, cap, sector headroom)``.
* HOLD / PASS -> unchanged.
Buys are scaled pro-rata so post-trade cash >= ``target_cash_pct``; funds are never sold to fund
stock buys (fund positions only change on their own verdicts). Shares are rounded DOWN to whole
shares unless ``allow_fractional_shares`` — EXCEPT sells that cure a hard-limit breach, which round
UP (``ceil``: the fewest whole shares that reach the sized target, never more than held): a TRIM of
a position above its cap (``trim_breach_position``, or any TRIM while ``weight > cap``) and the cuts
that bring a sector down to its limit/target (``trim_breach_sector``). Rounding those down could
leave the breach in place (e.g. 0.8 of a share -> 0). Trades below ``min_trade_usd`` are still
dropped (``dropped_reason`` ``below_min_trade_usd``, with a note) and the breach then stays flagged
in ``constraint_checks``. A sell that closes every share held carries ``full_exit: true``; for a
TRIM verdict (not SELL) a note on the position says so. ``weight_intent_pct`` is the weight the
sizing rules called for before cash scaling, whole-share rounding and the min-trade / no-price drop
(``weight_tgt_pct`` is the weight actually reached by the proposed trade). Sells with a cost basis
and a nominal USD gain carry an indicative tax estimate (``capital_gains_tax_pct`` x gain) — Israeli
tax is on the REAL gain (CPI/FX adjusted).
Every position also carries ``effective_action`` (the verdict, or HOLD for a TRIM/SELL whose trade was
dropped, with ``effective_action_reason``), ``trade_pct_of_position`` (shares sold / held) and
``exit_kind`` (``none`` | ``partial`` | ``full``).

API:
    build_plan(*, lookthrough, prices, actions, target_sector_weights, profile=None, cfg=None,
               portfolio_cfg=None, extra_prices=None, schema_name="portfolio_plan") -> dict
    run(run_dir, *, final) -> dict      # reads the run's phase files, writes phase4/...
CLI:
    python scripts/portfolio.py --run-dir runs/<id> [--final]
    python scripts/portfolio.py --run <id> [--final]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

SCHEMA_PLAN = "portfolio_plan"
#: accepted by the schema enum for callers that ask for it explicitly; run() writes the preview
#: as ``portfolio_plan/v1`` too (DESIGN §4.11), which validate.infer_schema recognizes
SCHEMA_PREVIEW = "portfolio_preview"
#: default file of each CIO mode when the manifest has no order for it
CIO_DEFAULT_FILES = {"allocation": ("phase1", "cio_allocation.json"),
                     "reconciliation": ("phase4", "cio_final.json")}

#: float tolerance for weight comparisons (percentage points)
EPS = 1e-9
#: rounding slack used by the post-trade constraint checks (pp) — a sector/position within this
#: of its limit after whole-share rounding is not reported as a breach
CHECK_SLACK_PP = 0.005
PCT_DP = 4
#: ``sizing.sector_trim_max_fraction`` default: the sector-limit cure may cut a TRIM-verdict stock by
#: at most this fraction of its CURRENT weight (step-1 trim included), so a TRIM never becomes a full exit
DEFAULT_SECTOR_TRIM_MAX_FRACTION = 0.5
USD_DP = 2

HOLDING_ACTIONS = ("ADD", "HOLD", "TRIM", "SELL")
BUY_ACTIONS = ("ADD", "BUY")

TAX_NOTE_TEMPLATE = (
    "Indicative estimate only: {rate:g}% x the nominal USD gain at the latest close. Israeli "
    "capital-gains tax is levied on the REAL gain (CPI / FX adjusted, computed in ILS), so the "
    "actual liability can differ materially — consult a tax advisor before selling."
)
FX_NOTE_TEMPLATE = (
    "Base currency {base}: USD amounts are converted at USD/ILS {fx} (as of {as_of}). Holdings are "
    "valued in USD (US listings, US proxy tickers and Tel Aviv quotes converted at spot), so the "
    "dollar-linked part of the portfolio carries USD/ILS exposure — a weaker dollar lowers its ILS value "
    "even when USD prices are unchanged. See the memo's currency section for the split by listing currency."
)
FULL_EXIT_SECTOR_NOTE = (
    "TRIM sizing exits the whole position: the {sector} sector limit/target requires it (the "
    "lowest-conviction TRIM-flagged stock is cut first). Confirm a full exit is intended - the "
    "verdict is TRIM, not SELL."
)
FULL_EXIT_ROUNDING_NOTE = (
    "TRIM sizing exits the whole position (whole-share rounding of a small holding). Confirm a full "
    "exit is intended - the verdict is TRIM, not SELL."
)
BREACH_DROPPED_NOTE = (
    "Breach trim of {shares:g} share(s) (about ${value:,.0f}) is below the ${min_trade:,.0f} minimum "
    "trade, so it was dropped and the limit breach remains - trim manually if you want it cured."
)
FUNDS_NOTE = (
    "Funds are never sold to fund stock buys: fund positions change only on their own verdicts; "
    "buys are scaled pro-rata instead so post-trade cash stays at or above the target."
)


# =====================================================================================
# small helpers
# =====================================================================================


def _num(x: Any) -> float | None:
    return common.fact_value(x)


def _r(x: Any, nd: int) -> float | None:
    v = common.safe_float(x)
    if v is None:
        return None
    out = round(v, nd)
    return 0.0 if out == 0 else out


def _shares_out(x: float | None) -> int | float | None:
    if x is None:
        return None
    if abs(x - round(x)) < 1e-9:
        return int(round(x))
    return round(x, 6)


def _req(mapping: Mapping[str, Any], dotted: str, what: str) -> Any:
    val = common.get_dotted(mapping, dotted)
    if val is None:
        raise common.ConfigError(f"{what}: missing required key {dotted!r}")
    return val


def _req_num(mapping: Mapping[str, Any], dotted: str, what: str) -> float:
    val = common.safe_float(_req(mapping, dotted, what))
    if val is None:
        raise common.ConfigError(f"{what}: {dotted!r} must be a number")
    return val


def _opt_fraction(mapping: Mapping[str, Any], dotted: str, default: float, what: str) -> float:
    """An optional config fraction in (0, 1]; ``default`` when the key is absent."""
    raw = common.get_dotted(mapping, dotted)
    if raw is None:
        return default
    val = common.safe_float(raw)
    if val is None or not (0.0 < val <= 1.0):
        raise common.ConfigError(f"{what}: {dotted!r} must be a number in (0, 1], got {raw!r}")
    return val


def _norm(t: Any) -> str:
    return common.norm_ticker(t)


def _split_fractions(split: Any, sector: str | None, asset_type: str) -> dict[str, float]:
    """Normalize a look-through ``sector_split`` to fractions summing to 1. Works whether the
    split is given as percent of the holding (sums to 100) or of the portfolio (sums to the
    holding's weight). Stocks without a split count fully toward their sector."""
    out: dict[str, float] = {}
    if isinstance(split, Mapping):
        for k, v in split.items():
            f = common.safe_float(v)
            if f is not None and f > 0:
                out[str(k)] = out.get(str(k), 0.0) + f
    total = sum(out.values())
    if total > 0:
        return {k: v / total for k, v in out.items()}
    if sector in common.SECTOR_KEYS:
        return {sector: 1.0}
    return {"unclassified": 1.0}


def _price_fact(entry: Any, ticker: str, as_of: Any) -> dict | None:
    """A price Fact from a prices.json ticker entry, a Fact, or a bare number."""
    if entry is None:
        return None
    if isinstance(entry, Mapping) and "last_close" in entry:
        entry = entry.get("last_close")
    if common.is_fact(entry):
        return dict(entry)
    v = common.safe_float(entry)
    if v is None:
        return None
    return common.fact(v, "USD/share", f"D:PX:{_norm(ticker)}", as_of, "price")


def _sector_limit(sector: str, max_sector: float, targets: Mapping[str, Any]) -> float:
    t = common.safe_float(targets.get(sector)) if targets else None
    return max_sector if t is None else min(max_sector, t)


# =====================================================================================
# build_plan
# =====================================================================================


def build_plan(*, lookthrough: dict, prices: dict | None, actions: Mapping[str, Mapping[str, Any]],
               target_sector_weights: Mapping[str, Any] | None, profile: dict | None = None,
               cfg: dict | None = None, portfolio_cfg: dict | None = None,
               extra_prices: Mapping[str, Any] | None = None, schema_name: str = SCHEMA_PLAN,
               as_of: Any = None, fx: Any = None) -> dict:
    """Size ``actions`` ({ticker: {"action","conviction","asset_type","sector", optional
    "source","trim_reason","sector_split","price"}}) into a portfolio plan (DESIGN §4.11).

    ``lookthrough`` = phase1/lookthrough.json payload (holdings, weights, sector look-through);
    ``prices`` = phase1/prices.json payload (``tickers.<T>.last_close``, ``fx.usdils``);
    ``target_sector_weights`` = CIO targets ({sector|cash|unclassified: pct});
    ``portfolio_cfg`` = portfolio.yaml (shares, cost basis, proxy tickers);
    ``extra_prices`` = {ticker: Fact|number} for tickers missing from ``prices`` (new BUYs);
    ``fx`` = optional USD/ILS Fact (or number) overriding ``prices.fx.usdils``.
    Configs default to the active config dir. Returns the Envelope-wrapped plan."""
    profile = profile if profile is not None else common.load_config("investor_profile")
    cfg = cfg if cfg is not None else common.committee_cfg()
    if portfolio_cfg is None:
        try:
            portfolio_cfg = common.load_config("portfolio")
        except common.ConfigError:
            portfolio_cfg = {}
    prices = prices or {}
    lookthrough = lookthrough or {}
    targets: dict[str, float] = {}
    for k, v in (target_sector_weights or {}).items():
        f = common.safe_float(v)
        if f is not None:
            targets[str(k)] = f

    # ---- configuration (every threshold from config) ---------------------------------
    what_p, what_c = "investor_profile.yaml", "committee.yaml"
    max_pos = _req_num(profile, "max_position_pct", what_p)
    max_fund = _req_num(profile, "max_fund_position_pct", what_p)
    max_sector = _req_num(profile, "max_sector_pct", what_p)
    min_trade = _req_num(profile, "min_trade_usd", what_p)
    target_cash = _req_num(profile, "target_cash_pct", what_p)
    tax_rate = _req_num(profile, "capital_gains_tax_pct", what_p)
    fractional = bool(profile.get("allow_fractional_shares", False))
    base_ccy = str(profile.get("base_currency") or "ILS")
    trim_fraction = _req_num(cfg, "sizing.trim_fraction", what_c)
    breach_buffer = _req_num(cfg, "sizing.breach_buffer_pp", what_c)
    add_step = _req_num(cfg, "sizing.add_step_pp", what_c)
    initial_pct = _req_num(cfg, "sizing.initial_position_pct", what_c)
    tol = _req_num(cfg, "actions.sector_breach_tolerance_pp", what_c)
    sector_trim_max = _opt_fraction(cfg, "sizing.sector_trim_max_fraction", DEFAULT_SECTOR_TRIM_MAX_FRACTION, what_c)

    as_of_s = common.date_str(as_of) or common.date_str(prices.get("as_of")) or \
        common.date_str(lookthrough.get("as_of")) or common.today().isoformat()

    missing: list[str] = []
    flags: list[dict] = []
    notes: list[str] = []
    used_source_ids: set[str] = set()

    # ---- totals, FX -----------------------------------------------------------------
    total_fact = lookthrough.get("total_value_usd")
    total = _num(total_fact)
    if fx is not None:
        fx_fact = dict(fx) if common.is_fact(fx) else \
            common.fact(common.safe_float(fx), "ILS/USD", "D:PX:ILS=X", as_of_s, "price")
    else:
        fx_fact = (prices.get("fx") or {}).get("usdils") if isinstance(prices.get("fx"), Mapping) else None
    fx = _num(fx_fact)
    if fx_fact is not None and common.is_fact(fx_fact) and fx_fact.get("source_id"):
        used_source_ids.add(str(fx_fact["source_id"]))
    if fx is None:
        missing.append("USD/ILS exchange rate (prices.json fx.usdils) — ILS values not computed")
        flags.append(common.flag("fx_missing", "medium", "fx.usdils unavailable; value_ils left null"))
    if common.is_fact(total_fact) and total_fact.get("source_id"):
        used_source_ids.add(str(total_fact["source_id"]))

    price_entries: Mapping[str, Any] = prices.get("tickers") or {}
    extra = {_norm(k): v for k, v in (extra_prices or {}).items()}
    cfg_holdings = {}
    for h in (portfolio_cfg or {}).get("holdings") or []:
        try:
            cfg_holdings[_norm(h.get("ticker"))] = h
        except (ValueError, AttributeError):
            continue
    acts = {}
    for k, v in (actions or {}).items():
        acts[_norm(k)] = dict(v or {})

    # ---- current positions ------------------------------------------------------------
    positions: list[dict] = []
    held: set[str] = set()
    for h in lookthrough.get("holdings") or []:
        t = _norm(h.get("ticker"))
        held.add(t)
        ch = cfg_holdings.get(t, {})
        asset_type = h.get("asset_type") or ch.get("asset_type") or "stock"
        sector = h.get("sector") or ("funds" if asset_type == "fund" else "unclassified")
        proxy = h.get("proxy_ticker") or ch.get("proxy_ticker")
        price_ticker = _norm(h.get("price_ticker") or proxy or t)
        pfact = _price_fact(price_entries.get(price_ticker), price_ticker, as_of_s) or \
            _price_fact(extra.get(price_ticker) or extra.get(t), price_ticker, as_of_s) or \
            _price_fact(h.get("price"), price_ticker, as_of_s)  # look-through's own (cached) price
        if pfact is not None and _num(pfact) is None:
            pfact = _price_fact(h.get("price"), price_ticker, as_of_s) or pfact
        price = _num(pfact)
        shares = common.safe_float(h.get("shares"))
        if shares is None:
            shares = common.safe_float(ch.get("shares"))
        if shares is not None and shares <= 0:
            shares = None
        w_cur = _num(h.get("weight_pct"))
        value_cur = None
        if shares is not None and price is not None:
            value_cur = shares * price
        elif w_cur is not None and total is not None:
            value_cur = w_cur * total / 100.0
        if w_cur is None and value_cur is not None and total:
            w_cur = value_cur / total * 100.0
        native = (price_entries.get(price_ticker) or {}).get("native") if not proxy else None
        native = native if isinstance(native, Mapping) else {}
        positions.append({
            "native_price_ils": _num(native.get("last_close")), "native_usdils": common.safe_float(native.get("usdils")),
            "ticker": t, "asset_type": asset_type, "sector": sector, "kind": "holding",
            "split": _split_fractions(h.get("sector_split"), sector, asset_type),
            "price_ticker": price_ticker, "proxy": bool(proxy), "price_fact": pfact, "price": price,
            "shares_cur": shares, "value_cur": value_cur, "w_cur_raw": w_cur,
            "w_cur": w_cur if w_cur is not None else 0.0,
            "cost_basis": common.safe_float(ch.get("cost_basis", h.get("cost_basis"))),
            "cost_basis_currency": str(ch.get("cost_basis_currency") or h.get("cost_basis_currency") or "USD"),
        })

    # ---- new candidates (BUY) -----------------------------------------------------------
    ignored: list[str] = []
    for t, a in acts.items():
        if t in held:
            continue
        action = str(a.get("action") or "").upper()
        if action != "BUY":
            if action in ("ADD", "TRIM", "SELL"):
                ignored.append(f"{t} ({action} but not held)")
            continue
        asset_type = a.get("asset_type") or "stock"
        sector = a.get("sector") or "unclassified"
        pfact = _price_fact(a.get("price"), t, as_of_s) or _price_fact(extra.get(t), t, as_of_s) or \
            _price_fact(price_entries.get(t), t, as_of_s)
        positions.append({
            "ticker": t, "asset_type": asset_type, "sector": sector, "kind": "candidate",
            "split": _split_fractions(a.get("sector_split"), sector, asset_type),
            "price_ticker": t, "proxy": False, "price_fact": pfact, "price": _num(pfact),
            "shares_cur": 0.0, "value_cur": 0.0, "w_cur_raw": 0.0, "w_cur": 0.0,
            "cost_basis": None, "cost_basis_currency": "USD",
        })
    if ignored:
        notes.append("Actions ignored because the ticker is not held: " + ", ".join(sorted(ignored)) + ".")

    if total is None:
        known = [p["value_cur"] for p in positions if p["value_cur"] is not None]
        total = sum(known) if known else None
        if total is not None:
            cash_guess = 0.0
            pc = portfolio_cfg or {}
            if common.safe_float(pc.get("cash_usd")) is not None:
                cash_guess = float(pc["cash_usd"])
            total += cash_guess
            total_fact = common.fact(total, "USD", "D:CFG:portfolio", as_of_s, "other",
                                     derivation="sum of holding values + cash_usd")
            flags.append(common.flag("total_value_derived", "low",
                                     "lookthrough total_value_usd missing; derived from holdings"))
    if not common.is_fact(total_fact):
        total_fact = common.fact(total, "USD", None, as_of_s, "other")

    # ---- action assignment ------------------------------------------------------------
    for p in positions:
        a = acts.get(p["ticker"])
        conv = common.safe_float(a.get("conviction")) if a else None
        p["conviction"] = conv
        p["notes"] = []
        p["dropped_reason"] = None
        p["sizing_rule"] = "unchanged"
        p["round_up"] = False     # breach-curing sells round UP to whole shares (see _finalize_trade)
        p["full_exit"] = False    # set when the executed trade sells every share held
        p["pos_breach"] = False   # the verdict is a TRIM of a position above its hard cap
        p["step1_w_tgt"] = p["w_cur"]   # target after step 1 (before the sector-limit cure)
        p["sector_cap_frac"] = None     # set when step 2 cut the stock: max share of the CURRENT weight
        if a is None:
            p["action"] = "HOLD"
            p["action_source"] = "no_verdict"
            missing.append(f"No verdict for held {p['ticker']} — kept unchanged (HOLD)")
            continue
        action = str(a.get("action") or "HOLD").upper()
        if p["kind"] == "holding" and action == "BUY":
            p["notes"].append("BUY on a held position sized as ADD")
            action = "ADD"
        if p["kind"] == "holding" and action == "PASS":
            action = "HOLD"
        p["action"] = action
        p["action_source"] = a.get("source") or a.get("action_source") or "actions"
        p["trim_reason"] = a.get("trim_reason")
        if a.get("limit_breach") is not None and p["trim_reason"] is None:
            p["trim_reason"] = "breach" if a.get("limit_breach") else "conviction"
        if p["price"] is None:
            missing.append(f"Price for {p['price_ticker']} — trade cannot be sized in shares")

    for p in positions:
        p["cap"] = max_fund if p["asset_type"] == "fund" else max_pos
        p["w_tgt"] = p["w_cur"]

    # ---- look-through state ---------------------------------------------------------------
    lt_cur: dict[str, float] = {}
    for k, v in (lookthrough.get("sector_weights_pct") or {}).items():
        f = common.safe_float(v)
        if f is not None:
            lt_cur[str(k)] = f
    if not lt_cur:
        for p in positions:
            for s, f in p["split"].items():
                lt_cur[s] = lt_cur.get(s, 0.0) + p["w_cur"] * f
        lt_cur["cash"] = max(0.0, 100.0 - sum(p["w_cur"] for p in positions))
    held_weight = sum(p["w_cur"] for p in positions if p["kind"] == "holding")
    lt_cash = lookthrough.get("cash") if isinstance(lookthrough.get("cash"), Mapping) else {}
    cash_cur = _num(lt_cash.get("weight_pct")) if lt_cash else None
    if cash_cur is None:  # portfolio-level cash (not fund-internal cash) = what holdings don't cover
        cash_cur = max(0.0, 100.0 - held_weight)

    def lt_post() -> dict[str, float]:
        out = dict(lt_cur)
        for p in positions:
            d = p["w_tgt"] - p["w_cur"]
            if abs(d) <= EPS:
                continue
            for s, f in p["split"].items():
                out[s] = out.get(s, 0.0) + d * f
            out["cash"] = out.get("cash", 0.0) - d
        return out

    def sector_breached(s: str) -> bool:
        return s in common.SECTOR_KEYS and lt_cur.get(s, 0.0) > _sector_limit(s, max_sector, targets) + tol + EPS

    # ---- step 1: SELL / TRIM base targets ---------------------------------------------
    for p in positions:
        if p["action"] == "SELL":
            p["w_tgt"] = 0.0
            p["sizing_rule"] = "sell_full_exit"
        elif p["action"] == "TRIM":
            w = p["w_cur"]
            pos_breach = w > p["cap"] + EPS
            sec_breach = p["asset_type"] == "stock" and sector_breached(p["sector"])
            reason = p.get("trim_reason")
            breach = (reason == "breach") if reason in ("breach", "conviction") else (pos_breach or sec_breach)
            if breach:
                p["w_tgt"] = max(0.0, min(p["cap"] - breach_buffer, w))
                p["sizing_rule"] = "trim_breach_position" if pos_breach else "trim_breach_sector"
            else:
                p["w_tgt"] = max(0.0, min(p["cap"], w * (1.0 - trim_fraction)))
                p["sizing_rule"] = "trim_conviction"
            # a TRIM of a position above its hard cap must actually get under it: round shares UP
            p["round_up"] = bool(breach or pos_breach)
            p["pos_breach"] = pos_breach
            p["step1_w_tgt"] = p["w_tgt"]

    breached_sectors: list[str] = []   # sectors over their limit before the trades (residuals are checked at the end)
    # ---- step 2: sector breaches -> TRIM-flagged stocks, lowest conviction first ----------
    for s in common.SECTOR_KEYS:
        if not sector_breached(s):
            continue
        breached_sectors.append(s)
        limit = _sector_limit(s, max_sector, targets)
        trims =[p for p in positions if p["action"] == "TRIM" and p["asset_type"] == "stock"
                 and p["split"].get(s, 0.0) > 0]
        trims.sort(key=lambda p: (p["conviction"] is not None,
                                  p["conviction"] if p["conviction"] is not None else 0.0, p["ticker"]))
        for p in trims:
            excess = lt_post().get(s, 0.0) - limit
            if excess <= EPS:
                break
            # a TRIM verdict is never a full exit through the sector rule: the stock may lose at most
            # ``sector_trim_max`` of its CURRENT weight in total (step-1 trim included)
            floor_w = min(p["w_tgt"], p["w_cur"] * (1.0 - sector_trim_max))
            wanted = excess / p["split"][s]
            cut = min(wanted, p["w_tgt"] - floor_w)
            if cut > EPS:
                p["w_tgt"] -= cut
                p["sizing_rule"] = "trim_breach_sector"
                p["round_up"] = True   # the cut cures a sector-limit breach: round shares UP
                p["sector_cap_frac"] = sector_trim_max
                note = f"reduced {cut:.2f} pp for the {s} sector limit {limit:g}%"
                if cut < wanted - EPS:
                    note += (f" (capped at {sector_trim_max:.0%} of the current position in total: a TRIM "
                             "verdict does not become a full exit through the sector rule)")
                p["notes"].append(note)

    # ---- step 3a: finalize sells (whole shares, min trade) ----------------------------
    for p in positions:
        p["w_intent"] = p["w_tgt"]   # what the sizing rules call for, before rounding / drops
    for p in positions:
        if p["w_tgt"] < p["w_cur"] - EPS:
            _finalize_trade(p, total, min_trade, fractional)

    # ---- step 3b: buys with position cap + sector headroom ----------------------------
    buyers = [p for p in positions if p["action"] in BUY_ACTIONS]
    buyers.sort(key=lambda p: (-(p["conviction"] if p["conviction"] is not None else -1.0), p["ticker"]))
    for p in buyers:
        w = p["w_cur"]
        if p["action"] == "ADD":
            desired = min(w + add_step, p["cap"])
            p["sizing_rule"] = "add_step"
        else:
            desired = min(initial_pct, p["cap"])
            p["sizing_rule"] = "buy_initial"
        if p["kind"] == "holding" and p["w_cur_raw"] is None:
            p["dropped_reason"] = "no_weight"
            continue
        if desired <= w + EPS:
            p["dropped_reason"] = "at_position_cap"
            continue
        post = lt_post()
        room = math.inf
        for s, f in p["split"].items():
            if s in common.SECTOR_KEYS and f > 0:
                room = min(room, max(0.0, _sector_limit(s, max_sector, targets) - post.get(s, 0.0)) / f)
        inc = min(desired - w, room)
        if inc <= EPS:
            p["dropped_reason"] = "no_sector_headroom"
            continue
        if inc < desired - w - EPS:
            p["notes"].append(f"capped by sector headroom ({inc:.2f} pp of {desired - w:.2f} pp)")
        p["w_tgt"] = w + inc
        p["w_intent"] = p["w_tgt"]

    # ---- step 4: pro-rata scaling so post-trade cash >= target -------------------------
    sells_pp = sum(p["w_cur"] - p["w_tgt"] for p in positions if p["w_tgt"] < p["w_cur"] - EPS)
    buys_pp = sum(p["w_tgt"] - p["w_cur"] for p in positions if p["w_tgt"] > p["w_cur"] + EPS)
    scale = 1.0
    if buys_pp > EPS:
        available = cash_cur + sells_pp - target_cash
        if available < buys_pp - EPS:
            scale = max(0.0, available) / buys_pp
            for p in positions:
                if p["w_tgt"] > p["w_cur"] + EPS:
                    if scale <= EPS:
                        p["w_tgt"] = p["w_cur"]
                        p["dropped_reason"] = "insufficient_cash"
                    else:
                        p["w_tgt"] = p["w_cur"] + (p["w_tgt"] - p["w_cur"]) * scale
                        p["notes"].append(f"scaled pro-rata x{scale:.4f} to keep cash >= {target_cash:g}%")
            notes.append(f"Buys scaled pro-rata by {scale:.4f} so post-trade cash stays >= "
                         f"{target_cash:g}% (cash {cash_cur:.2f}% + sells {sells_pp:.2f} pp).")

    # ---- step 5: finalize buys ------------------------------------------------------------
    for p in positions:
        if p["w_tgt"] > p["w_cur"] + EPS:
            _finalize_trade(p, total, min_trade, fractional)
        elif "trade" not in p:
            p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0}
            p["w_tgt"] = p["w_cur"]

    # ---- outputs -----------------------------------------------------------------------
    post = lt_post()
    rate_note = TAX_NOTE_TEMPLATE.format(rate=tax_rate)
    out_positions = []
    for p in positions:
        tr = p["trade"]
        full_exit = bool(p.get("full_exit")) and tr["side"] == "SELL"
        if full_exit and p["action"] != "SELL":
            p["notes"].append(FULL_EXIT_SECTOR_NOTE.format(sector=common.sector_display(p["sector"]))
                              if p["sizing_rule"] == "trim_breach_sector" else FULL_EXIT_ROUNDING_NOTE)
        value_usd = tr.get("value_usd")
        trade = {"side": tr["side"], "shares": _shares_out(tr.get("shares")),
                 "value_usd": _r(value_usd, USD_DP),
                 "value_ils": _r(value_usd * fx, USD_DP) if (fx is not None and value_usd is not None) else None}
        for k in ("proposed_shares", "proposed_value_usd"):
            if k in tr:
                trade[k] = _shares_out(tr[k]) if k == "proposed_shares" else _r(tr[k], USD_DP)
        if p["price_fact"] and p["price_fact"].get("source_id"):
            used_source_ids.add(str(p["price_fact"]["source_id"]))
        eff_action, eff_reason = _effective_action(p)
        pct_of_position, exit_kind = _trade_extent(p, tr, full_exit)
        out_positions.append({
            "ticker": p["ticker"], "asset_type": p["asset_type"], "sector": p["sector"],
            "kind": p["kind"], "action": p["action"], "action_source": p["action_source"],
            "effective_action": eff_action, "effective_action_reason": eff_reason,
            "conviction": p["conviction"], "sizing_rule": p["sizing_rule"],
            "price": p["price_fact"], "price_ticker": p["price_ticker"],
            "shares_cur": _shares_out(p["shares_cur"]),
            "value_usd_cur": _r(p["value_cur"], USD_DP),
            "value_ils_cur": _r(p["value_cur"] * fx, USD_DP) if (fx is not None and p["value_cur"] is not None) else None,
            "weight_cur_pct": _r(p["w_cur_raw"], PCT_DP),
            "weight_tgt_pct": _r(p["w_tgt"], PCT_DP) if p["w_cur_raw"] is not None or p["kind"] == "candidate" else None,
            "weight_intent_pct": _r(p["w_intent"], PCT_DP) if p["w_cur_raw"] is not None or p["kind"] == "candidate" else None,
            "cap_pct": p["cap"],
            "trade": trade,
            "full_exit": full_exit,
            "trade_pct_of_position": pct_of_position,
            "exit_kind": exit_kind,
            "dropped_reason": p["dropped_reason"],
            "tax": _tax(p, trade, tax_rate, rate_note),
            "notes": p["notes"],
        })

    sectors = []
    for s in list(common.SECTOR_KEYS) + ["cash", "unclassified"]:
        cur = lt_cur.get(s, 0.0)
        pst = post.get(s, 0.0)
        tgt = targets.get(s)
        mx = max_sector if s in common.SECTOR_KEYS else None
        sectors.append({
            "sector": s, "cur_lookthrough_pct": _r(cur, PCT_DP), "post_lookthrough_pct": _r(pst, PCT_DP),
            "target_pct": _r(tgt, PCT_DP), "max_pct": mx,
            "gap_pp": _r(pst - tgt, PCT_DP) if tgt is not None else None,
            "breach": bool(mx is not None and pst > mx + CHECK_SLACK_PP),
        })

    cash_post = cash_cur - sum(p["w_tgt"] - p["w_cur"] for p in positions)
    checks = _constraint_checks(positions, sectors, lt_cur, post, cash_cur, cash_post, target_cash,
                                max_pos, max_fund, max_sector, min_trade, fractional, tol, targets)

    residual = _residual_breaches(positions, post, breached_sectors, max_sector, targets)
    for rb in residual:
        notes.append(rb["note"])

    notes.insert(0, rate_note)
    notes.insert(1, FX_NOTE_TEMPLATE.format(base=base_ccy, fx=f"{fx:.4f}" if fx is not None else "n/a",
                                            as_of=(fx_fact or {}).get("as_of") if isinstance(fx_fact, Mapping) else "n/a"))
    notes.insert(2, FUNDS_NOTE)
    notes.append(("Trade sizes are rounded down to whole shares, except sells that cure a position-cap or "
                  "sector-limit breach, which round up" if not fractional else
                  "Fractional shares allowed (breach-curing sells round up at the 6th decimal)")
                 + f"; trades below ${min_trade:,.0f} are dropped.")
    proxies = sorted(p["ticker"] + "->" + p["price_ticker"] for p in positions if p["proxy"])
    if proxies:
        notes.append("Priced via US proxy tickers (share counts and tax estimates are approximate): "
                     + ", ".join(proxies) + ".")

    sources = _collect_sources(used_source_ids, [prices.get("sources"), lookthrough.get("sources")])
    for sid in sorted(used_source_ids):
        if sid.startswith("D:PX:") and sid not in {s["id"] for s in sources}:
            try:
                sources.append(common.dataset_source(sid))
            except ValueError:
                pass
    if scale < 1.0:
        flags.append(common.flag("buys_scaled", "low", f"buys scaled pro-rata x{scale:.4f} for target cash"))
    for c in checks:
        if c["status"] == "fail":
            flags.append(common.flag(f"check_failed:{c['check']}", "high", c["detail"] or ""))

    return common.envelope(
        schema_name, as_of_s, sources=sources, missing_data=missing, flags=flags,
        base_currency=base_ccy,
        fx_usdils=fx_fact if common.is_fact(fx_fact) else None,
        total_value_usd=total_fact,
        cash={"current_pct": _r(cash_cur, PCT_DP), "target_pct": target_cash, "post_pct": _r(cash_post, PCT_DP),
              "current_usd": _r(cash_cur * total / 100.0, USD_DP) if total is not None else None,
              "post_usd": _r(cash_post * total / 100.0, USD_DP) if total is not None else None},
        positions=out_positions, sectors=sectors, residual_breaches=residual,
        constraint_checks=checks, notes=notes,
        sizing={"trim_fraction": trim_fraction, "breach_buffer_pp": breach_buffer, "add_step_pp": add_step,
                "initial_position_pct": initial_pct, "sector_trim_max_fraction": sector_trim_max,
                "buy_scale": round(scale, 6)},
    )


def _effective_action(p: Mapping[str, Any]) -> tuple[str, str | None]:
    """(effective_action, reason). A TRIM/SELL verdict whose trade did not survive sizing (rounds to
    zero shares, below the minimum trade, no price ...) is effectively a HOLD; the verdict itself
    (``action``) is never changed. Everything else executes as its verdict."""
    action = p["action"]
    tr = p["trade"]
    if action not in ("TRIM", "SELL") or tr.get("side") == "SELL":
        return action, None
    verb = action.lower()
    reason = p.get("dropped_reason")
    if reason == "rounds_to_zero_shares":
        raw = common.safe_float(tr.get("proposed_shares"))
        if raw is None:
            amount = "the sized amount"
        else:
            amount = f"{raw:.2f} share" if raw <= 1.0 else f"{raw:.2f} shares"
        return "HOLD", f"{verb} of {amount} rounds to zero shares - no trade"
    if reason == "below_min_trade_usd":
        shares = common.safe_float(tr.get("proposed_shares"))
        value = common.safe_float(tr.get("proposed_value_usd"))
        what = verb
        if shares is not None:
            what = f"{verb} of {shares:g} share(s)" + (f" (about ${value:,.0f})" if value is not None else "")
        return "HOLD", f"{what} is below the minimum trade size - no trade"
    if reason == "no_price":
        return "HOLD", f"{verb} cannot be sized in shares (no price) - no trade"
    if reason:
        return "HOLD", f"{verb} trade dropped ({reason}) - no trade"
    return "HOLD", f"no {verb} trade was sized - no trade"


def _trade_extent(p: Mapping[str, Any], tr: Mapping[str, Any], full_exit: bool) -> tuple[float | None, str]:
    """(trade_pct_of_position, exit_kind): shares sold / shares held (0..1, null without a share
    count) and 'none' | 'partial' | 'full'."""
    held = common.safe_float(p.get("shares_cur"))
    if full_exit:
        return 1.0, "full"
    if tr.get("side") == "SELL":
        sold = common.safe_float(tr.get("shares"))
        if held and sold is not None:
            return _r(min(1.0, max(0.0, sold / held)), 6), "partial"
        return None, "partial"
    return (0.0 if held else None), "none"


def _residual_breaches(positions: list[dict], post: Mapping[str, float], breached: list[str],
                       max_sector: float, targets: Mapping[str, Any]) -> list[dict]:
    """Sectors that were over their limit and are STILL over it after the planned trades: what the
    stock trims achieved, the funds that hold the rest, and what the real lever is."""
    out: list[dict] = []
    for s in breached:
        limit = _sector_limit(s, max_sector, targets)
        pst = post.get(s, 0.0)
        if pst <= limit + CHECK_SLACK_PP:
            continue
        stock_trim = sum((p["w_cur"] - p["w_tgt"]) * p["split"].get(s, 0.0) for p in positions
                         if p["asset_type"] == "stock" and p["w_tgt"] < p["w_cur"] - EPS)
        funds = []
        for p in positions:
            if p["asset_type"] != "fund":
                continue
            contrib = p["w_tgt"] * p["split"].get(s, 0.0)
            if contrib > EPS:
                funds.append({"ticker": p["ticker"], "weight_pct": _r(p["w_tgt"], PCT_DP),
                              "sector_pct_of_portfolio": _r(contrib, PCT_DP)})
        funds.sort(key=lambda f: (-f["sector_pct_of_portfolio"], f["ticker"]))
        fund_total = sum(f["sector_pct_of_portfolio"] for f in funds)
        disp = common.sector_display(s)
        top = ", ".join(f"{f['ticker']} {f['sector_pct_of_portfolio']:.1f} pp" for f in funds[:4])
        note = (f"{disp} stays {pst - limit:.2f} pp above its {limit:g}% limit after the stock trims "
                f"(which remove {stock_trim:.2f} pp); {fund_total:.1f} pp of the exposure sits in funds"
                + (f" ({top})" if top else "") + ". Stocks alone cannot cure it, and a TRIM verdict is "
                "not turned into a full exit to chase it. The real lever is fund overlap: direct new "
                f"contributions to under-weight sectors and review the funds that stack {disp}.")
        out.append({"sector": s, "post_pct": _r(pst, PCT_DP), "limit_pct": _r(limit, PCT_DP),
                    "max_pct": max_sector, "excess_pp": _r(pst - limit, PCT_DP),
                    "stock_trim_pp": _r(stock_trim, PCT_DP), "fund_contributions": funds, "note": note})
    out.sort(key=lambda r: (-r["excess_pp"], r["sector"]))
    return out


def _finalize_trade(p: dict, total: float | None, min_trade: float, fractional: bool) -> None:
    """Turn ``p['w_tgt']`` into a whole-share trade; drop trades below ``min_trade``. Updates
    ``p['w_tgt']`` to the weight actually reached.

    Shares round DOWN, except for sells flagged ``p['round_up']`` (they cure a position-cap or
    sector-limit breach): those round UP so the sized target is actually reached, bounded by the
    shares held. Selling every share held is a full exit (``p['full_exit']``, ``w_tgt`` 0)."""
    w_cur, w_tgt, price = p["w_cur"], p["w_tgt"], p["price"]
    side = "BUY" if w_tgt > w_cur else "SELL"
    full_exit = side == "SELL" and w_tgt <= EPS
    round_up = side == "SELL" and bool(p.get("round_up"))
    if total is None:
        p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0}
        p["dropped_reason"] = "no_total_value"
        p["w_tgt"] = w_cur
        return
    if full_exit:
        shares = p["shares_cur"]
        value = p["value_cur"]
        if value is None:
            value = w_cur * total / 100.0
    else:
        value_wanted = abs(w_tgt - w_cur) * total / 100.0
        if price is None or price <= 0:
            # an unsized trade is not executable: drop buys AND partial sells alike
            p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0,
                          "proposed_value_usd": value_wanted}
            p["dropped_reason"] = "no_price"
            p["w_tgt"] = w_cur
            return
        raw = value_wanted / price
        if fractional:
            shares = math.ceil(raw * 1e6 - 1e-6) / 1e6 if round_up else round(raw, 6)
        else:
            shares = math.ceil(raw - 1e-9) if round_up else math.floor(raw + 1e-9)
        held = p["shares_cur"]
        frac_cap = p.get("sector_cap_frac")
        if side == "SELL" and frac_cap is not None and held:
            # the sector rule may sell at most ``frac_cap`` of the shares held (whole shares, rounded
            # DOWN) unless a position-cap breach needs more; rounding up must not exceed it
            allowed = frac_cap * held if fractional else math.floor(frac_cap * held + 1e-9)
            if p.get("pos_breach"):
                need = (w_cur - p["step1_w_tgt"]) * total / 100.0 / price
                allowed = max(allowed, math.ceil(need * 1e6 - 1e-6) / 1e6 if fractional
                              else math.ceil(need - 1e-9))
            shares = min(shares, allowed)
        if side == "SELL" and held is not None and shares >= held - 1e-9:
            shares = held          # never sell more than held; selling it all is a full exit
            full_exit = True
        value = shares * price
        if shares <= 0:
            p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0,
                          "proposed_shares": raw, "proposed_value_usd": value_wanted}
            p["dropped_reason"] = "rounds_to_zero_shares"
            p["w_tgt"] = w_cur
            return
    if value is not None and abs(value) < min_trade - 1e-9:
        p["trade"] = {"side": "NONE", "shares": 0, "value_usd": 0.0,
                      "proposed_shares": shares, "proposed_value_usd": value}
        p["dropped_reason"] = "below_min_trade_usd"
        if round_up and shares:
            p["notes"].append(BREACH_DROPPED_NOTE.format(shares=shares, value=value, min_trade=min_trade))
        p["w_tgt"] = w_cur
        return
    p["trade"] = {"side": side, "shares": shares, "value_usd": value}
    if full_exit:
        p["w_tgt"] = 0.0
        p["full_exit"] = True
    elif value is not None:
        p["w_tgt"] = w_cur + (value if side == "BUY" else -value) / total * 100.0


def _tax(p: dict, trade: Mapping[str, Any], rate: float, note: str) -> dict:
    """Capital-gains estimate for sells: applicable only with a USD cost basis and a gain."""
    if trade.get("side") != "SELL":
        return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None, "note": None}
    cb = p.get("cost_basis")
    shares = common.safe_float(trade.get("shares"))
    price = p.get("price")
    if cb is None:
        return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None,
                "note": "No cost basis in portfolio.yaml — tax impact not estimated."}
    if p.get("proxy"):
        return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None,
                "note": "Priced via a proxy ticker — gain vs cost basis not estimated."}
    cb_cur = str(p.get("cost_basis_currency") or "USD").upper()
    if cb_cur == "ILS" and p.get("native_price_ils") is not None and p.get("native_usdils"):
        # Tel Aviv-quoted holding with an ILS cost basis: the nominal gain is measured in ILS (the
        # currency Israeli tax is computed in), so it is closer to the taxable real gain than a USD gain.
        n_shares = common.safe_float(trade.get("shares"))
        if n_shares is None:
            return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None,
                    "note": "Share count unavailable — tax impact not estimated."}
        gain_ils = (p["native_price_ils"] - cb) * n_shares
        usdils = float(p["native_usdils"])
        if gain_ils <= 0:
            return {"applicable": False, "est_gain_usd": _r(gain_ils / usdils, USD_DP), "est_tax_usd": None,
                    "est_gain_ils": _r(gain_ils, 2), "est_tax_ils": None,
                    "note": "No nominal gain in ILS at the latest close — no capital-gains tax estimated."}
        return {"applicable": True, "est_gain_usd": _r(gain_ils / usdils, USD_DP),
                "est_tax_usd": _r(gain_ils * rate / 100.0 / usdils, USD_DP),
                "est_gain_ils": _r(gain_ils, 2), "est_tax_ils": _r(gain_ils * rate / 100.0, 2),
                "note": "Indicative estimate only: " + f"{rate:g}% x the nominal ILS gain (latest close vs the ILS "
                        "cost basis). Israeli tax applies to the real gain (CPI-adjusted), so the actual "
                        "liability can differ — consult a tax advisor before selling."}
    if cb_cur != "USD":
        return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None,
                "note": f"Cost basis in {p.get('cost_basis_currency')} — gain not estimated."}
    if shares is None or price is None:
        return {"applicable": False, "est_gain_usd": None, "est_tax_usd": None,
                "note": "Share count or price unavailable — tax impact not estimated."}
    gain = (price - cb) * shares
    if gain <= 0:
        return {"applicable": False, "est_gain_usd": _r(gain, USD_DP), "est_tax_usd": None,
                "note": "No nominal gain at the latest close — no capital-gains tax estimated."}
    return {"applicable": True, "est_gain_usd": _r(gain, USD_DP),
            "est_tax_usd": _r(gain * rate / 100.0, USD_DP), "note": note}


def _residual_reason(p: Mapping[str, Any]) -> str:
    """Why a position is still above its cap after the plan: no reducing verdict, a reducing
    verdict whose trade was dropped (and why), or a reducing trade that fell short."""
    action = p["action"]
    if action not in ("TRIM", "SELL"):
        return "no reducing verdict"
    if p["dropped_reason"]:
        return f"{action} verdict, but the trade was dropped: {p['dropped_reason']}"
    return f"{action} verdict, trade proposed but it does not reach the cap"


def _constraint_checks(positions: list[dict], sectors: list[dict], lt_cur: Mapping[str, float],
                       post: Mapping[str, float], cash_cur: float, cash_post: float, target_cash: float,
                       max_pos: float, max_fund: float, max_sector: float, min_trade: float,
                       fractional: bool, tol: float, targets: Mapping[str, float]) -> list[dict]:
    checks: list[dict] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    for label, kind, cap in (("max_position_pct", "stock", max_pos), ("max_fund_position_pct", "fund", max_fund)):
        worse, residual = [], []
        for p in positions:
            if p["asset_type"] != kind or p["w_tgt"] <= cap + CHECK_SLACK_PP:
                continue
            if p["w_tgt"] > p["w_cur"] + CHECK_SLACK_PP:
                worse.append(f"{p['ticker']} {p['w_tgt']:.2f}%")
            else:
                residual.append(f"{p['ticker']} {p['w_tgt']:.2f}% ({_residual_reason(p)})")
        if worse:
            add(label, "fail", f"plan raises positions above {cap:g}%: " + ", ".join(worse))
        elif residual:
            add(label, "warn", f"still above {cap:g}% after the proposed trades: " + "; ".join(residual))
        else:
            add(label, "pass", f"every {kind} position <= {cap:g}% after trades")

    worse, residual = [], []
    for s in common.SECTOR_KEYS:
        pst = post.get(s, 0.0)
        if pst <= max_sector + CHECK_SLACK_PP:
            continue
        item = f"{s} {pst:.2f}%"
        if pst > lt_cur.get(s, 0.0) + CHECK_SLACK_PP:
            worse.append(item)
            continue
        stuck = [f"{p['ticker']} {p['action']}: {p['dropped_reason']}" for p in positions
                 if p["asset_type"] == "stock" and p["action"] in ("TRIM", "SELL") and p["dropped_reason"]
                 and p["split"].get(s, 0.0) > 0]
        residual.append(item + (f" (reducing trade dropped: {', '.join(stuck)})" if stuck else ""))
    if worse:
        add("max_sector_pct", "fail", f"plan raises sectors above {max_sector:g}%: " + ", ".join(worse))
    elif residual:
        add("max_sector_pct", "warn", f"look-through still above {max_sector:g}% after the stock trims "
            "(the rest sits in funds that are not sold - see residual_breaches): " + ", ".join(residual))
    else:
        add("max_sector_pct", "pass", f"every sector look-through <= {max_sector:g}% after trades")

    gaps = []
    for s, t in targets.items():
        if s == "cash":
            continue
        pst = post.get(s, 0.0)
        if abs(pst - t) > tol + CHECK_SLACK_PP:
            gaps.append(f"{s} {pst:.2f}% vs target {t:g}%")
    add("sector_targets", "warn" if gaps else "pass",
        ("outside +/-" + f"{tol:g} pp of the CIO target: " + ", ".join(gaps)) if gaps
        else f"every targeted sector within +/-{tol:g} pp of the CIO target")

    buys = any(p["trade"]["side"] == "BUY" for p in positions)
    if cash_post >= target_cash - CHECK_SLACK_PP:
        add("target_cash_pct", "pass", f"post-trade cash {cash_post:.2f}% >= target {target_cash:g}%")
    elif not buys:
        add("target_cash_pct", "warn", f"cash {cash_post:.2f}% below target {target_cash:g}% "
            "(no buys proposed; raising cash would need sells)")
    else:
        add("target_cash_pct", "fail", f"post-trade cash {cash_post:.2f}% below target {target_cash:g}%")

    dropped = [p["ticker"] for p in positions if p["dropped_reason"] == "below_min_trade_usd"]
    small = [p["ticker"] for p in positions
             if p["trade"]["side"] != "NONE" and p["trade"].get("value_usd") is not None
             and abs(p["trade"]["value_usd"]) < min_trade - 1e-9]
    add("min_trade_usd", "fail" if small else "pass",
        (f"trades below ${min_trade:,.0f}: " + ", ".join(small)) if small else
        (f"{len(dropped)} trade(s) below ${min_trade:,.0f} dropped" + (f": {', '.join(dropped)}" if dropped else "")))

    if not fractional:
        frac = [p["ticker"] for p in positions if p["trade"]["side"] == "BUY"
                and p["trade"].get("shares") is not None
                and abs(p["trade"]["shares"] - round(p["trade"]["shares"])) > 1e-9]
        add("whole_shares", "fail" if frac else "pass",
            ("fractional buys: " + ", ".join(frac)) if frac else "all buys are whole shares")

    neg = [p["ticker"] for p in positions if p["w_tgt"] < -EPS]
    add("no_short", "fail" if neg else "pass", ("negative weights: " + ", ".join(neg)) if neg
        else "no short positions (allow_short false)")

    fund_sold_for_buys = [p["ticker"] for p in positions if p["asset_type"] == "fund"
                          and p["trade"]["side"] == "SELL" and p["action"] not in ("TRIM", "SELL")]
    add("funds_not_sold_for_buys", "fail" if fund_sold_for_buys else "pass",
        ("funds sold without a TRIM/SELL verdict: " + ", ".join(fund_sold_for_buys)) if fund_sold_for_buys
        else "fund positions change only on their own verdicts")
    return checks


def _collect_sources(ids: set[str], pools: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for pool in pools:
        for s in pool or []:
            if isinstance(s, Mapping) and str(s.get("id")) in ids and str(s.get("id")) not in seen:
                seen.add(str(s.get("id")))
                out.append(dict(s))
    return out


# =====================================================================================
# run(run_dir)
# =====================================================================================


def load_run_config(name: str, run_dir: Path, *, required: bool = True) -> dict:
    """``<run>/inputs/<name>.yaml`` when the run snapshot exists, else the active config dir."""
    snap = run_dir / "inputs" / f"{name}.yaml"
    try:
        if snap.is_file():
            if name == "committee":
                return common.committee_cfg(snap.parent)
            return common.load_config(name, snap.parent)
        if name == "committee":
            return common.committee_cfg()
        return common.load_config(name)
    except common.ConfigError:
        if required:
            raise
        return {}


def read_json_opt(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return common.read_json(path)
    except (OSError, ValueError):
        return None


def resolve_run_path(run_dir: Path, p: Any) -> Path | None:
    """Resolve a path recorded in the manifest (absolute, run-relative or repo-relative);
    re-roots ``.../phaseN/...`` onto ``run_dir`` when the recorded absolute path moved."""
    if not p:
        return None
    path = Path(str(p))
    cands = [path] if path.is_absolute() else [run_dir / path, common.REPO_ROOT / path, path]
    parts = path.parts
    for i, part in enumerate(parts):
        if part.startswith("phase") and part[5:].isdigit():
            cands.append(run_dir.joinpath(*parts[i:]))
            break
    for c in cands:
        if c.is_file():
            return c
    return None


def load_manifest(run_dir: Path) -> dict:
    m = read_json_opt(run_dir / "manifest.json")
    return m if isinstance(m, dict) else {}


def _r2_path(path: Path) -> Path:
    name = path.name
    stem = name[: -len(".json")] if name.endswith(".json") else name
    return path.with_name(stem + ".r2.json")


def order_output(run_dir: Path, order: Mapping[str, Any]) -> Path | None:
    """The usable output file of a manifest order, or None.

    * ``status == "failed"``, or a ``resolved_output`` that is a ``.failed.json`` stub or does not
      exist -> None (the attempt-1 file was rejected by validation and must not be used);
    * ``resolved_output`` set -> that file (attempt 1 or the ``.r2.json`` retry);
    * not resolved yet (pending/dispatched) -> ``<output>.r2.json`` when present, else ``output``."""
    if str(order.get("status") or "") == "failed":
        return None
    resolved = order.get("resolved_output")
    if resolved:
        p = resolve_run_path(run_dir, resolved)
        if p is None or p.name.endswith(".failed.json"):
            return None
        return p
    out = order.get("output")
    if not out:
        return None
    raw = Path(str(out))
    for cand in (_r2_path(raw), raw):
        p = resolve_run_path(run_dir, cand.as_posix())
        if p is not None and not p.name.endswith(".failed.json"):
            return p
    return None


def verdict_files(run_dir: Path, phase: int) -> list[Path]:
    """Valid sector-verdict files of a phase. When the manifest has sector_head orders for the
    phase, ONLY their usable outputs are returned (``order_output``: a failed order or stub yields
    nothing, so a phase whose sector heads all failed has no verdicts). Only without such orders
    is ``phase<N>/*_verdict.json`` globbed (``.r2.json`` preferred)."""
    run_dir = Path(run_dir)
    out: list[Path] = []
    has_orders = False
    orders = (load_manifest(run_dir).get("orders") or {})
    if isinstance(orders, Mapping):
        for o in orders.values():
            if not isinstance(o, Mapping) or o.get("agent_role") != "sector_head":
                continue
            if common.safe_float(o.get("phase")) != phase:
                continue
            has_orders = True
            p = order_output(run_dir, o)
            if p is not None and p not in out:
                out.append(p)
    if has_orders:
        return out
    pdir = run_dir / f"phase{phase}"
    if not pdir.is_dir():
        return []
    by_stem: dict[str, Path] = {}
    for f in sorted(pdir.glob("*_verdict.json")):
        by_stem[f.name[: -len(".json")]] = f
    for f in sorted(pdir.glob("*_verdict.r2.json")):
        by_stem[f.name[: -len(".r2.json")]] = f
    return [by_stem[k] for k in sorted(by_stem)]


def _cio_doc_ok(doc: Any, mode: str) -> bool:
    if not isinstance(doc, Mapping) or doc.get("status") == "failed":
        return False
    if mode == "reconciliation":
        return isinstance(doc.get("final_actions"), list)
    return True


def resolve_cio(run_dir: Path, mode: str, manifest: Mapping[str, Any] | None = None
                ) -> tuple[dict | None, Path | None]:
    """The valid CIO document of ``mode`` (``allocation`` | ``reconciliation``) and its path.

    Located through manifest orders (agent_role ``cio`` with that mode -> ``order_output``: the
    ``resolved_output``, i.e. attempt 1 or the ``.r2.json`` retry; a failed order or stub means
    the document is missing). Without such an order: ``<default>.r2.json``, then the attempt-1
    default file (``phase1/cio_allocation.json`` / ``phase4/cio_final.json``)."""
    run_dir = Path(run_dir)
    if manifest is None:
        manifest = load_manifest(run_dir)
    cands: list[Path] = []
    has_order = False
    orders = manifest.get("orders") if isinstance(manifest, Mapping) else None
    if isinstance(orders, Mapping):
        for o in orders.values():
            if isinstance(o, Mapping) and o.get("agent_role") == "cio" and o.get("mode") == mode:
                has_order = True
                p = order_output(run_dir, o)
                if p is not None:
                    cands.append(p)
    if not has_order:
        phase, name = CIO_DEFAULT_FILES[mode]
        default = run_dir / phase / name
        cands = [_r2_path(default), default]
    for p in cands:
        doc = read_json_opt(p)
        if _cio_doc_ok(doc, mode):
            return dict(doc), p
    return None, None


def collect_verdicts(run_dir: Path) -> dict[str, dict]:
    """{ticker: verdict entry + "sector", "phase", "path"} from phase 2 and phase 3 verdicts."""
    out: dict[str, dict] = {}
    for phase in (2, 3):
        for f in verdict_files(run_dir, phase):
            doc = read_json_opt(f)
            if not isinstance(doc, Mapping) or doc.get("status") == "failed":
                continue
            for v in doc.get("verdicts") or []:
                if not isinstance(v, Mapping) or not v.get("ticker"):
                    continue
                entry = dict(v)
                entry["sector"] = doc.get("sector")
                entry["phase"] = doc.get("phase", phase)
                entry["path"] = f.as_posix()
                out[_norm(v["ticker"])] = entry
    return out


def collect_aggregates(run_dir: Path) -> dict[str, dict]:
    """{ticker: aggregate entry} from phase2/aggregate.json then phase3/aggregate.json
    (phase3/aggregate_light.json fills tickers that never reached the full wave)."""
    out: dict[str, dict] = {}
    for rel in ("phase3/aggregate_light.json", "phase2/aggregate.json", "phase3/aggregate.json"):
        doc = read_json_opt(Path(run_dir) / rel)
        if isinstance(doc, Mapping):
            for t, e in (doc.get("tickers") or {}).items():
                if isinstance(e, Mapping):
                    out[_norm(t)] = dict(e)
    return out


def _candidate_price(run_dir: Path, ticker: str, as_of: Any) -> dict | None:
    """phase3/data/technicals_<T>.json ``close`` else the cached series (lazy, never network)."""
    for phase in ("phase3", "phase2"):
        doc = read_json_opt(Path(run_dir) / phase / "data" / common.data_filename("technicals", ticker))
        if isinstance(doc, Mapping) and _num(doc.get("close")) is not None:
            return dict(doc["close"]) if common.is_fact(doc.get("close")) else \
                common.fact(_num(doc.get("close")), "USD/share", f"D:PX:{ticker}", doc.get("as_of") or as_of, "price")
    return _series_close(ticker, as_of)


def _series_close(ticker: str, as_of: Any) -> dict | None:
    try:
        from scripts import fetch_prices  # lazy: sibling unit
    except ImportError:
        return None
    try:
        df = fetch_prices.load_series(ticker)
    except (common.DataUnavailable, OSError, ValueError, KeyError):
        return None
    if df is None or getattr(df, "empty", True) or "Close" not in df:
        return None
    try:
        import pandas as pd
        d = common.parse_date(as_of) if as_of else None
        s = df["Close"].dropna()
        if d is not None:
            s = s[s.index <= pd.Timestamp(d)]
        if s.empty:
            return None
        return common.fact(float(s.iloc[-1]), "USD/share", f"D:PX:{_norm(ticker)}",
                           s.index[-1], "price", derivation="cached series close")
    except (TypeError, ValueError):
        return None


#: aggregate.py rule labels of a TRIM -> portfolio sizing reason (DESIGN §4.11: conviction TRIM sells
#: ``trim_fraction``; limit-breach / insufficient-evidence TRIM sizes to the cap / sector limit).
_TRIM_LABEL_REASON = {"trim_conviction": "conviction", "limit_breach": "breach", "insufficient_evidence": "breach"}


def _trim_reason(action: Any, agg: Mapping[str, Any] | None) -> str | None:
    """Sizing reason of a TRIM taken from the aggregate rule label when the rule itself said TRIM;
    None (sizing falls back to the current breach test) for TRIMs that come from a deviation or override."""
    if str(action or "").upper() != "TRIM" or not isinstance(agg, Mapping):
        return None
    if str(agg.get("rule_action") or "").upper() != "TRIM":
        return None
    return _TRIM_LABEL_REASON.get(str(agg.get("rule_label") or ""))


def _actions_from_verdicts(verdicts: Mapping[str, dict], aggregates: Mapping[str, dict],
                           held: set[str]) -> dict[str, dict]:
    acts: dict[str, dict] = {}
    for t in held:
        v = verdicts.get(t)
        if v is not None and v.get("action"):
            acts[t] = {"action": v["action"], "conviction": v.get("agg_conviction"),
                       "source": f"sector_verdict_p{v.get('phase', 2)}",
                       "trim_reason": _trim_reason(v["action"], aggregates.get(t))}
        elif t in aggregates and aggregates[t].get("rule_action"):
            acts[t] = {"action": aggregates[t]["rule_action"], "conviction": aggregates[t].get("agg_conviction"),
                       "source": "aggregate_rule",
                       "trim_reason": _trim_reason(aggregates[t]["rule_action"], aggregates[t])}
    for t, v in verdicts.items():
        if t in held or str(v.get("action") or "").upper() != "BUY":
            continue
        acts[t] = {"action": "BUY", "conviction": v.get("agg_conviction"), "sector": v.get("sector"),
                   "asset_type": "stock", "source": f"sector_verdict_p{v.get('phase', 3)}"}
    return acts


def run(run_dir: Path, *, final: bool) -> dict:
    """Build the preview (``final=False``: sector verdicts + CIO allocation targets ->
    phase4/portfolio_preview.json) or the final plan (``final=True``: cio_final final_actions +
    final_target_sector_weights -> phase4/portfolio_plan.json). Returns the written document."""
    run_dir = Path(run_dir)
    cfg = load_run_config("committee", run_dir)
    profile = load_run_config("investor_profile", run_dir)
    portfolio_cfg = load_run_config("portfolio", run_dir, required=False)
    manifest = load_manifest(run_dir)
    lookthrough = read_json_opt(run_dir / "phase1" / "lookthrough.json")
    if not isinstance(lookthrough, Mapping):
        raise common.DataUnavailable(f"{(run_dir / 'phase1' / 'lookthrough.json').as_posix()} missing or invalid")
    prices = read_json_opt(run_dir / "phase1" / "prices.json") or {}
    as_of = manifest.get("as_of") or prices.get("as_of") or lookthrough.get("as_of")
    held = {_norm(h.get("ticker")) for h in lookthrough.get("holdings") or [] if h.get("ticker")}
    verdicts = collect_verdicts(run_dir)
    aggregates = collect_aggregates(run_dir)
    extra_flags: list[dict] = []
    extra_missing: list[str] = []

    cio_final = resolve_cio(run_dir, "reconciliation", manifest)[0] if final else None
    if final and isinstance(cio_final, Mapping) and isinstance(cio_final.get("final_actions"), list):
        acts: dict[str, dict] = {}
        for fa in cio_final["final_actions"]:
            if not isinstance(fa, Mapping) or not fa.get("ticker"):
                continue
            t = _norm(fa["ticker"])
            v = verdicts.get(t, {})
            agg = aggregates.get(t, {})
            conv = v.get("agg_conviction", agg.get("agg_conviction"))
            acts[t] = {"action": fa.get("action"), "conviction": conv,
                       "source": "cio_override" if fa.get("override") else "cio_final",
                       "trim_reason": None if fa.get("override") else _trim_reason(fa.get("action"), agg),
                       "sector": v.get("sector") or agg.get("sector"),
                       "asset_type": agg.get("asset_type") or ("stock" if t not in held else None)}
        for t, a in _actions_from_verdicts(verdicts, aggregates, held).items():
            if t in held and t not in acts:
                acts[t] = a
                extra_missing.append(f"cio_final has no final action for {t}; sector verdict used")
        targets = cio_final.get("final_target_sector_weights") or {}
        schema_name, out = SCHEMA_PLAN, run_dir / "phase4" / "portfolio_plan.json"
    else:
        acts = _actions_from_verdicts(verdicts, aggregates, held)
        alloc = resolve_cio(run_dir, "allocation", manifest)[0]
        targets = (alloc or {}).get("target_sector_weights") if isinstance(alloc, Mapping) else None
        if not isinstance(targets, Mapping) or not targets:
            targets = {}
            extra_missing.append("CIO target sector weights (valid phase1 cio_allocation) unavailable")
            extra_flags.append(common.flag("cio_targets_missing", "medium",
                                           "no target_sector_weights; only profile limits applied"))
        if final:
            extra_flags.append(common.flag("cio_final_missing", "high",
                                           "no valid cio_final (missing, invalid or failed); "
                                           "sector verdicts used"))
            schema_name, out = SCHEMA_PLAN, run_dir / "phase4" / "portfolio_plan.json"
        else:
            schema_name, out = SCHEMA_PLAN, run_dir / "phase4" / "portfolio_preview.json"

    price_tickers = set((prices.get("tickers") or {}).keys())
    extra_prices: dict[str, Any] = {}
    for t, a in acts.items():
        if t in held or str(a.get("action") or "").upper() != "BUY" or t in price_tickers:
            continue
        pf = _candidate_price(run_dir, t, as_of)
        if pf is not None:
            extra_prices[t] = pf

    plan = build_plan(lookthrough=dict(lookthrough), prices=prices, actions=acts,
                      target_sector_weights=targets, profile=profile, cfg=cfg,
                      portfolio_cfg=portfolio_cfg, extra_prices=extra_prices,
                      schema_name=schema_name, as_of=as_of)
    plan["flags"] = extra_flags + list(plan.get("flags") or [])
    for m in extra_missing:
        if m not in plan["missing_data"]:
            plan["missing_data"].append(m)
    common.write_json(out, plan)
    return plan


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    ap = argparse.ArgumentParser(description="Portfolio preview / final rebalancing plan (DESIGN §4.11).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="run directory (runs/<run_id>)")
    g.add_argument("--run", help="run id under the runs dir")
    ap.add_argument("--final", action="store_true",
                    help="use phase4/cio_final.json and write portfolio_plan.json (else preview)")
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    try:
        common.apply_common_args(args)
        run_dir = Path(args.run_dir) if args.run_dir else common.runs_dir() / args.run
        plan = run(run_dir, final=args.final)
    except (common.AginvError, OSError, ValueError) as exc:
        print(f"portfolio.py: error: {exc}", file=sys.stderr)
        return 1
    name = "portfolio_plan.json" if args.final else "portfolio_preview.json"
    out = (run_dir / "phase4" / name).as_posix()
    trades = sum(1 for p in plan["positions"] if p["trade"]["side"] != "NONE")
    print(common.dumps_json({"out": out, "trades": trades,
                             "checks": {c["check"]: c["status"] for c in plan["constraint_checks"]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
