"""Valuation grid (DESIGN §4.4) -> ``valuation_<T>.json`` (``valuation/v1``).

Implements the course method: reverse DCF first, then a NOPAT DCF with an exit multiple
(r 9-10 %), cross-checked with configured multiple bands.

* Base cash flows: ``nopat = (EBITDA_ttm - capex_ttm) x (1 - tax)`` ("course NOPAT", tax =
  fundamentals ``valuation.tax_rate_used`` else ``valuation.default_tax_rate``, flagged) and
  ``fcf = FCF_ttm``.
* DCF cells: base in {nopat, fcf} x r in ``valuation.discount_rates`` x g in
  ``valuation.growth_tiers``; ``valuation.years`` years; exit multiple from
  ``valuation.exit_multiple_by_growth`` (first ``[g_max, multiple]`` with g <= g_max):
  ``EV = sum_t CF*(1+g)^t/(1+r)^t + CF_N*M/(1+r)^N``; ``equity = EV - net_debt``;
  ``fv/share = equity / shares``; ``tv_share = PV(TV)/EV`` (flag ``tv_gt_90`` when above
  ``valuation.tv_share_flag_threshold``). Base CF <= 0 -> the cell is ``na``.
* Net debt basis (``net_debt_basis``): ``consolidated`` (default), ``captive_excluded`` or ``override``.
  Tickers in ``valuation.captive_finance_tickers`` (Caterpillar, Deere, Ford, GM, PACCAR ...) carry the
  debt of a captive finance subsidiary in consolidated net debt; that debt funds finance receivables
  (an asset that earns its own interest, already inside operating income net of interest expense), so
  deducting it from an industrial NOPAT enterprise value understates equity value. For them net debt
  is 0 (equity value = EV; flag ``captive_finance_debt_excluded``) unless
  ``valuation.captive_finance_net_debt_pct_override[<T>]`` gives the industrial fraction of
  consolidated net debt to deduct. The same net debt feeds the EV/EBIT cells and the reverse DCF.
  Cell ids: ``DCF-NOPAT-r10-g06`` (r and g in whole percent, two digits).
* Multiple cells from ``valuation.multiple_bands[<sector>]`` (configured ASSUMPTIONS, not market
  data; source id ``D:CFG:valuation.multiple_bands``): default sectors ``pe`` + ``ev_ebit``,
  financials ``pe`` + ``p_b``. Ids ``PE-low|mid|high``, ``EVEBIT-...``, ``PB-...``.
* Reverse DCF: bisection on g in ``valuation.reverse_dcf_growth_bounds`` with base nopat,
  r = ``valuation.reverse_dcf_discount_rate`` and exit multiple
  ``valuation.reverse_dcf_exit_multiple`` -> ``implied_growth_pct`` (null when no sign change).

API::

    build_grid(fundamentals, price, sector, cfg=None, *, as_of=None, out_path=None) -> dict
    run(ticker, fundamentals_path, out, *, sector, as_of=None, price=None) -> dict

CLI::

    python scripts/valuation.py --ticker KO --fundamentals fundamentals_KO.json --sector consumer_staples --out valuation_KO.json
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Mapping, Sequence  # noqa: E402

from scripts.risk_reward import round_half_up, to_decimal  # noqa: E402

#: Fallbacks used only for keys absent from committee.yaml ``valuation`` (they mirror it).
DEFAULTS: dict[str, Any] = {
    "discount_rates": [0.09, 0.10],
    "years": 5,
    "growth_tiers": [0.00, 0.03, 0.06, 0.10, 0.15],
    "exit_multiple_by_growth": [[0.00, 10], [0.04, 12], [0.07, 15], [0.11, 18], [1.00, 22]],
    "reverse_dcf_exit_multiple": 15,
    "reverse_dcf_discount_rate": 0.10,
    "reverse_dcf_growth_bounds": [-0.30, 0.60],
    "default_tax_rate": 0.21,
    "tv_share_flag_threshold": 0.9,
    "multiple_bands": {},
    "captive_finance_tickers": ["CAT", "DE", "F", "GM", "PCAR", "HOG", "CNH"],
    "captive_finance_net_debt_pct_override": {},
    "possible_captive_net_debt_to_mcap": 0.25,
}

#: Net-debt bases reported in ``net_debt_basis``.
NET_DEBT_BASES: tuple[str, ...] = ("consolidated", "captive_excluded", "override")
#: Sectors where an unlisted company may still own a captive finance arm (generic safeguard,
#: ``possible_captive_finance`` in fetch_fundamentals).
POSSIBLE_CAPTIVE_SECTORS: tuple[str, ...] = ("industrials", "consumer_discretionary")

METHODS: tuple[str, ...] = ("dcf_nopat", "dcf_fcf", "pe", "ev_ebit", "p_b")
MULTIPLE_METHODS: tuple[str, ...] = ("pe", "ev_ebit", "p_b")
MULTIPLE_ID_PREFIX = {"pe": "PE", "ev_ebit": "EVEBIT", "p_b": "PB"}
BANDS = ("low", "mid", "high")
CFG_SOURCE_ID = "D:CFG:valuation.multiple_bands"

FV_DP = 2          # fv_per_share rounding (USD/share)
RATIO_DP = 4       # tv_share rounding
MONEY_DP = 2       # USD amounts in params
BISECTION_ITERATIONS = 200
BISECTION_TOL = 1e-9

#: fundamentals flags worth repeating on the valuation file (they change the inputs).
PROPAGATED_FLAGS = ("share_count_ambiguous", "companyfacts_lag", "tag_mix", "mixed_instants",
                    "stb_assumed_zero", "default_tax_rate", "possible_captive_finance")

NA_BASE_NOT_POSITIVE = "base_cf_not_positive"
NA_EQUITY_NOT_POSITIVE = "equity_value_not_positive"


# =====================================================================================
# config + small helpers
# =====================================================================================


def resolve_cfg(cfg: Mapping[str, Any] | None = None) -> dict:
    """``valuation`` section (with defaults). ``cfg`` = whole committee config, the section
    itself, or None (committee.yaml from the active config dir)."""
    if cfg is None:
        try:
            cfg = common.committee_cfg()
        except common.ConfigError:
            cfg = {}
    section = cfg.get("valuation") if isinstance(cfg, Mapping) and isinstance(cfg.get("valuation"), Mapping) else cfg
    out = dict(DEFAULTS)
    out.update({k: v for k, v in dict(section or {}).items() if v is not None})
    return out


def _get(doc: Any, *path: str) -> Any:
    cur = doc
    for p in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(p)
    return cur


def _fact_val(doc: Any, *path: str) -> float | None:
    return common.fact_value(_get(doc, *path))


def _fact_attr(doc: Any, attr: str, *path: str) -> Any:
    node = _get(doc, *path)
    return node.get(attr) if isinstance(node, Mapping) else None


def exit_multiple(g: float, table: Sequence[Sequence[Any]]) -> float:
    """First ``[g_max, multiple]`` pair with ``g <= g_max`` wins (compared as decimals);
    beyond the last threshold the last multiple applies."""
    gd = to_decimal(g)
    pairs = [(to_decimal(p[0]), float(p[1])) for p in table]
    for gmax, mult in pairs:
        if gd is not None and gmax is not None and gd <= gmax:
            return mult
    return pairs[-1][1]


def rate_token(x: float) -> str:
    """``0.10 -> '10'``, ``0.09 -> '09'``, ``0.0 -> '00'``, ``0.095 -> '09p5'``, ``-0.05 -> 'm05'``."""
    pct = (to_decimal(x) or Decimal(0)) * 100
    sign = "m" if pct < 0 else ""
    pct = abs(pct)
    if pct == pct.to_integral_value():
        return f"{sign}{int(pct):02d}"
    whole = int(pct)
    frac = format(pct - whole, "f").split(".")[1].rstrip("0") or "0"
    return f"{sign}{whole:02d}p{frac}"


def dcf_cell_id(base: str, r: float, g: float) -> str:
    """``DCF-NOPAT-r10-g06`` style id."""
    return f"DCF-{base.upper()}-r{rate_token(r)}-g{rate_token(g)}"


def multiple_cell_id(method: str, band: str) -> str:
    return f"{MULTIPLE_ID_PREFIX[method]}-{band}"


def dcf_value(cf0: float, r: float, g: float, years: int, multiple: float) -> dict:
    """Enterprise value of a growing cash flow plus an exit multiple on year-N cash flow.

    Returns ``{"ev","pv_cf","pv_tv","tv_share","cf_n"}``."""
    pv_cf = 0.0
    for t in range(1, int(years) + 1):
        pv_cf += cf0 * (1.0 + g) ** t / (1.0 + r) ** t
    cf_n = cf0 * (1.0 + g) ** int(years)
    pv_tv = cf_n * multiple / (1.0 + r) ** int(years)
    ev = pv_cf + pv_tv
    tv_share = pv_tv / ev if ev else None
    return {"ev": ev, "pv_cf": pv_cf, "pv_tv": pv_tv, "tv_share": tv_share, "cf_n": cf_n}


def fair_value_per_share(cf0: float, r: float, g: float, years: int, multiple: float,
                         net_debt: float, shares: float) -> float:
    return (dcf_value(cf0, r, g, years, multiple)["ev"] - net_debt) / shares


def reverse_dcf_growth(cf0: float | None, price: float | None, net_debt: float | None,
                       shares: float | None, *, r: float, multiple: float, years: int,
                       bounds: Sequence[float]) -> tuple[float | None, str | None]:
    """Growth g at which the DCF fair value equals ``price`` (bisection). Returns
    ``(g, None)`` or ``(None, reason)``."""
    if cf0 is None or price is None or net_debt is None or shares is None:
        return None, "missing"
    if cf0 <= 0:
        return None, NA_BASE_NOT_POSITIVE
    if shares <= 0 or price <= 0:
        return None, "missing"
    lo, hi = float(bounds[0]), float(bounds[1])

    def f(g: float) -> float:
        return fair_value_per_share(cf0, r, g, years, multiple, net_debt, shares) - price

    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo, None
    if fhi == 0:
        return hi, None
    if (flo < 0) == (fhi < 0):
        return None, "no_sign_change"
    for _ in range(BISECTION_ITERATIONS):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if fm == 0 or (hi - lo) / 2.0 < BISECTION_TOL:
            return mid, None
        if (fm < 0) == (flo < 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2.0, None


def _tax_rate(fundamentals: Mapping[str, Any], vcfg: Mapping[str, Any]) -> tuple[float, bool]:
    """Effective tax rate as a fraction; ``(rate, used_default)``."""
    node = _get(fundamentals, "valuation", "tax_rate_used")
    v = common.fact_value(node)
    if v is not None:
        unit = str(node.get("unit") or "").lower() if isinstance(node, Mapping) else ""
        rate = v / 100.0 if (unit == "pct" or v > 1.0) else v
        if 0.0 <= rate < 1.0:
            return rate, False
    return float(vcfg["default_tax_rate"]), True


def _net_debt(fundamentals: Mapping[str, Any]) -> tuple[float | None, dict | None]:
    nd_node = _get(fundamentals, "balance", "net_debt")
    nd = common.fact_value(nd_node)
    if nd is not None:
        return nd, dict(nd_node)
    debt = _fact_val(fundamentals, "balance", "total_debt")
    cash = _fact_val(fundamentals, "balance", "cash")
    sti = _fact_val(fundamentals, "balance", "st_investments")
    if debt is not None and cash is not None:
        value = debt - cash - (sti or 0.0)
        src = _fact_attr(fundamentals, "source_id", "balance", "total_debt")
        as_of = _fact_attr(fundamentals, "as_of", "balance", "total_debt")
        return value, common.fact(value, "USD", src, as_of, "fundamental",
                                  derivation="total_debt - cash - st_investments")
    return None, (dict(nd_node) if isinstance(nd_node, Mapping) else None)


def _ticker_set(values: Any) -> set[str]:
    out: set[str] = set()
    for v in values or []:
        try:
            out.add(common.norm_ticker(str(v)))
        except ValueError:
            continue
    return out


def captive_tickers(vcfg: Mapping[str, Any]) -> set[str]:
    """Normalized ``valuation.captive_finance_tickers`` (shared with fetch_fundamentals)."""
    return _ticker_set(vcfg.get("captive_finance_tickers"))


def captive_override(ticker: str, vcfg: Mapping[str, Any]) -> tuple[float | None, bool]:
    """``(fraction, invalid)`` from ``valuation.captive_finance_net_debt_pct_override[<T>]``: the share
    of consolidated net debt that is industrial. ``invalid`` = an entry exists but is not a number in
    [0, 1] (it is then ignored)."""
    raw = vcfg.get("captive_finance_net_debt_pct_override") or {}
    if not isinstance(raw, Mapping):
        return None, False
    for key, val in raw.items():
        try:
            if common.norm_ticker(str(key)) != ticker:
                continue
        except ValueError:
            continue
        frac = common.safe_float(val)
        if frac is None or not 0.0 <= frac <= 1.0:
            return None, True
        return frac, False
    return None, False


def net_debt_basis(ticker: str, vcfg: Mapping[str, Any]) -> tuple[str, float | None, bool]:
    """``(basis, override_fraction, override_invalid)`` for ``ticker``: ``override`` when a valid
    fraction is configured, ``captive_excluded`` when the ticker is on the captive list, else
    ``consolidated``."""
    frac, invalid = captive_override(ticker, vcfg)
    if frac is not None:
        return "override", frac, False
    if ticker in captive_tickers(vcfg):
        return "captive_excluded", None, invalid
    return "consolidated", None, invalid


def _usd_b(x: float | None) -> str:
    return "n/a" if x is None else f"${x / 1e9:,.1f}B"


def _copy_fact(node: Any, unit: str, source_id: str | None, as_of: Any, kind: str) -> dict:
    if common.is_fact(node):
        out = dict(node)
        if out.get("value") is None and not out.get("na_reason"):
            out["na_reason"] = "missing"
        return out
    return common.na_fact(unit, source_id, as_of, kind)


def _na_cell(cell_id: str, method: str, params: dict, reason: str, flags: list | None = None) -> dict:
    return {"id": cell_id, "method": method, "params": params, "fv_per_share": None,
            "tv_share": None, "flags": list(flags or []), "na_reason": reason}


# =====================================================================================
# grid
# =====================================================================================


def build_grid(fundamentals: Mapping[str, Any], price: float | None, sector: str,
               cfg: Mapping[str, Any] | None = None, *, as_of: Any = None,
               out_path: str | Path | None = None) -> dict:
    """Build the ``valuation/v1`` document for one ticker from its ``fundamentals/v1`` doc."""
    vcfg = resolve_cfg(cfg)
    ticker = common.norm_ticker(fundamentals.get("ticker") or "UNKNOWN")
    run_as_of = common.date_str(as_of) or common.date_str(fundamentals.get("as_of")) or common.today().isoformat()
    cik = fundamentals.get("cik")
    sector_class = (_get(fundamentals, "entity", "sector_class")
                    or common.sector_class(sector, "stock"))
    if sector == "financials":
        sector_class = "financials"
    flags: list[dict] = []
    missing: list[str] = []

    # ---------------------------------------------------------------- inputs
    px_node = fundamentals.get("price")
    if price is not None and common.safe_float(price) is not None:
        px_value = float(price)
        price_fact = common.fact(px_value, "USD/share", f"D:PX:{ticker}", run_as_of, "price")
    else:
        px_value = common.fact_value(px_node)
        price_fact = _copy_fact(px_node, "USD/share", f"D:PX:{ticker}", run_as_of, "price")
    shares_fact = _copy_fact(fundamentals.get("shares"), "shares", f"D:SEC:{ticker}:CF", None, "fundamental")
    shares = common.fact_value(shares_fact)
    if shares is not None and shares <= 0:
        shares = None
    net_debt_cons, nd_fact = _net_debt(fundamentals)
    net_debt_consolidated_fact = _copy_fact(nd_fact, "USD", f"D:SEC:{ticker}:CF", None, "fundamental")
    basis, nd_fraction, override_invalid = net_debt_basis(ticker, vcfg)
    val_id = f"D:VAL:{ticker}"
    if basis == "consolidated":
        net_debt, net_debt_fact = net_debt_cons, net_debt_consolidated_fact
    else:
        if basis == "captive_excluded":
            net_debt = 0.0
            nd_deriv = "captive finance: consolidated net debt not deducted (industrial net debt taken as 0)"
        else:
            net_debt = net_debt_cons * nd_fraction if (net_debt_cons is not None and nd_fraction is not None) else None
            nd_deriv = f"consolidated net debt x {nd_fraction:g} (valuation.captive_finance_net_debt_pct_override)"
        nd_note = f"net_debt_basis={basis}; consolidated net debt {_usd_b(net_debt_cons)} includes captive finance debt"
        net_debt_fact = (common.fact(net_debt, "USD", val_id, run_as_of, "other", derivation=nd_deriv, note=nd_note)
                         if net_debt is not None else
                         common.na_fact("USD", val_id, run_as_of, "other", "missing", derivation=nd_deriv, note=nd_note))
    if override_invalid:
        flags.append(common.flag("captive_finance_override_invalid", "low",
                                 "valuation.captive_finance_net_debt_pct_override has an entry for "
                                 f"{ticker} that is not a number in [0, 1]; ignored"))

    ebitda = _fact_val(fundamentals, "ttm", "ebitda")
    capex_raw = _fact_val(fundamentals, "ttm", "capex")
    capex = abs(capex_raw) if capex_raw is not None else None
    fcf = _fact_val(fundamentals, "ttm", "fcf")
    net_income = _fact_val(fundamentals, "ttm", "net_income")
    ebit = _fact_val(fundamentals, "ttm", "operating_income")
    equity = _fact_val(fundamentals, "balance", "equity")
    tax, tax_default = _tax_rate(fundamentals, vcfg)
    if tax_default:
        flags.append(common.flag("default_tax_rate", "low",
                                 f"tax_rate_used unavailable; default {tax:.2%} applied to NOPAT"))

    ebitda_node = _get(fundamentals, "ttm", "ebitda")
    if ebitda is not None and capex is not None:
        nopat = (ebitda - capex) * (1.0 - tax)
        nopat_fact = common.fact(
            nopat, "USD", val_id,
            _fact_attr(fundamentals, "as_of", "ttm", "ebitda") or run_as_of, "fundamental",
            filed=_fact_attr(fundamentals, "filed", "ttm", "ebitda"),
            derivation=f"(EBITDA_ttm - capex_ttm) x (1 - {round(tax, 4)})")
    else:
        nopat = None
        gaps = [n for n, v in (("EBITDA TTM", ebitda), ("capex TTM", capex)) if v is None]
        nopat_fact = common.na_fact("USD", val_id, _fact_attr(fundamentals, "as_of", "ttm", "ebitda") or run_as_of,
                                    "fundamental", "missing", note="missing " + ", ".join(gaps))
        missing.append(f"{ticker}: course NOPAT not computable ({', '.join(gaps)} missing)")
    fcf_fact = _copy_fact(_get(fundamentals, "ttm", "fcf"), "USD", f"D:SEC:{ticker}:CF", None, "fundamental")
    if fcf is None:
        missing.append(f"{ticker}: FCF TTM missing")
    if shares is None:
        missing.append(f"{ticker}: share count missing - no per-share values")
    if net_debt is None:
        missing.append(f"{ticker}: net debt missing - EV-based cells not computable")
    if basis != "consolidated":
        if basis == "captive_excluded":
            detail = (f"{ticker} is on valuation.captive_finance_tickers: consolidated net debt "
                      f"({_usd_b(net_debt_cons)}) includes the captive finance subsidiary's debt, which funds "
                      "finance receivables, so it is NOT deducted (equity value = enterprise value). Industrial "
                      "net debt and cash are ignored (not separable in SEC companyfacts), so fair value may be "
                      "slightly high if the industrial business carries net debt; check the segment data in the 10-K")
            gap = f"{ticker}: industrial net debt/cash not separable from consolidated XBRL (captive finance) - net debt taken as 0"
        else:
            detail = (f"{ticker}: only {nd_fraction:.0%} of consolidated net debt ({_usd_b(net_debt_cons)}) is "
                      "deducted (valuation.captive_finance_net_debt_pct_override) because the rest is captive "
                      "finance debt funding finance receivables; the fraction is a configured assumption, not "
                      "a reported figure")
            gap = f"{ticker}: net debt deducted at {nd_fraction:.0%} of consolidated (configured captive finance override)"
        flags.append(common.flag("captive_finance_debt_excluded", "medium", detail))
        missing.append(gap)
    if px_value is None:
        missing.append(f"{ticker}: price missing - reverse DCF not computable")
    if sector_class == "financials":
        flags.append(common.flag("dcf_financials_caution", "medium",
                                 "EBITDA/FCF-based DCF and net debt are of limited meaning for financials; "
                                 "prefer the P/E and P/B cells"))

    # ---------------------------------------------------------------- DCF cells
    years = int(vcfg["years"])
    table = vcfg["exit_multiple_by_growth"]
    tv_thr = float(vcfg["tv_share_flag_threshold"])
    cells: list[dict] = []
    tv_flagged: list[str] = []
    for base_name, cf0 in (("nopat", nopat), ("fcf", fcf)):
        method = f"dcf_{base_name}"
        for r in vcfg["discount_rates"]:
            for g in vcfg["growth_tiers"]:
                r_f, g_f = float(r), float(g)
                mult = exit_multiple(g_f, table)
                cid = dcf_cell_id(base_name, r_f, g_f)
                params: dict[str, Any] = {"base": base_name, "base_cf": round_half_up(cf0, MONEY_DP),
                                          "discount_rate": r_f, "growth": g_f, "years": years,
                                          "exit_multiple": mult,
                                          "net_debt": round_half_up(net_debt, MONEY_DP), "shares": shares}
                cell_flags = ["financials_low_reliability"] if sector_class == "financials" else []
                if cf0 is None or shares is None or net_debt is None:
                    cells.append(_na_cell(cid, method, params, "missing", cell_flags))
                    continue
                if cf0 <= 0:
                    cells.append(_na_cell(cid, method, params, NA_BASE_NOT_POSITIVE, cell_flags))
                    continue
                v = dcf_value(cf0, r_f, g_f, years, mult)
                equity_value = v["ev"] - net_debt
                params.update({"ev": round_half_up(v["ev"], MONEY_DP), "pv_cash_flows": round_half_up(v["pv_cf"], MONEY_DP),
                               "pv_terminal": round_half_up(v["pv_tv"], MONEY_DP),
                               "equity_value": round_half_up(equity_value, MONEY_DP)})
                tv_share = round_half_up(v["tv_share"], RATIO_DP) if v["tv_share"] is not None else None
                if v["tv_share"] is not None and v["tv_share"] > tv_thr:
                    cell_flags.append("tv_gt_90")
                    tv_flagged.append(cid)
                if equity_value <= 0:
                    cell = _na_cell(cid, method, params, NA_EQUITY_NOT_POSITIVE, cell_flags)
                    cell["tv_share"] = tv_share
                    cells.append(cell)
                    continue
                cells.append({"id": cid, "method": method, "params": params,
                              "fv_per_share": round_half_up(equity_value / shares, FV_DP),
                              "tv_share": tv_share, "flags": cell_flags, "na_reason": None})
    if tv_flagged:
        flags.append(common.flag("tv_gt_90", "low",
                                 f"terminal value > {tv_thr:.0%} of EV in {len(tv_flagged)} cell(s): "
                                 + ", ".join(tv_flagged)))

    # ---------------------------------------------------------------- multiple cells
    bands_cfg = (vcfg.get("multiple_bands") or {}).get(sector) or {}
    if not bands_cfg:
        missing.append(f"{ticker}: no multiple_bands configured for sector {sector!r}")
    eps = net_income / shares if (net_income is not None and shares) else None
    bvps = equity / shares if (equity is not None and shares) else None
    for method in MULTIPLE_METHODS:
        trio = bands_cfg.get(method)
        if not trio:
            continue
        if method == "ev_ebit" and sector_class == "financials":
            continue  # EV is not meaningful for financials (DESIGN §4.3 step 11)
        for band, mult in zip(BANDS, trio):
            m = float(mult)
            cid = multiple_cell_id(method, band)
            params = {"multiple": m, "band": band, "assumption": True, "source_id": CFG_SOURCE_ID,
                      "label": f"configured {method} band ({band}) for {sector} - an assumption, not market data"}
            if method == "pe":
                params.update({"net_income_ttm": round_half_up(net_income, MONEY_DP), "shares": shares,
                               "eps_ttm": round_half_up(eps, 4)})
                if eps is None:
                    cells.append(_na_cell(cid, method, params, "missing"))
                elif eps <= 0:
                    cells.append(_na_cell(cid, method, params, "negative_earnings"))
                else:
                    cells.append({"id": cid, "method": method, "params": params,
                                  "fv_per_share": round_half_up(m * eps, FV_DP), "tv_share": None,
                                  "flags": [], "na_reason": None})
            elif method == "ev_ebit":
                params.update({"ebit_ttm": round_half_up(ebit, MONEY_DP), "net_debt": round_half_up(net_debt, MONEY_DP),
                               "shares": shares})
                if ebit is None or net_debt is None or shares is None:
                    cells.append(_na_cell(cid, method, params, "missing"))
                elif ebit <= 0:
                    cells.append(_na_cell(cid, method, params, "negative_ebit"))
                else:
                    eq = m * ebit - net_debt
                    params["equity_value"] = round_half_up(eq, MONEY_DP)
                    if eq <= 0:
                        cells.append(_na_cell(cid, method, params, NA_EQUITY_NOT_POSITIVE))
                    else:
                        cells.append({"id": cid, "method": method, "params": params,
                                      "fv_per_share": round_half_up(eq / shares, FV_DP), "tv_share": None,
                                      "flags": [], "na_reason": None})
            else:  # p_b
                params.update({"equity": round_half_up(equity, MONEY_DP), "shares": shares,
                               "book_value_per_share": round_half_up(bvps, 4)})
                if bvps is None:
                    cells.append(_na_cell(cid, method, params, "missing"))
                elif bvps <= 0:
                    cells.append(_na_cell(cid, method, params, "negative_book_value"))
                else:
                    cells.append({"id": cid, "method": method, "params": params,
                                  "fv_per_share": round_half_up(m * bvps, FV_DP), "tv_share": None,
                                  "flags": [], "na_reason": None})

    # ---------------------------------------------------------------- reverse DCF
    rev_r = float(vcfg["reverse_dcf_discount_rate"])
    rev_m = float(vcfg["reverse_dcf_exit_multiple"])
    bounds = [float(b) for b in vcfg["reverse_dcf_growth_bounds"]]
    g_impl, why = reverse_dcf_growth(nopat, px_value, net_debt, shares, r=rev_r, multiple=rev_m,
                                     years=years, bounds=bounds)
    assumptions = {"base": "nopat", "base_cf": round_half_up(nopat, MONEY_DP), "discount_rate": rev_r,
                   "exit_multiple": rev_m, "years": years, "growth_bounds": bounds, "price": px_value,
                   "net_debt": round_half_up(net_debt, MONEY_DP), "net_debt_basis": basis,
                   "shares": shares, "method": "bisection"}
    if g_impl is not None:
        implied = common.fact(round_half_up(g_impl * 100.0, 2), "pct", val_id, run_as_of, "other",
                              derivation="reverse DCF: bisection on g so that DCF fv/share = price")
    else:
        reason_map = {"missing": "missing", NA_BASE_NOT_POSITIVE: "not_meaningful",
                      "no_sign_change": "not_meaningful"}
        note = {"missing": "inputs missing", NA_BASE_NOT_POSITIVE: "base NOPAT <= 0",
                "no_sign_change": f"no sign change for g in [{bounds[0]:.0%}, {bounds[1]:.0%}]"}.get(why or "", why)
        implied = common.na_fact("pct", val_id, run_as_of, "other", reason_map.get(why or "", "missing"), note=note)
        assumptions["null_reason"] = why

    methods_available = [m for m in METHODS
                         if any(c["method"] == m and c["fv_per_share"] is not None for c in cells)]

    # ---------------------------------------------------------------- provenance
    for f in fundamentals.get("flags") or []:
        if isinstance(f, Mapping) and f.get("code") in PROPAGATED_FLAGS and f.get("severity") in common.SEVERITIES:
            flags.append(common.flag(str(f["code"]), str(f["severity"]),
                                     "from fundamentals: " + str(f.get("detail") or "")))
    sources: list[dict] = []
    try:
        val_url = None if cik is not None else _fundamentals_cf_url(fundamentals, ticker)
        sources.append(common.dataset_source(val_id, cik=cik, url=val_url, path=out_path,
                                             title=f"Valuation grid for {ticker} derived from SEC companyfacts"))
    except ValueError:
        sources.append(common.source(val_id, common.yf_quote_url(ticker),
                                     f"Valuation grid for {ticker} (no SEC companyfacts)", kind="dataset",
                                     path=out_path))
    for s in fundamentals.get("sources") or []:
        if isinstance(s, Mapping) and s.get("id") and s.get("url"):
            sources.append(dict(s))
    if price_fact.get("source_id") == f"D:PX:{ticker}" and not any(s.get("id") == f"D:PX:{ticker}" for s in sources):
        sources.append(common.dataset_source(f"D:PX:{ticker}"))
    cfg_url = vcfg.get("source_url") or vcfg.get("multiple_bands_source_url")
    if cfg_url:
        try:
            sources.append(common.dataset_source(CFG_SOURCE_ID, url=str(cfg_url)))
        except ValueError:
            pass
    if bands_cfg:
        flags.append(common.flag("multiple_bands_are_assumptions", "low",
                                 f"P/E, EV/EBIT and P/B cells use configured bands for {sector} "
                                 "(committee.yaml valuation.multiple_bands) - assumptions, not market data"))

    return common.envelope(
        "valuation", run_as_of, sources=sources, missing_data=missing, flags=flags,
        ticker=ticker, sector=sector, sector_class=sector_class, price=price_fact, shares=shares_fact,
        net_debt=net_debt_fact, net_debt_basis=basis, net_debt_consolidated=net_debt_consolidated_fact,
        tax_rate=common.fact(tax, "fraction", val_id, run_as_of, "other",
                                                     note="default" if tax_default else "fundamentals tax_rate_used"),
        base_cf={"nopat": nopat_fact, "fcf": fcf_fact},
        reverse_dcf={"implied_growth_pct": implied, "assumptions": assumptions},
        cells=cells, methods_available=methods_available)


def _fundamentals_cf_url(fundamentals: Mapping[str, Any], ticker: str) -> str | None:
    for s in fundamentals.get("sources") or []:
        if isinstance(s, Mapping) and s.get("id") == f"D:SEC:{ticker}:CF" and s.get("url"):
            return str(s["url"])
    return None


def run(ticker: str, fundamentals_path: str | Path, out: str | Path, *, sector: str,
        as_of: str | None = None, price: float | None = None) -> dict:
    """Read ``fundamentals_<T>.json``, build the grid and write ``out``."""
    fundamentals = common.read_json(fundamentals_path)
    t = common.norm_ticker(ticker)
    if fundamentals.get("ticker") and common.norm_ticker(fundamentals["ticker"]) != t:
        raise ValueError(f"{fundamentals_path}: ticker {fundamentals['ticker']!r} does not match {t!r}")
    fundamentals = dict(fundamentals)
    fundamentals.setdefault("ticker", t)
    doc = build_grid(fundamentals, price, sector, as_of=as_of, out_path=Path(out))
    common.write_json(out, doc)
    return doc


# =====================================================================================
# CLI
# =====================================================================================


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Course-method valuation grid (DESIGN 4.4).")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--fundamentals", required=True, help="fundamentals_<T>.json")
    parser.add_argument("--sector", required=True, help="GICS sector key (multiple bands)")
    parser.add_argument("--out", required=True, help="valuation_<T>.json to write")
    parser.add_argument("--price", type=float, default=None, help="override the price (USD/share)")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
    except common.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    price = args.price if (args.price is not None and math.isfinite(args.price)) else None
    doc = run(args.ticker, args.fundamentals, args.out, sector=args.sector, as_of=args.as_of, price=price)
    summary = {"out": Path(args.out).as_posix(), "ticker": doc["ticker"],
               "cells": len(doc["cells"]), "methods_available": doc["methods_available"],
               "implied_growth_pct": doc["reverse_dcf"]["implied_growth_pct"]["value"]}
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
