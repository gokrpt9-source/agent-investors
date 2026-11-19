---
sector: consumer_staples
name: Consumer Staples
benchmark_etf: XLP
sector_class: default
key_kpis:
  - "Organic sales growth split into price/mix vs volume"
  - "Price elasticity (volume response to pricing)"
  - "Gross margin vs input costs (commodities, packaging, freight) and hedging lag"
  - "Brand reinvestment: advertising and promotion as a share of sales"
  - "Market share in measured channels and private-label share"
  - "Emerging-market exposure and FX translation / transaction effects"
  - "Productivity savings vs reinvestment"
  - "Cash conversion (FCF / net income) and working capital incl. supplier finance"
  - "Dividend payout and coverage; net debt / EBITDA"
  - "Staples retail: comps, traffic, membership renewal, shrink, private-label penetration"
valuation_methods:
  - "dcf_nopat and dcf_fcf (stable cash flows suit DCF; base growth in low tiers)"
  - "ev_ebit (adjust qualitatively for equity-method stakes outside EBIT)"
  - "pe cross-check against the company's own history and bond yields (qualitative)"
  - "reverse DCF implied growth vs the company's long-run organic growth algorithm"
  - "FCF yield and dividend coverage as sanity checks (P/B not meaningful)"
macro_sensitivities:
  - {series: DFF, direction: negative, mechanism: "Raises commercial-paper and floating funding costs and makes cash yields compete with dividend yields"}
  - {series: DGS2, direction: negative, mechanism: "Front-end yields reprice short-term funding and the cash alternative to dividend income"}
  - {series: DGS10, direction: negative, mechanism: "Bond-proxy valuation: higher long yields compress multiples of low-growth, high-payout names"}
  - {series: T10Y2Y, direction: mixed, mechanism: "Relative: growth-led steepening rotates money to cyclicals; steepening driven by rate cuts supports bond-proxy defensives"}
  - {series: CPIAUCSL, direction: mixed, mechanism: "Food inflation lifts nominal sales via pricing, but input costs rise first and elasticity lags"}
  - {series: CPILFESL, direction: mixed, mechanism: "Wage and freight inflation raises conversion costs; sticky core keeps yields high"}
  - {series: UNRATE, direction: mixed, mechanism: "Demand is defensive, but rising unemployment speeds trade-down to private label and discounters"}
  - {series: PAYEMS, direction: mixed, mechanism: "Low sensitivity; strong payrolls support premiumization and away-from-home consumption"}
  - {series: INDPRO, direction: mixed, mechanism: "Weak link; mainly a signal for packaging and logistics cost pressure"}
  - {series: DTWEXBGS, direction: negative, mechanism: "Large international sales: strong dollar cuts translated revenue/EPS; EM devaluations force volume-hurting pricing"}
  - {series: DCOILWTICO, direction: negative, mechanism: "Oil drives resin packaging, freight and agricultural input costs, passed through with a lag"}
  - {series: BAMLH0A0HYM2, direction: mixed, mechanism: "Risk-off spread widening favours defensives relatively; highly levered staples face higher refinancing costs"}
  - {series: VIXCLS, direction: positive, mechanism: "Relative: defensive rotation during volatility spikes; absolute returns still fall in broad sell-offs"}
failure_modes:
  - "Paying bond-proxy multiples for low growth, then de-rating when yields rise"
  - "Price-led growth masking volume loss until elasticity and private label catch up"
  - "Cutting brand investment to protect margins, followed by share loss and impairments"
  - "Leveraged mega-mergers ending in goodwill impairments and dividend cuts"
  - "Emerging-market currency crises erasing reported growth"
  - "Category disruption by insurgent brands, health trends or channel shifts"
  - "Retailer power: delistings, private-label substitution, trade-spend escalation"
  - "Regulatory and litigation shocks (tobacco/nicotine rules, product-liability cases)"
