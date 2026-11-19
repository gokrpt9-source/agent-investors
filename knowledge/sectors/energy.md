---
sector: energy
name: Energy
benchmark_etf: XLE
sector_class: default
key_kpis:
  - Corporate breakeven oil and gas price (capex plus dividend covered)
  - Production growth and mix (oil, NGLs, gas)
  - Reserve life (proved reserves over annual production)
  - Reserve replacement ratio and source of additions
  - Finding and development (F&D) cost per BOE
  - Recycle ratio (cash margin per BOE over F&D cost)
  - Reinvestment rate (capex relative to operating cash flow)
  - Lease operating expense and cash cost per BOE
  - Net debt to EBITDA at mid-cycle prices
  - Refining crack spreads, utilization and capture rate
  - Midstream fee-based share, contract tenor and payout coverage
  - Oilfield services rig count, utilization, day rates and backlog
valuation_methods:
  - Mid-cycle normalized DCF on course NOPAT (zero or low growth tiers when TTM reflects peak prices)
  - EV/EBIT on mid-cycle earnings (ev_ebit band)
  - P/E on normalized earnings (pe band)
  - Reverse DCF read against the zero-growth DCF cells (its fixed exit multiple biases implied growth down)
  - Standardized measure / PV-10 / NAV from the 10-K as a cited cross-check
  - FCF yield at a cited price deck as context only
macro_sensitivities:
  - {series: DCOILWTICO, direction: positive, mechanism: "Higher crude lifts upstream revenue, cash flow and service activity; refiners are mixed because crude is their feedstock."}
  - {series: INDPRO, direction: positive, mechanism: "Industrial activity drives diesel, petrochemical and natural gas demand."}
  - {series: PAYEMS, direction: positive, mechanism: "Employment growth supports gasoline and jet fuel demand."}
  - {series: UNRATE, direction: negative, mechanism: "Rising unemployment signals weaker transport fuel demand and lower refining margins."}
  - {series: DTWEXBGS, direction: negative, mechanism: "Oil is priced in dollars; a stronger dollar tends to weigh on commodity prices and non-US demand."}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Energy is a large high-yield issuer group; wider spreads raise funding costs for levered E&Ps and service companies."}
  - {series: DFF, direction: negative, mechanism: "Tighter policy slows demand, strengthens the dollar and raises financing costs for levered producers."}
  - {series: DGS10, direction: mixed, mechanism: "Higher long yields raise discount rates on long-lived reserves and yield-oriented midstream, but often coincide with reflation that supports oil."}
  - {series: T10Y2Y, direction: mixed, mechanism: "Reflationary steepening supports demand; deep inversion signals recession and demand risk."}
  - {series: CPIAUCSL, direction: positive, mechanism: "Energy prices feed headline inflation, so energy equities tend to rise with it; service and steel cost inflation is the offset."}
  - {series: VIXCLS, direction: negative, mechanism: "Risk-off episodes coincide with demand fears and commodity deleveraging."}
failure_modes:
  - Extrapolating peak-price cash flows (cheap on peak earnings)
  - Growth over returns (high-decline production treadmill funded by debt or equity)
  - Leverage carried into a price downturn
  - Acquisitions priced off a peak strip
  - Impairments and ceiling-test write-downs after price falls
  - Refining margin mean reversion after supply disruptions
  - Midstream counterparty and re-contracting risk in declining basins
  - Terminal value risk from energy transition policy and stranded assets
  - Exhaustion of top-tier drilling inventory
  - Political shocks (windfall taxes, export limits, sanctions, permitting)
