---
sector: health_care
name: Health Care
benchmark_etf: XLV
sector_class: default
key_kpis:
  - Pipeline depth and stage mix (phase 2/3 assets, filings, decision dates)
  - Loss-of-exclusivity (LOE) schedule and revenue at risk
  - Product concentration (top product share of revenue)
  - R&D intensity and R&D productivity
  - Gross-to-net pricing and net price trend
  - Payer mix (commercial, Medicare, Medicaid, self-pay)
  - Medical loss ratio (MLR) and medical cost trend for insurers
  - Procedure volumes and utilization (devices, hospitals)
  - Reimbursement rate updates and policy exposure (CMS rules, drug price negotiation)
  - Organic revenue growth (ex FX, ex M&A)
  - Cash runway for pre-profit biotech
  - Orders, book-to-bill and customer destocking for life-science tools
valuation_methods:
  - Reverse DCF implied growth versus the LOE schedule and pipeline
  - DCF on course NOPAT with LOE handled through the zero-growth cells
  - P/E on GAAP earnings (pe band), reading IPR&D and amortization noise
  - EV/EBIT (ev_ebit band) for devices, tools, services and distributors
  - Risk-adjusted pipeline value (rNPV) cited from sources as context only
  - Cash and runway framing for pre-revenue biotech (no fair-value cells)
macro_sensitivities:
  - {series: DGS10, direction: negative, mechanism: "Higher long yields compress long-duration biotech valuations; large pharma is less sensitive."}
  - {series: DFF, direction: mixed, mechanism: "Higher short rates lift float income at insurers and distributors but raise funding costs for levered providers and biotech."}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Wider spreads close the biotech funding window and pressure levered hospital and services operators."}
  - {series: UNRATE, direction: negative, mechanism: "Job losses shrink employer coverage, shift payer mix toward Medicaid and self-pay and raise hospital bad debt."}
  - {series: PAYEMS, direction: positive, mechanism: "Payroll growth expands commercial enrollment, the highest-margin payer for providers and insurers."}
  - {series: CPILFESL, direction: mixed, mechanism: "Wage and supply inflation squeezes providers and raises insurer cost trend; drug and device pricing is regulated or contracted."}
  - {series: CPIAUCSL, direction: mixed, mechanism: "Headline inflation feeds medical cost trend and some indexed government rate updates."}
  - {series: DTWEXBGS, direction: negative, mechanism: "Pharma, medtech and tools earn a large share abroad; a stronger dollar cuts reported revenue."}
  - {series: VIXCLS, direction: mixed, mechanism: "Risk-off favors defensive large pharma and managed care relative to the market but hits speculative biotech."}
failure_modes:
  - Patent cliff without replacement revenue
  - Binary clinical or regulatory failure of a key asset
  - Drug pricing reform and Medicare negotiation exposure
  - Reimbursement or risk-adjustment cuts for insurers and providers
  - Medical cost trend running ahead of premium pricing
  - Mass-tort litigation and product liability
  - Overpaying for acquisitions to refill the pipeline
  - Pandemic-era or one-off windfall revenue extrapolated
  - Customer destocking cycles in life-science tools
edgar_tags_of_interest:
  - ResearchAndDevelopmentExpense
  - ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost
  - ResearchAndDevelopmentInProcess
  - AmortizationOfIntangibleAssets
  - FiniteLivedIntangibleAssetsNet
  - IndefiniteLivedIntangibleAssetsExcludingGoodwill
  - ImpairmentOfIntangibleAssetsExcludingGoodwill
  - GoodwillImpairmentLoss
  - LossContingencyAccrualAtCarryingValue
  - InventoryNet
  - AccountsReceivableNetCurrent
  - PremiumsEarnedNet
  - PolicyholderBenefitsAndClaimsIncurredNet
  - ShareBasedCompensation
  - CashCashEquivalentsAndShortTermInvestments
---

