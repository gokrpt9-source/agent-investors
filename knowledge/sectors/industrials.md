---
sector: industrials
name: Industrials
benchmark_etf: XLI
sector_class: default
key_kpis:
  - "Orders and book-to-bill (short-cycle vs long-cycle)"
  - "Backlog / remaining performance obligations (RPO) and conversion"
  - "Organic growth split into price vs volume"
  - "Segment margins and incremental/decremental margins through the cycle"
  - "Aftermarket / services share of revenue"
  - "Price-cost spread"
  - "Working capital intensity (inventory, receivables, contract assets and liabilities)"
  - "FCF conversion (FCF / net income)"
  - "Capex intensity (maintenance vs growth)"
  - "Pension and legacy liabilities"
valuation_methods:
  - "dcf_nopat on mid-cycle (not peak or trough) earnings"
  - "dcf_fcf cross-checked over several fiscal years (working-capital swings)"
  - "ev_ebit as the primary multiple, read against cycle position"
  - "pe as a secondary check (distorted by amortization, restructuring, pension income)"
  - "reverse DCF implied growth vs through-cycle organic growth"
  - "sum-of-the-parts for multi-segment conglomerates (qualitative, from the segment note)"
macro_sensitivities:
  - {series: DFF, direction: negative, mechanism: "Higher policy rates raise financing costs for equipment buyers, dealers, rental fleets and leveraged acquirers"}
  - {series: DGS2, direction: negative, mechanism: "Front-end yields track the expected policy path that sets equipment and project financing costs"}
  - {series: DGS10, direction: negative, mechanism: "Higher long yields raise hurdle rates for customer capex and compress multiples of long-duration compounders"}
  - {series: T10Y2Y, direction: mixed, mechanism: "Inversion warns of a capex slowdown; growth-led re-steepening is early-cycle positive, emergency-cut steepening is not"}
  - {series: CPIAUCSL, direction: mixed, mechanism: "Lifts nominal revenue and pricing, but fixed-price long-cycle contracts and lagged escalators compress margins"}
  - {series: CPILFESL, direction: mixed, mechanism: "Sticky wage and services inflation raises costs for labor-intensive services and keeps rates high"}
  - {series: UNRATE, direction: negative, mechanism: "Rising unemployment signals falling freight, staffing and non-residential construction demand"}
  - {series: PAYEMS, direction: positive, mechanism: "Payroll growth supports staffing volumes, freight demand and commercial construction"}
  - {series: INDPRO, direction: positive, mechanism: "Closest proxy for short-cycle demand: machinery, distribution, electrical components, freight"}
  - {series: DTWEXBGS, direction: negative, mechanism: "Stronger dollar cuts translated foreign revenue and export competitiveness of US-made equipment"}
  - {series: DCOILWTICO, direction: mixed, mechanism: "Lifts energy-equipment demand but raises fuel costs for airlines and truckers (surcharges lag)"}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Wider high-yield spreads signal tighter credit, deferred customer capex and stress on leveraged roll-ups"}
  - {series: VIXCLS, direction: negative, mechanism: "Above-market beta; volatility spikes coincide with de-rating of cyclicals"}
failure_modes:
  - "Buying peak-cycle margins at a 'cheap' peak P/E (cyclical value trap)"
  - "Channel destocking mistaken for end-demand collapse, or restocking mistaken for structural growth"
  - "Fixed-price long-cycle programs producing reach-forward losses and EAC charges"
  - "Serial acquirers whose adjusted margins exclude recurring restructuring and amortization"
  - "Conglomerate sprawl: goodwill impairments, complexity discount, stranded costs after spin-offs"
  - "Legacy liabilities (pension, asbestos, environmental) surfacing as cash calls"
  - "Leverage combined with fuel or labor shocks in airlines and trucking"
  - "Quality or certification failures halting production lines"
  - "Tariff and trade-policy shocks to supply chains and export demand"
edgar_tags_of_interest:
  - RevenueRemainingPerformanceObligation
  - ContractWithCustomerLiability
  - ContractWithCustomerAssetNetCurrent
  - InventoryNet
  - AccountsReceivableNetCurrent
  - StandardProductWarrantyAccrual
  - DefinedBenefitPlanFundedStatusOfPlan
  - OperatingLeaseLiability
  - AmortizationOfIntangibleAssets
  - RestructuringCharges
  - GoodwillImpairmentLoss
  - ResearchAndDevelopmentExpense
  - PaymentsToAcquireBusinessesNetOfCashAcquired
  - PaymentsForRepurchaseOfCommonStock