edgar_tags_of_interest:
  - RevenueFromContractWithCustomerExcludingAssessedTax
  - RevenueFromContractWithCustomerIncludingAssessedTax
  - ExciseAndSalesTaxes
  - CostOfGoodsAndServicesSold
  - AdvertisingExpense
  - SellingGeneralAndAdministrativeExpense
  - InventoryNet
  - Goodwill
  - IndefiniteLivedTrademarks
  - GoodwillImpairmentLoss
  - ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill
  - EquityMethodInvestments
  - IncomeLossFromEquityMethodInvestments
  - SupplierFinanceProgramObligation
  - PaymentsOfDividendsCommonStock
  - PaymentsForRepurchaseOfCommonStock
  - DefinedBenefitPlanFundedStatusOfPlan
---
# Consumer Staples — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **Benchmark ETF:** XLP (`sector_etfs.consumer_staples`). Cap-weighted and concentrated in a handful of
  household-products, beverage and staples-retail giants, so `vs_sector` may not be broad-sector strength.
  Top-10 weights: `universe[].etf_weight_pct` in `phase3/screen_consumer_staples.json` if it is among your
  inputs, else the issuer's holdings page (W#) — never from memory.
- **GICS industry groups covered:**
  - *Consumer Staples Distribution & Retail* — warehouse clubs, hypermarkets and supercenters, discount
    and dollar stores, food and drug retail, food distributors.
  - *Food, Beverage & Tobacco* — beverages (soft drinks, brewers, distillers and vintners), packaged
    foods and meats, agricultural products, tobacco and nicotine.
  - *Household & Personal Products* — household products (cleaning, paper, laundry) and personal care.
- **How a ticker lands here:** portfolio `sector_override` → `etf_overrides.yaml` → Yahoo `info.sector`
  "Consumer Defensive" (`YF_SECTOR_MAP`) → rough SIC fallback.
- **Label mismatches:** Yahoo's "Consumer Defensive" is Morningstar-based and includes education services
  (GICS Discretionary). General-merchandise and discount retailers have been re-assigned between
  Discretionary and Staples in GICS revisions, so Yahoo and the ETF may disagree; flag rather than
  re-classify. Drugstore chains with large pharmacy-benefit or health-services arms may behave more like
  Health Care.
- **Sub-industry differences:**
  - *Beverages:* concentrate/franchise-bottler models are capital-light with equity-method stakes in
    bottlers; integrated bottlers carry plants and fleets. Alcohol adds excise taxes and on-premise cycles.
  - *Household & personal care:* category-leadership economics, heavy advertising, innovation cadence,
    premiumization; large emerging-market exposure.
  - *Packaged food:* low volume growth, private-label pressure, input-cost volatility, frequent portfolio
    reshaping via M&A and divestitures.
  - *Tobacco/nicotine:* secular volume decline offset by pricing; regulatory and litigation risk;
    reduced-risk product transition; very high cash conversion.
  - *Staples retail:* thin margins, high turns, scale purchasing, membership fees, private label; returns
    driven by asset turnover rather than margin.

## 2. KPIs that matter

