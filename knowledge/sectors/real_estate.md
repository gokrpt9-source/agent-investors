---
sector: real_estate
name: Real Estate
benchmark_etf: XLRE
sector_class: default
key_kpis:
  - "FFO and AFFO per share (NAREIT FFO; company-defined AFFO) and their growth"
  - "AFFO payout ratio (dividend / AFFO)"
  - "Same-store NOI growth"
  - "Occupancy (leased vs commenced; physical vs economic)"
  - "Re-leasing spreads (cash and GAAP) and mark-to-market of in-place rents"
  - "Weighted-average lease term and lease-expiry schedule"
  - "Tenant concentration and tenant credit quality"
  - "Implied cap rate vs private-market cap rates vs the 10-year Treasury (DGS10)"
  - "Leverage: net debt / EBITDAre, secured-debt share, fixed-charge coverage"
  - "Debt maturity ladder, fixed vs floating mix, weighted-average cost of debt"
  - "Development pipeline size, pre-leasing and yield on cost"
  - "Recurring capex, tenant improvements and leasing commissions"
valuation_methods:
  - "P/FFO and P/AFFO with FFO/AFFO per share taken from the REIT's own filings (not EDGAR tags)"
  - "NAV and implied cap rate (sourced NOI and cap-rate evidence only)"
  - "DCF on FCF (operating cash flow less capex) as the grid's least-distorted cell, cross-checked to AFFO"
  - "Dividend yield vs AFFO growth and the spread to DGS10 (qualitative)"
  - "Grid P/E and EV/EBIT treated as distorted by real-estate depreciation and gains on sale"
macro_sensitivities:
  - series: DGS10
    direction: negative
    mechanism: "Cap rates and REIT multiples track long yields; higher yields raise the cost of capital and compress the cap-rate spread"
  - series: DFF
    direction: negative
    mechanism: "Raises the cost of floating-rate debt, credit lines and construction loans"
  - series: BAMLH0A0HYM2
    direction: negative
    mechanism: "Wider credit spreads raise unsecured borrowing costs and freeze transaction and financing markets"
  - series: CPIAUCSL
    direction: mixed
    mechanism: "Short-lease property types reprice with inflation and new supply gets costlier; long fixed-rent leases lose real value"
  - series: PAYEMS
    direction: positive
    mechanism: "Job growth drives office, apartment and retail absorption"
  - series: UNRATE
    direction: negative
    mechanism: "Rising unemployment weakens tenant demand, rent growth and collections"
  - series: T10Y2Y
    direction: mixed
    mechanism: "Bull steepening (short rates falling) helps floating-rate borrowers; bear steepening (long rates rising) hurts valuations"
failure_modes:
  - "Leverage and a maturity wall meeting higher rates: dilutive equity, forced asset sales, dividend cuts"
  - "Secular demand decline (office, enclosed retail) masquerading as a cheap P/FFO"
  - "Tenant or operator concentration and bankruptcy"
  - "New-supply waves depressing rents in fast-growing markets"
  - "External-management fee conflicts and growth for its own sake"
  - "Adjusted-FFO creep hiding recurring costs"
  - "Unhedged floating-rate debt"
edgar_tags_of_interest:
  - RealEstateInvestmentPropertyNet
  - RealEstateInvestmentPropertyAccumulatedDepreciation
  - OperatingLeaseLeaseIncome
  - OperatingLeasesIncomeStatementLeaseRevenue
  - GainsLossesOnSalesOfInvestmentRealEstate
  - ImpairmentOfRealEstate
  - PaymentsToAcquireRealEstate
  - PaymentsToDevelopRealEstateAssets
  - PaymentsForCapitalImprovements
  - SecuredDebt
  - UnsecuredDebt
  - LineOfCredit
  - InterestExpense
  - MinorityInterest
  - PaymentsOfDividendsCommonStock
---

# Real Estate — Sector Playbook

> Shared knowledge for every lens covering a real-estate name. Rules of thumb below are labeled
> **heuristic** — judgment aids, not thresholds the scripts apply. No market figures appear here:
> every number in a report comes from the run's data files or a cited source.

## 1. Scope & benchmark

- **GICS industries covered:** Equity REITs (diversified, industrial, hotel & resort, office,
  health care, residential, retail, and specialized REITs — data centers, towers, self-storage,
  timber, others) and Real Estate Management & Development (operating companies, developers,
  services firms). **Mortgage REITs are GICS Financials**, not Real Estate.