---
# Industrials — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **Benchmark ETF:** XLI (`sector_etfs.industrials` in `config/committee.yaml`); the technicals file's
  `relative_strength.vs_sector` is measured against it and the screen universe is its top holdings.
- **GICS industry groups covered:**
  - *Capital Goods* — aerospace & defense, building products, construction & engineering, electrical
    equipment, industrial conglomerates, machinery, trading companies & distributors.
  - *Commercial & Professional Services* — waste, environmental and facility services, security, office
    services; staffing, research & consulting, HR/payroll and outsourcing.
  - *Transportation* — air freight & logistics, airlines, marine, rails, trucking, transport infrastructure.
- **How a ticker lands here:** `classify_security` order is portfolio `sector_override` →
  `etf_overrides.yaml` → Yahoo `info.sector` "Industrials" (`YF_SECTOR_MAP`) → rough SIC fallback.
- **Label mismatches:** Yahoo uses a Morningstar-style taxonomy, not GICS. Payroll/HR processors may sit
  in Technology on Yahoo but Industrials under GICS; heavy-truck and auto-parts makers straddle
  Industrials and Discretionary; packaging is Materials. Flag an ambiguous label; never re-classify silently.
- **Sub-industry differences** (they change which KPIs dominate):
  - *Short-cycle* (distributors, tools, electrical components, flow control, trucking): orders turn
    within quarters; daily sales rates, book-to-bill and purchasing-manager surveys lead.
  - *Long-cycle* (commercial aerospace, defense primes, grid and power-generation equipment, rail
    equipment, E&C): multi-year backlog, program accounting, customer advances; the risk is execution
    and contract terms, not next quarter's orders.
  - *Aftermarket-heavy* (engines, aero components, elevators, installed-base services): recurring,
    higher-margin, less cyclical revenue; the installed base is the moat.
  - *Transports* (rails, truckload/LTL, parcel, airlines): network density, operating ratio, fuel and
    labor contracts; airlines are lease-intensive with thin through-cycle returns.
  - *Services* (waste, staffing, consulting, uniforms, security): renewals, escalators, wage costs;
    waste behaves like a quasi-utility, staffing is the most cyclical corner.

## 2. KPIs that matter

