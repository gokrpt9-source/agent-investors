---
sector: financials
name: Financials
benchmark_etf: XLF
sector_class: financials
key_kpis:
  - Net interest margin (NIM) and net interest income trend
  - CET1 ratio versus requirement plus buffers
  - Credit costs (provision expense) and net charge-offs (NCO ratio)
  - Allowance coverage and nonperforming assets
  - Deposit beta and deposit mix (noninterest-bearing and uninsured shares)
  - Loan-to-deposit ratio and liquidity versus uninsured deposits
  - Efficiency ratio (noninterest expense over revenue)
  - ROE and return on tangible common equity (ROTCE) versus cost of equity
  - Price to book and price to tangible book versus ROE/ROTCE
  - Unrealized securities losses (AOCI and HTM marks) versus tangible equity
  - Combined ratio and prior-year reserve development (P&C insurers)
  - AUM, net flows and fee rate (asset managers); client assets and net new assets (brokers)
valuation_methods:
  - P/B and P/TBV judged against sustainable ROE/ROTCE (justified P/B logic)
  - P/E on normalized earnings (normalized credit costs, reserve releases stripped out)
  - Excess capital and capital-return capacity as context
  - Sum-of-the-parts reading for conglomerates (BRK-B style), cited from filings
  - P/E and DCF for capital-light fee businesses (asset managers, exchanges, payment networks)
  - DCF and EV-based metrics treated as not meaningful for banks and insurers
macro_sensitivities:
  - {series: DFF, direction: mixed, mechanism: "Higher short rates lift asset yields at asset-sensitive banks until deposit costs catch up, and raise credit stress for borrowers."}
  - {series: DGS2, direction: mixed, mechanism: "Front-end yields set deposit pricing pressure and the yield on floating-rate loans."}
  - {series: DGS10, direction: mixed, mechanism: "Higher long yields lift reinvestment and insurer investment income but create unrealized losses on fixed-rate securities that cut tangible book."}
  - {series: T10Y2Y, direction: positive, mechanism: "A steeper curve widens the borrow-short lend-long spread; inversion squeezes NIM."}
  - {series: UNRATE, direction: negative, mechanism: "Unemployment drives consumer credit losses (cards, auto, mortgages) with a lag."}
  - {series: PAYEMS, direction: positive, mechanism: "Payroll growth supports loan demand, deposit growth and credit quality."}
  - {series: INDPRO, direction: positive, mechanism: "Industrial activity drives commercial and industrial loan demand and corporate credit quality."}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Widening high-yield spreads flag a turning credit cycle, mark-to-market losses and weaker capital-markets activity."}
  - {series: VIXCLS, direction: mixed, mechanism: "Volatility lifts trading and exchange volumes but hurts underwriting, M&A advisory and AUM-based fees."}
  - {series: CPIAUCSL, direction: mixed, mechanism: "Inflation raises insurance claims severity (auto, property, casualty) but lifts nominal loan and premium growth."}
failure_modes:
  - Asset-liability duration mismatch with deposit flight
  - Late-cycle loan growth in concentrated or weak credit (CRE, construction, subprime consumer)
  - Reserve releases flattering earnings at the top of the credit cycle
  - Insurance reserve deficiency in long-tail lines (adverse development)
  - Underpricing in a soft insurance market or underpriced catastrophe exposure
  - Low P/B value trap with structurally sub-cost-of-equity returns
  - Fee compression and active outflows at asset managers
  - Regulatory capital increases or limits on capital return
  - Operational, fraud or conduct failures (trading losses, sales-practice scandals)
edgar_tags_of_interest:
  - InterestAndDividendIncomeOperating
  - InterestIncomeExpenseNet
  - ProvisionForLoanLeaseAndOtherLosses
  - FinancingReceivableCreditLossExpenseReversal
  - NoninterestIncome
  - NoninterestExpense
  - RevenuesNetOfInterestExpense
  - Deposits
  - LoansAndLeasesReceivableNetReportedAmount
  - FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest
  - AvailableForSaleSecuritiesDebtSecurities
  - HeldToMaturitySecuritiesFairValue
  - AccumulatedOtherComprehensiveIncomeLossNetOfTax
  - PreferredStockValue
  - PremiumsEarnedNet
  - PolicyholderBenefitsAndClaimsIncurredNet
  - LiabilityForClaimsAndClaimsAdjustmentExpense
  - EquitySecuritiesFvNiGainLoss