- **Benchmark ETF:** XLRE (`committee.yaml: sector_etfs.real_estate`). Specialized REITs (towers,
  data centers) can carry a large weight, so XLRE behaves differently from a "property" index.
- **Yahoo mapping:** `info["sector"] == "Real Estate"` → `real_estate`; fund sector key `realestate`.
  Yahoo labels **mortgage REITs** "Real Estate" too, and SIC 6798 (all REITs) maps to `real_estate`
  with `sector_class: default`. A mortgage REIT is a levered lender: analyze it with the financials
  lens (book value, spread, leverage, credit), state the mismatch in `missing_data`/notes, and treat
  EV-based cells as not meaningful. Use `sector_override` in `portfolio.yaml` when the user agrees.
- **Sub-industry differences that change the analysis:**
  - *Short-lease types* (apartments, self-storage, hotels): rents reset quickly — fast inflation pass-through, fast pain in downturns.
  - *Long-lease types* (net lease, some industrial and health-care): bond-like, rate-sensitive; tenant credit and escalators matter most.
  - *Office and enclosed retail:* secular demand questions; capex (TIs, concessions) eats NOI.
  - *Health care:* operator health (senior housing, skilled nursing) and reimbursement exposure.
  - *Towers and data centers:* infrastructure-like growth, customer concentration, power availability.
  - *Developers / operating companies:* taxable, no distribution requirement, valued on NAV and land bank.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs (heuristics) |
|---|---|---|---|
| FFO / AFFO per share and growth | The sector's earnings measure; GAAP EPS is distorted by depreciation | Supplemental package / earnings release (8-K Ex. 99); 10-K Item 7 non-GAAP reconciliation | Healthy: steady per-share growth on a consistent definition. Warning: growth only in "core/normalized" FFO |
| AFFO payout ratio | Dividend safety after recurring capex | Supplemental; dividend history | Heuristic: comfortable with clear headroom below 100%; at or above 100% means the dividend relies on debt or asset sales |
| Same-store NOI growth | Organic growth excluding acquisitions | Supplemental same-store schedule | Healthy: positive and above inflation in short-lease types. Warning: negative, or the same-store pool redefined |
| Occupancy (leased vs commenced) | Utilization of the asset base | Supplemental; 10-K Item 2 Properties | Warning: occupancy held only through free rent or rising concessions; leased-not-commenced gap persisting |
| Re-leasing spreads; in-place rent mark-to-market | Pricing power on expiring leases | Supplemental leasing tables; earnings call | Healthy: positive cash spreads. Warning: negative cash spreads hidden behind GAAP (straight-line) spreads |
| WALT and expiry schedule | Revenue visibility and rollover risk | 10-K Item 2; supplemental expiry table | Warning: large expiries concentrated in one year or one tenant |
| Tenant concentration & credit | Single-tenant failures can impair FFO and NAV | Supplemental top-tenant table; Item 1A | Warning: top tenants with weak credit, operator rent deferrals, rising bad debt |
| Implied cap rate vs private cap rates vs DGS10 | Public vs private pricing; spread to the risk-free rate | Industry surveys and transaction reports (often paywalled); NAREIT data; `macro.json` DGS10 | Heuristic: judge the spread against its own history; a spread compressed versus history is a valuation risk |
| Leverage (net debt / EBITDAre, secured %) | Refinancing and covenant risk | Supplemental debt summary; 10-K debt note | Warning: leverage rising while asset values fall; secured debt growing (encumbered pool) |
| Maturity ladder, floating share, cost of debt | Rate-reset risk on refinancing | 10-K debt note; Item 7A interest-rate sensitivity | Warning: large near-term maturities vs liquidity; unhedged floating debt; cost of debt rising toward the cap rate |
| Development pipeline, pre-leasing, yield on cost | Value creation vs speculative risk | Supplemental development schedule | Warning: pipeline large relative to the company, low pre-leasing, yield on cost near market cap rates |
| Recurring capex, TIs, leasing commissions | The gap between FFO and AFFO | Supplemental AFFO reconciliation | Warning: recurring capex rising as a share of NOI; items reclassified as "non-recurring" |

## 3. Valuation methods

**What fits**
- **P/FFO and P/AFFO, with FFO/AFFO from filings.** FFO (NAREIT definition: net income excluding gains
  and losses on sales of depreciable real estate and real-estate impairments, plus real-estate
  depreciation and amortization, adjusted for joint ventures) is non-GAAP and **not** a us-gaap tag,
  and AFFO has no standard definition. Read both per share from the supplemental package or earnings
  release and cite them (W-sources). The valuation grid has no P/FFO cell. Do not compute P/FFO or
  implied cap rates in prose: cite a figure only if a source publishes it; otherwise record the inputs
  as key figures, describe the comparison qualitatively, and list the ratio in `missing_data`.