Agents quote KPIs from the run's data files or cited sources; they do not compute new ratios in prose.
A KPI that cannot be sourced is `null` and listed in `missing_data`.

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Orders & book-to-bill | Leads revenue by quarters (short-cycle) or years (long-cycle) | Earnings release / supplement; 10-K Item 7 MD&A (non-GAAP, definitions vary) | Healthy: rolling-year book-to-bill at or above 1 with volume, not just price. Warning (heuristic): below 1 for several consecutive quarters, or orders propped by a few mega-awards |
| Backlog / RPO & conversion | Revenue visibility and the quality of contract terms | Revenue footnote RPO (10-K Item 8); backlog in Item 1 / MD&A | Healthy: backlog grows with stable or better embedded margin, low cancellations. Warning: growth from fixed-price legacy work, rising push-outs or cancellations, a changed backlog definition |
| Organic growth: price vs volume | Separates pricing power from demand | Earnings-release sales bridge; MD&A | Healthy: positive volume, price holding as input costs ease. Warning (heuristic): price-only growth with volume negative for two or more quarters |
| Segment margin through the cycle | Structural improvement vs cyclical uplift | Segment note (Item 8); MD&A | Healthy: each trough margin above the prior trough. Warning: expansion only at peak volumes |
| Incremental / decremental margins | Operating leverage and cost discipline | Company or analyst commentary in releases (cite; do not compute) | Warning (heuristic): decrementals much steeper than the company's own past incrementals = fixed-cost bloat |
| Aftermarket / services mix | Recurring, higher-margin, counter-cyclical revenue | Segment note, Item 1, investor-day material | Healthy: share rising with installed-base growth. Warning: OE sold at a loss without later aftermarket capture |
| Price-cost spread | Whether pricing keeps pace with materials, freight, wages | Earnings-call commentary; MD&A | Warning: repeated "price-cost headwind", or margins relying on surcharges that roll off |
| Working capital | Cash tied up, channel health; customer advances flatter OCF | Balance sheet and revenue note (Item 8); MD&A liquidity | Warning: inventory growing faster than sales; contract liabilities shrinking (FCF headwind); contract assets / unbilled receivables rising (revenue ahead of cash) |
| FCF conversion | Earnings quality | Company-reported conversion (earnings release, MD&A; cite it); otherwise compare `fy[].fcf` with `fy[].net_income` qualitatively (FCF above or below net income, year by year) — no script outputs the ratio, so never quote a computed percentage | Heuristic: company-reported conversion persistently well below roughly 80–90% outside an investment phase, or FCF below net income in most of the `fy[]` years, needs an explanation; FCF above net income in a single year may be a one-off working-capital release |
| Capex intensity | Rails, airlines, rental fleets are capital-heavy | `ttm.capex`, `fy[].capex`; MD&A capex guidance | Warning: capex below depreciation for years (under-investment) or a capex surge without contracted demand |
| Pension & legacy liabilities | Debt-like claims outside `net_debt` | Pension and contingencies notes (Item 8); Item 1A | Warning: large unfunded status, rising asbestos/environmental reserves, stepped-up required contributions |
| ROIC | Whether growth creates value | `valuation.roic_pct` in the fundamentals file | Warning: ROIC falling while goodwill-funded acquisitions rise |
| Sub-industry metrics | Rails/trucks: operating ratio, carloads/tonnage; airlines: unit revenue vs unit cost, load factor; distributors: daily sales; defense: funded backlog, contract mix | Earnings supplements; public industry data (AAR weekly rail traffic, ATA tonnage, BTS/DOT airline data, DoD contract announcements) | Warning: unit costs outrunning unit revenue; volume declines hidden by mix or surcharges |

## 3. Valuation methods

- **What fits:** EV/EBIT and DCF on *mid-cycle* earnings; P/E as a secondary check; reverse DCF to ask
  what growth the price already assumes. Nothing in this sector needs P/B.