---

# Financials — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **GICS industries covered:** Banks; Financial Services (multi-sector holdings, diversified
  financial services, commercial and residential mortgage finance, transaction and payment
  processing services); Consumer Finance; Capital Markets (asset management and custody banks,
  investment banking and brokerage, financial exchanges and data); Mortgage REITs; Insurance
  (brokers, life and health, multi-line, property and casualty, reinsurance).
- **Benchmark:** XLF (`sector_etfs.financials` in `config/committee.yaml`). Cap-weighted, it mixes
  money-center banks, a conglomerate holding company, payment networks, insurers and exchanges —
  relative strength vs XLF compares a regional bank with a very different basket.
- **Classification:** `etf_lookthrough.classify_security`: `sector_override` → `etf_overrides.yaml` →
  Yahoo ("Financial Services" → `financials`) → SIC fallback (6798 → real estate). Payment processors
  belong here since 2023 GICS, but Yahoo labels some fintechs "Technology" — use `sector_override`.
- **Sector class:** `common.sector_class` returns `financials` for this sector. Consequences:
  fundamentals report EV, EV/EBIT, EV/Sales, net debt/EBITDA and gross margin as `not_meaningful`
  (not penalized) and compute P/B, P/TBV and ROE instead; the valuation grid uses `pe` and `p_b`
  bands; `evidence.required_fields.industry_ops.financials` drops supply chain and capacity/backlog.
- **BRK-B and conglomerates:** `etf_overrides.yaml` classifies BRK-B as financials with flag
  `conglomerate`. Analyze it here, but as a holding company: insurance (with float), railroad,
  energy and utilities, manufacturing/service/retail businesses, a large equity portfolio and cash.
- **Sub-industry differences:**
  - *Banks* — money-center (NII plus trading, investment banking, wealth fees) vs regional (spread
    lending, deposit franchise, CRE concentration); card and consumer lenders are the most credit-sensitive.
  - *Insurers* — P&C and reinsurance: underwriting cycle, catastrophe exposure, reserve adequacy;
    life: spread and asset-liability management on long-duration liabilities.
  - *Fee businesses* — asset managers, brokers, exchanges, data and payment networks: capital-light,
    market- and flow-driven, valued like quality franchises rather than balance-sheet lenders.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Net interest margin (NIM) | Core spread profitability of a lender | Earnings release; 10-K Item 7 average balance and yield tables | Healthy: stable with a sticky deposit base. Warning: NIM held up by hedges or accretion that roll off |