Agents quote KPIs from the run's data files or cited sources; they do not compute new ratios in prose.
A KPI that cannot be sourced is `null` and listed in `missing_data`.

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Organic growth: price/mix vs volume | Separates pricing power from demand; volume is the long-run health signal | Earnings release sales bridge; 10-K Item 7 MD&A | Healthy: balanced growth with volume flat-to-positive. Warning (heuristic): price-led growth with negative volume for several consecutive quarters |
| Price elasticity | Shows how much pricing the consumer will absorb | Management commentary; MD&A; public scanner-data summaries | Warning: volume declines accelerating after successive price increases; "elasticities above historical norms" language |
| Gross margin vs input costs | Commodity, packaging, freight and conversion costs hit before pricing | Income statement; MD&A margin bridge; hedging disclosures (Item 7A) | Healthy: margin recovering as pricing and productivity catch up. Warning: margin falling while pricing is still positive (pricing exhausted) |
| Advertising & promotion (A&P) | Brand reinvestment protects share and pricing power | `AdvertisingExpense` tag / accounting-policy note; MD&A | Warning: A&P cut as a share of sales to hit EPS targets, followed by share loss |
| Market share & private-label share | Brand health in measured channels | Company commentary citing scanner data; public industry summaries (private-label trade associations, retailer disclosures) | Warning: share losses across most categories; private-label gaining in the company's core categories |
| Emerging-market exposure & FX | Growth engine and currency risk | Segment/geographic note (Item 8); MD&A FX impact; Item 7A | Warning: reported growth far below organic for years; hyperinflation accounting; currency-driven pricing |
| Productivity savings | Funds reinvestment and offsets inflation | Investor-day targets; MD&A restructuring discussion | Warning: savings program renewed every few years with recurring restructuring charges but no margin progress |
| Cash conversion & working capital | Earnings quality; supplier-finance programs can flatter OCF | Company-reported conversion (release, MD&A; cite it); otherwise compare `fy[].fcf` with `fy[].net_income` qualitatively — no script outputs the ratio, so never quote a computed percentage; supplier-finance note | Heuristic: staples typically generate FCF close to net income; FCF below net income in most `fy[]` years, or reliance on extended payables, is a warning |
| Dividend payout & leverage | Total-return model depends on a covered dividend | Cash-flow statement (dividends, buybacks); `valuation.net_debt_ebitda` | Warning: dividends plus buybacks exceeding FCF for years, funded by debt; leverage rising after M&A |
| Staples retail metrics | Retail economics differ from brand owners | Earnings release (comps, traffic, ticket, membership fee income and renewal rate, e-commerce growth); 10-K Item 2 | Warning: traffic falling, renewal rates slipping, shrink rising, gross margin funded by membership-fee increases alone |
| ROIC | Brand moat should earn high returns | `valuation.roic_pct` (distorted by goodwill and buyback-shrunk equity) | Warning: ROIC declining after acquisitions; big impairments signal overpayment |

## 3. Valuation methods

- **What fits:** DCF works well — cash flows are stable and capex is moderate. EV/EBIT and P/E against
  the company's own history are useful cross-checks. The reverse DCF frames what growth is priced in
  relative to the company's stated long-run "algorithm" (cite it from investor materials).
- **Reading the `valuation_<T>.json` grid:**
  - `dcf_nopat` (course NOPAT = (EBITDA − capex) × (1 − tax), TTM) and `dcf_fcf` usually agree here;
    a large gap signals a working-capital or supplier-finance effect worth explaining.
  - Growth tiers: mature brand owners rarely justify more than the `g03`/`g06` cells as base; `g00` is a
    sensible low case for volume-declining categories (tobacco, some packaged food). `g10`/`g15` need
    specific evidence (emerging-market or category growth, cited).
  - `pe` and `ev_ebit` cells use `valuation.multiple_bands.consumer_staples` (source
    `D:CFG:valuation.multiple_bands`) — configured assumptions, labeled as such.
  - `cells_base` must span DCF and multiple families; `fair_value_*` = mean of cited cells. Read each
    cell's `tv_share` rather than relying on the `tv_gt_90` flag: under the configured 5-year
    exit-multiple grid terminal value is most of EV in every cell and the flag rarely if ever fires.
    A higher `tv_share` comes from a higher growth tier — check that tier is justified.
  - Reverse DCF `implied_growth_pct` above the company's own long-run organic growth range means the
    price already assumes outperformance.
- **What misleads:**
  - **Bond-proxy anchoring:** a premium multiple "because it is safe" ignores duration risk; rising
    yields de-rate staples even with unchanged fundamentals.
  - **Equity-method stakes** (e.g. holdings in bottlers or affiliates): their earnings sit below EBIT,
    while `ev` in the fundamentals file does not subtract the stakes, so EV/EBIT overstates the multiple.
    Note the stakes' carrying value from the balance sheet qualitatively.
  - **Book-based metrics:** goodwill and trademarks from decades of M&A make tangible book negative and
    P/B meaningless; buybacks can make reported equity tiny or negative, inflating `roe_pct`/`roic_pct`.
  - **Excise-tax presentation:** tobacco and alcohol revenue can be shown gross or net of excise; margins
    and EV/Sales differ widely depending on the tag used (see Data notes).
  - **Adjusted EPS** that excludes restructuring every year, or currency-neutral figures presented as if
    the currency loss were not real.