# Health Care — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **GICS industries covered:** Pharmaceuticals, Biotechnology, Life Sciences Tools & Services,
  Health Care Equipment & Supplies, Health Care Providers & Services (managed care, facilities,
  distributors, services) and Health Care Technology.
- **Benchmark:** XLV (`sector_etfs.health_care` in `config/committee.yaml`). It is cap-weighted and
  large-cap biased — big pharma, managed care, devices and tools dominate; small and mid-cap biotech
  barely registers, so relative strength vs XLV is a weak yardstick for a small biotech.
- **Classification:** `etf_lookthrough.classify_security` applies portfolio `sector_override` →
  `etf_overrides.yaml` → Yahoo `info.sector` via `common.YF_SECTOR_MAP` ("Healthcare" →
  `health_care`) → SIC fallback (2830–2836, 3840–3851, 5122, 8000–8099). Edge cases: Yahoo may label
  drug retailers "Healthcare" while GICS places pure drug retail in Consumer Staples; managed-care
  SIC codes (63xx) fall in the financials SIC range (see §8). Use `sector_override` when sources disagree.
- **Sub-industry differences:**
  - *Large pharma* — portfolio of patented products; value hinges on LOE timing vs pipeline
    replacement; steady cash, dividends, frequent bolt-on M&A.
  - *Biotech* — pre-revenue (binary, cash-runway driven) to commercial-stage (single-franchise risk).
  - *Medtech / devices* — procedure volumes, innovation cycles, hospital capital budgets, recalls.
  - *Life-science tools* — selling to pharma, biotech, academia and diagnostics; exposed to biotech
    funding, research budgets and customer inventory cycles.
  - *Managed care* — premiums vs medical costs, government program rates (Medicare Advantage,
    Medicaid), risk adjustment, regulated capital.
  - *Providers and facilities* — payer mix, labor costs, volumes, leverage.
  - *Distributors* — enormous pass-through revenue on thin margins, negative working capital,
    litigation liabilities.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Pipeline depth and stage | Future revenue that must replace expiring products | 10-K Item 1 pipeline table, company pipeline page, ClinicalTrials.gov, FDA calendars | Healthy: several late-stage assets in large indications. Warning: thin phase 3, one asset carrying the story, repeated delays |
| LOE schedule / revenue at risk | Patent expiry turns a franchise into a melting ice cube | 10-K Item 1 (patents and exclusivity table), Item 1A; FDA Orange Book / Purple Book | Warning: a top product loses exclusivity inside the DCF horizon (`valuation.years`) without replacements ramping |
| Product concentration | Single-product dependence magnifies LOE, safety and pricing shocks | 10-K revenue by product / segment note | Warning: one product above roughly a third of revenue (*heuristic*) |
| R&D intensity and productivity | Sustains the pipeline; productivity separates spenders from builders | Income statement; approvals and late-stage readouts over time | Warning: R&D cut while the pipeline is thin, or rising spend with no late-stage output |
| Gross-to-net / net price | List price is fiction; rebates, chargebacks and 340B decide realized price | 10-K Item 7 (rebate and chargeback accruals), earnings calls | Warning: widening gross-to-net, product selected for government price negotiation |
| Payer mix | Commercial pays more than government programs; self-pay drives bad debt | Provider 10-K Item 7 revenue by payer | Warning: commercial share falling, Medicaid/self-pay and uncompensated care rising |
| Medical loss ratio (insurers) | Core profitability of risk-bearing insurers | Earnings release, 10-Q | Healthy: within guided range. Warning: above guidance, utilization commentary, reserve builds |
| Days claims payable (insurers) | Reserve adequacy signal | Earnings release / supplement | Warning: falling days claims payable alongside beats (possible reserve release) |
| Reimbursement updates | Government rate changes reset revenue per unit | CMS final rules and rate notices (cms.gov, Federal Register) | Warning: rate cuts, risk-adjustment model changes, quality/star-rating downgrades |
| Procedure volumes / utilization | Volume driver for devices and facilities | Earnings calls, hospital admissions commentary | Warning: deferrals, capital budget freezes, competitive procedure substitution |
| Organic growth | Separates franchise strength from FX and M&A | Earnings release reconciliation | Warning: reported growth carried by acquisitions or one-off contracts |
| Cash runway (biotech) | Determines dilution risk and negotiating power | 10-Q liquidity section ("sufficient to fund operations into...") | Warning: runway under about two years (*heuristic*) — financing or partnering likely on weak terms |
| Tools orders / book-to-bill | Leading indicator of tools revenue | Earnings release; management commentary on destocking | Warning: book-to-bill below 1 (*heuristic*), destocking, academic or China funding cuts |
| Quality / regulatory record | Warning letters, recalls and import alerts stop revenue | FDA warning letters, recall and inspection databases; 10-K Item 1A | Warning: open warning letters at key plants, Class I recalls, consent decrees |

