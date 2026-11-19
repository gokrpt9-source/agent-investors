---
sector: consumer_discretionary
name: Consumer Discretionary
benchmark_etf: XLY
sector_class: default
key_kpis:
  - "Same-store / comparable sales growth"
  - "Traffic (transactions) vs ticket (average spend) decomposition"
  - "Inventory turns and inventory growth vs sales growth"
  - "Gross margin vs promotions and markdowns"
  - "SG&A leverage and four-wall / unit-level margins"
  - "E-commerce penetration and unit economics (fulfillment cost, returns, CAC payback)"
  - "Store count growth and new-unit productivity"
  - "Autos: units, pricing vs incentives, dealer days' supply, warranty, captive-finance credit"
  - "Housing-linked: orders, cancellation rate, backlog, incentives"
  - "Travel and leisure: occupancy, rate, yield, booking curve, customer deposits"
valuation_methods:
  - "ev_ebit as the primary multiple (lease-adjust qualitatively for store-heavy retailers)"
  - "dcf_nopat / dcf_fcf for mature, cash-generative retailers, restaurants and brands"
  - "reverse DCF for high-growth e-commerce and platforms (implied growth vs runway)"
  - "pe as a cross-check (unreliable when equity is negative or earnings are cyclical)"
  - "Autos: industrial-only net cash and P/E on normalized earnings; consolidated EV not meaningful"
  - "Homebuilders: P/B vs ROE from filings as a cross-check outside the grid"
macro_sensitivities:
  - {series: DFF, direction: negative, mechanism: "Raises card, auto-loan and buy-now-pay-later costs; squeezes leveraged retailers' interest coverage"}
  - {series: DGS2, direction: negative, mechanism: "Front-end yields feed auto-loan and card APRs and floating-rate retailer debt"}
  - {series: DGS10, direction: negative, mechanism: "Sets mortgage rates (housing turnover, home-related spend, builder orders); compresses long-duration growth multiples"}
  - {series: T10Y2Y, direction: mixed, mechanism: "Growth-led steepening is early-cycle positive for discretionary; steepening from emergency cuts signals recession"}
  - {series: CPIAUCSL, direction: negative, mechanism: "Inflation in food, energy and shelter crowds out discretionary spend; nominal sales can rise while units fall"}
  - {series: CPILFESL, direction: negative, mechanism: "Sticky services inflation (rent, insurance) erodes budgets, keeps policy tight and lifts store wage costs"}
  - {series: UNRATE, direction: negative, mechanism: "Job losses hit discretionary spend first, especially big-ticket items and lower-income cohorts"}
  - {series: PAYEMS, direction: positive, mechanism: "Payroll growth drives aggregate income, the main driver of spending"}
  - {series: INDPRO, direction: mixed, mechanism: "Weak link except autos and durables production, where supply disruptions cap unit sales"}
  - {series: DTWEXBGS, direction: mixed, mechanism: "Stronger dollar cheapens imported COGS but cuts translated overseas sales and inbound tourist spend"}
  - {series: DCOILWTICO, direction: negative, mechanism: "Gasoline acts as a tax on lower-income consumers; raises freight and cruise fuel costs; shifts auto mix"}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Wider spreads tighten consumer credit and refinancing for leveraged and sponsor-owned chains"}
  - {series: VIXCLS, direction: negative, mechanism: "High-beta sector; equity wealth effects support high-end spending"}
failure_modes:
  - "Secular decliners that screen cheap (mall-based and department-store retail value traps)"
  - "Inventory gluts forcing markdowns and multi-quarter gross-margin resets"
  - "Fads and pull-forward demand mistaken for a new run-rate"
  - "Over-expansion: new stores cannibalizing existing ones and diluting returns"
  - "Leveraged buyouts and debt-funded buybacks leaving no cushion for a downturn"
  - "E-commerce growth with negative unit economics funded by capital markets"
  - "Autos and housing bought at peak volumes and peak pricing"
  - "Tariff and sourcing-concentration shocks"
  - "Franchisee distress undermining an asset-light franchisor"
