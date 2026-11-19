---
sector: information_technology
name: Information Technology
benchmark_etf: XLK
sector_class: default
key_kpis:
  - Organic revenue growth (constant currency, ex M&A)
  - Net revenue retention (NRR) and gross revenue retention (GRR)
  - Rule of 40 (revenue growth plus FCF margin, read side by side)
  - Remaining performance obligations (RPO) and current RPO growth
  - Billings and deferred revenue trend
  - Gross margin and its mix drivers (subscription, services, hosting)
  - Stock-based compensation as a share of revenue and net share dilution
  - FCF margin after stock-based compensation
  - Semiconductor inventory days (DIO) and channel inventory
  - Book-to-bill, backlog and lead times (semis, equipment, hardware)
  - Capex intensity (capex relative to revenue)
  - R&D intensity and customer concentration
valuation_methods:
  - Reverse DCF implied growth versus delivered and end-market growth (start here)
  - DCF on course NOPAT (dcf_nopat) as the primary DCF, SBC already treated as a cost
  - DCF on FCF (dcf_fcf) only with an explicit SBC and capitalized-software caveat
  - P/E on GAAP earnings (pe band) for profitable software, hardware and semis
  - EV/EBIT (ev_ebit band) for mature hardware, semis and IT services
  - Mid-cycle normalization of margins for semis, memory and hardware
macro_sensitivities:
  - {series: DGS10, direction: negative, mechanism: "Higher long yields raise discount rates and compress multiples on long-duration growth cash flows."}
  - {series: DFF, direction: mixed, mechanism: "Tighter policy hurts unprofitable growth and risk appetite, but cash-rich mega-caps earn more interest income."}
  - {series: T10Y2Y, direction: mixed, mechanism: "Steepening on growth hopes helps cyclical semis; steepening driven by rising long yields hurts long-duration software."}
  - {series: CPILFESL, direction: negative, mechanism: "Sticky core inflation keeps rates high (multiple pressure) and lifts engineering wage costs."}
  - {series: INDPRO, direction: positive, mechanism: "Industrial and auto activity drives analog, MCU and equipment demand and the semis inventory cycle."}
  - {series: PAYEMS, direction: positive, mechanism: "Employment growth supports seat-based software expansion and enterprise IT budgets."}
  - {series: UNRATE, direction: negative, mechanism: "Rising unemployment shrinks seats, lengthens sales cycles and raises churn in SMB-heavy software."}
  - {series: DTWEXBGS, direction: negative, mechanism: "A large share of sector revenue is earned abroad, so a stronger dollar reduces reported revenue and EPS."}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Wider credit spreads signal risk-off, close funding for unprofitable tech and remove the private-equity bid for software."}
  - {series: VIXCLS, direction: negative, mechanism: "High-beta, high-multiple names de-rate fastest when volatility spikes."}
failure_modes:
  - Peak-cycle value trap in semis, memory and hardware (low P/E on peak earnings)
  - Duration de-rating of high-multiple software when rates rise
  - Retention decay masked by price increases or acquisitions
  - SBC-funded growth with persistent dilution
  - Platform shift that strands a profitable incumbent (secular decliner looks cheap)
  - Customer concentration and insourcing by large customers
  - Export controls and supply-chain geography shocks
  - Double-ordering in shortages followed by cancellations
edgar_tags_of_interest:
  - RevenueFromContractWithCustomerExcludingAssessedTax
  - RevenueRemainingPerformanceObligation
  - ContractWithCustomerLiabilityCurrent
  - ContractWithCustomerLiabilityNoncurrent
  - IncreaseDecreaseInContractWithCustomerLiability
  - ResearchAndDevelopmentExpense
  - ShareBasedCompensation
  - AllocatedShareBasedCompensationExpense
  - PaymentsToDevelopSoftware
  - CapitalizedComputerSoftwareAdditions
  - PaymentsForRepurchaseOfCommonStock
  - WeightedAverageNumberOfDilutedSharesOutstanding
  - InventoryNet
  - AmortizationOfIntangibleAssets
  - FinanceLeasePrincipalPayments
  - MarketableSecuritiesNoncurrent