## 3. Valuation methods

**What `scripts/valuation.py` provides:** DCF cells (`dcf_nopat`, `dcf_fcf`) over
`valuation.discount_rates` × `valuation.growth_tiers`, multiple cells from
`valuation.multiple_bands.health_care` (`pe`, `ev_ebit`), and a reverse DCF. Bands are investor
assumptions (source `D:CFG:valuation.multiple_bands`). Fair values must equal the mean of cited cells;
`cells_base` must span DCF and multiple families when both exist.

- **Patent cliffs and the growth tiers.** The configured growth tiers start at zero — the grid cannot
  express the revenue decline of a cliff. When a top product loses exclusivity inside the horizon, the
  zero-growth DCF cells are a ceiling for that name, not a floor: use them as base or high, put the
  lowest cells in `cells_low`, and say why in `method`. The reverse DCF growth bounds
  (`valuation.reverse_dcf_growth_bounds`) do allow negative growth, but its fixed
  `valuation.reverse_dcf_exit_multiple` exceeds the zero-growth multiple in
  `valuation.exit_multiple_by_growth`, so a price equal to the grid's own `g00` value already shows
  negative `implied_growth_pct`. Judge whether the price discounts the cliff by comparing it with the
  `DCF-NOPAT-*-g00` cells; treat implied decline as priced in only when the price is clearly below them.
- **GAAP noise in P/E.** Acquired in-process R&D is expensed at deal close and intangible amortization
  is large after M&A, so GAAP net income understates earnings power in deal years and P/E cells skew
  high. Course NOPAT adds back D&A (amortization included only if the D&A tag covers it — check
  `tag_map.da`), but IPR&D charges stay in operating income. Note the distortion in `quality_notes`;
  cite non-GAAP figures only from the company release with a URL and never use them as cell inputs.
- **Devices, tools, services:** DCF on NOPAT, P/E and EV/EBIT all work; normalize tools companies
  for destocking and pandemic-era demand that will not recur.
- **Managed care:** prefer NOPAT cells and P/E. Operating cash flow swings with the timing of
  government premium receipts, so single-year `dcf_fcf` cells are noisy. Large investment portfolios
  are partly held in regulated subsidiaries, so net cash in the EV→equity bridge overstates what the
  parent can deploy — check parent-company liquidity in the 10-K.
- **Distributors:** revenue is mostly pass-through drug cost, so margins look tiny and EV/Sales is
  meaningless; use EV/EBIT and P/E. Treat litigation settlement liabilities as debt-like in
  `balance_sheet_notes` (they are not in `net_debt`).
- **Pre-profit biotech:** P/E is null (`negative_denominator`) and NOPAT ≤ 0 makes DCF cells `na` —
  the grid offers no fair value. Leave fair values null with the reason in `missing_data` rather than
  inventing an rNPV; a cited third-party pipeline valuation can inform `quality_notes` only.