edgar_tags_of_interest:
  - DepreciationDepletionAndAmortization
  - PaymentsToAcquireOilAndGasPropertyAndEquipment
  - PaymentsToAcquireOilAndGasProperty
  - CostsIncurredDevelopmentCosts
  - CostsIncurredExplorationCosts
  - CostsIncurredAcquisitionOfProvedOilAndGasProperties
  - CostsIncurredAcquisitionOfUnprovedOilAndGasProperties
  - StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves
  - ProvedDevelopedAndUndevelopedReservesNet
  - ImpairmentOfOilAndGasProperties
  - OilAndGasPropertySuccessfulEffortMethodNet
  - OilAndGasPropertyFullCostMethodNet
  - AssetRetirementObligation
  - InventoryLIFOReserve
  - RevenueFromContractWithCustomerIncludingAssessedTax
---

# Energy — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **GICS industries covered:** Energy Equipment & Services (drilling; equipment and services) and
  Oil, Gas & Consumable Fuels (integrated oil and gas; exploration and production; refining and
  marketing; storage and transportation, i.e. midstream; coal and consumable fuels, including uranium).
- **Not here:** renewable power producers (Utilities), solar manufacturers (Information
  Technology), wind and electrical equipment (Industrials), chemicals (Materials).
- **Benchmark:** XLE (`sector_etfs.energy` in `config/committee.yaml`). Cap-weighted and dominated by
  the integrated majors and a few large producers, it behaves largely like an oil-price proxy;
  relative strength vs XLE for a refiner, driller or pipeline compares it with that proxy.
- **Classification:** `etf_lookthrough.classify_security` applies `sector_override` → `etf_overrides.yaml`
  → Yahoo `info.sector` ("Energy" → `energy`) → SIC fallback, which misroutes gas transmission (49xx)
  to utilities and crude pipelines (46xx) and oilfield machinery (35xx) to industrials — use
  `sector_override` when Yahoo data is missing.
- **Sub-industry differences:**
  - *Integrated majors* — upstream plus refining and chemicals; diversified, lower beta, dividend-led.
  - *E&P* — pure commodity exposure; value rests on reserves, decline rates, inventory depth and
    costs; oil-weighted and gas-weighted producers respond to different prices.
  - *Refining & marketing* — earn a margin (crack spread) rather than a price; feedstock
    differentials, complexity and turnaround execution matter.
  - *Midstream* — fee-based volumes and contracts; leverage, payout coverage, some partnerships.
  - *Equipment, services and drilling* — lagged, high-operating-leverage cycle after producer capex.
  - *Coal and uranium* — contract books, policy and long-term supply agreements.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Corporate breakeven | Commodity price at which capex and the base dividend are funded; resilience in a downturn | Investor presentations; 10-K Item 7 price sensitivities | Healthy: below peers and stable. Warning: rising with cost inflation or new dividend commitments |
| Production growth and mix | Volume drives revenue; mix sets realized price | 10-K Item 2 production tables; earnings release | Warning: growth only through M&A; falling oil cut; flat output despite rising capex |
| Reserve life | Years of production in proved reserves; inventory to sustain the business | 10-K Item 2 and the ASC 932 supplemental reserve disclosures | Warning: reserve life well under a decade (*heuristic*) — the company must keep buying or drilling just to stand still |
| Reserve replacement | Whether the asset base is growing or being liquidated | Supplemental reserve rollforward (extensions, discoveries, revisions, purchases) | Warning: organic replacement below 100% for several years (*heuristic*); additions mostly from price revisions |
| PUD share and conversion | Undeveloped reserves need capital and must be developed within the SEC five-year window | 10-K reserves section | Warning: high PUD share, PUDs rolled forward or removed |
| F&D cost / recycle ratio | Capital efficiency of reserve additions | Costs incurred plus reserve additions in the supplemental data; company-reported figures | Healthy: recycle ratio around 2x or better (*heuristic*). Cite reported figures; do not compute them yourself |
| Reinvestment rate | Growth vs capital discipline | Read `ttm.capex` and `ttm.ocf` side by side; cite company-reported rates | Warning: reinvestment rising toward all of cash flow during a price spike |
| Unit costs (LOE, cash cost per BOE) | Margin per barrel and competitiveness | 10-K Item 7; earnings release | Warning: unit costs rising faster than peers or than realized prices |
| Base decline rate | Capital needed just to hold production flat | Investor presentations | Warning: high decline with shrinking top-tier inventory |
| Net debt / EBITDA | Balance-sheet resilience | `valuation.net_debt_ebitda` (TTM prices) | Warning: low only because TTM EBITDA reflects peak prices; judge it at mid-cycle |
| Crack spread, capture, utilization | Refining earnings power | Refiner earnings release (throughput, capture rate); EIA data | Warning: falling capture, extended turnarounds, rising compliance (RIN) costs |
| Fee-based share, contract tenor, coverage | Midstream cash-flow durability | 10-K Item 1 and 7; earnings release | Warning: thin dividend or distribution coverage, commodity-exposed processing, expiring contracts |
| Rig count, day rates, backlog | Services and drilling cycle position | Public rig count reports; earnings release | Warning: falling backlog, pricing concessions, idle-rig stacking |
| Asset retirement obligations | Debt-like decommissioning liabilities | 10-K ARO note | Warning: large offshore or legacy obligations relative to equity |