| Deposit beta | How fast funding costs follow policy rates | Earnings materials (cumulative beta); 10-K rate paid on interest-bearing deposits | Warning: beta accelerating, noninterest-bearing share falling, reliance on brokered deposits or wholesale funding |
| Uninsured / concentrated deposits | Run risk in a confidence shock | 10-K uninsured deposit disclosure; Item 1A | Warning: high uninsured share with a concentrated depositor base (one industry or region) |
| CET1 ratio | Loss-absorbing capital and capacity to return cash | 10-K Item 7 regulatory capital table; earnings supplement | Healthy: comfortable buffer over requirement plus stress buffer. Warning: ratio falling while buybacks continue |
| Provisions and NCOs | Credit cost drives the earnings cycle | Earnings release credit tables; 10-K credit quality note | Warning: provisions below NCOs (reserve release) while delinquencies rise |
| Allowance coverage / NPAs | Cushion against future losses | 10-K allowance rollforward and nonaccrual tables | Warning: coverage falling as nonperformers rise; office and construction CRE concentration |
| Loan-to-deposit / liquidity | Funding stability | Balance sheet; liquidity section of Item 7 | Warning: loans growing much faster than deposits; liquidity below uninsured deposits |
| Efficiency ratio | Cost discipline and scale | Earnings release | Healthy: low-50s or better for a regional bank (*heuristic*). Warning: drifting up with revenue under pressure |
| ROE / ROTCE vs cost of equity | Whether the franchise creates value at all | `valuation.roe_pct` in fundamentals; ROTCE from the release | Healthy: sustained above cost of equity. Warning: ROE propped up by reserve releases or leverage |
| Securities marks | Hidden equity erosion when rates rise | 10-K securities note (AFS losses in AOCI; HTM fair value vs amortized cost) | Warning: combined AFS and HTM losses large relative to tangible common equity |
| Combined ratio (P&C) | Underwriting profitability | Earnings release; 10-K Item 7 | Healthy: below 100% on an underlying basis (*heuristic*). Warning: accident-year ratio worsening behind favorable development |
| Prior-year reserve development | Whether past reserves were adequate | 10-K loss reserve rollforward and development tables; statutory Schedule P (NAIC) | Healthy: consistent modest favorable development. Warning: adverse development in casualty / long-tail lines |
| Investment yield / new-money yield | Insurer earnings sensitivity to rates | Insurer supplements | Warning: reaching for yield in credit or illiquid assets |
| AUM, net flows, fee rate; client assets (brokers) | Revenue engine of asset managers and brokers | Monthly AUM and client-asset releases, 10-Q | Warning: persistent active outflows, fee-rate compression, performance fees as a large earnings share, client cash sorting into higher-yield products |

## 3. Valuation methods

**What `scripts/valuation.py` provides:** multiple cells from `valuation.multiple_bands.financials`
(`pe`, `p_b`; investor assumptions, source `D:CFG:valuation.multiple_bands`), DCF cells where base
cash flows exist, and a reverse DCF. Fair values must equal the mean of cited cells; `cells_base` must
span DCF and multiple families when both exist.

- **No EV and little DCF for lenders and insurers.** Deposits and policy reserves are raw material,
  not financing, so EV metrics are `not_meaningful` by design. Banks and insurers rarely tag
  `OperatingIncomeLoss`, so `dcf_nopat` cells are normally `na`; operating cash flow is driven by
  trading assets, loans held for sale and reserve flows, so `dcf_fcf` cells are noise. If DCF cells
  are populated, the family rule (`validation.min_base_method_families`) forces one into `cells_base`.
  Choose it by a rule fixed before reading any result: the zero-growth cell at the highest rate in
  `valuation.discount_rates`, `DCF-NOPAT` before `DCF-FCF`; if that cell is `na`, the lowest populated
  DCF cell. State the rule in `method` and flag the DCF family as low reliability in `quality_notes`
  (the cells carry `financials_low_reliability`). Never pick the cell that best matches the P/B answer.
- **P/B vs ROE is the anchor.** Justified P/B = (ROE − g) / (COE − g): a bank that cannot earn its cost
  of equity deserves a discount to book however cheap it looks; durable excess ROTCE earns a premium.
  Read `valuation.p_b`, `p_tbv` and `roe_pct` together (qualitatively); prefer P/TBV for acquirers.
- **P/E needs normalized credit.** Benign-cycle P/E (low provisions, reserve releases) understates
  risk; trough provisions inflate it. Say where TTM sits in the credit cycle and lean on the low `pe`
  cells when credit costs look abnormally low.
- **Insurers:** P/B ex-AOCI (where disclosed) vs ROE, and P/E on operating earnings; life insurers'
  GAAP book swings with AOCI and long-duration remeasurement — cite book ex-AOCI from the supplement.
- **Fee businesses** (asset managers, exchanges, payment networks, brokers): buybacks shrink and
  goodwill inflates equity, so `p_b` cells mislead. Base on `pe` plus DCF cells (FCF is meaningful
  here) and note in `method` that the financials class suppressed EV/EBIT.
- **BRK-B-style conglomerates:** GAAP net income includes unrealized equity-portfolio gains and
  losses, so P/E cells and `ni_cagr_3y` swing with markets. Use cited operating earnings for context,
  treat P/B as the steadier anchor, and frame value as a sum of parts (insurance and float, rail,
  energy/utilities, other businesses, equity portfolio, cash) from the 10-K or annual letter — never
  compute a SOTP in prose.