## 4. Macro sensitivities

Direction = effect on sector earnings or relative valuation when the series **rises**. Agents cite
current readings from `phase1/macro.json` (source `D:FRED:<ID>`); the table lists transmission channels.
Staples are defensive: several effects are *relative* to the market, as noted.

| Series | Direction | Mechanism |
|---|---|---|
| DFF | negative | Raises commercial-paper and floating funding costs; cash yields compete with dividend yields |
| DGS2 | negative | Reprices short-term funding and the cash alternative to dividend income |
| DGS10 | negative | Bond-proxy valuation: higher long yields compress multiples of low-growth, high-payout names |
| T10Y2Y | mixed | Relative: growth-led (bear) steepening rotates money to cyclicals; steepening driven by rate cuts (bull steepening) supports bond-proxy defensives; flat or inverted curves favour defensives |
| CPIAUCSL | mixed | Food inflation lifts nominal sales via pricing, but input costs rise first and volume elasticity lags |
| CPILFESL | mixed | Wage and freight inflation raises conversion costs; sticky core keeps yields high |
| UNRATE | mixed | Demand is resilient, but rising unemployment accelerates trade-down — bad for premium brands, good for discounters and clubs |
| PAYEMS | mixed | Low sensitivity; strong payrolls support premiumization and away-from-home consumption |
| INDPRO | mixed | Weak link; mainly a signal of packaging and logistics cost pressure |
| DTWEXBGS | negative | Large international sales: strong dollar cuts translated revenue and EPS; EM devaluations force pricing that hurts volume |
| DCOILWTICO | negative | Drives resin packaging, freight, fertilizer and agricultural inputs, with a hedging lag |
| BAMLH0A0HYM2 | mixed | Relative: risk-off spread widening favours defensives; levered staples still refinance at higher cost |
| VIXCLS | positive | Relative: defensive rotation in volatility spikes; absolute returns still fall in broad sell-offs |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  agricultural and soft commodities (grains, oilseeds, sugar, cocoa, coffee, dairy, cattle — USDA, CME,
  ICE or World Bank data), packaging inputs (resins, aluminum, pulp and paperboard), food-at-home vs
  away-from-home CPI (BLS) — `DCOILWTICO` only proxies these — plus food-store retail sales (Census) and
  consumer sentiment (University of Michigan) for trade-down signals.

## 5. Typical failure modes

- **Overpaying for safety:** high multiples on low growth de-rate sharply when bond yields rise; the
  business does fine while the stock does not.
- **Pricing without volume:** several years of price-led growth hide volume erosion; once elasticity
  bites and private label closes the gap, organic growth stalls.
- **Starving the brands:** aggressive cost-cutting that slashes marketing and innovation lifts margins
  for a few years, then shows up as share loss, trademark impairments and dividend pressure.
- **Leveraged mega-mergers:** synergy-driven deals load the balance sheet; later impairments and
  dividend cuts follow when categories underperform.
- **EM currency crises:** devaluations and hyperinflation erase reported growth and force pricing
  that damages volume.
- **Category disruption:** insurgent and direct-to-consumer brands, health and wellness shifts
  (including appetite-suppressing medications), and channel migration to clubs, discounters and online.
- **Retailer power:** consolidated retailers push private label, demand trade spend and delist slow SKUs.
- **Regulation and litigation:** flavor bans, nicotine limits, sugar taxes, labeling rules and
  product-liability cases can impair whole categories.

## 6. Red flags for the Devil's Advocate

- Organic growth almost entirely price/mix with volume negative; management blaming "temporary" elasticity.
- Advertising and promotion cut as a share of sales while EPS beats.
- Gross margin falling despite positive pricing (input costs or mix deteriorating faster than pricing).
- Market-share losses in core categories; private label gaining; retailer delistings.
- Dividend plus buybacks exceeding FCF for several years; leverage creeping up; supplier-finance
  balances growing (hidden working-capital support).