## 3. Valuation methods

**What `scripts/valuation.py` provides:** DCF cells (`dcf_nopat`, `dcf_fcf`) over
`valuation.discount_rates` × `valuation.growth_tiers`, multiple cells from
`valuation.multiple_bands.energy` (`pe`, `ev_ebit`), and a reverse DCF. Bands are investor
assumptions (source `D:CFG:valuation.multiple_bands`). Fair values must equal the mean of cited
cells; `cells_base` must span DCF and multiple families when both exist.

- **Normalize to mid-cycle — the central adjustment.** TTM cash flows embed TTM commodity prices.
  The grid grows that base for `valuation.years` and attaches an exit multiple, so at peak prices
  every DCF cell overstates value and P/E and EV/EBIT look cheap; at troughs the reverse. Use the
  `fy[]` history of operating income, OCF and FCF to judge where TTM sits. Near a cycle high, base on
  zero-growth DCF cells and the low multiple band; state the normalization in `method`.
- **Reverse DCF: zero implied growth is not the neutral line.** It applies the fixed
  `valuation.reverse_dcf_exit_multiple`, while zero-growth grid cells use the lower multiple from
  `valuation.exit_multiple_by_growth`, so a price equal to the grid's own `g00` value already shows a
  negative `implied_growth_pct` — common at energy multiples. Compare the price with the `DCF-NOPAT-*-g00`
  cells; read priced-in decline only when the price sits clearly below them, never from the sign alone.
- **Course NOPAT fits E&P well** — EBITDA minus capex approximates owner earnings better than GAAP net
  income when DD&A and impairments are noisy — provided capex is captured (see §8).
- **EV/EBIT** suits integrated, refining, midstream and services on normalized earnings. The industry
  convention of EV/EBITDA and EV/DACF is context only; it is not a grid method.
- **Standardized measure / PV-10 / NAV:** the 10-K standardized measure uses SEC pricing (a trailing
  average) and a fixed discount rate, so it is a regulatory cross-check, not a forward value. Cite it
  from the filing; never recompute NAV in prose.
- **Midstream:** FCF-based DCF cells and P/E work for stable fee-based models; leverage and payout
  coverage drive the multiple. Partnership units, IDRs and K-1 structures need a note.
- **Refiners and services:** deep cyclicals; peak-margin P/E is the classic trap. Prefer the low
  multiple band and zero-growth cells unless TTM is clearly below the multi-year pattern.
- **What misleads:** FCF yield at spot prices; peak P/E; reserve-based value that ignores decline
  rates and development capital; EV that ignores asset retirement and pension obligations.

## 4. Macro sensitivities

Direction = effect on the sector's earnings or valuation when the series **rises**. Read levels and
changes only from `macro.json`; never quote a macro figure from memory.