- **What misleads:** peak pandemic-related earnings; P/E on a franchise approaching LOE (looks cheap,
  is not); EV/Sales for distributors; pipeline "peak sales" claims taken from promotional sources.

## 4. Macro sensitivities

Direction = effect on the sector's earnings or valuation when the series **rises**. Read levels and
changes only from `macro.json`; never quote a macro figure from memory.

| Series | Direction | Mechanism |
|---|---|---|
| DGS10 | negative | Discount-rate pressure on long-duration biotech; large pharma less sensitive |
| DFF | mixed | Float income up for insurers/distributors; funding costs up for levered providers and biotech |
| BAMLH0A0HYM2 | negative | Wider spreads close the biotech equity/debt window and squeeze levered operators |
| UNRATE | negative | Lost employer coverage shifts payer mix to Medicaid/self-pay; bad debt rises at hospitals |
| PAYEMS | positive | Commercial enrollment grows; best-paying payer mix for providers and insurers |
| CPILFESL | mixed | Wage and supply costs squeeze providers; insurers face cost trend; pricing largely regulated |
| CPIAUCSL | mixed | Headline inflation feeds medical cost trend and some indexed government rate updates |
| DTWEXBGS | negative | Large non-US revenue at pharma, medtech and tools; strong dollar cuts reported results |
| VIXCLS | mixed | Defensive large caps hold up relative to the market; speculative biotech sells off |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  medical-care CPI and medical cost trend (BLS, insurer disclosures), CMS rate notices and final rules
  (Medicare Advantage, physician fee schedule, hospital payment systems), drug-pricing negotiation lists,
  utilization and procedure-volume data, NIH budget and biotech financing activity.

## 5. Typical failure modes

- **Patent cliff without replacement:** revenue and margin fall for years after LOE; trailing P/E
  makes the stock look cheap all the way down.
- **Binary failures:** a single phase 3 readout, FDA complete response letter or safety signal can
  erase most of a concentrated biotech's value.
- **Policy shocks:** drug price negotiation, rebate rule changes, 340B and Medicare Advantage rate or
  risk-adjustment changes re-rate whole sub-industries at once.
- **Cost trend vs pricing:** insurers price a year ahead; a utilization surge shows up in the MLR
  before premiums can reset.
- **Litigation:** mass torts and product liability create multi-year, hard-to-size liabilities.
- **Pipeline M&A overpayment:** buying growth at peak valuations, then IPR&D or goodwill impairments.
- **Windfall extrapolation:** pandemic products and testing demand collapsed; tools customers destocked.
- **Leverage at providers:** thin margins plus labor inflation plus debt is a recurring path to distress.

## 6. Red flags for the Devil's Advocate

- Top product's LOE inside the horizon while management guides to "growth through the cliff" with
  mostly early-stage assets.
- Late-stage trial delays, protocol amendments, endpoint changes or enrollment problems.
- Revenue growth driven by price while volumes fall; widening gross-to-net.
- Inventory or receivables building faster than sales (channel stuffing at wholesalers).
- Insurers: days claims payable falling while earnings beat; repeated prior-period reserve releases.
- Open FDA warning letters, import alerts or consent decrees at key manufacturing sites.
- Litigation accruals small relative to the number of claims disclosed in the contingencies note.
- Contingent consideration and milestone liabilities that are large relative to cash.
- Biotech cash runway under the next major readout date; at-the-market equity programs in use.
- Adjusted EPS that excludes recurring items (amortization, IPR&D, restructuring) every year.

## 7. Lens checklist

- **fundamental**
  - Map the LOE schedule against `valuation.years`; decide whether zero-growth cells are the ceiling.
  - Read `reverse_dcf.implied_growth_pct` against LOE exposure and pipeline, but test "cliff priced in"
    against the `g00` DCF cells — the fixed reverse-DCF exit multiple biases implied growth down (§3).
  - Check `tag_map.da` and the 10-K for whether amortization sits in D&A; note IPR&D charges.
  - Record debt-like items (litigation accruals, contingent consideration) in `balance_sheet_notes`.
  - Pre-profit biotech: null fair values with reasons; cash runway from the 10-Q (cited).
