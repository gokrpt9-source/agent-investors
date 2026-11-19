"""Programmatic authors of the dry-run agent outputs (used by ``build_fixtures.py agents``).

``build_agent_outputs(fixtures_root)`` runs the REAL dry-run (``committee.run_dry_run``) with
``committee.simulate_agents`` replaced by ``Builder.batch``: every work order of a batch is
answered by a role author below that reads ONLY the order's own input files (so every
``data_ref``, valuation cell, technical level, aggregate value and look-through weight matches the
run), validates it with ``validate.validate_order_output_report`` and writes it

* into the run (the order's output path), and
* to ``agent_outputs/<order_id>.json`` with the run dir replaced by ``${RUN_DIR}`` and the repo
  root by ``${REPO_ROOT}`` (``committee.simulate_agents`` substitutes them back).

All prose is SYNTHETIC placeholder research written for the offline pipeline test — paraphrased,
neutral and not investment advice. Every number is copied from the run's data files.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import committee, common, validate  # noqa: E402

AS_OF = "2026-10-02"
SCHEMA_VERSION = "1.0"

# ================================================================================================
# Narrative profiles (synthetic). stance/conviction per role drive the aggregate rule outcomes:
#   KO HOLD (OSINT high flag answered by the DA) · PG rule SELL via DA "broken" -> sector head
#   HOLD (rule deviation + da_rebuttal) · JPM TRIM (DA "damaged") · BRK-B HOLD · QQQ HOLD ·
#   SPMO ADD · health_care: one BUY (MRK), the other finalists PASS.
# ================================================================================================

COMPANY: dict[str, dict[str, Any]] = {
    "KO": dict(name="Coca-Cola", biz="asset-light concentrate and brand franchise",
               moat="global brand portfolio and bottler distribution network",
               risk="volume pressure from changing consumer habits and currency translation",
               fund=("bearish", 6, "g06"), tech=None, osint=("neutral", 5), oper=("bullish", 7),
               da="survives", flag_status="mitigated"),
    "PG": dict(name="Procter & Gamble", biz="diversified household and personal-care brands",
               moat="category-leading brands, retail shelf access and scale in advertising",
               risk="private-label share gains and commodity input costs",
               fund=("bullish", 8, "g06"), tech=None, osint=("neutral", 5), oper=("bullish", 8),
               da="broken"),
    "JPM": dict(name="JPMorgan Chase", biz="universal bank with consumer, corporate and markets franchises",
                moat="deposit franchise scale and diversified fee businesses",
                risk="credit normalisation and lower net interest income as policy rates fall",
                fund=("bearish", 7, None), tech=None, osint=("neutral", 5), oper=("neutral", 6),
                da="damaged"),
    "BRK-B": dict(name="Berkshire Hathaway", biz="insurance-led conglomerate with rail, energy and industrial units",
                  moat="low-cost insurance float and a decentralised operating model",
                  risk="succession and the difficulty of deploying a very large cash pile",
                  fund=("neutral", 6, None), tech=None, osint=("neutral", 5), oper=("bullish", 7),
                  da="survives"),
    # Phase 3 universe (health_care)
    "MRK": dict(name="Merck", biz="large pharmaceutical company with oncology and vaccine franchises",
                moat="patent-protected oncology franchise and global vaccine manufacturing",
                risk="loss of exclusivity on its largest oncology product later in the decade",
                fund=("bullish", 8, "g06"), tech=None, light=8, oper=("bullish", 8), ind=("bullish", 8),
                osint=("neutral", 5), da="survives", flag_status="confirmed"),
    "LLY": dict(name="Eli Lilly", biz="pharmaceutical company led by incretin metabolic medicines",
                moat="first-mover scale in incretin therapies and manufacturing capacity",
                risk="a valuation that already discounts years of very high growth",
                fund=("bearish", 6, "g15"), tech=None, light=6, oper=("bullish", 8), ind=("bullish", 7),
                osint=("neutral", 5), da="damaged"),
    "JNJ": dict(name="Johnson & Johnson", biz="diversified pharmaceutical and medtech group",
                moat="broad product portfolio and hospital relationships in medtech",
                risk="litigation exposure and biosimilar erosion in immunology",
                fund=("neutral", 6, "g03"), tech=None, light=6, oper=("bullish", 7), ind=("neutral", 6),
                osint=("neutral", 5), da="survives"),
    "ABBV": dict(name="AbbVie", biz="immunology-led pharmaceutical company",
                 moat="successor immunology products with long patent runways",
                 risk="high leverage from past acquisitions and amortisation-heavy GAAP earnings",
                 fund=("neutral", 5, "g06"), tech=None, light=6, oper=("bullish", 7), ind=("neutral", 6),
                 osint=("neutral", 5), da="damaged"),
    "ABT": dict(name="Abbott Laboratories", biz="diversified medical devices, diagnostics and nutrition",
                moat="diabetes-care device platform and diagnostics installed base",
                risk="pricing pressure in diagnostics and nutrition recalls",
                fund=("neutral", 5, "g06"), tech=None, light=5, oper=("bullish", 6), ind=("neutral", 6),
                osint=("neutral", 5), da="survives"),
    "TMO": dict(name="Thermo Fisher Scientific", biz="life-science tools and laboratory services",
                moat="installed instrument base with recurring consumables",
                risk="biotech funding cycles and customer destocking",
                fund=("neutral", 5, "g06"), tech=None, light=5, oper=("neutral", 6), ind=("neutral", 6),
                osint=("neutral", 5), da="survives"),
    "ISRG": dict(name="Intuitive Surgical", biz="robotic surgery systems and recurring instruments",
                 moat="installed robot base, surgeon training and procedure data",
                 risk="a premium valuation and emerging robotic competition",
                 fund=("bearish", 6, "g15"), tech=None, light=4, oper=("bullish", 7), ind=("bullish", 7),
                 osint=("neutral", 5), da="damaged"),
}

FUND_PROFILE: dict[str, dict[str, Any]] = {
    "QQQ": dict(stance="neutral", conviction=6, action="HOLD", role="core", tech=("bullish", 7),
                view="Nasdaq-100 tracker; low cost and liquid but concentrated in mega-cap technology."),
    "SPMO": dict(stance="bullish", conviction=8, action="ADD", role="factor_tilt", tech=("bullish", 8),
                 view="S&P 500 momentum tilt; low cost with rules-based rebalancing toward leaders."),
}

DISPLAY = {s: common.sector_display(s) for s in common.SECTOR_KEYS}


# ================================================================================================
# helpers
# ================================================================================================


def posix(p: Any) -> str:
    return Path(str(p)).as_posix()


def load(path: Any) -> Any:
    with open(str(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def ptr_get(doc: Any, ptr: str) -> Any:
    return common.json_pointer_get(doc, "#" + ptr if not ptr.startswith("#") else ptr)


def fval(x: Any) -> float | None:
    return common.fact_value(x) if isinstance(x, Mapping) else common.safe_float(x)


def r2(x: Any, dp: int = 2) -> float | None:
    v = common.safe_float(x)
    return None if v is None else round(v, dp)


def fmt(x: Any, dp: int = 1) -> str:
    v = common.safe_float(x)
    return "n/a" if v is None else f"{v:,.{dp}f}"


def words(text: str) -> int:
    return len(str(text).split())


class Order:
    """One work order: front matter + convenience accessors for its inputs."""

    def __init__(self, md_path: Path, rec: Mapping[str, Any], run_dir: Path):
        self.fm = validate.parse_order_md(md_path)
        self.rec = dict(rec)
        self.run_dir = run_dir
        self.oid: str = self.fm["order_id"]
        self.role: str = self.fm["agent_role"]
        self.ticker: str | None = self.fm.get("ticker")
        self.sector: str | None = self.fm.get("sector")
        self.mode: str | None = self.fm.get("mode")
        self.seeds = {s["id"]: dict(s) for s in self.fm.get("seed_sources") or []}
        self.inputs = [i["path"] if isinstance(i, Mapping) else str(i) for i in self.fm.get("inputs") or []]

    def inp(self, name: str) -> str | None:
        """Input path whose file name is ``name`` (or ends with it)."""
        for p in self.inputs:
            if Path(p).name == name:
                return p
        return None

    def inp_like(self, suffix: str) -> list[str]:
        return [p for p in self.inputs if Path(p).name.endswith(suffix)]

    def report(self, role: str) -> tuple[str | None, dict | None]:
        """A specialist report listed in the inputs (skips failed stubs)."""
        for p in self.inputs:
            nm = Path(p).name
            if nm.startswith(f"{role}_") and nm.endswith(".json") and ".failed" not in nm:
                try:
                    return p, load(p)
                except (OSError, ValueError):
                    return p, None
        return None, None

    def src(self, *ids: str) -> list[dict]:
        out = []
        for i in ids:
            if i in self.seeds and all(s["id"] != i for s in out):
                out.append(dict(self.seeds[i]))
        return out

    def base_doc(self, **extra: Any) -> dict:
        doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": "${RUN_ID}", "order_id": self.oid,
                               "agent_role": self.role}
        doc.update(extra)
        return doc


def ref(path: str, ptr: str) -> str:
    return f"{posix(path)}#{ptr}"


def key_figure(name: str, path: str, ptr: str, source_id: str, *, kind: str | None = None,
               unit: str | None = None, dp: int | None = None) -> dict | None:
    """Key figure copied from the Fact (or number) at ``path#ptr``; None when the value is null."""
    target = ptr_get(load(path), ptr)
    if isinstance(target, Mapping):
        value = common.safe_float(target.get("value"))
        if value is None:
            return None
        fig = {"name": name, "value": round(value, dp) if dp is not None else value,
               "unit": unit or target.get("unit") or "n/a", "source_id": source_id,
               "as_of": target.get("as_of") or AS_OF, "kind": kind or target.get("kind") or "other",
               "data_ref": ref(path, ptr)}
        if target.get("filed"):
            fig["filed"] = target["filed"]
        return fig
    value = common.safe_float(target)
    if value is None:
        return None
    return {"name": name, "value": value, "unit": unit or "n/a", "source_id": source_id, "as_of": AS_OF,
            "kind": kind or "other", "data_ref": ref(path, ptr)}