- **Mortgage REITs:** reported book value per share and P/B; "distributable earnings" are non-GAAP.

## 4. Macro sensitivities

Direction = effect on the sector's earnings or valuation when the series **rises**. Read levels and
changes only from `macro.json`; never quote a macro figure from memory.

| Series | Direction | Mechanism |
|---|---|---|
| DFF | mixed | Asset-sensitive banks gain NII until deposit betas catch up; borrower stress rises |
| DGS2 | mixed | Front-end yields set deposit pricing pressure and floating loan yields |
| DGS10 | mixed | Better reinvestment and insurer investment income vs unrealized losses that cut tangible book |
| T10Y2Y | positive | Steeper curve widens the lending spread; inversion compresses NIM |
| UNRATE | negative | Consumer credit losses follow unemployment with a lag |
| PAYEMS | positive | Loan demand, deposit growth, credit quality |
| INDPRO | positive | Commercial and industrial loan demand and corporate credit quality |
| BAMLH0A0HYM2 | negative | Credit-cycle turn, mark-to-market losses, weaker issuance and M&A activity |
| VIXCLS | mixed | Trading and exchange volumes up; underwriting, advisory and AUM fees down |
| CPIAUCSL | mixed | Claims severity up for insurers; nominal loan and premium growth up |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  30-year mortgage rate (Freddie Mac PMMS), Fed H.8 bank loans and deposits, the Senior Loan Officer
  Opinion Survey (SLOOS), delinquency and charge-off rates (Federal Reserve, FDIC Quarterly Banking
  Profile), commercial real estate price indices, and catastrophe-loss estimates for insurers.

## 5. Typical failure modes

- **Duration mismatch plus deposit flight:** rate rises leave long fixed-rate securities underwater;
  uninsured depositors run; forced sales crystallize losses (the 2023 regional-bank failures).
- **Late-cycle credit:** fast loan growth in commercial real estate, construction, leveraged lending
  or subprime consumer; reserves released at the peak; losses arrive after the turn.
- **Reserve deficiency:** long-tail casualty and social inflation surface years later, in clusters.
- **Soft-market underpricing and cat exposure:** outsized premium growth; cat losses above models.
- **Low P/B value traps:** structurally sub-cost-of-equity returns, legacy cost bases and regulatory
  constraints keep cheap banks cheap; book value is not cash if assets are marked optimistically.
- **Fee compression:** passive shifts erode active managers' fee rates for years.
- **Regulatory shocks:** higher capital or stress buffers, fee caps, limits on buybacks and dividends.
- **Operational and conduct failures:** rogue trading, sales-practice scandals, cyber incidents, consent orders.

## 6. Red flags for the Devil's Advocate

- NIM expansion from securities repositioning, hedge gains or purchase accounting accretion.
- Provisions below NCOs while early-stage delinquencies rise.
- Rapid loan growth funded by brokered deposits or wholesale borrowings; loan-to-deposit climbing.
- High uninsured deposits combined with large unrealized HTM losses relative to tangible equity.
- CET1 falling while buybacks continue; capital flattered by excluding AOCI from regulatory capital.
- Growing Level 3 assets, large "other assets", or frequent changes in fair-value methodology.
- Insurers: reserve releases propping earnings as accident-year results worsen; premium growth far above peers.
- Asset managers: performance fees driving earnings; flagship fund underperformance.
- Late filings (NT 10-K/10-Q), auditor changes, material weaknesses, regulatory consent orders.
- BRK-B: succession and capital-deployment questions, buyback pauses, GAAP EPS presented as operating performance.

## 7. Lens checklist

- **fundamental**
  - Base on `pe` and `p_b` cells; a DCF cell the family rule forces in follows the fixed rule in §3.
  - Read `valuation.p_b`, `p_tbv` and `roe_pct` against cited ROTCE and cost-of-equity context.
  - Normalize credit: compare provisions with NCOs from the release; say where TTM sits in the cycle.
  - Check preferred stock in equity, AOCI and HTM marks (10-K securities note) before trusting book.
  - Conglomerates: cite operating earnings and segment results; note unrealized gains in GAAP NI.