| Series | Direction | Mechanism |
|---|---|---|
| DCOILWTICO | positive | Upstream revenue and cash flow, service activity; refiners mixed (crude is feedstock) |
| INDPRO | positive | Diesel, petrochemical and gas demand from industry |
| PAYEMS | positive | Gasoline and jet demand from employment and travel |
| UNRATE | negative | Weaker fuel demand and refining margins |
| DTWEXBGS | negative | Dollar-priced commodity; strong dollar weighs on prices and non-US demand |
| BAMLH0A0HYM2 | negative | Energy is a large high-yield issuer group; wider spreads raise funding costs and distress risk |
| DFF | negative | Slower demand, stronger dollar, higher financing costs for levered producers |
| DGS10 | mixed | Higher discount rates on long-lived assets and yield-oriented midstream vs reflation support for oil |
| T10Y2Y | mixed | Reflationary steepening helps; deep inversion signals recession and demand risk |
| CPIAUCSL | positive | Energy drives headline inflation; offset by service and steel cost inflation |
| VIXCLS | negative | Risk-off episodes bring demand fears and commodity deleveraging |

- **Not configured — source on the web (URL and date, never from memory) or record in `missing_data`:**
  Henry Hub natural gas (the price that drives gas-weighted E&Ps and LNG; `DCOILWTICO` is no proxy),
  Brent and regional differentials, NGL prices, 3-2-1 crack spreads, EIA weekly inventories and
  production, the Baker Hughes rig count, and OPEC+ quota decisions.

## 5. Typical failure modes

- **Peak-cycle extrapolation:** buying low P/E and high FCF yield at peak prices before they normalize.
- **Growth over returns:** the shale-era pattern of funding high-decline production growth with debt
  and equity destroyed capital even while volumes rose.
- **Leverage into a downturn:** fixed obligations plus a price collapse produce restructurings and
  heavy dilution in every oil bust.
- **Peak-strip M&A:** acquisitions priced off high forward curves, followed by impairments.
- **Write-downs:** ceiling-test and impairment charges after price falls wipe out book value and earnings.
- **Refining mean reversion:** windfall cracks from outages or sanctions fade as capacity returns.
- **Midstream contract risk:** counterparty bankruptcies, contract rejection, re-contracting at lower
  rates in declining basins.
- **Terminal value risk:** transition policy, demand peaks and stranded assets shorten asset lives
  that DCF exit multiples assume are long.
- **Inventory exhaustion:** running out of top-tier locations leads to rising well costs and falling
  productivity (well interference), masked for a while by acquisitions.
- **Political shocks:** windfall taxes, export restrictions, sanctions, leasing and permitting changes.

## 6. Red flags for the Devil's Advocate

- Reserve additions driven by price revisions rather than extensions and discoveries.
- PUD share rising, or PUDs removed or rolled forward against the five-year development window.
- Production growth alongside rising unit costs and falling well productivity.
- Breakeven rising; dividends funded by debt, asset sales or working-capital release.
- Capex guidance cut with unchanged production guidance (efficiency claims that need proof).
- Hedge book rolling off at lower prices, or large unrealized derivative gains inflating TTM.
- Growing asset retirement obligations, especially offshore, without funding plans.
- Midstream: drop-down and related-party transactions, incentive distribution rights, coverage
  supported by one-offs.
- Refiners: falling capture rates, extended turnarounds, rising renewable fuel compliance costs.
- Services: backlog with cancellable contracts; customer concentration.
- Executive pay tied to production or reserve growth rather than returns or per-share value.
- Accounting method differences (full cost vs successful efforts) used to flatter peer comparisons.

## 7. Lens checklist