def figs(*items: dict | None) -> list[dict]:
    return [i for i in items if i]


def sec_url(cik: int | str, form: str) -> str:
    return (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}"
            f"&type={form.replace(' ', '+')}&dateb=&owner=include&count=40")


def web_sources(ticker: str, cik: int | None, *forms: tuple[str, str, str]) -> list[dict]:
    """W1.. web sources: SEC EDGAR filing-index pages for the company (form, id, title)."""
    out = []
    for form, wid, title in forms:
        out.append({"id": wid, "url": sec_url(cik or 0, form), "title": f"{title} — {ticker} (SEC EDGAR filing index)",
                    "accessed": AS_OF, "kind": "web"})
    return out


# ================================================================================================
# valuation cell selection (fundamental lens)
# ================================================================================================


def _cells(vdoc: Mapping[str, Any]) -> dict[str, float]:
    return {c["id"]: float(c["fv_per_share"]) for c in vdoc.get("cells") or []
            if isinstance(c, Mapping) and common.safe_float(c.get("fv_per_share")) is not None}


def pick_cells(vdoc: Mapping[str, Any], base_tier: str | None) -> tuple[list[str], list[str], list[str]]:
    cells = _cells(vdoc)
    if "DCF-NOPAT-r10-g00" in cells:
        tier = base_tier or "g03"
        order = ["g00", "g03", "g06", "g10", "g15"]
        i = order.index(tier)
        lo_t, hi_t = order[max(0, i - 1)], order[min(len(order) - 1, i + 1)]
        mult = "PE" if "PE-mid" in cells else "EVEBIT"
        low = [f"DCF-NOPAT-r10-{lo_t}", f"{mult}-low"]
        base = [f"DCF-NOPAT-r10-{tier}", f"DCF-FCF-r10-{tier}", f"{mult}-mid"]
        high = [f"DCF-NOPAT-r09-{hi_t}", f"{mult}-high"]
        if "EVEBIT-mid" in cells and mult == "PE":
            base.append("EVEBIT-mid")
    else:  # financials: P/E and P/B bands (NOPAT DCF is not computed)
        fams = [m for m in ("PE", "PB", "EVEBIT") if f"{m}-mid" in cells]
        low, base, high = [f"{m}-low" for m in fams], [f"{m}-mid" for m in fams], [f"{m}-high" for m in fams]
        if "DCF-FCF-r10-g00" in cells:
            # a financial with a reported FCF still gets FCF DCF cells: the base must span BOTH
            # method families (validate: min_base_method_families), so one FCF cell joins each list
            tier = base_tier or "g03"
            order = ["g00", "g03", "g06", "g10", "g15"]
            i = order.index(tier)
            lo_t, hi_t = order[max(0, i - 1)], order[min(len(order) - 1, i + 1)]
            low.append(f"DCF-FCF-r10-{lo_t}")
            base.append(f"DCF-FCF-r10-{tier}")
            high.append(f"DCF-FCF-r09-{hi_t}")
    keep = lambda ids: [c for c in ids if c in cells]  # noqa: E731
    return keep(low), keep(base), keep(high)


def mean_fv(vdoc: Mapping[str, Any], ids: Iterable[str]) -> float:
    cells = _cells(vdoc)
    vals = [cells[c] for c in ids]
    return round(sum(vals) / len(vals), 2)


# ================================================================================================
# technical helpers
# ================================================================================================


def tech_levels(tdoc: Mapping[str, Any]) -> list[tuple[str, float, str, str]]:
    """(id, price, type, json pointer) of every computed level / stop."""
    out = []
    lv = tdoc.get("levels") or {}
    for key, typ in (("supports", "support"), ("resistances", "resistance")):
        for i, l in enumerate(lv.get(key) or []):
            p = fval(l.get("price"))
            if p is not None:
                out.append((l["id"], p, typ, f"/levels/{key}/{i}/price"))
    for i, s in enumerate(tdoc.get("stop_candidates") or []):
        p = fval(s.get("price"))
        if p is not None:
            out.append((s["id"], p, "stop", f"/stop_candidates/{i}/price"))
    return out


def tech_stop(tdoc: Mapping[str, Any]) -> tuple[str, float, str] | None:
    """Highest stop candidate below the close (else the highest support below the close)."""
    close = fval(tdoc.get("close"))
    lv = tech_levels(tdoc)
    stops = sorted([(p, i, ptr) for i, p, t, ptr in lv if t == "stop" and close and p < close], reverse=True)
    if not stops:
        stops = sorted([(p, i, ptr) for i, p, t, ptr in lv if t == "support" and close and p < close], reverse=True)
    if not stops:
        return None
    p, i, ptr = stops[0]
    return i, p, ptr


def tech_resistance(tdoc: Mapping[str, Any]) -> tuple[str, float, str] | None:
    close = fval(tdoc.get("close"))
    res = sorted([(p, i, ptr) for i, p, t, ptr in tech_levels(tdoc) if t == "resistance" and close and p > close])
    if not res:
        return None
    p, i, ptr = res[0]
    return i, p, ptr


REGIME_TEXT = {
    "trend_up": "an established uptrend", "trend_down": "a downtrend", "range": "a trading range",
    "choppy": "a choppy, directionless tape", "transition": "a transition between regimes",
}


def tech_stance(regime: str, rs12: float | None) -> tuple[str, int]:
    if regime == "trend_up":
        return ("bullish", 7) if (rs12 or 0) >= -5 else ("bullish", 6)
    if regime == "trend_down":
        return "bearish", 6
    if regime == "choppy":
        return "neutral", 4
    return "neutral", 5


# ================================================================================================
# role authors
# ================================================================================================


def _cik_of(fdoc: Mapping[str, Any] | None) -> int | None:
    return common.safe_float((fdoc or {}).get("cik")) and int((fdoc or {})["cik"])