edgar_tags_of_interest:
  - InventoryNet
  - CostOfGoodsAndServicesSold
  - SellingGeneralAndAdministrativeExpense
  - AdvertisingExpense
  - OperatingLeaseLiability
  - OperatingLeaseLiabilityCurrent
  - OperatingLeaseLiabilityNoncurrent
  - OperatingLeaseCost
  - ContractWithCustomerLiability
  - StandardProductWarrantyAccrual
  - NumberOfStores
  - ShareBasedCompensation
  - PaymentsForRepurchaseOfCommonStock
  - StockholdersEquity
---
# Consumer Discretionary — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **Benchmark ETF:** XLY (`sector_etfs.consumer_discretionary`). Cap-weighted and often dominated by a
  few mega-caps, so `relative_strength.vs_sector` may reflect one or two constituents. Top-10 weights:
  `universe[].etf_weight_pct` in `phase3/screen_consumer_discretionary.json` if it is among your inputs,
  else the issuer's holdings page (W#) — never from memory.
- **GICS industry groups covered:**
  - *Automobiles & Components* — automakers, auto parts and equipment, tires.
  - *Consumer Durables & Apparel* — homebuilders, household durables and appliances, leisure products,
    apparel, footwear, luxury goods.
  - *Consumer Services* — hotels, resorts, cruise lines, casinos, restaurants, leisure facilities,
    education and other diversified consumer services.
  - *Consumer Discretionary Distribution & Retail* — broadline and internet retail, specialty retail,
    home improvement, apparel retail, distributors.
- **How a ticker lands here:** portfolio `sector_override` → `etf_overrides.yaml` → Yahoo `info.sector`
  "Consumer Cyclical" (`YF_SECTOR_MAP`) → rough SIC fallback.
- **Label mismatches:** Yahoo's "Consumer Cyclical" is Morningstar-based: it includes packaging &
  containers (GICS Materials) and residential construction, while education services sit under Yahoo's
  "Consumer Defensive" but GICS Discretionary. General-merchandise and discount retailers have moved
  between Discretionary and Staples in GICS revisions; check the label in `lookthrough.json` and flag
  ambiguity rather than re-classifying.
- **Sub-industry differences:**
  - *Specialty & apparel retail:* comps, inventory, markdowns, lease-heavy store fleets, fashion risk.
  - *Broadline / e-commerce:* growth, fulfillment cost, marketplace take rate, advertising and
    subscription income; may consolidate unrelated high-margin segments (cloud, ads) that drive value.
  - *Restaurants:* franchised (asset-light, royalty streams, often negative equity) vs company-operated
    (four-wall margins, labor and food costs).
  - *Autos:* deep cyclicality, captive finance, regulatory credits, electrification capex.
  - *Homebuilders & home-related:* mortgage rates, land inventory, incentives; home improvement tracks
    housing turnover and home-equity wealth.
  - *Travel & leisure:* operating leverage on occupancy and yield; customer deposits fund working capital.
  - *Brands & luxury:* pricing power, wholesale vs direct-to-consumer mix, aspirational-customer cyclicality.

## 2. KPIs that matter