- **Reading the `valuation_<T>.json` grid:**
  - `dcf_nopat` cells (ids like `DCF-NOPAT-r10-g03`) use course NOPAT = (EBITDA − capex) × (1 − tax)
    on TTM figures, which overstate the sustainable base at a cyclical peak and understate it at a
    trough. Near a peak the `g00`/`g03` cells are the realistic base; `g10`/`g15` belong only to secular
    growers (grid and electrification equipment, aftermarket compounders) with backlog evidence cited.
  - `dcf_fcf` cells use TTM FCF, which swings with inventory and customer advances; check several years
    of `fy[].fcf` first — a year of advance-payment inflows inflates FCF temporarily.
  - `pe` and `ev_ebit` cells come from `valuation.multiple_bands.industrials` (source
    `D:CFG:valuation.multiple_bands`) — investor assumptions, not market data; label them so. Low band
    on peak earnings, mid band mid-cycle, high band only for asset-light, aftermarket-heavy businesses.
  - `cells_base` must span both families (DCF and multiple) when both exist; each `fair_value_*` equals
    the mean of its cited cells. Terminal value is most of EV in every cell of the configured
    5-year exit-multiple grid (read each cell's `tv_share`; `tv_gt_90` rarely if ever fires), so a
    missing flag is no comfort — note the terminal dependence in `extensions.valuation.notes`
    (≤ 60 words).
  - Reverse DCF `implied_growth_pct`: compare with cited through-cycle organic growth. An implied rate
    above long-run history while margins sit at a peak is a warning, not a bargain — except for
    captive-finance companies, where consolidated `net_debt` inflates it (see below).
- **What misleads:**
  - A low P/E on peak earnings (the classic cyclical trap); a high P/E on trough earnings is often the
    better entry.
  - **Captive finance arms** (machinery, truck and equipment makers): consolidated `net_debt` includes
    the finance subsidiary's funding of its loan and lease book, so EV and leverage are overstated and
    **every cell that bridges through `net_debt` is biased** — `dcf_nopat`, `dcf_fcf` and `ev_ebit` fair
    values come out too low (or `na` with `equity_value_not_positive`) and the reverse DCF
    `implied_growth_pct` too high (not "priced for perfection"). Only `pe` cells are unaffected: lean on
    them. Cite equipment-operations net debt or net cash from the 10-K or release (W#); never adjust a
    grid cell by hand. If the two-family rule forces a DCF cell into `cells_base`, use the single cell
    the cycle justifies (do not jump growth tiers to offset the bias), state the bias in
    `extensions.valuation.notes`, and raise it as a red flag for the sector head and Devil's Advocate.
  - Airlines and lessors: `net_debt` excludes operating lease liabilities, flattering EV/EBIT.
  - Adjusted EPS that excludes acquisition amortization and "recurring non-recurring" restructuring;
    non-operating pension income inflating EPS.
  - Conglomerates priced on one multiple: describe a sum-of-the-parts view qualitatively from the
    segment note; do not compute it.

## 4. Macro sensitivities

Direction = effect on sector earnings or relative valuation when the series **rises**. Agents cite
current readings from `phase1/macro.json` (source `D:FRED:<ID>`); the table lists transmission channels.

| Series | Direction | Mechanism |
|---|---|---|
| DFF | negative | Raises financing costs for equipment buyers, dealers, rental fleets and leveraged acquirers; short-cycle orders soften with a lag |
| DGS2 | negative | Tracks the expected policy path that prices equipment loans, leases and project finance |
| DGS10 | negative | Raises hurdle rates for customer capex and infrastructure projects; compresses multiples of long-duration compounders |
| T10Y2Y | mixed | Inversion warns of a capex slowdown; growth-led re-steepening is early-cycle positive, steepening from emergency cuts is not |
| CPIAUCSL | mixed | Lifts nominal revenue and list prices, but fixed-price long-cycle contracts and lagged escalators squeeze margins |
| CPILFESL | mixed | Sticky wage and services inflation hits labor-intensive services (staffing, waste, security) and keeps rates high |
| UNRATE | negative | Rising unemployment = falling freight, staffing hours and non-residential construction; tight labor raises wage costs |
| PAYEMS | positive | Payroll growth supports staffing volumes, freight demand and commercial construction |
| INDPRO | positive | Best proxy for short-cycle demand: machinery, distribution, electrical components, freight volumes |
| DTWEXBGS | negative | Stronger dollar cuts translated foreign sales and export competitiveness of US-made equipment and aircraft parts |
| DCOILWTICO | mixed | Lifts demand for energy-related equipment; raises fuel costs for airlines and truckers (surcharges recover with a lag) |
| BAMLH0A0HYM2 | negative | Wider spreads signal tighter credit, deferred customer capex and refinancing stress for leveraged roll-ups |
| VIXCLS | negative | Above-market beta; volatility spikes coincide with de-rating of cyclicals |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  ISM / S&P Global manufacturing PMI and new orders, core capital-goods orders (Census M3), capacity
  utilization (Federal Reserve G.17) and freight volumes (Cass Freight Index, AAR rail traffic, ATA
  tonnage) lead short-cycle orders better than `INDPRO`; also non-residential construction spending
  (Census C30), aircraft orders and deliveries, and defense appropriations status.

## 5. Typical failure modes

- **Peak-cycle value trap:** record margins plus a low P/E look cheap right before orders roll over;
  the earnings base, not the multiple, is what collapses.
- **Inventory-cycle misreads:** destocking looks like end-demand collapse (restocking like structural
  growth); the bullwhip reverses within a few quarters.
- **Long-cycle execution:** fixed-price development programs (aerospace, defense, large E&C) produce
  reach-forward losses and estimate-at-completion charges years after the contract is won.
- **Roll-up fatigue:** "adjusted" margins rise while goodwill, amortization and restructuring pile up
  and organic growth quietly turns negative.
- **Conglomerate sprawl:** complexity discount, impairments, stranded costs after spin-offs.
- **Legacy liabilities:** pensions, asbestos and environmental reserves turn into cash calls.
- **Transport leverage:** high fixed costs plus fuel and labor shocks; past airline and trucking
  downturns ended in restructurings that wiped out equity.
- **Quality and certification failures:** a production halt or grounding cascades through suppliers.
- **Policy shocks:** tariffs, export controls and defense continuing resolutions delay orders.

## 6. Red flags for the Devil's Advocate

- Book-to-bill below 1 while management talks up backlog "quality" or switches backlog definition.
- Revenue growth that is mostly price while volumes decline; surcharge revenue presented as organic.
- Inventory and receivables outgrowing sales; customer advances falling while revenue still rises.
- Margin expansion that depends on excluding restructuring every year, or on pension income.
- Rising warranty accruals, quality escapes, or regulatory production caps.
- Fixed-price share of backlog rising; EAC adjustments turning negative; program charges "one-time" twice.
- Goodwill a large share of equity with ROIC falling; acquisitions funded by debt near the cycle peak.
- Captive-finance credit losses rising or dealer inventory building (machinery/trucks).
- Captive-finance company valued on DCF or `ev_ebit` cells, or on a face-value reverse-DCF implied
  growth, without noting that finance-arm debt in `net_debt` biases them (see §3).
- Fundamental base case using `g10`/`g15` DCF cells on peak-margin TTM earnings without backlog support.
- Technical uptrend driven by the whole sector (strong `vs_spy`, flat `vs_sector`) — beta, not alpha.

## 7. Lens checklist

- **fundamental**
  - Place the company in its cycle (short vs long, OE vs aftermarket) before picking grid cells.
  - Compare FCF with net income across `fy[]` qualitatively (or cite company-reported conversion) and
    reconcile swings with working capital; never quote a self-computed conversion ratio.
  - Cover pension deficit, operating leases and captive-finance debt in `balance_sheet_notes` (§3).
  - Cite the multiple bands as configured assumptions; justify the band from cycle position.
- **technical**
  - Read `relative_strength.vs_sector` (vs XLI) separately from `vs_spy`: sector beta vs stock-specific.
  - Cyclicals trend hard both ways: respect `trend_down` even when valuation looks cheap; invalidation
    comes from computed `stop_candidates` or levels only.
- **operator_ceo**
  - Capital allocation through the cycle: buybacks at peaks vs troughs, acquisition returns vs promises.
  - Incentive metrics in the DEF 14A: adjusted EPS alone is weak; ROIC, FCF and organic growth are better.
  - Guidance credibility in the last downturn; insider Form 4 selling into peak orders.
- **industry_ops**
  - Lead times, capacity utilization, supplier constraints (castings, forgings, electronics, skilled labor).
  - Backlog conversion, contract mix (fixed-price vs cost-plus), price escalation clauses.
  - Installed base and aftermarket attach; dealer and distributor inventory levels.
- **osint**
  - Job postings and plant-expansion or WARN-notice layoff filings; supplier and customer commentary.
  - Regulator actions (FAA airworthiness directives, NTSB reports, OSHA citations), recalls,
    labor negotiations and strike threats; freight and rail traffic releases from public associations.

## 8. Data notes

- **Not in EDGAR's standard tags:** orders, book-to-bill, backlog (beyond RPO), organic growth,
  incrementals, operating ratio, unit revenue/cost — and companyfacts has no segment dimensions. Read
  and cite releases, supplements and the 10-K segment note, or record the gap in `missing_data`.
- **RPO (`RevenueRemainingPerformanceObligation`)** may cover only part of backlog (e.g. excludes
  unexercised options or unfunded defense orders) and is tagged inconsistently; prefer the company's
  own backlog disclosure and say which one is cited.
- **`net_debt` excludes** pension deficits and operating lease liabilities, but includes captive-finance
  debt when the finance arm is consolidated. `fetch_fundamentals.py` does not adjust for any of these,
  and `valuation.py` subtracts this `net_debt` in every DCF cell, every `ev_ebit` cell and the reverse DCF.
- **Spin-offs and divestitures:** recast years and "latest filed wins" de-duplication can mix restated
  and unrestated years in `fy[]`; check `tag_map` `stitched` and the `tag_mix` flag before trusting CAGRs.
- **Fiscal calendars:** 52/53-week years inflate YoY growth; watch `ttm.*.derivation` for how TTM was built.
- **Customer advances:** large `ContractWithCustomerLiability` balances make OCF/FCF lumpy; not recurring.
- **Yahoo `info` fields** (trailing P/E, sector label) can disagree with EDGAR-derived data; copy
  numbers from the run's data files only. Technicals use dividend-adjusted prices (`auto_adjust=True`),
  so older levels sit slightly below raw-chart levels.