def author_fundamental(o: Order) -> dict:
    t = o.ticker
    prof = COMPANY[t]
    fpath = o.inp(common.data_filename("fundamentals", t))
    vpath = o.inp(common.data_filename("valuation", t))
    fdoc, vdoc = load(fpath), load(vpath)
    stance, conv, tier = prof["fund"]
    if o.fm.get("step") == "p3.light":
        conv = prof.get("light", conv)
    low, base, high = pick_cells(vdoc, tier)
    fv_lo, fv_base, fv_hi = mean_fv(vdoc, low), mean_fv(vdoc, base), mean_fv(vdoc, high)
    price = fval(fdoc.get("price"))
    cf, px, val = f"D:SEC:{t}:CF", f"D:PX:{t}", f"D:VAL:{t}"
    fin = (fdoc.get("entity") or {}).get("sector_class") == "financials" or "PB-mid" in _cells(vdoc)
    kfs = figs(
        key_figure("Revenue TTM", fpath, "/ttm/revenue", cf),
        key_figure("Net income TTM", fpath, "/ttm/net_income", cf),
        None if fin else key_figure("Operating margin TTM", fpath, "/margins_pct/operating", cf),
        None if fin else key_figure("FCF yield", fpath, "/valuation/fcf_yield_pct", cf),
        None if fin else key_figure("ROIC", fpath, "/valuation/roic_pct", cf),
        key_figure("ROE", fpath, "/valuation/roe_pct", cf) if fin else None,
        key_figure("P/B", fpath, "/valuation/p_b", cf) if fin else None,
        key_figure("P/E", fpath, "/valuation/pe", cf),
        key_figure("Last close", fpath, "/price", px, kind="price"),
        key_figure("Reverse DCF implied growth", vpath, "/reverse_dcf/implied_growth_pct", val),
    )
    upside = (fv_base - price) / price * 100 if price else None
    opm = fval(ptr_get(fdoc, "/margins_pct/operating")) if not fin else None
    roe = fval(ptr_get(fdoc, "/valuation/roe_pct"))
    pe = fval(ptr_get(fdoc, "/valuation/pe"))
    rdcf = fval(ptr_get(vdoc, "/reverse_dcf/implied_growth_pct"))
    if fin:
        quality = f"ROE of {fmt(roe)}% TTM on reported equity"
    else:
        quality = f"operating margin of {fmt(opm)}% TTM"
    if upside is None:
        verdict = "cannot be valued on the grid"
    elif upside >= 10:
        verdict = "leaves a margin of safety"
    elif upside >= -10:
        verdict = "looks roughly fairly priced"
    else:
        verdict = "already prices in more than the filings support"
    thesis = (f"{prof['name']} is a {prof['biz']} with {quality}. Against the configured valuation grid the "
              f"base fair value is {fmt(fv_base, 2)} versus a {fmt(price, 2)} close ({fmt(upside)}%), so the "
              f"stock {verdict}.")
    missing = list(fdoc.get("missing_data") or [])[:1]
    missing = [f"From fundamentals data: {m}"[:300] for m in missing]
    if rdcf is None:
        missing.append("Reverse DCF implied growth (not computable for this sector class)")
    risks = [{"point": f"Key risk: {prof['risk']}.", "evidence_refs": [cf], "severity": "medium"}]
    if upside is not None and upside < 0:
        risks.append({"point": f"Base fair value sits {fmt(abs(upside))}% below the last close.",
                      "evidence_refs": [val], "severity": "high" if upside < -20 else "medium"})
    if rdcf is not None:
        risks.append({"point": f"The reverse DCF implies {fmt(rdcf)}% annual growth is priced in.",
                      "evidence_refs": [val], "severity": "medium" if rdcf < 10 else "high"})
    doc = o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", thesis=thesis, stance=stance,
        strengths=[{"point": f"Durable {prof['moat']}.", "evidence_refs": [cf]},
                   {"point": f"P/E of {fmt(pe)}x on TTM earnings with {quality}.", "evidence_refs": [cf, px]}],
        risks=risks[:3], key_figures=kfs,
        invalidation_price=None,
        invalidation_rationale=("The fundamental kill condition is a business metric, not a price: a sustained "
                                "decline in TTM operating income or a cut to capital returns would break the thesis."),
        conviction=conv,
        conviction_reason={"bullish": "Valuation cells and quality metrics agree.",
                           "neutral": "Quality is clear but the valuation grid gives no edge.",
                           "bearish": "The price sits above most of the valuation grid."}[stance],
        self_evidence=8 if not missing else 7,
        self_evidence_reason="TTM, FY history and the valuation grid were available from SEC companyfacts.",
        missing_data=missing, red_flags=[], flag_responses=[],
        sources=o.src(cf, val, px, f"D:SEC:{t}:SUB"),
        extensions={
            "valuation": {
                "fair_value_low": fv_lo, "fair_value_base": fv_base, "fair_value_high": fv_hi,
                "method": ("Course NOPAT/FCF DCF (r 9-10%, 5 years, growth-tiered exit multiple) blended with the "
                           "configured multiple bands." if not fin else
                           "Configured P/E and P/B bands (financials: DCF cells have limited meaning) plus one FCF DCF cell."),
                "input_refs": [ref(vpath, "/cells"), ref(vpath, "/reverse_dcf")],
                "cells_low": low, "cells_base": base, "cells_high": high,
                "reverse_dcf_implied_growth_pct": rdcf,
            },
            "quality_notes": f"{prof['name']}: {quality}; moat rests on its {prof['moat']}.",
            "balance_sheet_notes": _balance_note(fdoc, fin),
        },
    )
    return doc


def _balance_note(fdoc: Mapping[str, Any], fin: bool) -> str:
    nd = fval(ptr_get(fdoc, "/balance/net_debt"))
    nde = fval(ptr_get(fdoc, "/valuation/net_debt_ebitda"))
    if fin:
        return ("Balance-sheet leverage is assessed through capital ratios that SEC companyfacts does not expose; "
                "equity and earnings figures are taken as reported.")
    if nd is None:
        return "Net debt could not be computed from companyfacts; leverage judged qualitatively."
    return (f"Net debt of {fmt(nd / 1e9)} bn USD"
            + (f", about {fmt(nde)}x EBITDA" if nde is not None else "") + "; no near-term refinancing strain visible.")


def author_technical(o: Order) -> dict:
    t = o.ticker
    tpath = o.inp(common.data_filename("technicals", t))
    tdoc = load(tpath)
    regime = tdoc["trend"]["regime"]
    rs12 = fval(ptr_get(tdoc, "/relative_strength/vs_spy/12m"))
    if t in FUND_PROFILE:
        stance, conv = FUND_PROFILE[t]["tech"]
        if regime == "trend_down":
            stance, conv = "bearish", 5
    else:
        stance, conv = tech_stance(regime, rs12)
    stop = tech_stop(tdoc)
    res = tech_resistance(tdoc)
    close = fval(tdoc.get("close"))
    key_levels = []
    for lid, p, typ, ptr in tech_levels(tdoc):
        if (stop and lid == stop[0]) or (res and lid == res[0]) or lid == "S1":
            key_levels.append({"ref_id": lid, "price": p, "type": typ, "data_ref": ref(tpath, ptr)})
    if stance == "bearish" and res:
        inv_id, inv_p = res[0], res[1]
        inv_rat = f"A daily close above {inv_id} would negate the downtrend read and reset the regime."
    elif stop:
        inv_id, inv_p = stop[0], stop[1]
        inv_rat = f"A daily close below {inv_id} breaks the nearest support structure and invalidates the setup."
    else:
        inv_id, inv_p, inv_rat = None, None, "No computed level below the close."
    rsi = fval(ptr_get(tdoc, "/momentum/rsi14"))
    adx = fval(ptr_get(tdoc, "/momentum/adx14"))
    vs200 = fval(ptr_get(tdoc, "/trend/close_vs_sma200_pct"))
    thesis = (f"{t} trades in {REGIME_TEXT.get(regime, regime)}: close {fmt(vs200)}% versus the 200-day average, "
              f"ADX {fmt(adx)} and RSI {fmt(rsi)}. Twelve-month relative strength versus SPY is {fmt(rs12)} pp.")
    setup = (f"Hold above {inv_id} at {fmt(inv_p, 2)}; a close beyond the nearest resistance would confirm strength."
             if inv_id and stance != "bearish" else
             f"Rallies into {inv_id} at {fmt(inv_p, 2)} are suspect while the downtrend persists.")
    bench = tdoc.get("benchmark") or "SPY"
    sec_etf = tdoc.get("sector_etf")
    kfs = figs(
        key_figure("Last close", tpath, "/close", f"D:PX:{t}", kind="price"),
        key_figure("RSI 14", tpath, "/momentum/rsi14", f"D:PX:{t}", kind="price"),
        key_figure("ADX 14", tpath, "/momentum/adx14", f"D:PX:{t}", kind="price"),
        key_figure("RS vs SPY 12m", tpath, "/relative_strength/vs_spy/12m", f"D:PX:{t}", kind="price"),
    )
    srcs = o.src(f"D:PX:{t}", f"D:PX:{bench}", *( [f"D:PX:{sec_etf}"] if sec_etf else []))
    return o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", thesis=thesis, stance=stance,
        strengths=[], risks=[{"point": f"Twelve-month relative strength versus SPY is {fmt(rs12)} pp.",
                              "evidence_refs": [f"D:PX:{t}"], "severity": "medium" if (rs12 or 0) < 0 else "low"}],
        key_figures=kfs, invalidation_price=inv_p, invalidation_rationale=inv_rat, conviction=conv,
        conviction_reason=f"Computed regime is {regime}; levels are well defined by swing pivots.",
        self_evidence=9, self_evidence_reason="Three years of daily bars for the ticker and its benchmarks.",
        missing_data=[], red_flags=[], flag_responses=[], sources=srcs,
        extensions={"trend_regime": regime, "key_levels": key_levels, "setup": setup,
                    "indicator_refs": [ref(tpath, "/trend/regime"), ref(tpath, "/momentum/adx14"),
                                       ref(tpath, "/momentum/rsi14"), ref(tpath, "/relative_strength/vs_spy/12m")],
                    "invalidation_ref": inv_id},
    )


OSINT_FLAGS: dict[str, list[dict]] = {
    "KO": [{"id": "F1", "flag": "A long-running tax dispute over intercompany pricing remains open in the courts; "
            "an adverse outcome could require a sizeable cash payment.", "severity": "high", "must_address": True,
            "source_ids": ["W1"]},
           {"id": "F2", "flag": "Proposed sugar-tax measures are under discussion in several markets.",
            "severity": "low", "must_address": False, "source_ids": ["W2"]}],
    "JPM": [{"id": "F1", "flag": "Regulatory capital proposals could raise required capital for the largest banks.",
             "severity": "medium", "must_address": False, "source_ids": ["W1"]}],
    "MRK": [{"id": "F1", "flag": "Exclusivity on the lead oncology product expires within the forecast horizon; "
             "replacement revenue depends on pipeline readouts.", "severity": "high", "must_address": True,
             "source_ids": ["W1"]}],
    "LLY": [{"id": "F1", "flag": "Drug-pricing negotiation rules may extend to newer metabolic medicines.",
             "severity": "medium", "must_address": False, "source_ids": ["W1"]}],
}