- **NAV / implied cap rate** is the industry's anchor (public price vs private-market value). Use only
  sourced NAV estimates or sourced cap-rate evidence; consensus NAV is usually paywalled → null.
- **DCF on FCF** (grid `dcf_fcf` cells): operating cash flow less capex is the closest grid proxy to
  AFFO-before-growth-capex — **if** the capex tag captured recurring property capex (see §8).

**What misleads**
- **P/E** (grid `pe` cells): GAAP net income is depressed by real-estate depreciation and inflated by
  gains on property sales. The configured `real_estate` P/E band is set high to absorb the depreciation
  drag; it is an assumption, not a market multiple. If TTM net income includes large gains on sale
  (`GainsLossesOnSalesOfInvestmentRealEstate`), the P/E cells overstate value.
- **EV/EBIT**: EBIT is after real-estate depreciation, so EV/EBIT looks expensive, and EV misses debt
  the formula did not capture (secured mortgages) and operating-partnership units (§8).
- **Course NOPAT** `(EBITDA − capex) × (1 − tax)`: if `tax_rate_used` carries the `default_tax_rate` flag
  (REITs often lack a usable income-tax line), course NOPAT includes an artificial tax haircut and
  understates distributable cash; otherwise the REIT's own near-zero rate is used. Acquisitions are not
  "capex" and may not be in the capex tag.

**Reading the grid** — `cells_base` must span two method families when both exist: pair a `dcf_fcf`
cell with the least-distorted multiple cell and explain the distortion in `method`. Read
`reverse_dcf.implied_growth_pct` against sourced same-store NOI growth, not against revenue growth
from acquisitions. EV-based metrics are **not meaningful for mortgage REITs**.

## 4. Macro sensitivities

| FRED id | Direction | Mechanism |
|---|---|---|
| DGS10 | negative | Cap rates and REIT multiples track long yields; higher yields compress the cap-rate spread |
| DFF | negative | Cost of floating-rate debt, credit lines and construction loans |
| BAMLH0A0HYM2 | negative | Wider spreads raise unsecured borrowing costs and freeze transaction markets |
| CPIAUCSL | mixed | Short-lease types reprice with inflation and new supply gets costlier; long fixed leases lose real value |
| PAYEMS | positive | Job growth drives office, apartment and retail absorption |
| UNRATE | negative | Weaker tenant demand, rent growth and collections |
| T10Y2Y | mixed | Bull steepening helps floating-rate borrowers; bear steepening hurts valuations |

- The **spread between cap rates and DGS10** is the sector's core macro gauge. DGS10 comes from
  `macro.json` (cite `D:FRED:DGS10` with `data_ref`); cap rates must come from a cited survey or
  filing. Without a sourced cap rate, discuss the rate direction only.
- Rate *changes* (`chg_3m`, `chg_12m`) move REIT prices faster than fundamentals; distinguish
  multiple compression (rates) from NOI deterioration (demand) in the thesis.
- Credit availability matters as much as the rate level — watch high-yield spreads alongside
  lender pullbacks and CMBS stress reported in the news (cited).

## 5. Typical failure modes

1. **Maturity wall meets higher rates.** Cheap debt refinances at much higher cost; FFO falls, then
   equity is raised below NAV or the dividend is cut.
2. **Secular decline priced as "cheap".** Office after the shift to hybrid work and enclosed malls
   after e-commerce traded at low P/FFO for years while NAV kept falling — a classic value trap.
3. **Tenant or operator failure.** Retail bankruptcies and health-care operator distress turn
   occupancy and rent coverage overnight.
4. **Supply waves.** Fast-growing markets attract development; deliveries arrive together and
   flatten rents for several years.
5. **External management conflicts.** Fees tied to assets under management reward growth over
   per-share value.
6. **FFO-adjustment creep.** "Core", "normalized" and "adjusted" FFO add back costs that recur.
7. **Floating-rate exposure.** Unhedged or short-hedged floating debt reprices before rents do.

## 6. Red flags for the Devil's Advocate