Agents quote KPIs from the run's data files or cited sources; they do not compute new ratios in prose.
A KPI that cannot be sourced is `null` and listed in `missing_data`.

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Comparable / same-store sales | Organic demand excluding new-store growth | Earnings release; 10-K Item 7 MD&A (company definitions differ) | Healthy: positive comps on positive traffic. Warning: comps decelerating while store growth accelerates; comp definition changed |
| Traffic vs ticket | Ticket-only growth (price, mix) is fragile; traffic is demand | Earnings release / call; restaurant and retail disclosures | Warning (heuristic): negative traffic for several quarters masked by price increases |
| Inventory turns / inventory vs sales growth | Leading indicator of markdowns | Balance sheet (Item 8); company-reported turns or days | Warning (heuristic): inventory growing materially faster than sales for two or more quarters; aged or in-transit inventory building |
| Gross margin vs promotions | Pricing power vs clearance activity | Income statement; MD&A margin bridge (markdowns, freight, shrink) | Healthy: stable margin with low promotional cadence. Warning: margin held by cutting freight or occupancy while markdowns rise |
| SG&A leverage | Fixed-cost absorption; wage pressure | Income statement; MD&A | Warning: SG&A growing faster than sales at positive comps (deleverage in good times) |
| Unit-level / four-wall economics | Store or restaurant returns drive expansion value | Investor-day material; 10-K Item 1 / MD&A (average unit volume, restaurant-level margin, cash-on-cash return) | Warning: new units opening at lower volumes or longer payback than the mature base |
| E-commerce unit economics | Growth only creates value if each order earns money | Segment note; MD&A; shareholder letters (fulfillment cost, return rates, contribution margin, CAC payback) | Warning: rising fulfillment or marketing cost per order; growth bought with discounts or free shipping |
| Store count & productivity | Runway and saturation | 10-K Item 2 (Properties); `NumberOfStores` where tagged | Warning: closures accelerating, sales per square foot falling |
| Autos | Volume and price discipline | Delivery/production releases; MD&A; public dealer-inventory data | Warning: rising incentives, dealer days' supply building, warranty accruals up, captive-finance delinquencies up |
| Housing-linked | Orders and margins lead revenue | Homebuilder releases (orders, cancellation rate, backlog, incentives) | Warning: rising cancellations and incentives, slowing absorption per community |
| Travel & leisure | Operating leverage on occupancy and yield | Releases (RevPAR, occupancy, net yields, booking curve); customer-deposit balance | Warning: discounting to fill capacity, shrinking advance bookings or deposits |
| Leverage incl. leases | Lease obligations are fixed charges | Lease note (Item 8); rating-agency lease-adjusted metrics (cite) | Warning: debt-funded buybacks with negative equity and falling interest coverage |

## 3. Valuation methods

- **What fits:** EV/EBIT for mature retailers, restaurants and brands; DCF for steady cash generators;
  reverse DCF for high-growth e-commerce and platforms, where the question is "what growth is priced in".