def author_osint(o: Order) -> dict:
    t = o.ticker
    prof = COMPANY[t]
    fpath = o.inp(common.data_filename("fundamentals", t))
    fdoc = load(fpath)
    cik = _cik_of(fdoc)
    stance, conv = prof["osint"]
    flags = OSINT_FLAGS.get(t, [])
    webs = web_sources(t, cik, ("8-K", "W1", "Current reports (8-K)"), ("10-Q", "W2", "Quarterly reports (10-Q)"),
                       ("4", "W3", "Insider transaction reports (Form 4)"))
    signals = [
        {"signal": f"Recent current reports focus on routine business updates; {prof['risk']} is the recurring theme.",
         "direction": "negative" if flags else "neutral", "source_ids": ["W1"], "trend": "stable"},
        {"signal": "Quarterly filings arrived on the usual schedule with no late-filing notices.",
         "direction": "positive", "source_ids": ["W2", f"D:SEC:{t}:SUB"], "trend": "stable"},
        {"signal": "Insider filings show routine award and plan-based transactions, no unusual clusters.",
         "direction": "neutral", "source_ids": ["W3"], "trend": "stable"},
    ]
    high = [f for f in flags if f["severity"] == "high"]
    thesis = (f"Public signals for {prof['name']} are mostly routine: filings are timely and insider activity is "
              f"unremarkable. " + (f"One high-severity item needs a response: {high[0]['flag'].split(';')[0].lower()}."
                                   if high else "No item requires a committee response."))
    return o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", thesis=thesis, stance=stance, strengths=[], risks=[],
        key_figures=[], invalidation_price=None,
        invalidation_rationale="Signals are qualitative; a change in the flagged items would change the read.",
        conviction=conv, conviction_reason="Signals point in different directions with no clear trend.",
        self_evidence=6, self_evidence_reason="Filing indexes were sampled; news-tone and hiring data were not available.",
        missing_data=["Hiring and employee-sentiment data (no source in the inputs)"], red_flags=flags,
        flag_responses=[], sources=o.src(f"D:SEC:{t}:SUB", f"D:SEC:{t}:CF") + webs,
        extensions={"signals": signals},
    )


def author_operator(o: Order) -> dict:
    t = o.ticker
    prof = COMPANY[t]
    fpath = o.inp(common.data_filename("fundamentals", t))
    fdoc = load(fpath)
    cik = _cik_of(fdoc)
    stance, conv = prof["oper"]
    webs = web_sources(t, cik, ("DEF 14A", "W1", "Proxy statements (DEF 14A)"), ("4", "W2", "Insider transaction reports (Form 4)"),
                       ("10-K", "W3", "Annual reports (10-K)"))
    fcf = key_figure("FCF TTM", fpath, "/ttm/fcf", f"D:SEC:{t}:CF")
    ni = key_figure("Net income TTM", fpath, "/ttm/net_income", f"D:SEC:{t}:CF")
    tone = {"bullish": "disciplined and shareholder-aligned", "neutral": "competent but not distinctive",
            "bearish": "inconsistent"}[stance]
    thesis = (f"Management at {prof['name']} looks {tone}: capital returns are funded from operating cash flow, "
              f"incentives reference multi-year targets and guidance has broadly been delivered. Insider activity is "
              f"routine.")
    ext = {
        "management_quality": {"assessment": f"Leadership has kept a consistent strategy around its {prof['moat']}.",
                               "source_ids": ["W1", "W3"]},
        "capital_allocation": {"assessment": "Dividends and buybacks are funded from free cash flow; acquisitions are "
                                             "bolt-on rather than transformational.", "source_ids": ["W3", f"D:SEC:{t}:CF"]},
        "incentives": {"assessment": "Annual bonus and long-term awards reference earnings, cash flow and relative "
                                     "shareholder return targets.", "source_ids": ["W1"]},
        "insider_activity": {"assessment": "Form 4 activity is dominated by award vesting and plan-based sales.",
                             "source_ids": ["W2"], "net_direction": "selling"},
        "guidance_vs_delivery": {"assessment": "Recent annual targets were broadly met; misses were flagged early.",
                                 "source_ids": ["W3"]},
    }
    return o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", thesis=thesis, stance=stance, strengths=[], risks=[],
        key_figures=figs(fcf, ni), invalidation_price=None,
        invalidation_rationale="Governance view: a dividend cut or a large debt-funded acquisition would break it.",
        conviction=conv, conviction_reason="Long, verifiable record in filings; pay-mix detail not quantified.",
        self_evidence=7, self_evidence_reason="Proxy, annual report and Form 4 indexes reviewed.",
        missing_data=[], red_flags=[], flag_responses=[],
        sources=o.src(f"D:SEC:{t}:CF", f"D:SEC:{t}:SUB") + webs, extensions=ext,
    )


def author_industry(o: Order) -> dict:
    t = o.ticker
    prof = COMPANY[t]
    fpath = o.inp(common.data_filename("fundamentals", t))
    fdoc = load(fpath)
    cik = _cik_of(fdoc)
    stance, conv = prof["ind"]
    webs = web_sources(t, cik, ("10-K", "W1", "Annual reports (10-K)"), ("10-Q", "W2", "Quarterly reports (10-Q)"),
                       ("8-K", "W3", "Current reports (8-K)"))
    gm = key_figure("Gross margin TTM", fpath, "/margins_pct/gross", f"D:SEC:{t}:CF")
    om = key_figure("Operating margin TTM", fpath, "/margins_pct/operating", f"D:SEC:{t}:CF")
    thesis = (f"Operationally {prof['name']} benefits from its {prof['moat']}. Unit economics are strong at the "
              f"reported margins; the main operating exposure is {prof['risk']}.")
    ext = {
        "supply_chain": {"assessment": "Manufacturing is concentrated in a few qualified sites with dual sourcing "
                                       "for critical inputs.", "source_ids": ["W1"]},
        "capacity_backlog": {"assessment": "Capacity additions are disclosed in the annual report; no backlog metric "
                                           "applies to most products.", "source_ids": ["W1", "W2"]},
        "execution": {"assessment": "Launch cadence and integration of acquired assets have been on plan per recent "
                                    "quarterly reports.", "source_ids": ["W2", "W3"]},
        "unit_economics": {"assessment": "Reported gross and operating margins show high incremental returns on "
                                         "established products.", "source_ids": [f"D:SEC:{t}:CF"]},
        "operational_moat": {"assessment": f"The {prof['moat']} is hard to replicate quickly.", "source_ids": ["W1"]},
    }
    return o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", thesis=thesis, stance=stance, strengths=[], risks=[],
        key_figures=figs(gm, om), invalidation_price=None,
        invalidation_rationale="Evidence of supply disruption or sustained share loss would undermine the moat.",
        conviction=conv, conviction_reason="Structural advantages are documented in filings.",
        self_evidence=7, self_evidence_reason="Business description and MD&A reviewed; no independent volume data.",
        missing_data=[], red_flags=[], flag_responses=[],
        sources=o.src(f"D:SEC:{t}:CF", f"D:SEC:{t}:SUB") + webs, extensions=ext,
    )


DA_ATTACKS: dict[str, str] = {
    "survives": "The challenges raise valid questions but none overturns the core case.",
    "damaged": "The core case still stands, but valuation and the flagged risks weaken the argument for owning it at this price.",
    "broken": "The bull case relies on a fair value the cash-flow cells do not support and on margins that the filings show are already at a peak.",
}


DA_REASON: dict[str, str] = {
    "KO": "Quality and cash generation hold up; the open tax matter is disclosed and bounded, so the thesis survives "
          "despite a full valuation.",
    "PG": "The bull case needs the multiple bands; on cash-flow cells the shares are well above fair value and the "
          "downtrend shows the market is not paying for the brand premium any more.",
    "JPM": "Franchise quality is not in doubt, but the price sits above the top of the P/B band while net interest "
           "income faces falling policy rates.",
    "BRK-B": "The conglomerate case is intact; valuation is near the middle of the multiple bands.",
    "MRK": "The patent-expiry flag is real and confirmed, but even the zero-growth cells cover most of the price, so "
           "the thesis survives.",
    "ABBV": "Leverage and amortisation-heavy earnings leave little room if the successor products slow.",
    "ISRG": "The franchise is strong but the price discounts growth far above any tier of the valuation grid.",
    "JNJ": "Diversification protects the thesis; the challenges concern valuation rather than the business.",
}