---

# Information Technology — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **GICS industries covered:** Software, IT Services (incl. internet services and infrastructure),
  Communications Equipment, Technology Hardware, Storage & Peripherals, Electronic Equipment,
  Instruments & Components, and Semiconductors & Semiconductor Equipment.
- **Not here:** payment networks and processors (Financials since the 2023 GICS revision); search,
  social and gaming (Communication Services); e-commerce (Consumer Discretionary).
- **Benchmark:** XLK (`sector_etfs.information_technology` in `config/committee.yaml`). Cap-weighted
  and dominated by a few mega-caps, so "relative strength vs sector" largely measures a name against
  those leaders; if XLK is held, read its top-10 concentration from `lookthrough.json`.
- **Classification:** `etf_lookthrough.classify_security` applies `sector_override` → `etf_overrides.yaml`
  → Yahoo `info.sector` ("Technology" → `information_technology`) → SIC fallback. Yahoo is not GICS:
  check edge cases (solar equipment, test and measurement, contract manufacturers, fintech) against
  XLK membership or company disclosure, and use `sector_override` when they disagree.
- **Sub-industry differences:**
  - *Software / SaaS* — recurring revenue, high gross margin, heavy SBC; value rests on retention and growth duration.
  - *Semiconductors and equipment* — inventory and capacity cycles; fabless, IDM and foundry models
    differ sharply in capex and margins (compare within model); equipment orders are lumpy,
    concentrated in a few customers and exposed to export controls.
  - *Hardware & storage* — lower gross margin, component-cost exposure; per-share growth often from buybacks.
  - *IT services* — a labor model: utilization, wage inflation, attrition, pricing, deal bookings.
  - *Communications equipment* — follows carrier and cloud capex cycles and customer digestion.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Net and gross revenue retention (NRR, GRR) | Expansion inside the installed base is the cheapest growth; GRR sets the floor under recurring revenue | Earnings release, investor day, 10-K Item 7 when disclosed (definitions vary by company) | Healthy: NRR well above 100% and stable (*heuristic*), GRR steady through a slowdown. Warning: NRR sliding toward 100%, SMB churn rising, definition changed or disclosure dropped |