- Recurring "one-time" restructuring; serial trademark or goodwill impairments.
- Reported growth persistently below organic growth because of FX, with no plan beyond pricing.
- Base case assuming `g06`+ growth for a volume-declining category, or a premium multiple justified only
  by "defensiveness".
- OSINT: recalls (FDA/USDA), contamination incidents, ingredient controversies, boycott campaigns.

## 7. Lens checklist

- **fundamental**
  - Decompose organic growth into price/mix and volume (cited); compare with gross-margin trend.
  - Check FCF against net income across `fy[]` (qualitatively, or via company-reported conversion) and
    supplier-finance programs; note equity-method stakes and excise presentation.
  - Address goodwill/trademark intensity and negative tangible book in `balance_sheet_notes`.
  - Frame reverse DCF implied growth against the company's stated long-run algorithm.
- **technical**
  - Staples are low-beta: `atr14_pct` is usually modest and `range` regimes are common; a `trend_down`
    regime with weak `vs_spy` often reflects rate-driven de-rating rather than company news.
  - High dividend yields mean adjusted-price levels differ from raw charts; use computed levels only.
- **operator_ceo**
  - Reinvestment discipline: A&P and innovation vs margin targets; portfolio pruning and M&A returns.
  - Incentives in the DEF 14A: organic growth, FCF and ROIC vs adjusted EPS alone.
  - CEO tenure and succession; activist involvement; guidance track record on organic growth and margins.
- **industry_ops**
  - Input-cost basket and hedging horizon; co-manufacturing and plant utilization; SKU complexity.
  - Route to market (direct-store delivery vs warehouse), retailer relationships, e-commerce economics.
  - Emerging-market supply chain and local manufacturing that offsets FX transaction risk.
- **osint**
  - FDA and USDA FSIS recalls, contamination reports, ingredient and packaging controversies.
  - Consumer reviews and social sentiment on reformulations and price increases ("shrinkflation").
  - Boycotts, retailer shelf-space disputes, employee reviews and hiring trends.

## 8. Data notes

- **Revenue tag choice matters:** candidates are `Revenues`,
  `RevenueFromContractWithCustomerExcludingAssessedTax` and `...IncludingAssessedTax` (then legacy tags);
  TTM takes the tag with the latest period end (priority breaks ties); FY history fills each year from the
  chosen tag first, then the others in priority order. Tobacco and alcohol companies may report gross
  (incl. excise) and net figures, so history can mix them. Check `tag_map.revenue.tag`,
  `tag_map.revenue.stitched`, `tag_mix` and `ExciseAndSalesTaxes` before comparing growth or EV/Sales.
- **Non-GAAP KPIs:** organic growth, price/mix, volume, market share and A&P ratios are in earnings
  releases and MD&A, not in standard tags; cite them or record them as `missing_data`. Scanner-data
  sources (NielsenIQ, Circana) are paid — cite only public summaries or company quotations of them.
- **Fiscal calendars:** several food and retail companies use 52/53-week years; some grocers and
  distributors report a 16-week first quarter that falls outside the quarter bucket
  (`fundamentals.duration_days.Q`); check `ttm.*.derivation` and as-of dates for a lagging TTM.
- **Equity-method income** (`IncomeLossFromEquityMethodInvestments`) is not in operating income; it
  inflates P/E-based value relative to EV/EBIT-based value.
- **Supplier finance programs** (`SupplierFinanceProgramObligation`, disclosed since recent ASU changes)
  extend payables and support OCF; earlier years lack the tag.
- **FX:** companyfacts reports USD consolidated figures only; currency-neutral growth comes from releases.
  Hyperinflationary-economy accounting can distort segment margins.
- **Negative or tiny equity** from buybacks: `p_b`/`roe_pct` are null (`negative_denominator`) when equity
  ≤ 0 (meaningless when tiny), `roic_pct` when equity + `net_debt` ≤ 0 — read the `na_reason` first.
- **Technicals use dividend-adjusted prices** (`auto_adjust=True`); older levels sit below raw-chart levels.