def _tech_attack(t: str, tdoc: Mapping[str, Any]) -> dict:
    rs12 = fval(ptr_get(tdoc, "/relative_strength/vs_spy/12m"))
    if rs12 is not None and rs12 < 0:
        ce = (f"Twelve-month relative strength versus SPY is {fmt(rs12)} pp; support levels fail more often while "
              "the benchmark keeps outperforming.")
    else:
        ce = (f"Relative strength versus SPY is positive ({fmt(rs12)} pp over twelve months), but much of it came in "
              "one leg and the ADX does not confirm a durable trend.")
    return {"target_claim": "The technical structure supports the position.", "target_agent": "technical",
            "counter_evidence": ce, "source_ids": [f"D:PX:{t}", "D:PX:SPY"], "severity": "medium"}


def author_da(o: Order) -> dict:
    t = o.ticker
    prof = COMPANY[t]
    verdict = prof["da"]
    tpath = o.inp(common.data_filename("technicals", t))
    fpath = o.inp(common.data_filename("fundamentals", t))
    vpath = o.inp(common.data_filename("valuation", t))
    tdoc, vdoc = load(tpath), load(vpath)
    cells = _cells(vdoc)
    fund_path, fund = o.report("fundamental")
    osint_path, osint = o.report("osint")
    # bear case: the lowest zero-growth DCF / low multiple cell, else the lowest stop
    cand = [c for c in ("DCF-NOPAT-r10-g00", "DCF-FCF-r10-g00", "PE-low", "PB-low") if c in cells]
    if verdict == "broken" and "DCF-FCF-r10-g03" in cells:
        cand = ["DCF-FCF-r10-g03"]
    if cand:
        bear_ref = cand[0]
        bear_price = cells[bear_ref]
        bear_idx = next(i for i, c in enumerate(vdoc["cells"]) if c.get("id") == bear_ref)
        bear_fig = key_figure("Bear-case fair value", vpath, f"/cells/{bear_idx}/fv_per_share", f"D:VAL:{t}",
                              unit="USD/share")
    else:
        stop = tech_stop(tdoc)
        bear_ref, bear_price = stop[0], stop[1]
        bear_fig = key_figure("Bear-case stop level", tpath, stop[2], f"D:PX:{t}", kind="price")
    fv_base = common.get_dotted(fund or {}, "extensions.valuation.fair_value_base")
    attacks = [
        {"target_claim": f"The base fair value of {fmt(fv_base, 2)} offers protection.", "target_agent": "fundamental",
         "counter_evidence": f"The {bear_ref} cell values the shares at {fmt(bear_price, 2)}; the base case leans on "
                             "growth or multiples that the zero-growth cells do not support.",
         "source_ids": [f"D:VAL:{t}"], "severity": "high" if verdict == "broken" else "medium"},
        _tech_attack(t, tdoc),
    ]
    responses = []
    for f in (osint or {}).get("red_flags") or []:
        if f.get("severity") == "high" and f.get("must_address"):
            status = prof.get("flag_status", "mitigated")
            if status == "mitigated":
                note = ("Filings disclose the matter and the potential cash outflow is small relative to annual free "
                        "cash flow; the exposure is real but bounded.")
                counter = ("Disclosure is adequate and the potential payment is small relative to annual free cash "
                           "flow, so the flag is mitigated rather than confirmed.")
            else:
                note = ("Confirmed: the exclusivity date is disclosed in the annual report and replacement revenue is "
                        "not yet visible in reported figures.")
                counter = ("The flag stands: the pipeline must replace a large revenue stream within the forecast "
                           "horizon, which the zero-growth DCF cells already reflect.")
            responses.append({"flag_ref": f"osint:{f['id']}", "status": status, "note": note,
                              "source_ids": [f"D:SEC:{t}:SUB"]})
            attacks.append({"target_claim": f["flag"][:200], "target_agent": "osint", "counter_evidence": counter,
                            "source_ids": [f"D:SEC:{t}:CF"], "severity": "low" if status == "mitigated" else "medium"})
    stop = tech_stop(tdoc)
    kills = [f"Two consecutive quarters of declining TTM operating income in SEC filings.",
             f"A daily close below {stop[0]} at {fmt(stop[1], 2)}." if stop else "A break of the 52-week low."]
    if verdict == "broken":
        kills.insert(0, "Any guidance cut that pushes TTM free cash flow below the dividend.")
    return o.base_doc(
        sector=o.sector, ticker=t, as_of="${AS_OF}", attacks=attacks[:6], bear_case_price=bear_price,
        bear_case_ref=bear_ref, kill_criteria=kills[:5], verdict_on_thesis=verdict,
        verdict_reason=DA_REASON.get(t, DA_ATTACKS[verdict]), self_evidence=7,
        self_evidence_reason="Read every specialist report and the data files listed in the order.",
        missing_data=[], red_flags=[], flag_responses=responses, key_figures=figs(bear_fig),
        sources=o.src(f"D:VAL:{t}", f"D:PX:{t}", f"D:SEC:{t}:CF", f"D:SEC:{t}:SUB", "D:PX:SPY"),
    )


def author_etf(o: Order) -> dict:
    t = o.ticker
    prof = FUND_PROFILE[t]
    lpath = o.inp("lookthrough.json")
    tpath = o.inp(common.data_filename("technicals", t))
    lk, tdoc = load(lpath), load(tpath)
    fund = lk["funds"][t]
    split = fund.get("sector_split") or {}
    top = sorted(split.items(), key=lambda kv: -kv[1])[:3]
    pairs = (lk.get("overlap") or {}).get("pairs") or []
    overlap = []
    for p in pairs:
        if t in (p.get("a"), p.get("b")):
            other = p["b"] if p["a"] == t else p["a"]
            overlap.append({"with": other, "overlap_pct": p["overlap_pct"], "basis": p.get("basis", "top10")})
    rs = {w: fval(ptr_get(tdoc, f"/relative_strength/vs_spy/{w}")) for w in ("3m", "6m", "12m")}
    er = fval(fund.get("expense_ratio_pct"))
    conc = fval(fund.get("top10_concentration_pct"))
    fid = f"D:YF:{t}:FUND"
    thesis = (f"{t}: {prof['view']} Top-10 holdings are {fmt(conc)}% of the fund, expense ratio {fmt(er, 2)}%, and "
              f"twelve-month relative strength versus SPY is {fmt(rs['12m'])} pp.")
    kfs = figs(
        key_figure("Expense ratio", lpath, f"/funds/{t}/expense_ratio_pct", fid),
        key_figure("Top-10 concentration", lpath, f"/funds/{t}/top10_concentration_pct", fid),
        key_figure("RS vs SPY 12m", tpath, "/relative_strength/vs_spy/12m", f"D:PX:{t}", kind="price"),
    )
    risks = [{"point": f"Top-10 holdings are {fmt(conc)}% of assets.", "evidence_refs": [fid], "severity": "medium"}]
    if overlap:
        risks.append({"point": f"Top-10 overlap with {overlap[0]['with']} is {fmt(overlap[0]['overlap_pct'])}%.",
                      "evidence_refs": [fid], "severity": "medium"})
    return o.base_doc(
        sector="funds", ticker=t, as_of="${AS_OF}", thesis=thesis, stance=prof["stance"],
        strengths=[{"point": f"Low expense ratio of {fmt(er, 2)}% and deep liquidity.", "evidence_refs": [fid]}],
        risks=risks,
        lookthrough_summary={"top_sectors": [{"sector": s, "pct": v} for s, v in top],
                             "data_ref": ref(lpath, f"/funds/{t}/sector_split")},
        expense_ratio_pct=er, top10_concentration_pct=conc, overlap=overlap,
        momentum_vs_benchmark={"benchmark": "SPY", "rs_3m": rs["3m"], "rs_6m": rs["6m"], "rs_12m": rs["12m"]},
        role_in_portfolio=prof["role"], conviction=prof["conviction"],
        conviction_reason="Cost, liquidity and momentum support the role; concentration tempers it.",
        action=prof["action"], key_figures=kfs, invalidation_price=None,
        invalidation_rationale="A fund is reviewed on role, cost and overlap; no single price invalidates it.",
        self_evidence=8, self_evidence_reason="Fund composition and three years of prices were available.",
        missing_data=[], red_flags=[], flag_responses=[],
        sources=o.src(fid, f"D:PX:{t}", "D:PX:SPY"),
    )


# ------------------------------------------------------------------------------------------------
# macro / CIO
# ------------------------------------------------------------------------------------------------

#: macro_view sector constraints (min, max) — IT max 20 is deliberately below the current
#: look-through weight so the CIO documents a constraint override.
MACRO_CONSTRAINTS = [
    ("information_technology", 12, 20, "Rate-sensitive valuations and mega-cap concentration argue for a cap."),
    ("health_care", 6, 18, "Defensive growth at reasonable multiples; currently under-owned."),
    ("financials", 10, 28, "A re-steepened curve supports margins while credit risk caps the upside."),
    ("consumer_staples", 8, 22, "Defensive ballast for a slowdown, but valuations are full."),
    ("energy", 0, 8, "Falling crude prices weigh on earnings momentum."),
]