| Rule of 40 | Balances growth against cash profitability | Read `growth_pct.revenue_yoy_ttm` and `margins_pct.fcf` side by side; cite a company-disclosed figure if given | Healthy: sum at or above 40 (*heuristic*). Warning: growth bought with deeply negative FCF. Do not report your own sum as a key figure (no data_ref) |
| RPO and current RPO | Contracted forward revenue; best visibility metric for subscription models | 10-K/10-Q revenue note (`RevenueRemainingPerformanceObligation`); cRPO usually in the release | Healthy: cRPO growing at least as fast as revenue. Warning: RPO growth driven only by longer contract duration |
| Billings / deferred revenue | Leading indicator of revenue; reveals pull-forward | Balance sheet contract liabilities; cash-flow change in deferred revenue | Warning: billings decelerating faster than revenue for consecutive quarters |
| Gross margin | Pricing power and mix (license vs hosting vs services) | `margins_pct.gross`; 10-K Item 7 | Warning: decline from cloud hosting costs or services mix without a stated path back |
| SBC and dilution | SBC is a real cost; dilution transfers value from holders to staff | Cash-flow statement (`ShareBasedCompensation`), diluted share count, equity note | Warning: SBC a high-teens share of revenue or more (*heuristic*), or share count rising despite buybacks |
| FCF margin after SBC | True owner cash generation of a software model | `margins_pct.fcf` plus the SBC line from the 10-K (read, don't net yourself) | Warning: FCF positive only because SBC is added back |
| Inventory days (semis, hardware) | Classic cycle signal; excess inventory precedes price cuts and write-downs | 10-Q balance sheet (`InventoryNet`) vs cost of revenue trend; management comments on channel inventory | Warning: inventory rising while revenue falls; "digestion" or "normalization" language |
| Book-to-bill, backlog, lead times | Order momentum ahead of revenue | Earnings release; equipment backlog in 10-K Item 7 | Warning: book-to-bill below 1 for consecutive quarters (*heuristic*); backlog cancellations |
| Capex intensity | Capital needs and overbuild risk | `ttm.capex` vs `ttm.revenue`; 10-K liquidity section; capex guidance | Warning: rising capex intensity without contracted demand; growth capex labeled maintenance |
| R&D intensity | Product competitiveness, node and roadmap leadership | Income statement; 10-K Item 1 | Warning: R&D cut to defend margins in a share-losing market |
| Customer concentration | Single-customer bargaining power and insourcing risk | 10-K notes and Item 1A (customers above 10% of revenue) | Warning: largest customers growing as a share while designing their own products |
| Geographic / export exposure | Policy risk can remove addressable markets overnight | 10-K revenue by geography; Item 1A | Warning: rising share of revenue in restricted regions |
| IT services metrics | Utilization, attrition, pricing and bookings drive margins | Earnings release, investor materials | Warning: book-to-bill weakening alongside rising attrition or wage pressure |

## 3. Valuation methods

**What `scripts/valuation.py` provides:** DCF cells (`dcf_nopat`, `dcf_fcf`) over
`valuation.discount_rates` × `valuation.growth_tiers`, multiple cells from
`valuation.multiple_bands.information_technology` (`pe`, `ev_ebit`), and a reverse DCF. Bands are investor
assumptions (source `D:CFG:valuation.multiple_bands`). Fair values must equal the mean of cited cells;
`cells_base` must span DCF and multiple families when both exist.

- **Start with the reverse DCF.** For IT the question is "what growth is already priced in?".
  Compare `implied_growth_pct` with `growth_pct.revenue_cagr_3y`, `revenue_yoy_ttm` and cited
  end-market growth. Implied growth near the top of the grid's tiers means the price needs years of
  execution with no cycle. Caveat: the reverse DCF's fixed `valuation.reverse_dcf_exit_multiple` differs
  from the grid's `valuation.exit_multiple_by_growth`, so implied growth reads below the matching tier at
  low multiples and above it at high ones — to see which tier the price matches, compare it with the `DCF-NOPAT` cells.
- **`dcf_nopat` is the primary DCF for software.** Course NOPAT starts from GAAP operating income
  plus D&A, so stock-based compensation is already a cost. `dcf_fcf` inherits the SBC add-back in
  operating cash flow and overstates owner cash flow for SBC-heavy companies. Use FCF cells in the
  base only with an explicit dilution caveat in `method`.
- **Terminal value dominance.** High-growth names often carry `tv_gt_90`; when most value sits in the
  exit multiple, say so in `quality_notes` and widen the low–high range rather than trust the base.
- **Semis, memory and hardware: normalize to mid-cycle.** At a cycle peak TTM makes P/E and EV/EBIT
  look cheap and DCF cells look rich; at a trough the reverse. Use the `fy[]` history to place TTM
  margins in the cycle. If TTM sits well above the multi-year pattern, base on the zero or low growth
  tiers and the low multiple band, and state the normalization in `method`.
- **Capex-heavy models (foundry, IDM, data-center hardware):** course NOPAT subtracts capex, so
  build-out years depress it. Decide from the 10-K liquidity section whether capex is maintenance or
  growth before reading a low NOPAT as low value.
- **P/E** is meaningful for profitable software, hardware and semis. For loss-makers it is null
  (`negative_denominator`); the grid may then hold only a few cells — report a wider range and list
  the gap in `missing_data` rather than inventing a method.
- **EV/EBIT** fits mature hardware, semis and IT services. Large net cash matters to the EV→equity
  bridge, so confirm `balance.cash` and `balance.st_investments` share the latest instant (no
  `mixed_instants` flag), and check for non-current marketable securities the bridge omits (§8).
- **What misleads:** EV/Sales for unprofitable software (context only, never a fair-value cell);
  non-GAAP earnings that exclude SBC and acquired-intangible amortization; peak-cycle P/E for semis;
  a "cheap" P/E on a hardware company whose revenue is shrinking.

## 4. Macro sensitivities

Direction = effect on the sector's earnings or valuation when the series **rises**. Read levels and
changes only from `macro.json`; never quote a macro figure from memory.

| Series | Direction | Mechanism |
|---|---|---|
| DGS10 | negative | Long-duration cash flows: higher long yields raise discount rates and compress multiples, hardest on unprofitable growth |
| DFF | mixed | Tight policy hurts risk appetite and funding for loss-makers; cash-rich mega-caps earn more interest income |
| T10Y2Y | mixed | Steepening on growth hopes helps cyclicals (semis); steepening driven by rising long yields hurts duration |
| CPILFESL | negative | Sticky core inflation keeps rates high and lifts engineering wages |
| INDPRO | positive | Industrial and auto activity drives analog/MCU demand, equipment orders and the inventory cycle |
| PAYEMS | positive | Hiring supports seat-based software and enterprise IT budgets |
| UNRATE | negative | Seat contraction, longer sales cycles, higher SMB churn |
| DTWEXBGS | negative | Large non-US revenue share; a stronger dollar reduces reported revenue and EPS |
| BAMLH0A0HYM2 | negative | Wider spreads mean risk-off, closed funding windows and no private-equity bid for software |
| VIXCLS | negative | High-beta, high-multiple names de-rate fastest in volatility spikes |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  semiconductor billings (SIA / WSTS), semiconductor equipment billings (SEMI), PC and smartphone
  shipments (IDC, Gartner), hyperscaler capex (cited from those companies' filings), memory pricing,
  and enterprise IT spending surveys.

## 5. Typical failure modes

- **Peak-cycle value trap:** semis, memory and hardware look cheapest on trailing P/E before earnings fall.
- **Duration de-rating:** high-multiple software de-rates on rate moves alone, then budgets tighten too.
- **Retention decay hidden by pricing:** price rises and acquisitions mask eroding seats and retention.
- **SBC-funded growth:** "FCF positive" models that rely on equity pay; buybacks merely offset dilution.
- **Platform shift:** on-premise to cloud, CPU to accelerator — incumbents become decliners that screen as value.
- **Customer insourcing and concentration:** large customers design their own silicon or software.
- **Export controls and geography:** policy removes markets overnight; one-region supply chains.
- **Double-ordering:** shortages trigger over-ordering; when lead times normalize, orders collapse.
- **Acquisition-fuelled growth:** serial acquirers mask organic deceleration until goodwill is impaired.

## 6. Red flags for the Devil's Advocate

- Revenue growing faster than billings or cRPO for consecutive quarters (pull-forward).
- NRR or other KPI definitions changed, restated or quietly dropped from disclosure.
- Inventory or receivables (DSO) rising faster than revenue while management calls demand strong.
- Capitalized software growing faster than R&D expense (costs moved below the line).
- Buybacks that only offset SBC dilution presented as shareholder returns.
- A customer above the 10% disclosure threshold gaining share of revenue.
- Guidance cut blamed on "macro" while direct peers hold or raise.
- Recurring "one-time" restructuring charges excluded from adjusted earnings every year.
- Most base valuation cells flagged `tv_gt_90`, or reverse-DCF implied growth far above the 3-year CAGR.
- Changes in revenue recognition, segment reporting or fiscal calendar near a slowdown.

## 7. Lens checklist

- **fundamental**
  - Reverse DCF first; compare `implied_growth_pct` with `revenue_cagr_3y` and `revenue_yoy_ttm`.
  - Prefer `dcf_nopat` cells for SBC-heavy names; justify any `dcf_fcf` cell in `method`.
  - Place TTM margins against `fy[]` history to judge cycle position for semis and hardware.
  - Read SBC, diluted share trend and capitalized software from the 10-K (cite the URL).
  - Check the net cash bridge (cash, short-term investments, debt), any `mixed_instants` flag and
    non-current marketable securities left out of `net_debt` (note them in `balance_sheet_notes`).
- **technical**
  - Use only `technicals_<T>.json`. Compare `relative_strength.vs_sector` with `vs_spy`: XLK
    concentration means a name can lag XLK yet lead SPY.
  - Expect high `atr14_pct`; the computed `stop_candidates` may sit far from price — report that
    honestly rather than pick a tighter, non-computed level. `atr_pctile_252` is self-relative: a high
    reading means volatility is unusual for this stock, not normal for the sector.
  - Semis regimes flip around earnings and cycle news; treat `transition` with low conviction.
- **operator_ceo**
  - Founder-led vs professional management; dual-class control and board independence (DEF 14A).
  - Equity plan design: PSU metrics (revenue vs FCF per share vs relative TSR), refresh grants,
    SBC trajectory.
  - M&A record: prices paid, integration, later impairments; buybacks vs dilution.
  - Guidance credibility: beat-and-raise patterns vs cuts; consistency of KPI disclosure.
  - Form 4: scheduled 10b5-1 sales are routine in tech; focus on pattern changes and open-market buys.
- **industry_ops**
  - Supply chain: foundry/OSAT dependence, single-source components, regional concentration (Item 1A).
  - Capacity & backlog: lead times, book-to-bill, purchase commitments vs demand.
  - Unit economics: customer acquisition payback, gross margin by segment, hosting cost per customer.
  - Operational moat: switching costs, ecosystem lock-in, process or design leadership, installed base.
- **osint**
  - Customer and developer sentiment (review sites, community forums), app ratings for consumer software.
  - Hiring trends: sales hiring freezes vs engineering growth; layoff announcements.
  - Earnings-call language shifts: "elongated sales cycles", "deal scrutiny", "digestion", "inventory correction".
  - Component pricing and lead-time reports in trade press; outages and security incidents.
  - Regulatory actions: antitrust, export-control rule changes, data-privacy enforcement.

## 8. Data notes

- **FCF excludes the SBC cost.** `fundamentals_<T>.json` computes FCF as OCF minus capex; OCF adds
  SBC back. For software this flatters `margins_pct.fcf`, `valuation.fcf_yield_pct` and every
  `dcf_fcf` cell.
- **Capitalized software is not capex here.** The fetcher reads `PaymentsToAcquirePropertyPlantAndEquipment`
  / `PaymentsToAcquireProductiveAssets`. `PaymentsToDevelopSoftware` and
  `CapitalizedComputerSoftwareAdditions` sit elsewhere in investing cash flow, so capex is understated
  and FCF plus course NOPAT are overstated. Read the cash-flow statement and say so.
- **Non-current securities are outside net cash.** `net_debt` and `ev` subtract only cash and
  `ShortTermInvestments` / `MarketableSecuritiesCurrent`; `MarketableSecuritiesNoncurrent` and other
  long-term investments are ignored, so EV-bridged DCF and EV/EBIT cells understate equity for
  cash-rich mega-caps. Read the balance sheet; note material amounts in `balance_sheet_notes`.
- **Finance leases:** servers and data-center equipment acquired under finance leases bypass the
  capex line; check `FinanceLeasePrincipalPayments` in the filing before calling capex intensity low.
- **RPO and cRPO:** total RPO is usually tagged; the current portion is often only in text or
  dimensional facts that companyfacts omits — read the revenue note.
- **Fiscal calendars:** non-calendar fiscal years and 52/53-week years are common. Compare peers on
  `period_end`, not on fiscal-year labels.
- **Foreign private issuers** (20-F/40-F, often IFRS): the fetcher reads us-gaap and dei tags only,
  so values may come back null, and TTM can equal the last fiscal year (stale). Read the annual report.
- **Dual-class shares:** expect `share_count_ambiguous` when per-class dei counts do not add up
  cleanly; market cap then comes from Yahoo (`derivation: yfinance_marketCap`).
- **Acquisitions:** `tag_mix` / `stitched` flags and revenue CAGR include acquired revenue — the
  fetcher cannot separate organic growth; take organic growth from the release and cite it.
- **Tax noise:** IP transfers, repatriation and R&D credits distort `tax_rate_used`; if it was clamped
  or defaulted (flagged), note it when reading NOPAT cells.
- **Companyfacts lag:** follow the `companyfacts_lag` missing_data URL to read the newest 10-Q directly.