- **fundamental**
  - Place TTM margins and cash flow against `fy[]` history; normalize before choosing base cells.
  - Check `tag_map.capex`: null means FCF, NOPAT and DCF cells are `na`; a small generic PP&E tag means overstated (§8).
  - Read `valuation.net_debt_ebitda` as a TTM-price figure; judge leverage at mid-cycle in notes.
  - Cite the standardized measure, reserve life and breakeven from the 10-K or presentations.
  - Note ARO and pension liabilities in `balance_sheet_notes` (not in `net_debt`).
- **technical**
  - Use only `technicals_<T>.json`. Energy names track crude, which this lens cannot see; use
    `vs_sector` to separate stock-specific moves from sector (oil) moves.
  - Expect `atr_pctile_252` spikes around supply events; gaps can jump `stop_candidates`.
  - A sector-wide `trend_down` regime with positive `vs_sector` is relative, not absolute, strength.
- **operator_ceo**
  - Capital-return framework (base plus variable dividend, buybacks) vs growth capex; consistency
    through the cycle.
  - Incentives (DEF 14A): ROCE, FCF per share and returns vs production or reserve growth targets.
  - M&A record: timing in the cycle, price paid, synergy delivery.
  - Insider buying near troughs is informative; guidance vs delivery on production, capex and unit costs.
- **industry_ops**
  - Supply chain: service and rig availability, tubulars and equipment lead times, frac crews.
  - Capacity & backlog: takeaway and export capacity, refinery utilization, drilling backlog.
  - Execution: well results vs type curves, megaproject (LNG, offshore) schedule and budget delivery.
  - Unit economics: LOE, F&D, recycle ratio, refinery cash cost per barrel, contract day rates.
  - Operational moat: acreage quality and inventory depth, advantaged crude access, integrated logistics.
- **osint**
  - OPEC+ decisions, EIA inventory and production data, public rig counts, sanctions news.
  - Safety and environmental incidents (pipeline and offshore regulators), spills, permit actions.
  - Activist campaigns and shareholder proposals on capital allocation or transition strategy.
  - Hiring and layoffs at service companies; earnings-call language ("discipline", "maintenance
    mode", "inventory depth", "efficiency gains").

## 8. Data notes

- **Capex tags:** E&Ps often tag `PaymentsToAcquireOilAndGasPropertyAndEquipment` or
  `PaymentsToAcquireOilAndGasProperty` instead of the generic PP&E tags the fetcher reads. If
  `tag_map.capex` is null, FCF and course NOPAT come back null and every DCF cell is `na` (listed in
  `missing_data`). If it picked a generic PP&E tag while the oil-and-gas tags carry the bulk, or capex
  looks small relative to DD&A, FCF, FCF yield and NOPAT are overstated — read the cash-flow statement.
- **Reserves, production and realized prices** sit in 10-K Item 2 and the ASC 932 supplemental
  (unaudited) section, often dimensional by product and region, so companyfacts usually lacks them.
- **Accounting method:** full-cost filers capitalize dry holes and take ceiling-test write-downs;
  successful-efforts filers expense exploration failures. Compare DD&A, margins and book value
  across methods with care.
- **Refiners:** revenue may include excise taxes (`RevenueFromContractWithCustomerIncludingAssessedTax`),
  understating margins; LIFO inventory (`InventoryLIFOReserve`) shifts earnings vs FIFO peers.
- **Derivatives:** unrealized hedge gains and losses run through revenue or other income and distort
  TTM; the release usually separates realized from unrealized.
- **Impairments** can turn TTM net income negative (P/E null, `negative_denominator`) and break
  `ni_cagr_3y`; read the cash flow line instead.
- **Partnerships (MLPs):** units rather than shares and partners' capital rather than stockholders'
  equity, so `shares`, `equity`, P/B and ROE may be null or misleading.
- **Royalty trusts** (no capex, pass-through cash) are not comparable with producers; **foreign
  majors** (20-F, IFRS) return mostly nulls — read the annual report.
- **Large acquisitions:** `tag_mix` / `stitched` flags; production and revenue growth may be inorganic.