def author_macro(o: Order) -> dict:
    mpath = o.inp("macro.json")
    m = load(mpath)

    def d(sid: str, ptr: str) -> str:
        return ref(mpath, f"/series/{sid}/{ptr}")

    kf = figs(
        key_figure("Fed funds effective rate", mpath, "/series/DFF/latest", "D:FRED:DFF"),
        key_figure("Fed funds 12m change", mpath, "/series/DFF/chg_12m", "D:FRED:DFF"),
        key_figure("2-year Treasury yield", mpath, "/series/DGS2/latest", "D:FRED:DGS2"),
        key_figure("10-year Treasury yield", mpath, "/series/DGS10/latest", "D:FRED:DGS10"),
        key_figure("10y-2y spread", mpath, "/series/T10Y2Y/latest", "D:FRED:T10Y2Y"),
        key_figure("CPI YoY", mpath, "/series/CPIAUCSL/yoy_pct", "D:FRED:CPIAUCSL"),
        key_figure("Core CPI YoY", mpath, "/series/CPILFESL/yoy_pct", "D:FRED:CPILFESL"),
        key_figure("Unemployment rate", mpath, "/series/UNRATE/latest", "D:FRED:UNRATE"),
        key_figure("HY OAS", mpath, "/series/BAMLH0A0HYM2/latest", "D:FRED:BAMLH0A0HYM2"),
        key_figure("WTI crude", mpath, "/series/DCOILWTICO/latest", "D:FRED:DCOILWTICO"),
        key_figure("VIX", mpath, "/series/VIXCLS/latest", "D:FRED:VIXCLS"),
    )
    v = {f["name"]: f["value"] for f in kf}
    drivers = [
        {"driver": f"Policy easing under way: fed funds at {fmt(v.get('Fed funds effective rate'), 2)}%, "
                   f"{fmt(v.get('Fed funds 12m change'), 2)} pp over twelve months.",
         "direction": "tailwind", "series": ["DFF", "DGS2"],
         "data_refs": [d("DFF", "latest"), d("DFF", "chg_12m"), d("DGS2", "latest")],
         "source_ids": ["D:FRED:DFF", "D:FRED:DGS2"]},
        {"driver": f"Curve re-steepened to {fmt(v.get('10y-2y spread'), 2)} pp with the 10-year near "
                   f"{fmt(v.get('10-year Treasury yield'), 2)}%.",
         "direction": "mixed", "series": ["T10Y2Y", "DGS10"], "data_refs": [d("T10Y2Y", "latest"), d("DGS10", "latest")],
         "source_ids": ["D:FRED:T10Y2Y", "D:FRED:DGS10"]},
        {"driver": f"Inflation easing but above target: CPI {fmt(v.get('CPI YoY'))}% and core {fmt(v.get('Core CPI YoY'))}% YoY.",
         "direction": "mixed", "series": ["CPIAUCSL", "CPILFESL"],
         "data_refs": [d("CPIAUCSL", "yoy_pct"), d("CPILFESL", "yoy_pct")],
         "source_ids": ["D:FRED:CPIAUCSL", "D:FRED:CPILFESL"]},
        {"driver": f"Labour market softening at the margin: unemployment {fmt(v.get('Unemployment rate'))}%.",
         "direction": "headwind", "series": ["UNRATE", "PAYEMS"], "data_refs": [d("UNRATE", "latest"), d("PAYEMS", "latest")],
         "source_ids": ["D:FRED:UNRATE", "D:FRED:PAYEMS"]},
        {"driver": f"Credit calm: high-yield spread {fmt(v.get('HY OAS'), 2)} pp, VIX {fmt(v.get('VIX'))}.",
         "direction": "tailwind", "series": ["BAMLH0A0HYM2", "VIXCLS"],
         "data_refs": [d("BAMLH0A0HYM2", "latest"), d("VIXCLS", "latest")],
         "source_ids": ["D:FRED:BAMLH0A0HYM2", "D:FRED:VIXCLS"]},
    ]
    scenarios = [
        {"name": "Soft landing", "probability": 0.5,
         "description": "Inflation drifts toward target while growth stays positive; gradual further cuts.",
         "sector_implications": [{"sector": "information_technology", "impact": "positive"},
                                 {"sector": "health_care", "impact": "neutral"},
                                 {"sector": "financials", "impact": "positive"}]},
        {"name": "Growth scare", "probability": 0.3,
         "description": "Hiring stalls, spreads widen and markets rotate toward defensives.",
         "sector_implications": [{"sector": "consumer_staples", "impact": "positive"},
                                 {"sector": "health_care", "impact": "positive"},
                                 {"sector": "financials", "impact": "negative", "note": "Credit costs rise."}]},
        {"name": "Sticky inflation", "probability": 0.2,
         "description": "Core inflation stalls above target and long yields rise again.",
         "sector_implications": [{"sector": "information_technology", "impact": "negative"},
                                 {"sector": "energy", "impact": "positive"}]},
    ]
    return {
        "schema_version": SCHEMA_VERSION, "run_id": "${RUN_ID}", "order_id": o.oid, "agent_role": "macro",
        "as_of": "${AS_OF}", "regime": "late-cycle easing",
        "regime_summary": ("Policy rates are falling from restrictive levels and the curve has re-steepened. Inflation "
                           "is easing but still above target while unemployment edges up. Credit spreads are calm. The "
                           "mix favours quality and defensive growth over rate-sensitive valuation extremes."),
        "drivers": drivers, "scenarios": scenarios,
        "sector_constraints": [{"sector": s, "min_weight": lo, "max_weight": hi, "rationale": r}
                               for s, lo, hi, r in MACRO_CONSTRAINTS],
        "conviction": 6, "conviction_reason": "Series agree on direction; the growth outlook is finely balanced.",
        "key_figures": kf,
        "sources": o.src("D:FRED:DFF", "D:FRED:DGS2", "D:FRED:DGS10", "D:FRED:T10Y2Y", "D:FRED:CPIAUCSL",
                         "D:FRED:CPILFESL", "D:FRED:UNRATE", "D:FRED:PAYEMS", "D:FRED:BAMLH0A0HYM2",
                         "D:FRED:DCOILWTICO", "D:FRED:VIXCLS"),
        "missing_data": [],
    }


def _targets(current: Mapping[str, float]) -> dict[str, float]:
    """CIO targets: health care +4.5 pp, staples and financials -0.8 pp (inside the 1 pp breach
    tolerance, so the reduction is executed through the sector verdicts, not forced TRIMs), cash =
    remainder; IT kept at the look-through weight (macro-cap override)."""
    t = {k: round(float(v), 2) for k, v in current.items() if k != "unclassified" or float(v) > 0}
    t["health_care"] = round(t.get("health_care", 0.0) + 4.5, 2)
    t["consumer_staples"] = round(t.get("consumer_staples", 0.0) - 0.8, 2)
    t["financials"] = round(t.get("financials", 0.0) - 0.8, 2)
    t["cash"] = 0.0
    t["cash"] = round(100.0 - sum(t.values()), 2)
    return t


def _it_override(target: Mapping[str, float], why: str) -> list[dict]:
    lo_hi = {s: (lo, hi) for s, lo, hi, _r in MACRO_CONSTRAINTS}
    out = []
    for s, (lo, hi) in lo_hi.items():
        v = target.get(s, 0.0)
        if v > hi:
            out.append({"sector": s, "macro_bound": "max", "macro_value": hi, "cio_target": v, "justification": why})
        elif v < lo:
            out.append({"sector": s, "macro_bound": "min", "macro_value": lo, "cio_target": v,
                        "justification": f"{DISPLAY.get(s, s)} sits below the macro floor because the portfolio holds "
                                         "no direct position there and the committee will not add one this cycle "
                                         "without a vetted candidate."})
    return out


IT_JUSTIFICATION = ("Technology exposure comes entirely through the QQQ and SPMO core funds; cutting it to the macro "
                    "cap would mean selling core funds at a taxable gain, so the excess is tolerated and reviewed "
                    "again next run.")


def author_cio_allocation(o: Order) -> dict:
    lpath = o.inp("lookthrough.json")
    lk = load(lpath)
    current = {k: round(float(fval(v) if isinstance(v, Mapping) else v), 4)
               for k, v in (lk.get("sector_weights_pct") or {}).items()}
    target = _targets(current)
    overrides = _it_override(target, IT_JUSTIFICATION)
    return {
        "schema_version": SCHEMA_VERSION, "run_id": "${RUN_ID}", "order_id": o.oid, "agent_role": "cio",
        "mode": "allocation", "as_of": "${AS_OF}", "current_sector_weights": current,
        "target_sector_weights": target, "expand_sectors": ["health_care"],
        "reduce_sectors": ["consumer_staples", "financials"], "constraint_overrides": overrides,
        "rationale": (f"Look-through exposure is concentrated: financials {fmt(current.get('financials'))}% and staples "
                      f"{fmt(current.get('consumer_staples'))}% through single stocks, technology "
                      f"{fmt(current.get('information_technology'))}% through the two core funds. The macro view "
                      "favours defensive growth, so health care is the one expansion sector for Phase 3. Staples and "
                      "financials are trimmed toward the middle of their macro bands. Technology stays above the "
                      "macro cap for tax reasons (see the override)."),
        "conviction": 6, "conviction_reason": "Direction is clear; the size of the shifts is deliberately modest.",
        "key_figures": figs(
            key_figure("Look-through technology weight", lpath, "/sector_weights_pct/information_technology",
                       "D:YF:QQQ:FUND", kind="other"),
            key_figure("Look-through health care weight", lpath, "/sector_weights_pct/health_care",
                       "D:YF:QQQ:FUND", kind="other")),
        "sources": o.src("D:YF:QQQ:FUND", "D:YF:SPMO:FUND", "D:PX:QQQ", "D:PX:SPMO", "D:PX:KO", "D:PX:JPM"),
        "missing_data": [],
    }