- AFFO payout near or above 100%; dividend funded by asset sales or new debt.
- Frequent changes to the FFO/AFFO definition or the same-store pool; growing "non-recurring" add-backs.
- Occupancy stable but cash re-leasing spreads negative, concessions and TIs rising.
- Near-term maturities large relative to cash plus undrawn credit lines; covenant headroom shrinking.
- Equity issued below sourced NAV estimates; asset sales at cap rates far above where NAV marks them.
- Top-tenant credit deterioration, rent deferrals, operator restructurings.
- Rising secured debt share (encumbered pool) or reliance on a single lender or market.
- External manager, related-party transactions, or fee structures rewarding size.
- Impairments in the 10-K that contradict a stable NAV narrative.
- Thesis leaning on falling rates while the company has little floating-rate debt to benefit.

## 7. Lens checklist

- **fundamental**
  - Cite FFO/AFFO per share from the supplemental package; never derive them from EDGAR tags.
  - Explain why the chosen grid cells are least distorted (depreciation, gains on sale, tax, capex capture).
  - In `balance_sheet_notes`: maturities, floating share, secured share, OP units, liquidity.
  - Record P/FFO, NAV or implied cap rate only when a source publishes them; else null + `missing_data`.
- **technical**
  - Use only `technicals_<T>.json`: regime, levels, RS vs XLRE and SPY, stop candidates.
  - REITs often trade as a rate-sensitive group — RS vs XLRE separates stock-specific moves from rate moves.
  - Technicals use dividend-adjusted prices (`auto_adjust=True`), so the series has no ex-dividend
    gaps; for high-yield REITs older levels sit below raw-chart prices. Use only the computed levels
    and `stop_candidates`, and treat a break of them as real.
- **operator_ceo**
  - Capital allocation: buybacks below NAV vs issuance below NAV; disposals vs acquisitions discipline.
  - DEF 14A: pay tied to per-share FFO/AFFO and total return vs pay tied to asset growth; external manager terms.
  - Guidance track record on FFO, same-store NOI and occupancy; insider Form 4 trades.
- **industry_ops**
  - Market-level supply pipeline vs absorption; rent growth by market; property quality vs competing stock.
  - Lease structure (net vs gross, escalators, expense recovery); recurring capex intensity.
  - Development execution: cost overruns, delays, pre-leasing; operator quality for health care and hotels.
- **osint**
  - Tenant bankruptcies, store closures, operator distress, rent-collection news.
  - Local supply announcements, zoning/rent-control legislation, insurance-cost shocks.
  - Lender pullbacks, CMBS delinquencies in the property type, debt-market headlines (cited).
  - Raise `must_address` flags on refinancing stress or major-tenant risk.

## 8. Data notes

- **FFO/AFFO are not in companyfacts.** They are non-GAAP; some REITs use company-extension tags that
  the script does not read. Read the supplemental package or earnings release (8-K exhibit) instead.
- **Revenue tags:** REITs often report under `OperatingLeaseLeaseIncome` / lease-revenue tags or plain
  `Revenues`; check `tag_map` and the income statement if revenue looks partial (only one revenue line).
- **Capex capture:** acquisitions and development are usually tagged `PaymentsToAcquireRealEstate`,
  `PaymentsToDevelopRealEstateAssets` or `PaymentsForCapitalImprovements`, not the PP&E capex tags the
  script reads. Capex can therefore be null or understated → FCF and course NOPAT overstated. Verify in
  the cash-flow statement before citing `dcf_fcf` / `dcf_nopat` cells.
- **Debt capture:** mortgages and unsecured notes may sit under `SecuredDebt`, `UnsecuredDebt`,
  `NotesPayable` or `LineOfCredit`, which the debt formulas may not use → `total_debt`, net debt and EV
  understated. Cross-check with the debt note and the supplemental maturity schedule.
- **OP units (UPREIT structure):** operating-partnership units are economically equivalent to shares but
  sit in noncontrolling interests (`MinorityInterest`). The share count — and market cap — can exclude
  them, while reported FFO per share uses shares plus units. Flag the mismatch rather than mixing bases.
- **Tax rate:** REIT income tax is near zero; if `tax_rate_used` shows the default-rate flag, NOPAT cells
  carry an artificial tax haircut.
- **Gains and impairments** swing GAAP net income; P/E and ROE from `fundamentals.json` inherit the noise.
- **Mortgage REITs:** SIC 6798 gives `sector_class: default`, so the script computes EV, EV/EBIT and gross
  margin that are not meaningful for a lender; treat them as such and use book-value based reasoning.
- **Fund data:** `yf_funds("XLRE")` returns the top 10 holdings only; mid-cap REITs enter the Phase 3
  universe only through `universe.yaml` additions.
