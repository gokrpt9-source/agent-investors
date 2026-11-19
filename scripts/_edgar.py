"""Pure SEC EDGAR companyfacts / submissions parsing (DESIGN.md §4.3, algorithm steps 3-13).

No IO and no network: ``fetch_fundamentals.py`` downloads (through the ``common`` seam), caches and
turns the results of this module into Facts. Everything here works on the JSON shapes that
``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` and
``https://data.sec.gov/submissions/CIK##########.json`` return.

Algorithm (thresholds come from ``committee.yaml: fundamentals``; ``settings()`` merges them over
``DEFAULTS``):

* **Tag coverage**: capex also tries ``PaymentsToAcquireOtherPropertyPlantAndEquipment`` and
  ``PaymentsForCapitalImprovements``; D&A falls back to ``Depreciation`` + ``AmortizationOfIntangibleAssets``
  (same period) when no named D&A tag is current.
* **Normalize** (step 5): keep forms 10-K/10-Q(/A) (20-F/40-F only for FY values and instants);
  an observation without ``start`` is an instant (bucket ``I``); durations are bucketed by days
  (``Q`` 80-100, ``H`` 160-200, ``9M`` 245-290, ``FY`` 350-380, anything else dropped; the wider
  H/9M bands and ``quarter_gap_days`` 84-120 cover 52/53-week retail calendars); duplicates
  of the same ``(start, end)`` keep the latest ``filed`` (restatements win).
* **Tag choice** (step 6): the candidate tag with the latest ``end`` wins (ties -> priority order).
  FY history: per fiscal year the first tag with a value, trying the chosen (current) tag first and
  then the others in priority order; mixing tags inside the CAGR window -> ``stitched`` + ``tag_mix``.
* **TTM** (step 7): latest period is a FY -> that FY; else ``FY_prev + YTD_cur - YTD_prior`` (same
  bucket, prior end one year +/- tolerance); fallback: the last four contiguous discrete quarters
  (Q4 = FY - 9M; quarter ends 84-120 days apart). Neither -> null ``insufficient_history``.
* **Instants** (step 8): latest ``end``; components on different instants -> ``mixed_instants``.
* **CAGR** (step 9): FY_latest vs FY_latest-N, both > 0 else ``negative_denominator``.
* **Debt** (step 4): formulas (a)-(f) (``DEBT_FORMULAS``); a formula is a candidate when all its legs
  exist at one common instant within ``debt_formula_max_gap_days`` of the newest balance sheet, and the
  candidate with the LATEST common instant wins (ties -> priority a, b, c, d, e, f), so a stale
  ``LongTermDebt`` never beats a newer noncurrent + current pair (ABT, GOOGL). (b)'s current leg is
  ``LongTermDebtCurrent`` or ``DebtCurrent`` (which already includes short-term borrowings: no STB then);
  (e) ``LongTermNotesAndLoans`` + ``NotesPayableCurrent`` (ORCL); (f) ``NotesPayable`` + ``LoansPayable``
  (Realty Income). STB = ShortTermBorrowings else CommercialPaper (at that instant) else 0
  (``stb_assumed_zero``). If no formula is complete, a noncurrent leg alone (b/d/e) is used with flag
  ``debt_current_portion_missing`` (medium).
  If NO formula qualifies and no liability-side debt-related us-gaap tag (``DEBT_TAG_RE``, e.g.
  ``Debt|Borrowing|Notes?Payable|Loans?Payable|ConvertibleNotes|CommercialPaper|LineOfCredit``;
  investment-side ``AvailableForSaleDebtSecurities*`` etc. are skipped) has a USD instant within
  ``debt_formula_max_gap_days`` of the newest balance sheet, the company is treated as debt-free:
  total debt = 0 with flag ``debt_not_reported_assumed_zero`` (medium) and derivation
  ``"no debt-related XBRL tags reported -> 0"`` (live PLTR). Any such tag present keeps the value
  null + ``debt_unavailable`` (the tags exist but are not covered by (a)-(d)).
* **Shares** (step 10): several dei values with the same accession and end are summed (multi-class);
  optional ``shares_tags`` / ``class_factor`` overrides.
* **Lag** (step 13): newest 10-K/10-Q in ``filings.recent`` whose accession is absent from the
  companyfacts accessions -> lag.

Staleness guard (extension of DESIGN §4.3): a concept whose newest value is older than
``concept_max_gap_days`` (default = ``debt_formula_max_gap_days``) before the latest reported period
is treated as not reported (null + ``stale_tag`` flag) instead of silently presenting e.g. a 2013
operating income as current.

Further extensions: gross profit = revenue - cogs also when the GrossProfit TTM ends on a different
period than revenue's (and cogs is aligned); intangibles fall back to
IndefiniteLivedIntangibleAssetsExcludingGoodwill + FiniteLivedIntangibleAssetsNet (both required);
lag detection ignores amendments (``LAG_FORMS``).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

#: Fallback thresholds (mirror ``committee.yaml: fundamentals``; the config always wins).
DEFAULTS: dict[str, Any] = {
    "forms": ["10-K", "10-Q", "10-K/A", "10-Q/A"],
    "fy_extra_forms": ["20-F", "40-F"],
    # H/9M/quarter-gap are wide enough for 52/53-week retail calendars (COST, PEP: 12/12/12/16-week
    # quarters -> 24-week H = 168 d, 36-week 9M = 252 d, a 16-week Q4 ends 112 d after the 9M end)
    "duration_days": {"Q": [80, 100], "H": [160, 200], "9M": [245, 290], "FY": [350, 380]},
    "ytd_prior_tolerance_days": 10,
    "quarter_gap_days": [84, 120],
    "debt_formula_max_gap_days": 400,
    "share_count_divergence_pct": 10,
    "tax_rate_bounds": [0.0, 0.35],
    "default_tax_rate": 0.21,
    "cagr_years": 3,
    "financials_sic": [6000, 6799],
    "reit_sic": [6798],
}
#: Not (yet) in committee.yaml -> module defaults, overridable through the same section.
DEFAULT_FY_HISTORY_YEARS = 5          # fundamentals.fy_history_years
DEFAULT_SUBMISSIONS_KEEP = 40         # fundamentals.submissions_keep_recent
DEFAULT_SUBMISSIONS_PERIODIC = 8      # periodic filings kept beyond the first N recent rows

YTD_BUCKETS = ("9M", "H", "Q")        # longest first
BUCKET_MONTHS = {"Q": 3, "H": 6, "9M": 9, "FY": 12}
PERIODIC_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")
#: Lag detection (step 13) looks at the newest ORIGINAL 10-Q/10-K only: an amendment (often
#: exhibit-only, without financial XBRL) must not raise companyfacts_lag.
LAG_FORMS = ("10-K", "10-Q")
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "40-F", "20-F/A", "40-F/A")


def settings(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``DEFAULTS`` overlaid with ``cfg`` (the ``fundamentals`` section of committee.yaml)."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        out[k] = v
    out.setdefault("fy_history_years", DEFAULT_FY_HISTORY_YEARS)
    out.setdefault("concept_max_gap_days", out["debt_formula_max_gap_days"])
    out.setdefault("submissions_keep_recent", DEFAULT_SUBMISSIONS_KEEP)
    return out


# --------------------------------------------------------------------------------------------
# Concepts -> candidate tags (priority order). Bare names are us-gaap; "dei:" prefix for dei.
# --------------------------------------------------------------------------------------------

FLOW_CONCEPTS: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "RevenuesNetOfInterestExpense"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsForCapitalImprovements"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
           "DepreciationAmortizationAndAccretionNet"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
}
INSTANT_CONCEPTS: dict[str, list[str]] = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill"],
}
SHARE_TAGS: list[str] = ["dei:EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding",
                         "WeightedAverageNumberOfDilutedSharesOutstanding"]
#: Debt formulas in priority order. Each formula is a list of LEGS; every leg is a list of alternative
#: tags (the first one present at the chosen instant is used), all legs must exist at ONE common instant.
#: (b)'s current leg may be ``LongTermDebtCurrent`` or ``DebtCurrent`` (LLY, UNH); ``DebtCurrent`` already
#: contains short-term borrowings, so no STB is added with it. (e) ORCL, (f) Realty Income style filers.
DEBT_FORMULAS: list[tuple[str, list[list[str]]]] = [
    ("a", [["LongTermDebt"]]),
    ("b", [["LongTermDebtNoncurrent"], ["LongTermDebtCurrent", "DebtCurrent"]]),
    ("c", [["LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"]]),
    ("d", [["LongTermDebtAndCapitalLeaseObligations"], ["LongTermDebtAndCapitalLeaseObligationsCurrent"]]),
    ("e", [["LongTermNotesAndLoans"], ["NotesPayableCurrent"]]),
    ("f", [["NotesPayable"], ["LoansPayable"]]),
]
#: Current legs that already include short-term borrowings (never add STB on top of them).
STB_INCLUSIVE_TAGS = frozenset({"DebtCurrent"})
#: Formulas whose first leg is a NONCURRENT balance: usable alone (flag ``debt_current_portion_missing``)
#: when the current leg is not reported at that instant and no complete formula qualifies.
NONCURRENT_LEG_FORMULAS = frozenset({"b", "d", "e"})
STB_TAGS: list[str] = ["ShortTermBorrowings", "CommercialPaper"]
#: D&A fallback when none of FLOW_CONCEPTS["da"] is current (MSFT, GOOGL, AVGO, TSLA, INTC):
#: Depreciation + AmortizationOfIntangibleAssets, summed over the SAME period.
DA_PARTS: list[str] = ["Depreciation", "AmortizationOfIntangibleAssets"]
#: Debt-like us-gaap tag names. When formulas (a)-(d) all fail, a company with NONE of these at a
#: recent USD instant is treated as debt-free (``debt_not_reported_assumed_zero``); any hit keeps total
#: debt null because it has debt-like items the formulas do not cover.
DEBT_TAG_RE = re.compile(r"Debt|Borrowing|Notes?Payable|Loans?Payable|ConvertibleNotes|CommercialPaper|LineOfCredit",
                         re.IGNORECASE)
#: Investment-side tags that match ``DEBT_TAG_RE`` but are ASSETS (a debt-free company with a treasury
#: portfolio reports AvailableForSaleDebtSecurities* every quarter - live: PLTR), so they never count.
DEBT_TAG_SKIP_RE = re.compile(r"AvailableForSale|HeldToMaturity|TradingSecurities|MarketableSecurities|"
                              r"DebtAndEquitySecurities|(?<!Convertible)DebtSecurities|InvestmentsInDebt",
                              re.IGNORECASE)
ASSUMED_ZERO_DERIVATION = "no debt-related XBRL tags reported -> 0"
#: Intangibles fallback (extension of DESIGN step 3) when IntangibleAssetsNetExcludingGoodwill is not
#: reported: indefinite-lived + finite-lived intangibles, both required at one common instant.
INTANGIBLE_PARTS: list[str] = ["IndefiniteLivedIntangibleAssetsExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]
#: Flow concepts reported in the per-FY history.
FY_HISTORY_CONCEPTS = ("revenue", "operating_income", "net_income", "ocf", "capex")


def all_tags() -> list[str]:
    """Every tag this module reads (used to trim cached companyfacts)."""
    tags: list[str] = []
    for group in (*FLOW_CONCEPTS.values(), *INSTANT_CONCEPTS.values(), SHARE_TAGS, STB_TAGS, INTANGIBLE_PARTS,
                  DA_PARTS, *(alts for _f, legs in DEBT_FORMULAS for alts in legs)):
        for t in group:
            if t not in tags:
                tags.append(t)
    return tags


def split_tag(tag: str) -> tuple[str, str]:
    """``"dei:X"`` -> ("dei", "X"); bare names -> ("us-gaap", name)."""
    if ":" in tag:
        ns, name = tag.split(":", 1)
        return ns, name
    return "us-gaap", tag


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def to_date(x: Any) -> date | None:
    if x is None or x == "":
        return None
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def to_num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fiscal_year(end: date) -> int:
    """Fiscal-year label of a period ending on ``end`` (52/53-week years ending in the first days
    of January belong to the previous year)."""
    return end.year - 1 if (end.month == 1 and end.day <= 10) else end.year


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


# --------------------------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Obs:
    """One normalized companyfacts observation (or a derived discrete Q4)."""
    tag: str
    val: float
    end: date
    start: date | None
    bucket: str            # "I" | "Q" | "H" | "9M" | "FY"
    accn: str
    form: str
    filed: date
    derived: str | None = None   # e.g. "FY2025-9M2025" for a derived Q4

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


@dataclass
class Value:
    """A derived number with provenance (turned into a Fact by fetch_fundamentals)."""
    value: float | None
    end: date | None = None
    filed: date | None = None
    tag: str | None = None
    derivation: str | None = None
    na_reason: str | None = None
    accn: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None

    @staticmethod
    def na(reason: str = "missing", **kw: Any) -> "Value":
        return Value(None, na_reason=reason, **kw)


def bucket_for(days: int | None, cfg: Mapping[str, Any]) -> str | None:
    """Duration bucket for ``days`` (None -> instant "I"); None when outside every bucket."""
    if days is None:
        return "I"
    for name, (lo, hi) in (cfg.get("duration_days") or DEFAULTS["duration_days"]).items():
        if lo <= days <= hi:
            return str(name)
    return None


def _units(cf: Mapping[str, Any], tag: str) -> dict[str, list[dict]]:
    ns, name = split_tag(tag)
    node = ((cf or {}).get("facts") or {}).get(ns, {}).get(name)
    if not isinstance(node, Mapping):
        return {}
    units = node.get("units")
    return dict(units) if isinstance(units, Mapping) else {}


def _form_ok(form: str, bucket: str, cfg: Mapping[str, Any]) -> bool:
    if form in (cfg.get("forms") or DEFAULTS["forms"]):
        return True
    return form in (cfg.get("fy_extra_forms") or DEFAULTS["fy_extra_forms"]) and bucket in ("FY", "I")


def raw_facts(cf: Mapping[str, Any], tag: str, unit: str = "USD", cfg: Mapping[str, Any] | None = None,
              as_of: date | None = None) -> list[Obs]:
    """All accepted observations of ``tag`` in ``unit`` (form filter + bucketing, NOT deduped)."""
    cfg = cfg or settings()
    out: list[Obs] = []
    for f in _units(cf, tag).get(unit) or []:
        if not isinstance(f, Mapping):
            continue
        end, start, filed = to_date(f.get("end")), to_date(f.get("start")), to_date(f.get("filed"))
        val = to_num(f.get("val"))
        if end is None or filed is None or val is None:
            continue
        if as_of is not None and filed > as_of:
            continue                                   # point-in-time: not yet filed on as_of
        days = None if start is None else (end - start).days
        bucket = bucket_for(days, cfg)
        if bucket is None:
            continue
        form = str(f.get("form") or "")
        if not _form_ok(form, bucket, cfg):
            continue
        out.append(Obs(tag=tag, val=val, end=end, start=start, bucket=bucket,
                       accn=str(f.get("accn") or ""), form=form, filed=filed))
    return out


def normalize(cf: Mapping[str, Any], tag: str, unit: str = "USD", cfg: Mapping[str, Any] | None = None,
              as_of: date | None = None) -> list[Obs]:
    """Accepted observations deduped by ``(start, end)`` keeping the latest ``filed`` (then the
    larger accession), sorted by ``(end, start)``."""
    best: dict[tuple[date | None, date], Obs] = {}
    for o in raw_facts(cf, tag, unit, cfg, as_of):
        key = (o.start, o.end)
        cur = best.get(key)
        if cur is None or (o.filed, o.accn) > (cur.filed, cur.accn):
            best[key] = o
    return sorted(best.values(), key=lambda o: (o.end, o.start or o.end))


# --------------------------------------------------------------------------------------------
# TTM (step 7)
# --------------------------------------------------------------------------------------------


def _label(o: Obs) -> str:
    if o.bucket == "FY":
        return f"FY{fiscal_year(o.end)}"
    return f"{BUCKET_MONTHS[o.bucket]}M{o.end.year}"


def ttm(obs: Iterable[Obs], cfg: Mapping[str, Any] | None = None, *, at: date | None = None) -> Value:
    """Trailing-twelve-month value from one tag's normalized observations.

    ``at`` restricts to observations ending on/before ``at`` and requires the TTM to end within
    ``ytd_prior_tolerance_days`` of it (used for the prior-year TTM)."""
    cfg = cfg or settings()
    tol = int(cfg.get("ytd_prior_tolerance_days", 10))
    dur = [o for o in obs if o.bucket != "I" and (at is None or o.end <= at + timedelta(days=tol))]
    if not dur:
        return Value.na("missing")
    latest_end = max(o.end for o in dur)
    if at is not None and abs((latest_end - at).days) > tol:
        return Value.na("insufficient_history")
    fys = [o for o in dur if o.bucket == "FY"]
    tag = dur[0].tag
    # (1) latest period is a fiscal year
    fy_now = [o for o in fys if o.end == latest_end]
    if fy_now:
        o = max(fy_now, key=lambda x: (x.filed, x.accn))
        return Value(o.val, o.end, o.filed, o.tag, _label(o), accn=o.accn)
    # (2) FY_prev + YTD_cur - YTD_prior
    cands = sorted((o for o in dur if o.end == latest_end and o.bucket in YTD_BUCKETS),
                   key=lambda o: -(o.days or 0))
    for ytd in cands:
        if ytd.start is None:
            continue
        prev = [f for f in fys if abs((ytd.start - (f.end + timedelta(days=1))).days) <= tol]
        if not prev:
            continue
        fy_prev = max(prev, key=lambda f: f.end)
        priors = [o for o in dur if o.bucket == ytd.bucket and o.end < ytd.end
                  and abs((ytd.end - o.end).days - 365) <= tol]
        if not priors:
            continue
        prior = min(priors, key=lambda o: abs((ytd.end - o.end).days - 365))
        value = fy_prev.val + ytd.val - prior.val
        deriv = f"{_label(fy_prev)}+{_label(ytd)}-{_label(prior)}"
        return Value(value, ytd.end, max(ytd.filed, fy_prev.filed, prior.filed), tag, deriv, accn=ytd.accn)
    # (3) four contiguous discrete quarters (Q4 = FY - 9M)
    q = quarters(dur, cfg)
    chain = _quarter_chain(q, latest_end, cfg)
    if chain:
        value = sum(o.val for o in chain)
        parts = [o.end.isoformat() + (f"({o.derived})" if o.derived else "") for o in chain]
        return Value(value, latest_end, max(o.filed for o in chain), tag, "4Q[" + "+".join(parts) + "]",
                     accn=chain[-1].accn)
    return Value.na("insufficient_history", tag=tag)


def quarters(dur: Iterable[Obs], cfg: Mapping[str, Any] | None = None) -> dict[date, Obs]:
    """Discrete quarters by end date: reported Q observations plus derived Q4 = FY - 9M."""
    cfg = cfg or settings()
    tol = int(cfg.get("ytd_prior_tolerance_days", 10))
    lo, hi = (cfg.get("quarter_gap_days") or DEFAULTS["quarter_gap_days"])
    dur = list(dur)
    out: dict[date, Obs] = {}
    for o in dur:
        if o.bucket == "Q":
            cur = out.get(o.end)
            if cur is None or (o.filed, o.accn) > (cur.filed, cur.accn):
                out[o.end] = o
    for fy in (o for o in dur if o.bucket == "FY"):
        if fy.end in out or fy.start is None:
            continue
        nines = [o for o in dur if o.bucket == "9M" and o.start is not None
                 and abs((o.start - fy.start).days) <= tol and o.end < fy.end
                 and lo <= (fy.end - o.end).days <= hi]
        if not nines:
            continue
        nine = max(nines, key=lambda o: o.end)
        out[fy.end] = Obs(tag=fy.tag, val=fy.val - nine.val, end=fy.end, start=nine.end + timedelta(days=1),
                          bucket="Q", accn=fy.accn, form=fy.form, filed=max(fy.filed, nine.filed),
                          derived=f"{_label(fy)}-{_label(nine)}")
    return out


def _quarter_chain(q: Mapping[date, Obs], latest_end: date, cfg: Mapping[str, Any]) -> list[Obs] | None:
    lo, hi = (cfg.get("quarter_gap_days") or DEFAULTS["quarter_gap_days"])
    if latest_end not in q:
        return None
    chain = [q[latest_end]]
    while len(chain) < 4:
        prev_end = chain[0].end
        options = [e for e in q if lo <= (prev_end - e).days <= hi]
        if not options:
            return None
        chain.insert(0, q[max(options)])
    return chain


# --------------------------------------------------------------------------------------------
# Concept selection, FY history (step 6)
# --------------------------------------------------------------------------------------------


@dataclass
class FlowResult:
    concept: str
    tag: str | None
    ttm: Value
    prior_ttm: Value | None
    fy: dict[int, Obs]
    fy_tags: dict[int, str]
    stitched: bool
    tags_present: list[str]


def flow(cf: Mapping[str, Any], concept: str, cfg: Mapping[str, Any] | None = None, *,
         as_of: date | None = None, tags: list[str] | None = None) -> FlowResult:
    """TTM + FY history of a flow concept (candidate tag with the latest end wins)."""
    cfg = cfg or settings()
    tags = list(tags or FLOW_CONCEPTS[concept])
    series = {t: [o for o in normalize(cf, t, "USD", cfg, as_of) if o.bucket != "I"] for t in tags}
    present = [t for t in tags if series[t]]
    if not present:
        return FlowResult(concept, None, Value.na("missing"), None, {}, {}, False, [])
    order = sorted(present, key=lambda t: (-max(o.end for o in series[t]).toordinal(), tags.index(t)))
    chosen, result = order[0], None
    for t in order:
        v = ttm(series[t], cfg)
        if v.ok:
            chosen, result = t, v
            break
    if result is None:
        result = ttm(series[order[0]], cfg)
        result.tag = order[0]
    prior = None
    if result.ok and result.end is not None:
        prior = ttm(series[chosen], cfg, at=result.end - timedelta(days=365))
    # FY history: chosen tag first, then the rest in priority order
    hist_order = [chosen] + [t for t in tags if t != chosen and t in present]
    fy: dict[int, Obs] = {}
    fy_tags: dict[int, str] = {}
    for t in hist_order:
        by_year: dict[int, Obs] = {}
        for o in series[t]:
            if o.bucket != "FY":
                continue
            y = fiscal_year(o.end)
            cur = by_year.get(y)
            if cur is None or (o.end, o.filed, o.accn) > (cur.end, cur.filed, cur.accn):
                by_year[y] = o
        for y, o in by_year.items():
            if y not in fy:
                fy[y], fy_tags[y] = o, t
    stitched = False
    if fy:
        last = max(fy)
        window = [fy_tags[y] for y in fy if last - int(cfg.get("cagr_years", 3)) <= y <= last]
        stitched = len(set(window)) > 1
    return FlowResult(concept, chosen, result, prior, fy, fy_tags, stitched, present)


@dataclass
class InstantResult:
    concept: str
    tag: str | None
    value: Value
    series: dict[str, list[Obs]]


def instant(cf: Mapping[str, Any], concept: str | None = None, cfg: Mapping[str, Any] | None = None, *,
            as_of: date | None = None, tags: list[str] | None = None, unit: str = "USD") -> InstantResult:
    """Latest instant value of a balance-sheet concept (candidate tag with the latest end)."""
    cfg = cfg or settings()
    tags = list(tags or INSTANT_CONCEPTS[concept or ""])
    series = {t: [o for o in normalize(cf, t, unit, cfg, as_of) if o.bucket == "I"] for t in tags}
    present = [t for t in tags if series[t]]
    if not present:
        return InstantResult(concept or "", None, Value.na("missing"), series)
    chosen = sorted(present, key=lambda t: (-series[t][-1].end.toordinal(), tags.index(t)))[0]
    o = max(series[chosen], key=lambda x: (x.end, x.filed, x.accn))
    return InstantResult(concept or "", chosen, Value(o.val, o.end, o.filed, chosen, None, accn=o.accn), series)


def da_from_parts(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *, as_of: date | None = None,
                  ref_end: date | None = None) -> Value | None:
    """D&A TTM = Depreciation + AmortizationOfIntangibleAssets when both TTMs end on the same period
    (and are current); None otherwise."""
    cfg = cfg or settings()
    max_gap = int(cfg.get("concept_max_gap_days", cfg.get("debt_formula_max_gap_days", 400)))
    dep = flow(cf, "da_depreciation", cfg, as_of=as_of, tags=[DA_PARTS[0]]).ttm
    amo = flow(cf, "da_amortization", cfg, as_of=as_of, tags=[DA_PARTS[1]]).ttm
    if not (dep.ok and amo.ok) or dep.end != amo.end or dep.end is None:
        return None
    if ref_end and (ref_end - dep.end).days > max_gap:
        return None
    tag = "+".join(DA_PARTS)
    return Value(dep.value + amo.value, dep.end, max(dep.filed, amo.filed), tag,  # type: ignore[operator,type-var]
                 f"depreciation + amortization of intangibles ({dep.derivation})", accn=dep.accn)


def intangibles_from_parts(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *,
                           as_of: date | None = None) -> InstantResult:
    """Intangibles = IndefiniteLivedIntangibleAssetsExcludingGoodwill + FiniteLivedIntangibleAssetsNet
    at their latest common instant (both required; otherwise missing)."""
    cfg = cfg or settings()
    series = {t: {o.end: o for o in normalize(cf, t, "USD", cfg, as_of) if o.bucket == "I"}
              for t in INTANGIBLE_PARTS}
    plain = {t: sorted(s.values(), key=lambda o: o.end) for t, s in series.items()}
    if not all(series.values()):
        return InstantResult("intangibles", None, Value.na("missing"), plain)
    common_ends = set.intersection(*(set(s) for s in series.values()))
    if not common_ends:
        return InstantResult("intangibles", None, Value.na("missing"), plain)
    inst = max(common_ends)
    parts = [series[t][inst] for t in INTANGIBLE_PARTS]
    tag = "+".join(INTANGIBLE_PARTS)
    return InstantResult("intangibles", tag,
                         Value(sum(o.val for o in parts), inst, max(o.filed for o in parts), tag,
                               "indefinite-lived + finite-lived intangibles", accn=parts[0].accn), plain)


# --------------------------------------------------------------------------------------------
# Debt (step 4)
# --------------------------------------------------------------------------------------------


@dataclass
class DebtResult:
    value: Value
    formula: str | None
    components: dict[str, float]
    stb_tag: str | None
    stb_assumed_zero: bool
    rejected: list[str]          # "(a) LongTermDebt latest 2024-03-29 is 735 d before 2026-04-03"
    any_tags: bool
    assumed_zero: bool = False   # no formula qualified and no debt-related tag is current -> 0
    current_portion_missing: bool = False   # noncurrent leg used alone (current leg absent at its instant)
    blocking_tags: list[str] = field(default_factory=list)   # debt-related tags that kept the value null
    scan_available: bool = True  # False: trimmed (legacy) cache without the debt-tag scan -> never assume 0


def _leg_name(alts: list[str]) -> str:
    return "|".join(alts)


def total_debt(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *, newest_bs: date | None = None,
               as_of: date | None = None) -> DebtResult:
    """Total debt from the best qualifying formula.

    Every formula whose legs all exist at one common instant within ``debt_formula_max_gap_days`` of
    the newest balance sheet is a candidate; the one with the LATEST common instant wins (ties ->
    priority order a, b, c, d, e, f), so a stale ``LongTermDebt`` series never beats a newer
    ``LongTermDebtNoncurrent`` + current-leg pair. If no complete formula qualifies, a formula's
    noncurrent leg alone is used at its latest instant (``current_portion_missing``)."""
    cfg = cfg or settings()
    max_gap = int(cfg.get("debt_formula_max_gap_days", 400))
    comp_tags = sorted({t for _f, legs in DEBT_FORMULAS for alts in legs for t in alts})
    series = {t: {o.end: o for o in normalize(cf, t, "USD", cfg, as_of) if o.bucket == "I"}
              for t in comp_tags + STB_TAGS}
    any_tags = any(series[t] for t in comp_tags)
    reference = newest_bs
    if reference is None:
        ends = [e for t in comp_tags for e in series[t]]
        reference = max(ends) if ends else None
    ref_txt = reference.isoformat() if reference else "?"
    rejected: list[str] = []
    cands: list[tuple[date, int, str, list[list[str]]]] = []
    for idx, (fid, legs) in enumerate(DEBT_FORMULAS):
        leg_ends = [set().union(*(set(series[t]) for t in alts)) for alts in legs]
        if not all(leg_ends):
            continue
        label = "+".join(_leg_name(alts) for alts in legs)
        common_ends = set.intersection(*leg_ends)
        if not common_ends:
            rejected.append(f"({fid}) {label}: no common instant")
            continue
        inst = max(common_ends)
        gap = (reference - inst).days if reference else 0
        if gap > max_gap:
            rejected.append(f"({fid}) {label}: latest {inst.isoformat()} is {gap} d before {ref_txt} (> {max_gap} d)")
            continue
        cands.append((inst, idx, fid, legs))
    partial = False
    if cands:
        inst, _idx, fid, legs = max(cands, key=lambda c: (c[0], -c[1]))
        chosen = [next(t for t in alts if inst in series[t]) for alts in legs]
    else:
        # last resort: a noncurrent leg whose current partner is not reported at its instant
        part: list[tuple[date, int, str, list[str]]] = []
        for idx, (fid, legs) in enumerate(DEBT_FORMULAS):
            if fid not in NONCURRENT_LEG_FORMULAS or not any(series[t] for t in legs[0]):
                continue
            nc = next(t for t in legs[0] if series[t])
            inst = max(series[nc])
            if reference and (reference - inst).days > max_gap:
                continue
            part.append((inst, idx, fid, [nc]))
        if not part:
            return DebtResult(Value.na("missing"), None, {}, None, False, rejected, any_tags)
        inst, _idx, fid, chosen = max(part, key=lambda c: (c[0], -c[1]))
        partial = True
    parts = {t: series[t][inst].val for t in chosen}
    stb_tag, stb_zero = None, False
    if not any(t in STB_INCLUSIVE_TAGS for t in chosen):
        for t in STB_TAGS:
            if inst in series[t]:
                stb_tag = t
                parts[t] = series[t][inst].val
                break
        else:
            stb_zero = True
    total = sum(parts.values())
    filed = max(series[t][inst].filed for t in chosen)
    terms = list(chosen)
    if stb_tag:
        terms.append(stb_tag)
    elif stb_zero:
        terms.append("STB(0, not reported)")
    deriv = f"({fid}) " + " + ".join(terms)
    if partial:
        deriv += " [noncurrent leg only: current portion not reported]"
    return DebtResult(Value(total, inst, filed, "+".join(chosen + ([stb_tag] if stb_tag else [])), deriv,
                            accn=series[chosen[0]][inst].accn, extra={"formula": fid}),
                      fid, parts, stb_tag, stb_zero, rejected, any_tags, current_portion_missing=partial)


def debt_scan_available(cf: Mapping[str, Any]) -> bool:
    """True when ``cf`` still carries every debt-related us-gaap tag: raw companyfacts always do; a
    trimmed cache payload (``trim_companyfacts``, marked by ``accns_all``) only when it was trimmed by a
    version that keeps them (marker ``debt_scan``)."""
    return "accns_all" not in (cf or {}) or bool((cf or {}).get("debt_scan"))


def is_debt_tag(name: str) -> bool:
    """Liability-side debt-like us-gaap tag name (``DEBT_TAG_RE`` minus the investment-side skips)."""
    return bool(DEBT_TAG_RE.search(name)) and not DEBT_TAG_SKIP_RE.search(name)


def debt_tags_near(cf: Mapping[str, Any], reference: date, cfg: Mapping[str, Any] | None = None, *,
                   as_of: date | None = None) -> dict[str, date]:
    """Debt-like us-gaap tags with an accepted currency INSTANT observation within
    ``debt_formula_max_gap_days`` of ``reference`` (the newest balance sheet): ``{tag: newest such end}``."""
    cfg = cfg or settings()
    max_gap = int(cfg.get("debt_formula_max_gap_days", 400))
    out: dict[str, date] = {}
    nodes = ((cf or {}).get("facts") or {}).get("us-gaap") or {}
    for name, node in nodes.items():
        if not is_debt_tag(name) or not isinstance(node, Mapping):
            continue
        for unit in (node.get("units") or {}):
            if not re.fullmatch(r"[A-Z]{3}", str(unit)):
                continue                                  # shares, pure, USD/shares ... are not balances
            for o in raw_facts(cf, name, str(unit), cfg, as_of):
                if o.bucket == "I" and (reference - o.end).days <= max_gap:
                    if name not in out or o.end > out[name]:
                        out[name] = o.end
    return out


def _assume_debt_free(cf: Mapping[str, Any], cfg: Mapping[str, Any], debt: DebtResult,
                      instants: Mapping[str, "InstantResult"], balance: Mapping[str, Value],
                      newest_bs: date | None, as_of: date | None) -> DebtResult:
    """Formulas (a)-(d) failed: if the company reports no liability-side debt-like tag at a recent
    instant (and a real balance sheet exists), total debt is 0 (``assumed_zero``); otherwise keep the
    null result, annotated with the tags (or the legacy-cache reason) that prevented the assumption."""
    scan = debt_scan_available(cf)
    has_bs = newest_bs is not None and (balance["cash"].ok or balance["equity"].ok)
    if not scan or not has_bs:
        return replace(debt, scan_available=scan)
    near = debt_tags_near(cf, newest_bs, cfg, as_of=as_of)          # type: ignore[arg-type]
    if near:
        return replace(debt, blocking_tags=[f"{t} ({e.isoformat()})" for t, e in sorted(near.items())])
    at_bs = [o for r in instants.values() for s in r.series.values() for o in s if o.end == newest_bs]
    filed = max((o.filed for o in at_bs), default=None)
    accn = max(at_bs, key=lambda o: (o.filed, o.accn)).accn if at_bs else None
    zero = Value(0.0, newest_bs, filed, None, ASSUMED_ZERO_DERIVATION, accn=accn,
                 extra={"assumed": "zero"})
    return replace(debt, value=zero, assumed_zero=True)


# --------------------------------------------------------------------------------------------
# Shares (step 10)
# --------------------------------------------------------------------------------------------


@dataclass
class SharesResult:
    value: Value                 # after class_factor
    raw_value: float | None      # before class_factor
    tag: str | None
    classes_summed: int          # >1 when several class counts of one filing were summed
    class_factor: float | None


def share_count(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *, as_of: date | None = None,
                shares_tags: list[str] | None = None, class_factor: float | None = None) -> SharesResult:
    """Share count: candidate tag with the latest end (ties -> priority). Instant tags (dei cover
    page, CommonStockSharesOutstanding): the latest end, latest filing; several values of that
    filing for the same end are per-class counts and are ALL summed (equal per-class counts are
    distinct classes, not duplicates; a rare duplicated fact is caught by the Yahoo marketCap
    cross-check in fetch_fundamentals). Weighted-average tags (durations):
    the shortest period ending last."""
    cfg = cfg or settings()
    tags = [t for t in (shares_tags or SHARE_TAGS) if t]
    best: tuple[date, int] | None = None
    chosen: SharesResult | None = None
    for idx, tag in enumerate(tags):
        raw = raw_facts(cf, tag, "shares", cfg, as_of)
        if not raw:
            continue
        inst = [o for o in raw if o.bucket == "I"]
        if inst:
            end = max(o.end for o in inst)
            at_end = [o for o in inst if o.end == end]
            top = max(at_end, key=lambda o: (o.filed, o.accn))
            vals = [o.val for o in at_end if o.accn == top.accn]
            total, n = float(sum(vals)), len(vals)
            deriv = tag if n == 1 else f"sum of {n} class counts of {tag} ({top.accn})"
            cand = SharesResult(Value(total, end, top.filed, tag, deriv, accn=top.accn), total, tag, n, None)
        else:
            dedup = normalize(cf, tag, "shares", cfg, as_of)
            end = max(o.end for o in dedup)
            o = min((x for x in dedup if x.end == end), key=lambda x: (x.days or 0, -x.filed.toordinal()))
            cand = SharesResult(Value(o.val, o.end, o.filed, tag, f"{tag} ({_label(o)} average)", accn=o.accn),
                                o.val, tag, 1, None)
        key = (cand.value.end, -idx)
        if best is None or key > best:
            best, chosen = key, cand
    if chosen is None:
        return SharesResult(Value.na("missing"), None, None, 0, None)
    if class_factor is not None and chosen.value.value is not None:
        cf_val = float(class_factor)
        chosen.value = replace(chosen.value, value=chosen.value.value * cf_val,
                               derivation=f"{chosen.value.derivation} x {cf_val:g} (class_factor)")
        chosen.class_factor = cf_val
    return chosen


# --------------------------------------------------------------------------------------------
# Filings: latest companyfacts filing, submissions, lag (step 13)
# --------------------------------------------------------------------------------------------


def companyfacts_accns(cf: Mapping[str, Any]) -> set[str]:
    """Every accession number present in companyfacts (plus a cached ``accns_all`` list)."""
    out = {str(a) for a in (cf or {}).get("accns_all") or []}
    for ns in ((cf or {}).get("facts") or {}).values():
        if not isinstance(ns, Mapping):
            continue
        for node in ns.values():
            for arr in ((node or {}).get("units") or {}).values():
                for f in arr or []:
                    if isinstance(f, Mapping) and f.get("accn"):
                        out.add(str(f["accn"]))
    return out


def companyfacts_latest(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *,
                        as_of: date | None = None) -> dict | None:
    """``{"form","filed","period_end","accn"}`` of the newest accepted-form filing in companyfacts
    (period_end = latest us-gaap ``end`` inside that filing)."""
    cfg = cfg or settings()
    forms = set(cfg.get("forms") or DEFAULTS["forms"]) | set(cfg.get("fy_extra_forms") or DEFAULTS["fy_extra_forms"])
    best: tuple[date, str] | None = None
    form = None
    ends: dict[str, date] = {}
    for node in (((cf or {}).get("facts") or {}).get("us-gaap") or {}).values():
        for arr in ((node or {}).get("units") or {}).values():
            for f in arr or []:
                if not isinstance(f, Mapping) or f.get("form") not in forms:
                    continue
                filed, end, accn = to_date(f.get("filed")), to_date(f.get("end")), str(f.get("accn") or "")
                if filed is None or end is None or (as_of is not None and filed > as_of):
                    continue
                if end > ends.get(accn, date.min):
                    ends[accn] = end
                if best is None or (filed, accn) > best:
                    best, form = (filed, accn), f.get("form")
    if best is None:
        return None
    return {"form": form, "filed": best[0].isoformat(), "period_end": _iso(ends.get(best[1])), "accn": best[1]}


def filing_url(cik: int | str, accn: str, primary_document: str | None = None) -> str:
    """Archive URL of a filing (primary document when known, else the filing index)."""
    nodash = str(accn).replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    return f"{base}/{primary_document}" if primary_document else f"{base}/{accn}-index.htm"


def latest_periodic(sub: Mapping[str, Any], *, forms: Iterable[str] = PERIODIC_FORMS,
                    as_of: date | None = None) -> dict | None:
    """Newest periodic filing (10-K/10-Q[/A]) in ``filings.recent``:
    ``{"form","filed","report_date","accn","primary_document"}`` or None."""
    rec = (((sub or {}).get("filings") or {}).get("recent")) or {}
    accns = rec.get("accessionNumber") or []
    forms = set(forms)
    best: tuple[date, int] | None = None
    out = None
    for i, accn in enumerate(accns):
        form = _at(rec, "form", i)
        filed = to_date(_at(rec, "filingDate", i))
        if form not in forms or filed is None or (as_of is not None and filed > as_of):
            continue
        key = (filed, -i)
        if best is None or key > best:
            best = key
            out = {"form": form, "filed": filed.isoformat(), "report_date": _iso(to_date(_at(rec, "reportDate", i))),
                   "accn": str(accn), "primary_document": _at(rec, "primaryDocument", i)}
    return out


def _at(rec: Mapping[str, Any], key: str, i: int) -> Any:
    arr = rec.get(key) or []
    return arr[i] if i < len(arr) else None


@dataclass
class LagResult:
    lag: bool
    latest: dict | None
    url: str | None
    message: str | None


def detect_lag(cf: Mapping[str, Any], sub: Mapping[str, Any] | None, cik: int | str, *,
               as_of: date | None = None) -> LagResult:
    """Step 13: the newest 10-Q/10-K (amendments excluded, ``LAG_FORMS``) in ``filings.recent``
    whose accession is absent from companyfacts -> lag."""
    latest = latest_periodic(sub or {}, forms=LAG_FORMS, as_of=as_of) if sub else None
    if latest is None:
        return LagResult(False, None, None, None)
    if latest["accn"] in companyfacts_accns(cf):
        return LagResult(False, latest, None, None)
    url = filing_url(cik, latest["accn"], latest.get("primary_document"))
    msg = (f"latest {latest['form']} for {latest.get('report_date') or '?'} (filed {latest['filed']}) "
           f"not yet in companyfacts — read it directly: {url}")
    return LagResult(True, latest, url, msg)


# --------------------------------------------------------------------------------------------
# Ratios helpers (steps 9, 11, 12)
# --------------------------------------------------------------------------------------------


def cagr_pct(start_val: float | None, end_val: float | None, years: int) -> tuple[float | None, str | None]:
    """Compound annual growth in percent; both values must be > 0."""
    if start_val is None or end_val is None:
        return None, "insufficient_history"
    if start_val <= 0 or end_val <= 0 or years <= 0:
        return None, "negative_denominator"
    return ((end_val / start_val) ** (1.0 / years) - 1.0) * 100.0, None


def effective_tax_rate(tax: float | None, pretax: float | None, cfg: Mapping[str, Any] | None = None
                       ) -> tuple[float, str | None, str]:
    """``(rate_fraction, flag_code|None, derivation)``: tax/pretax clamped to ``tax_rate_bounds``;
    unusable -> ``default_tax_rate`` with flag ``default_tax_rate``."""
    cfg = cfg or settings()
    lo, hi = (float(x) for x in (cfg.get("tax_rate_bounds") or DEFAULTS["tax_rate_bounds"]))
    default = float(cfg.get("default_tax_rate", DEFAULTS["default_tax_rate"]))
    if tax is None or pretax is None or pretax <= 0:
        return default, "default_tax_rate", f"default {default:g} (income_tax/pretax unusable)"
    raw = tax / pretax
    if raw < lo or raw > hi:
        clamped = min(max(raw, lo), hi)
        return clamped, "tax_rate_clamped", f"income_tax/pretax_income = {raw:.4f} clamped to [{lo:g}, {hi:g}]"
    return raw, None, "income_tax/pretax_income"


def sector_class_from_sic(sic: Any, cfg: Mapping[str, Any] | None = None) -> str:
    """``financials`` for SIC in ``financials_sic`` except ``reit_sic``; else ``default``."""
    cfg = cfg or settings()
    n = to_num(sic)
    if n is None:
        return "default"
    lo, hi = (cfg.get("financials_sic") or DEFAULTS["financials_sic"])
    reits = {int(x) for x in (cfg.get("reit_sic") or DEFAULTS["reit_sic"])}
    return "financials" if (lo <= int(n) <= hi and int(n) not in reits) else "default"


# --------------------------------------------------------------------------------------------
# One-call parse (steps 3-10 + staleness guard)
# --------------------------------------------------------------------------------------------

BALANCE_COMPONENTS = ("cash", "st_investments", "equity", "goodwill", "intangibles")


@dataclass
class Parsed:
    latest: dict | None                  # companyfacts_latest()
    ref_end: date | None                 # latest reported period end (staleness reference)
    flows: dict[str, FlowResult]
    ttm: dict[str, Value]                # concept -> TTM after staleness guard (+ gross profit fallback)
    instants: dict[str, InstantResult]
    balance: dict[str, Value]            # concept -> instant value after staleness guard
    newest_bs: date | None
    debt: DebtResult
    shares: SharesResult
    shares_stale: bool
    flags: list[tuple[str, str, str]]    # (code, severity, detail)
    missing: list[str]


def parse(cf: Mapping[str, Any], cfg: Mapping[str, Any] | None = None, *, as_of: date | None = None,
          shares_tags: list[str] | None = None, class_factor: float | None = None) -> Parsed:
    """Run steps 3-10 on a companyfacts payload. Pure; returns provenance-carrying ``Value``s."""
    cfg = settings(cfg)
    flags: list[tuple[str, str, str]] = []
    missing: list[str] = []
    max_gap = int(cfg.get("concept_max_gap_days", cfg.get("debt_formula_max_gap_days", 400)))
    latest = companyfacts_latest(cf, cfg, as_of=as_of)
    ref_end = to_date(latest.get("period_end")) if latest else None

    flows = {c: flow(cf, c, cfg, as_of=as_of) for c in FLOW_CONCEPTS}
    ttm_vals: dict[str, Value] = {}
    for c, r in flows.items():
        v = r.ttm
        if c == "da" and not (v.ok and not (ref_end and v.end and (ref_end - v.end).days > max_gap)):
            alt = da_from_parts(cf, cfg, as_of=as_of, ref_end=ref_end)
            if alt is not None:
                ttm_vals[c] = alt
                continue
        if v.ok and ref_end and v.end and (ref_end - v.end).days > max_gap:
            flags.append(("stale_tag", "low", f"{c}: newest {r.tag} period ends {v.end.isoformat()}, "
                          f"more than {max_gap} d before the latest reported period {ref_end.isoformat()}; ignored"))
            missing.append(f"{c}: no current value in companyfacts (newest {r.tag} ends {v.end.isoformat()})")
            v = Value.na("missing", tag=r.tag)
        ttm_vals[c] = v
        if r.stitched:
            tags = sorted({r.fy_tags[y] for y in r.fy_tags})
            flags.append(("tag_mix", "medium", f"{c}: FY history stitched from tags {', '.join(tags)}"))
    gp, rev, cogs = ttm_vals["gross_profit"], ttm_vals["revenue"], ttm_vals["cogs"]
    # revenue-cogs when GrossProfit is absent OR its TTM covers a different period than revenue's
    # (e.g. GrossProfit tagged only in the 10-K) while cogs is aligned with revenue.
    gp_misaligned = gp.ok and rev.ok and gp.end != rev.end
    if (not gp.ok or gp_misaligned) and rev.ok and cogs.ok and rev.end == cogs.end:
        note = f"; GrossProfit TTM ends {gp.end.isoformat()}" if (gp_misaligned and gp.end) else ""
        ttm_vals["gross_profit"] = Value(rev.value - cogs.value, rev.end, max(rev.filed, cogs.filed),  # type: ignore[operator,type-var]
                                         f"{rev.tag}-{cogs.tag}", f"revenue-cogs ({rev.derivation}{note})",
                                         accn=rev.accn)

    instants = {c: instant(cf, c, cfg, as_of=as_of) for c in INSTANT_CONCEPTS}
    all_ends = [o.end for r in instants.values() for s in r.series.values() for o in s]
    newest_bs = max(all_ends) if all_ends else ref_end
    if not instants["intangibles"].value.ok:
        alt = intangibles_from_parts(cf, cfg, as_of=as_of)
        if alt.value.ok:
            instants["intangibles"] = alt
    balance: dict[str, Value] = {}
    for c, r in instants.items():
        v = r.value
        ref = newest_bs or ref_end
        if v.ok and ref and v.end and (ref - v.end).days > max_gap:
            flags.append(("stale_tag", "low", f"{c}: newest {r.tag} is {v.end.isoformat()}, more than {max_gap} d "
                          f"before the newest balance sheet {ref.isoformat()}; ignored"))
            v = Value.na("missing", tag=r.tag)
        balance[c] = v

    debt = total_debt(cf, cfg, newest_bs=newest_bs, as_of=as_of)
    if not debt.value.ok:
        debt = _assume_debt_free(cf, cfg, debt, instants, balance, newest_bs, as_of)
    if debt.value.ok and debt.current_portion_missing:
        flags.append(("debt_current_portion_missing", "medium",
                      f"{debt.value.tag}: no current-portion tag at "
                      f"{debt.value.end.isoformat() if debt.value.end else '?'}; the noncurrent leg is used alone, "
                      "so total debt may be understated"))
    if debt.value.ok and debt.assumed_zero:
        flags.append(("debt_not_reported_assumed_zero", "medium",
                      f"no qualifying debt formula (a)-(d) and no debt-related us-gaap tag within "
                      f"{int(cfg.get('debt_formula_max_gap_days', 400))} d of the newest balance sheet "
                      f"{newest_bs.isoformat() if newest_bs else '?'}: total debt taken as 0 (debt-free issuer "
                      "or debt reported outside XBRL face tags - verify in the 10-K/10-Q balance sheet)"))
    elif debt.value.ok:
        if debt.stb_assumed_zero:
            flags.append(("stb_assumed_zero", "low", f"no ShortTermBorrowings/CommercialPaper at "
                          f"{debt.value.end.isoformat() if debt.value.end else '?'}; short-term borrowings taken as 0"))
    else:
        detail = "; ".join(debt.rejected) if debt.rejected else "no debt tags (a)-(d) reported"
        if debt.blocking_tags:
            detail += ("; debt-related tags reported but not covered by (a)-(d): " + ", ".join(debt.blocking_tags[:6]))
        elif not debt.scan_available:
            detail += "; cached companyfacts predates the debt-tag scan (refetch to assume zero debt)"
        flags.append(("debt_unavailable", "medium", f"total debt not derivable from EDGAR: {detail}"))
        missing.append("total_debt: no qualifying EDGAR debt formula (a)-(d)")

    ends = {c: v.end for c, v in balance.items() if v.ok and v.end}
    if debt.value.ok and debt.value.end:
        ends["total_debt"] = debt.value.end
    if len(set(ends.values())) > 1:
        flags.append(("mixed_instants", "medium", "balance components on different dates: " +
                      ", ".join(f"{k} {d.isoformat()}" for k, d in sorted(ends.items()))))

    shares = share_count(cf, cfg, as_of=as_of, shares_tags=shares_tags, class_factor=class_factor)
    shares_stale = False
    ref = newest_bs or ref_end
    if shares.value.ok and ref and shares.value.end and (ref - shares.value.end).days > max_gap:
        shares_stale = True
    if shares.classes_summed > 1:
        flags.append(("multi_class_shares_summed", "low", f"{shares.value.derivation}"))
    return Parsed(latest, ref_end, flows, ttm_vals, instants, balance, newest_bs, debt, shares, shares_stale,
                  flags, missing)


# --------------------------------------------------------------------------------------------
# Trimming (cache payloads)
# --------------------------------------------------------------------------------------------


def trim_companyfacts(cf: Mapping[str, Any]) -> dict:
    """Keep only the tags this module reads (all their units/facts) + every accession number
    (``accns_all``, so lag detection still sees filings whose facts were trimmed away)."""
    facts: dict[str, dict] = {}
    for tag in all_tags():
        ns, name = split_tag(tag)
        node = ((cf or {}).get("facts") or {}).get(ns, {}).get(name)
        if isinstance(node, Mapping) and node.get("units"):
            facts.setdefault(ns, {})[name] = {"label": node.get("label"), "units": node.get("units")}
    # debt-like us-gaap tags (instants only, currency units) for the "no debt reported -> 0" scan
    for name, node in (((cf or {}).get("facts") or {}).get("us-gaap") or {}).items():
        if name in facts.get("us-gaap", {}) or not is_debt_tag(name) or not isinstance(node, Mapping):
            continue
        units = {u: [o for o in obs if isinstance(o, Mapping) and not o.get("start")]
                 for u, obs in (node.get("units") or {}).items() if re.fullmatch(r"[A-Z]{3}", str(u))}
        units = {u: obs for u, obs in units.items() if obs}
        if units:
            facts.setdefault("us-gaap", {})[name] = {"label": node.get("label"), "units": units}
    return {"cik": (cf or {}).get("cik"), "entityName": (cf or {}).get("entityName"), "facts": facts,
            "accns_all": sorted(companyfacts_accns(cf)), "debt_scan": True, "trim_rev": TRIM_REV}


#: Format revision of ``trim_companyfacts`` payloads (bump when the set of kept tags changes): a cached
#: trim with a lower revision lacks tags this version reads and is refetched (fetch_fundamentals).
TRIM_REV = 2


SUBMISSION_FIELDS = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")


def trim_submissions(sub: Mapping[str, Any], keep: int = DEFAULT_SUBMISSIONS_KEEP,
                     periodic: int = DEFAULT_SUBMISSIONS_PERIODIC) -> dict:
    """Entity fields + the first ``keep`` rows of ``filings.recent`` plus the next ``periodic``
    10-K/10-Q rows (order preserved)."""
    sub = sub or {}
    rec = ((sub.get("filings") or {}).get("recent")) or {}
    n = len(rec.get("accessionNumber") or [])
    rows: list[int] = []
    seen_periodic = 0
    for i in range(n):
        is_periodic = _at(rec, "form", i) in PERIODIC_FORMS
        if i < keep:
            rows.append(i)
        elif is_periodic and seen_periodic < periodic:
            rows.append(i)
        if is_periodic:
            seen_periodic += 1
        if i >= keep and seen_periodic >= periodic:
            break
    recent = {k: [_at(rec, k, i) for i in rows] for k in SUBMISSION_FIELDS}
    top = {k: sub.get(k) for k in ("cik", "entityType", "sic", "sicDescription", "name", "tickers",
                                    "exchanges", "fiscalYearEnd", "stateOfIncorporation")}
    top["filings"] = {"recent": recent}
    return top


__all__ = [n for n in dir() if not n.startswith("_") and n not in (
    "annotations", "math", "dataclass", "field", "replace", "date", "timedelta", "Any", "Iterable", "Mapping")]