# ------------------------------------------------------------------------------------------------
# sector heads
# ------------------------------------------------------------------------------------------------

DEVIATIONS: dict[str, dict[str, str]] = {
    "PG": {"action": "HOLD",
           "justification": ("The Devil's Advocate verdict rests on the zero- and low-growth cash-flow cells, which "
                             "ignore the multiple bands and a decade of steady organic growth; the fundamental, "
                             "operator and technical lenses do not show deterioration, so selling on that verdict "
                             "alone is not warranted. Hold and re-test at the next filing."),
           "rebuttal": ("The broken verdict anchors on the low-growth DCF cell only; reported margins and cash "
                        "conversion are stable, and the multiple cells sit near the price.")},
}


def _narrative(t: str, e: Mapping[str, Any], da: Mapping[str, Any]) -> str:
    reps = {r["role"]: r for r in e.get("reports") or []}
    if t in FUND_PROFILE:
        etf = reps.get("etf") or {}
        return (f"{FUND_PROFILE[t]['view']} The ETF lens rates it {etf.get('stance')} "
                f"({etf.get('conviction')}/10) as a {etf.get('role_in_portfolio', 'core').replace('_', ' ')} holding.")
    name = COMPANY.get(t, {}).get("name", t)
    fv = ((reps.get("fundamental") or {}).get("fair_value") or {}).get("base")
    da_v = (da or {}).get("verdict")
    bits = [f"{name}: base fair value {fmt(fv, 2)}" if fv is not None else f"{name}: no base fair value"]
    if da_v:
        bits.append(f"the Devil's Advocate verdict is {da_v}")
    return "; ".join(bits) + "."


def _rr_of(entry: Mapping[str, Any]) -> Any:
    rr = entry.get("rr")
    return rr.get("rr") if isinstance(rr, Mapping) else rr


def author_sector_head(o: Order) -> dict:
    ppath = o.inputs[0]
    packet = load(ppath)
    phase = int(packet.get("phase") or (3 if o.mode == "phase3" else 2))
    verdicts = []
    buys, holds = [], []
    srcs: list[str] = []
    for e in packet.get("tickers") or []:
        t = e["ticker"]
        a = e.get("aggregate") or {}
        rule = a.get("rule_action")
        kind = a.get("kind") or e.get("kind")
        unresolved = [f.get("flag_id") for f in ((a.get("flags") or {}).get("unresolved") or []) if f.get("flag_id")]
        dev = DEVIATIONS.get(t) if phase == 2 else None
        action = dev["action"] if dev and dev["action"] != rule else rule
        deviation = action != rule
        reps = {r["role"]: r for r in e.get("reports") or []}
        da = e.get("devils_advocate") or {}
        dissents = []
        for role, r in reps.items():
            if r.get("status") == "ok" and r.get("stance") == "bearish":
                last = [x for x in (r.get("thesis") or "").split(". ") if x][-1].rstrip(".")
                dissents.append({"role": role, "view": f"Bearish ({r.get('conviction')}/10): {last}."})
        if da.get("verdict") in ("damaged", "broken"):
            dissents.append({"role": "devils_advocate",
                             "view": f"Thesis {da['verdict']}: {(da.get('verdict_reason') or '')[:160]}"})
        dissents = [{"role": d["role"], "view": " ".join(d["view"].split()[:38])} for d in dissents][:4]
        conv, ev, rr = a.get("agg_conviction"), a.get("agg_evidence"), _rr_of(a)
        rationale = (_narrative(t, e, da) + " "
                     + f"Rules give {rule} (conviction {fmt(conv)}, evidence {fmt(ev)}"
                     + (f", rr {fmt(rr, 2)}" if rr is not None else "") + "). "
                     + ({"BUY": "All buy tests pass and the DA did not break the thesis.",
                         "PASS": "The buy tests are not met, so the name is passed for now.",
                         "HOLD": "No rule fires; the position is kept at its current weight.",
                         "TRIM": "Conviction sits in the trim band, so the position is reduced.",
                         "ADD": "Conviction, evidence and the position cap allow an increase.",
                         "SELL": "The sell rule fires."}.get(action, ""))
                     + (" The committee deviates from the rule output; see the justification." if deviation else ""))
        v: dict[str, Any] = {
            "ticker": t, "kind": kind, "action": action, "rule_action": rule, "rule_label": a.get("rule_label"),
            "agg_conviction": conv, "agg_evidence": ev, "rr": rr, "rationale": " ".join(rationale.split()[:78]),
            "dissents": dissents, "unresolved_red_flags": unresolved, "rule_deviation": deviation,
            "deviation_justification": dev["justification"] if deviation else None,
            "da_rebuttal": dev["rebuttal"] if deviation and da.get("verdict") == "broken" else None,
        }
        if action == "BUY":
            fund = reps.get("fundamental") or {}
            fv = fund.get("fair_value") or {}
            tech = reps.get("technical") or {}
            prof = COMPANY.get(t, {})
            v.update(thesis=(f"{prof.get('name', t)}: quality {prof.get('biz', 'franchise')} priced below the blended "
                             f"valuation grid; base fair value {fmt(fv.get('base'), 2)} with a defined technical stop "
                             f"at {fmt(tech.get('invalidation_price'), 2)}."),
                     fair_value_low=fv.get("low"), fair_value_base=fv.get("base"), fair_value_high=fv.get("high"),
                     invalidation_price=tech.get("invalidation_price"))
            buys.append(t)
        elif action in ("HOLD", "ADD", "TRIM", "SELL"):
            holds.append(f"{t} {action}")
        verdicts.append(v)
        for sid in (f"D:PX:{t}", f"D:SEC:{t}:CF", f"D:YF:{t}:FUND"):
            if sid in o.seeds:
                srcs.append(sid)
    disp = DISPLAY.get(o.sector, o.sector or "")
    if phase == 3:
        summary = (f"{disp} expansion review of {len(verdicts)} finalists: "
                   + (f"{', '.join(buys)} passes every buy rule and is proposed as a new position; " if buys else
                      "no finalist passes the buy rules; ")
                   + "the others are passed on valuation or risk/reward.")
    else:
        summary = (f"{disp} holdings review: " + "; ".join(holds) + "."
                   + (" One verdict deviates from the rule output with a documented justification."
                      if any(v["rule_deviation"] for v in verdicts) else " Every verdict follows the rule output."))
    return {
        "schema_version": SCHEMA_VERSION, "run_id": "${RUN_ID}", "order_id": o.oid, "agent_role": "sector_head",
        "phase": phase, "sector": o.sector, "as_of": "${AS_OF}", "summary": " ".join(summary.split()[:78]),
        "verdicts": verdicts, "sources": o.src(*srcs) or o.src(*list(o.seeds)[:2]), "missing_data": [],
    }


# ------------------------------------------------------------------------------------------------
# CIO reconciliation
# ------------------------------------------------------------------------------------------------


#: Phase 4 final-target deltas vs the current look-through (pp): health care funds the MRK starter
#: position, financials fall with the JPM trim, and every sector SPMO touches keeps enough headroom
#: for the SPMO ADD step (portfolio.py caps buys at the final sector targets). Cash = remainder.
FINAL_DELTAS = {"health_care": 3.4, "financials": -2.2, "information_technology": 0.8,
                "communication_services": 0.4, "industrials": 0.3, "consumer_discretionary": 0.2,
                "consumer_staples": 0.15, "energy": 0.05, "utilities": 0.05, "materials": 0.05,
                "real_estate": 0.05}


def _final_targets(packet: Mapping[str, Any]) -> dict[str, float]:
    cur = {s["sector"]: float(s["cur_lookthrough_pct"]) for s in (packet.get("preview") or {}).get("sectors") or []
           if s.get("sector") in common.SECTOR_KEYS and s.get("cur_lookthrough_pct") is not None}
    t = {k: round(v + FINAL_DELTAS.get(k, 0.0), 2) for k, v in cur.items()}
    t["cash"] = round(100.0 - sum(t.values()), 2)
    return t