- **Reading the `valuation_<T>.json` grid:**
  - `dcf_nopat` (course NOPAT = (EBITDA − capex) × (1 − tax), TTM) suits store-based and franchised
    models with stable capex. For rapidly investing e-commerce or EV makers, TTM NOPAT/FCF understates
    the long-run base and cells go `na` if the base is ≤ 0 — for pre-profit growers the reverse DCF is
    the main tool; say the grid is low-information. Under the configured 5-year exit-multiple grid,
    terminal value is most of EV in every cell (read each cell's `tv_share`), so the absence of a
    `tv_gt_90` flag is no comfort.
  - `dcf_fcf` is distorted by inventory swings and customer deposits (travel); check `fy[].fcf` across years.
  - Growth tiers: mature retail and restaurants rarely justify more than the `g03`/`g06` cells as base;
    `g10`/`g15` need unit-growth runway evidence (cited store or market share data).
  - `pe` and `ev_ebit` cells use `valuation.multiple_bands.consumer_discretionary` (source
    `D:CFG:valuation.multiple_bands`) — configured assumptions; cite them as such. Low band for
    cyclical peaks (autos, housing), mid for steady franchises.
  - `cells_base` must span DCF and multiple families when both exist; `fair_value_*` = mean of cited cells.
- **What misleads:**
  - **Autos with captive finance:** consolidated debt funds loan and lease books, so `net_debt` and EV
    are not meaningful and **every cell that bridges through `net_debt` is biased** — `dcf_nopat`,
    `dcf_fcf` and `ev_ebit` fair values come out too low (often `na` with `equity_value_not_positive`)
    and the reverse DCF `implied_growth_pct` too high. Only `pe` cells are unaffected: lean on them (low
    band on peak-cycle earnings). Cite automotive-segment net cash and operating income from the 10-K or
    release (W#); never adjust a grid cell by hand. If the two-family rule forces a DCF cell into
    `cells_base`, use the single cell the cycle justifies, state the bias in
    `extensions.valuation.notes`, and raise it as a red flag.
  - **Negative equity** (franchisors and heavy buyback programs): P/B, ROE and often `roic_pct` are null
    or meaningless; use EV/EBIT and FCF yield, not book-based measures.
  - **Leases:** `net_debt` excludes operating lease liabilities; EV/EBIT flatters lease-heavy store
    fleets relative to owners. Mention lease intensity qualitatively.
  - **Homebuilders:** P/E on peak margins misleads; book value (largely land) vs ROE is the usual anchor,
    but the grid has no P/B cell here. Cite `valuation.p_b` / `valuation.roe_pct` from the fundamentals
    file as key figures (or, if null, company-reported values with W#, else `missing_data`).
    `fair_value_*` is still the mean of grid cells: prefer low-band `pe` / `ev_ebit` plus a `g00`/`g03`
    DCF cell, and note that peak-margin TTM earnings bias the grid upward.
  - **Mixed conglomerates** (retail plus cloud or advertising): one multiple misprices the mix; describe
    segment contributions from the segment note.

## 4. Macro sensitivities

Direction = effect on sector earnings or relative valuation when the series **rises**. Agents cite
current readings from `phase1/macro.json` (source `D:FRED:<ID>`); the table lists transmission channels.

| Series | Direction | Mechanism |
|---|---|---|
| DFF | negative | Raises card, auto-loan and buy-now-pay-later costs; squeezes leveraged retailers' interest coverage |
| DGS2 | negative | Feeds auto-loan and card APRs and floating-rate retailer debt |
| DGS10 | negative | Sets mortgage rates: housing turnover, home-related spend and builder orders; compresses long-duration growth multiples |
| T10Y2Y | mixed | Growth-led steepening is early-cycle positive; steepening from emergency cuts signals recession |
| CPIAUCSL | negative | Inflation in food, energy and shelter crowds out discretionary spend; nominal sales may rise while units fall |
| CPILFESL | negative | Sticky services inflation erodes budgets, keeps policy tight and lifts store and restaurant wage costs |
| UNRATE | negative | Job losses hit discretionary spend first, especially big-ticket items and lower-income cohorts |
| PAYEMS | positive | Payroll growth drives aggregate income, the main spending driver |
| INDPRO | mixed | Weak link except autos and durables production, where supply disruptions cap unit sales |
| DTWEXBGS | mixed | Stronger dollar cheapens imported goods but cuts translated overseas sales and inbound tourism |
| DCOILWTICO | negative | Gasoline is a tax on lower-income consumers; raises freight and cruise fuel costs; shifts auto mix |
| BAMLH0A0HYM2 | negative | Tighter credit for consumers and for leveraged or sponsor-owned chains facing refinancing |
| VIXCLS | negative | High-beta sector; equity wealth effects support high-end spending |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  30-year mortgage rates (Freddie Mac PMMS; `DGS10` is only a proxy), housing starts, permits and home
  sales (Census, NAR), retail sales (Census advance report), consumer sentiment (University of Michigan,
  Conference Board), real disposable income and saving rate (BEA), and consumer credit and delinquencies
  (Federal Reserve G.19, New York Fed household debt report).

## 5. Typical failure modes

- **Secular-decline value traps:** mall-based and department-store retail looked cheap on P/E and
  book value for years while traffic migrated online; "cheap" kept getting cheaper.
- **Inventory gluts:** a demand miss plus long lead times forces markdowns that reset gross margin for
  quarters; the balance sheet shows it before the income statement.
- **Pull-forward and fads:** stimulus- or trend-driven demand gets extrapolated; normalization looks
  like collapse.
- **Over-expansion:** new stores cannibalize the base and underperform; returns fall as revenue grows.
- **Leverage:** sponsor buyouts and debt-funded buybacks turn one bad season into a restructuring.
- **Growth without unit economics:** money-losing orders funded by capital markets; when funding
  tightens, growth stops and losses remain.
- **Peak-cycle durables:** autos, RVs, boats and homebuilders bought on peak volumes and peak pricing.
- **Sourcing shocks:** tariffs and single-country sourcing hit margins faster than pricing responds.
- **Franchise-system stress:** franchisee margins squeezed by wages and remodel mandates slow unit
  growth and royalty income.

## 6. Red flags for the Devil's Advocate

- Positive comps driven entirely by ticket while traffic declines; comp definition or calendar changes.
- Inventory outgrowing sales; aged inventory building; gross margin "protected" by reversible tailwinds.
- Debt-funded buybacks with negative equity and shrinking interest coverage.
- Lease obligations ignored in leverage discussion; closures accelerating while guidance assumes openings.
- Rising return rates, fulfillment cost per order, or customer-acquisition cost.
- Autos: incentives rising, dealer inventory building, captive-finance delinquencies and used-car
  prices falling (residual-value risk).
- Homebuilders: cancellations and incentives rising, land bought at peak prices.
- Valuation base case using `g10`/`g15` cells on peak-cycle TTM earnings.
- Captive-finance automaker valued on DCF or `ev_ebit` cells, or a face-value reverse-DCF implied growth,
  without noting that finance-arm debt in `net_debt` biases them (see §3).
- OSINT signals (reviews, app ratings, complaint volumes) deteriorating while management cites brand strength.

## 7. Lens checklist

- **fundamental**
  - Identify the business model (store, franchise, e-commerce, auto, builder, travel) before choosing cells.
  - Check inventory vs sales and gross-margin trend; cite comps, traffic and ticket from releases.
  - Cover leases, negative equity and captive-finance debt in `balance_sheet_notes` (see §3).
  - For high-growth names, frame the reverse DCF implied growth against cited runway evidence.
- **technical**
  - XLY is top-heavy: compare `vs_sector` with `vs_spy` before calling sector-relative strength.
  - Names gap on earnings and comps: note elevated `atr14_pct`; invalidation from `stop_candidates` only.
- **operator_ceo**
  - Capital allocation: store-growth discipline, buybacks vs balance-sheet strength, M&A record.
  - Incentives in the DEF 14A: comps and EPS alone vs ROIC, FCF, unit returns.
  - Founder/merchant leadership changes and creative-director turnover (brands); Form 4 selling patterns.
- **industry_ops**
  - Sourcing concentration and tariff exposure, lead times, distribution-center network, fulfillment cost.
  - Store or unit economics (four-wall margins, payback), labor scheduling and wage pressure.
  - Autos: production constraints, EV/hybrid mix, warranty and recall costs, dealer inventory.
- **osint**
  - Product and app reviews, complaint trends, web-traffic and search-interest trends from free sources.
  - NHTSA and CPSC recalls, store-closure announcements, hiring freezes, employee reviews; CFPB
    complaints for captive auto lenders and store cards.

## 8. Data notes

- **Non-GAAP KPIs:** comps, traffic, ticket, average unit volume, RevPAR, net yields, deliveries and
  orders are not standard us-gaap tags; read and cite releases and MD&A or record them as `missing_data`.
- **Fiscal calendars:** many retail years end in late January/early February (label and end date differ
  by a calendar year); 53-week years inflate YoY; a 16-week first quarter falls outside the quarter
  bucket (`fundamentals.duration_days.Q`) — check `ttm.*.derivation` and as-of dates for a lagging TTM.
- **Leases:** `OperatingLeaseLiability` (current + noncurrent) is excluded from `total_debt` and
  `net_debt`; lease-adjusted leverage must come from the lease note or a cited rating-agency view.
- **Negative equity:** `p_b`/`roe_pct` are null (`negative_denominator`) when equity ≤ 0 and `roic_pct`
  when equity + `net_debt` ≤ 0 — not a data error; read the `na_reason` (it may also be a missing input).
- **Captive finance:** `fetch_fundamentals.py` sums industrial and finance debt, and `valuation.py`
  subtracts that `net_debt` in every DCF, `ev_ebit` and reverse-DCF calculation; segment figures are in
  the filing only.
- **Gross vs net revenue:** marketplaces recognizing third-party sales net (agent) vs first-party gross
  (principal) are not comparable on revenue growth or gross margin; read the revenue-recognition note.
- **Deferred revenue:** gift cards, loyalty and travel deposits (`ContractWithCustomerLiability`) swing OCF/FCF.
- **Technicals use dividend-adjusted prices** (`auto_adjust=True`), slightly below raw-chart levels.