- **technical**
  - Use only `technicals_<T>.json`. Biotech gaps on binary events make `stop_candidates` unreliable
    protection — state that a gap can jump the stop.
  - Compare `vs_sector` with `vs_spy`: defensive sub-industries can lag XLV in risk-on phases yet hold
    absolute trend.
  - High `atr_pctile_252` ahead of known readouts or rulings argues for lower conviction.
- **operator_ceo**
  - Capital allocation: pipeline M&A record, prices paid, later impairments; dividend vs R&D trade-offs.
  - Incentives (DEF 14A): metrics tied to revenue and EPS vs pipeline milestones, ROIC or TSR.
  - Guidance credibility: launch curves vs guidance, cost-trend guidance at insurers.
  - Form 4: open-market buys in small biotech are informative; option exercises are not.
  - Regulatory and legal history under current management (settlements, corporate integrity agreements).
- **industry_ops**
  - Supply chain: API and sterile manufacturing concentration, single-source suppliers, plant quality record.
  - Capacity & backlog: manufacturing capacity for launches (biologics), tools order backlog.
  - Execution: launch trajectories, trial enrollment speed, integration of acquisitions.
  - Unit economics: gross-to-net, cost per procedure, MLR by product line, revenue per bed.
  - Operational moat: installed base and consumables pull-through, provider networks, manufacturing know-how.
- **osint**
  - FDA databases (warning letters, recalls, adverse event signals), ClinicalTrials.gov changes.
  - Physician and patient sentiment on launches; conference presentations and trade press.
  - Policy news: CMS rules, negotiation lists, state Medicaid actions, PBM reform.
  - Hiring trends: sales-force builds before launches, layoffs after failures.
  - Earnings-call language: "elevated utilization", "destocking", "pricing headwinds", "pushout".

## 8. Data notes

- **Acquired IPR&D:** shows up as `ResearchAndDevelopmentInProcess` or inside R&D expense; a single
  deal can turn TTM operating income negative, making P/E null and NOPAT cells `na`. Check the income
  statement before reading "unprofitable".
- **D&A scope:** the fetcher's D&A tag may or may not include intangible amortization
  (`AmortizationOfIntangibleAssets` is often separate). If excluded, EBITDA and course NOPAT are
  understated for acquisitive pharma and medtech.
- **Managed-care SIC codes** (63xx, insurance) sit inside `fundamentals.financials_sic`. If
  `entity.sector_class` in `fundamentals_<T>.json` reads `financials` for an insurer classified here,
  EV-based metrics come back `not_meaningful` and P/B, ROE are computed instead — say so in
  `quality_notes` and lean on P/E and NOPAT cells.
- **Collaboration and royalty revenue** is often tagged with elements outside the fetcher's revenue
  candidates; if revenue is null or implausibly small for a commercial biotech, read the income
  statement and cite it.
- **Spin-offs and divestitures** restate history: watch `tag_mix` / `stitched` flags, discontinued
  operations inside `NetIncomeLoss`, and CAGR computed across different perimeters.
- **Foreign pharma ADRs** (20-F, IFRS): us-gaap tags are absent, so expect nulls and fiscal-year-only
  data; read the annual report and cite it.
- **Medical-cost metrics** (MLR, days claims payable) and pipeline data are not in companyfacts —
  read the earnings supplement, 10-K and company pipeline pages.
- **Litigation and settlement liabilities** (`LossContingencyAccrualAtCarryingValue`) are not part of
  `balance.total_debt`; read the contingencies note.
- **Companyfacts lag:** follow the `companyfacts_lag` missing_data URL to read the newest filing directly.