def author_cio_final(o: Order) -> dict:
    ppath = o.inp("cio_packet.json")
    packet = load(ppath)
    p1 = packet.get("cio_allocation") or {}
    p1_t = p1.get("target_sector_weights") or {}
    target = _final_targets(packet)
    final_actions, buys, passes, devs = [], [], [], []
    acts: dict[str, str] = {}
    for dg in packet.get("verdicts") or []:
        for tk in dg.get("tickers") or []:
            act = tk.get("action")
            acts[tk["ticker"]] = act
            if tk.get("kind") == "holding" or act == "BUY":
                final_actions.append({"ticker": tk["ticker"], "action": act, "source_verdict_action": act,
                                      "override": False})
            if act == "BUY":
                buys.append(tk["ticker"])
            elif act == "PASS":
                passes.append(tk["ticker"])
            if tk.get("rule_deviation"):
                devs.append(f"{tk['ticker']} ({tk.get('rule_action')} -> {act})")
    for fa in final_actions:
        if fa["ticker"] == "SPMO" and fa["action"] == "ADD":
            fa["note"] = "Final sector targets leave headroom for the add step."
    recon = [
        {"topic": "Technology above the macro cap",
         "phase1_position": f"Keep technology near {fmt(p1_t.get('information_technology'))}% despite the 20% macro cap.",
         "findings": f"Fund verdicts: QQQ {acts.get('QQQ')}, SPMO {acts.get('SPMO')}; overlap between the funds is moderate.",
         "resolution": "Tolerate the excess (override kept) and allow the small SPMO add."},
        {"topic": "Expand health care",
         "phase1_position": f"Raise health care to {fmt(p1_t.get('health_care'))}% through Phase 3 stock picking.",
         "findings": (f"Phase 3 produced {len(buys)} BUY ({', '.join(buys) or 'none'}) and passed "
                      f"{', '.join(passes) or 'none'}."),
         "resolution": f"Initiate {', '.join(buys) or 'nothing'} at the starter size; the final target is "
                       f"{fmt(target.get('health_care'))}%, the rest of the gap stays in cash."},
        {"topic": "Reduce consumer staples",
         "phase1_position": f"Trim staples to {fmt(p1_t.get('consumer_staples'))}%.",
         "findings": f"The staples head held KO and PG ({'; '.join(devs) or 'no deviation'}).",
         "resolution": "Defer the staples reduction; keep the current weight and re-test after the next filings."},
        {"topic": "Reduce financials",
         "phase1_position": f"Trim financials to {fmt(p1_t.get('financials'))}%.",
         "findings": f"The financials head returned JPM {acts.get('JPM')} and BRK-B {acts.get('BRK-B')}.",
         "resolution": f"The JPM trim delivers the reduction; final target {fmt(target.get('financials'))}%."},
    ]
    for c in (packet.get("conflicts") or [])[:6]:
        recon.append({"topic": str(c.get("topic") or c.get("type") or "Conflict")[:120],
                      "phase1_position": str(c.get("phase1") or c.get("cio_position") or "Phase 1 target.")[:300],
                      "findings": str(c.get("detail") or c.get("findings") or c.get("message") or "See the verdict.")[:300],
                      "resolution": "Follow the sector verdict; sizing is set by portfolio.py within the CIO target."})
    overrides = _it_override(target, IT_JUSTIFICATION)
    da_broken = [t for t, c in COMPANY.items() if c.get("da") == "broken" and t in acts]
    da_damaged = [t for t, c in COMPANY.items() if c.get("da") == "damaged" and t in acts]
    return {
        "schema_version": SCHEMA_VERSION, "run_id": "${RUN_ID}", "order_id": o.oid, "agent_role": "cio",
        "mode": "reconciliation", "as_of": "${AS_OF}",
        "executive_summary": [
            "Late-cycle easing: tilt toward defensive growth while keeping the core funds.",
            f"Health care is the expansion sector; Phase 3 proposes {', '.join(buys) or 'no'} BUY at starter size.",
            "SPMO is added on momentum and cost; JPM is trimmed after the Devil's Advocate damaged its thesis.",
            ("PG is held: the staples head rebutted a broken Devil's Advocate verdict." if devs else
             "Every sector verdict follows its rule output."),
            "Technology stays above the macro cap through the core funds (documented override).",
        ],
        "reconciliation": recon[:12], "final_target_sector_weights": target, "final_actions": final_actions,
        "constraint_overrides": overrides,
        "top_risks": ["A growth scare would hit financials and the technology-heavy funds together.",
                      "The new health care position depends on pipeline delivery before a patent expiry.",
                      "Fund overlap concentrates exposure in a handful of mega-cap names.",
                      "Open litigation and regulatory items flagged by OSINT could resurface."],
        "dissent_summary": (f"The Devil's Advocate broke the {', '.join(da_broken) or 'no'} thesis and damaged "
                            f"{', '.join(da_damaged) or 'none'}; the staples head rebutted the broken verdict and held. "
                            "Bearish fundamental reads on JPM and on expensive health care finalists were reflected in "
                            "the TRIM and PASS outcomes. No dissent changed a final action."),
        "conviction": 6, "conviction_reason": "Most findings agree with the Phase 1 direction; sizing is conservative.",
        "sources": o.src("D:PX:QQQ", "D:PX:SPMO", "D:PX:KO", "D:PX:JPM", "D:FRED:DFF", "D:YF:QQQ:FUND"),
        "missing_data": [],
    }


AUTHORS = {
    "fundamental": author_fundamental, "technical": author_technical, "osint": author_osint,
    "operator_ceo": author_operator, "industry_ops": author_industry, "devils_advocate": author_da,
    "etf": author_etf, "macro": author_macro, "sector_head": author_sector_head,
}


def author(o: Order) -> dict:
    if o.role == "cio":
        return author_cio_allocation(o) if o.mode == "allocation" else author_cio_final(o)
    return AUTHORS[o.role](o)


# ================================================================================================
# driver
# ================================================================================================


def _tokenize(text: str, run_dir: Path) -> str:
    text = text.replace(posix(run_dir), "${RUN_DIR}")
    return text.replace(common.REPO_ROOT.as_posix(), "${REPO_ROOT}")


class Builder:
    def __init__(self, fixtures_root: Path):
        self.fx = Path(fixtures_root)
        self.out_dir = self.fx / "agent_outputs"
        self.errors: list[str] = []
        self.count = 0

    def batch(self, run_dir: Path, payload: dict, fixtures_root: Path) -> dict:
        manifest = committee.load_manifest(run_dir)
        written = []
        for order in payload.get("orders") or []:
            oid = order["order_id"]
            rec = manifest["orders"][oid]
            if int(rec.get("attempt") or 1) > 1:
                fm = validate.parse_order_md(rec["order_path"])
                raise RuntimeError(f"{oid} was rejected by the validator:\n  " + "\n  ".join(fm.get("prior_errors") or []))
            o = Order(Path(rec["order_path"]), rec, run_dir)
            doc = author(o)
            text = json.dumps(doc, indent=1, ensure_ascii=False)
            run_text = text.replace("${RUN_ID}", manifest["run_id"]).replace("${AS_OF}", str(manifest["as_of"]))
            out = Path(rec["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(run_text + "\n")
            rep = validate.validate_order_output_report(rec["order_path"], out)
            if rep.errors:
                self.errors += [f"{oid}: {e}" for e in rep.errors]
                print(f"INVALID {oid}:\n  " + "\n  ".join(rep.errors[:12]), flush=True)
            with open(self.out_dir / f"{oid}.json", "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_tokenize(text, run_dir) + "\n")
            written.append(oid)
            self.count += 1
        committee.log(run_dir, f"[build_fixtures] authored {len(written)} agent output(s)")
        return {"written": written, "missing": []}


def build_agent_outputs(fixtures_root: Path, *, keep_run: Path | None = None) -> dict:
    fx = Path(fixtures_root)
    out_dir = fx / "agent_outputs"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    builder = Builder(fx)
    tmp = Path(tempfile.mkdtemp(prefix="aginv_build_")) if keep_run is None else Path(keep_run)
    import os
    saved = {k: os.environ.get(k) for k in common.AGINV_ENV_VARS}
    os.environ[common.ENV_RUNS_DIR] = str(tmp / "runs")
    for k in (common.ENV_CACHE_DIR, common.ENV_LEDGER):
        os.environ.pop(k, None)
    orig = committee.simulate_agents
    committee.simulate_agents = builder.batch
    try:
        code, res = committee.run_dry_run(fixtures_dir=fx)
    finally:
        committee.simulate_agents = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        common.clear_caches()
    print(json.dumps({k: res.get(k) for k in ("type", "run_dir", "memo_md", "orders", "summary", "message",
                                              "error", "step")}, indent=1))
    print(f"authored {builder.count} outputs; {len(builder.errors)} validation errors")
    if keep_run is None and not builder.errors and code == 0:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"code": code, "result": res, "errors": builder.errors}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-run", default=None, help="runs parent dir to keep for inspection")
    a = ap.parse_args()
    r = build_agent_outputs(HERE, keep_run=Path(a.keep_run) if a.keep_run else None)
    sys.exit(0 if r["code"] == 0 and not r["errors"] else 1)