- **technical**
  - Use only `technicals_<T>.json`. Banks move with rates and credit spreads as a group; separate
    stock-specific from group moves with `vs_sector` vs `vs_spy`.
  - Confidence shocks gap through `stop_candidates`; say so for names with deposit or credit concerns.
  - Elevated `atr_pctile_252` in a financial is often a stress signal, not just noise.
- **operator_ceo**
  - Capital allocation: buybacks vs CET1 headroom, dividend policy, bank M&A (tangible book dilution
    and earn-back period).
  - Incentives (DEF 14A): EPS-based pay encourages reserve releases; prefer ROTCE or risk-adjusted metrics.
  - Guidance credibility: NII, expense and credit-cost guidance vs delivery.
  - Insider activity: open-market buying by bank executives during stress is informative.
  - Regulatory standing: consent orders, enforcement history, risk-culture signals.
- **industry_ops** (required fields for this class: execution, unit economics, operational moat)
  - Execution: technology spend and digital adoption, branch optimization, acquisition integration.
  - Unit economics: cost of deposits, revenue per account, underwriting margin by line, cost to serve.
  - Operational moat: low-cost deposits, payment-network scale, insurance distribution, exchange network effects.
  - Supply chain and capacity/backlog are usually not applicable — say so rather than padding.
- **osint**
  - CFPB complaint database for banks, card issuers, servicers and lenders; app ratings.
  - Regulatory actions: OCC, Federal Reserve, FDIC, SEC, state insurance regulators.
  - Deposit-run chatter and social media stress signals; short-seller reports.
  - Rating agency actions (credit ratings, insurer financial strength ratings).
  - Earnings-call language: "normalization", "reserve build", "deposit remix", "pricing discipline".

## 8. Data notes

- **Not-meaningful metrics:** with `sector_class: financials`, EV, EV/EBIT, EV/Sales, net
  debt/EBITDA and gross margin carry `na_reason: not_meaningful` — not a data gap, not penalized.
- **Revenue and operating income tags:** banks may report `Revenues`, `RevenuesNetOfInterestExpense`
  or only components (`InterestAndDividendIncomeOperating`, `NoninterestIncome`); if `tag_map.revenue`
  picked gross interest income, margins and growth are distorted. `OperatingIncomeLoss` is rarely
  tagged by banks and insurers, so operating margin, EBITDA and course NOPAT are usually null.
- **Debt:** the debt formulas capture long-term debt and short-term borrowings; deposits are excluded
  and FHLB advances may sit in tags the fetcher does not read. `net_debt` is not a solvency measure here.
- **Equity includes preferred stock** (`PreferredStockValue`), so P/B, P/TBV and ROE are measured
  against total rather than common equity. Cite common TBV per share from the release when it matters.
- **Regulatory capital, NIM, NCO ratio, efficiency ratio and combined ratio** are rarely in
  companyfacts in usable form — read the earnings supplement or 10-K tables and cite the URL.
- **Insurance reserves:** Schedule P is a statutory (NAIC) filing, not EDGAR; use the 10-K development tables.
- **BRK-B share classes:** Class A converts into a fixed number of Class B shares (ratio stated in the
  10-K), so summing raw per-class dei counts is wrong. Expect `share_count_ambiguous` and a Yahoo
  market cap; check per-share values against `shares` before citing them. Unrealized equity gains in
  NI (`EquitySecuritiesFvNiGainLoss`) also distort `tax_rate_used`, ROE and growth figures.
- **Mortgage REITs** carry SIC 6798, which the SIC fallback maps to real estate although GICS places
  them in Financials — use `sector_override` when held.
- **Payment networks and processors** inherit the financials class, so meaningful EV metrics are
  suppressed; note it in `quality_notes` and do not rely on `p_b`.
- **Foreign banks and insurers** (20-F, IFRS) return mostly nulls; read the annual report.
