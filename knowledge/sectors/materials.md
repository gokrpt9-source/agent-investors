---
sector: materials
name: Materials
benchmark_etf: XLB
sector_class: default
key_kpis:
  - "Realized price vs benchmark commodity price (price/mix contribution)"
  - "Volume growth vs price contribution (volume/price bridge)"
  - "Cost-curve position (cash cost or AISC per unit, cost quartile)"
  - "Through-cycle EBITDA margin and product spreads (price minus feedstock/energy)"
  - "Capacity utilization / operating rates"
  - "Capex split: sustaining vs growth; capex relative to D&A"
  - "Net debt / EBITDA measured on mid-cycle, not peak, earnings"
  - "Reserve life and reserve replacement (miners)"
  - "End-market and China exposure"
  - "Working capital and inventory days through price swings"
valuation_methods:
  - "Mid-cycle normalized EV/EBIT and P/E (grid multiple cells read against through-cycle margins)"
  - "DCF on course NOPAT / FCF at low growth tiers (never extrapolate peak earnings)"
  - "Reverse DCF to test what the price implies about mid-cycle cash flow"
  - "Replacement cost / EV per unit of capacity (sourced industry figures only)"
  - "P/NAV for miners (sourced NAV at a stated commodity price deck)"
macro_sensitivities:
  - series: INDPRO
    direction: positive
    mechanism: "Industrial output drives volumes of chemicals, metals, gases and packaging"
  - series: DTWEXBGS
    direction: negative
    mechanism: "A strong dollar weighs on USD-priced commodities and on US exporters' competitiveness and translated sales"
  - series: DCOILWTICO
    direction: mixed
    mechanism: "Feedstock and energy cost for converters and chemicals; also a reflation signal that lifts miners and pricing"
  - series: CPIAUCSL
    direction: mixed
    mechanism: "Producers capture inflation in realized prices; converters with lagged pass-through see margins squeezed"
  - series: DGS10
    direction: mixed
    mechanism: "Higher real yields hurt non-yielding gold and housing-linked materials; a growth-driven rise often coincides with cyclical demand"
  - series: T10Y2Y
    direction: positive
    mechanism: "Re-steepening from inversion has tended to accompany early-cycle restocking; deep inversion has preceded industrial downturns"
  - series: BAMLH0A0HYM2
    direction: negative
    mechanism: "Widening spreads signal recession risk and raise refinancing cost for leveraged chemical and packaging issuers"
  - series: VIXCLS
    direction: negative
    mechanism: "Risk-off episodes hit high-beta cyclicals and commodity equities first"
failure_modes:
  - "Buying peak earnings on a low P/E at the top of the commodity cycle"
  - "Industry-wide capex boom that becomes oversupply a few years later"
  - "Debt-funded acquisitions at cycle peaks followed by impairments"
  - "China demand shock or Chinese export overcapacity collapsing prices"
  - "Resource nationalism, royalty or permitting shocks and operational disasters"
  - "Environmental and product-liability litigation (e.g. legacy chemical liabilities)"
  - "Cost inflation and reserve-replacement failure eroding miner margins despite high prices"
edgar_tags_of_interest:
  - InventoryNet
  - InventoryLIFOReserve
  - CostOfGoodsAndServicesSold
  - DepreciationDepletionAndAmortization
  - PaymentsToAcquirePropertyPlantAndEquipment
  - AssetRetirementObligation
  - AccrualForEnvironmentalLossContingencies
  - DefinedBenefitPlanFundedStatusOfPlan
  - ImpairmentOfLongLivedAssetsHeldForUse
  - GoodwillImpairmentLoss
  - IncomeLossFromEquityMethodInvestments
---

# Materials — Sector Playbook

> Shared knowledge for every lens covering a materials name. Rules of thumb below are labeled
> **heuristic** — they are judgment aids, not thresholds the scripts apply. This file contains no
> market figures: every number in a report comes from the run's data files or a cited source.

## 1. Scope & benchmark

- **GICS industries covered:** Chemicals (commodity, diversified, specialty, fertilizers &
  agricultural chemicals, industrial gases), Construction Materials (cement, aggregates),
  Containers & Packaging (metal, glass, plastic, paper), Metals & Mining (aluminum, copper,
  diversified metals, gold, precious metals, steel), Paper & Forest Products.
- **Benchmark ETF:** XLB (`committee.yaml: sector_etfs.materials`). Cap-weighted and top-heavy —
  a few large chemical and industrial-gas names can drive it. Check the screen universe and fund
  data in the run before reading XLB-relative strength as "the sector".
- **Yahoo mapping:** `info["sector"] == "Basic Materials"` → `materials` (`common.YF_SECTOR_MAP`);
  fund sector key `basic_materials`. Yahoo follows a Morningstar-style taxonomy, so edge cases
  differ from GICS (packaging can show up as Consumer Cyclical; some coal and building-products
  names land elsewhere). Classification order: portfolio `sector_override` → `etf_overrides.yaml`
  → Yahoo → SIC fallback (metal mining, nonmetallic minerals, lumber, paper, chemicals, rubber
  and plastics, stone/clay/glass, primary metals) → `unclassified`. When a name looks misfiled,
  read `classification_source` in `lookthrough.json`.
- **Sub-industry differences that change the analysis:**
  - *Miners* (copper, gold, iron ore, diversified): price takers. Value ≈ reserves × (price −
    cost) × time; cost-curve position and reserve life dominate everything else.
  - *Commodity chemicals, steel, aluminum smelting:* spread businesses (product price − feedstock
    and energy). Utilization drives margins; industry capacity additions drive the cycle.
  - *Specialty chemicals and industrial gases:* formulation know-how, long take-or-pay contracts,
    cost pass-through clauses. They behave closer to industrials — pricing power and volume
    matter more than spot commodity prices.
  - *Construction materials:* local oligopolies (haul distance limits competition), real pricing
    power, tied to construction and public infrastructure budgets.
  - *Packaging:* converters with pass-through lags; volumes track staples demand; often levered.
  - *Fertilizers:* crop economics, natural-gas feedstock (nitrogen), concentrated global supply
    (potash, phosphate) exposed to sanctions and export policy.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs (heuristics) |
|---|---|---|---|
| Realized price vs benchmark price | Shows hedging, contract lags, grade/quality premia or discounts | 10-K Item 7 (MD&A price/volume bridge); earnings release; LME/CME/USGS commodity data | Healthy: realized tracks or beats benchmark with explained differences. Warning: unexplained discount widening |
| Volume vs price contribution | Separates durable growth from a commodity tailwind | MD&A segment bridge; earnings presentation | Healthy: volume growth with flat-to-up price. Warning: all growth from price while volumes fall |
| Cost-curve position (cash cost, AISC, C1) | Low-cost producers survive troughs and keep investing | Item 2 Properties (S-K 1300 for miners); MD&A; investor day; industry cost curves (often paywalled → null) | Healthy: first/second quartile, stable unit costs. Warning: unit costs rising faster than price; drifting to the top quartile |
| Through-cycle margin / product spread | Peak margins are not run-rate margins | `fundamentals.json` TTM `margins_pct` beside the `fy[]` revenue, operating income and FCF values (copied side by side, never averaged); MD&A; industry spread data | Healthy: TTM operating income and FCF in line with the `fy[]` values. Warning: TTM operating income and FCF far above every `fy[]` value (peak) or company guiding to "new normal" margins |
| Capacity utilization / operating rate | Utilization drives fixed-cost absorption and pricing | MD&A; American Chemistry Council, World Steel Association, Aluminum Association | Healthy: high and stable industry rates. Warning: industry adding capacity while rates already falling |
| Capex: sustaining vs growth; capex vs D&A | Growth capex at the peak becomes oversupply; under-investment hides as FCF | Cash-flow statement (Item 8); MD&A capital-allocation section; earnings call | Heuristic: capex persistently far above D&A late in a boom = cycle risk; persistently below D&A = deferred maintenance |
| Net debt / EBITDA on mid-cycle earnings | Covenants bite at the trough, not the peak | `fundamentals.json` `valuation.net_debt_ebitda` (TTM; state the cycle position next to it, do not recompute it); debt note; a published mid-cycle leverage figure (company target, rating agency) if one is cited | Heuristic: a TTM ratio from a peak year flatters; comfortable only if the cited debt note and any published mid-cycle figure show leverage staying manageable in a weaker year |
| Reserve life & replacement (miners) | Depleting assets must be replaced; acquisitions often overpay | Item 2 Properties / S-K 1300 technical report summaries | Healthy: long reserve life, replacement by drilling. Warning: reserve downgrades, life shrinking, replacement only by M&A |
| End-market and China exposure | China sets marginal demand and supply for many metals and chemicals | Item 1 Business; segment and geographic note (Item 8); Item 1A | Warning: rising China share without pricing power; customers in destocking |
| Inventory days / working capital | Commodity swings flow through inventory (LIFO/FIFO effects) | Balance sheet; MD&A; LIFO reserve note | Warning: inventory build into falling prices; LIFO liquidation gains flattering margins |
| Environmental, ARO and pension liabilities | Debt-like claims missing from EV | Notes (AROs, environmental accruals, pensions); Item 3 Legal Proceedings; Item 1A | Warning: growing accruals, new litigation, underfunded pensions relative to equity value |

## 3. Valuation methods

**What fits**
- **Mid-cycle normalization first.** Decide where in the cycle TTM sits by setting TTM operating
  income and FCF beside the `fy[]` values in `fundamentals_<T>.json` (a qualitative side-by-side of
  copied values, not a computed average). Cyclicals look cheapest on P/E at
  the peak and most expensive at the trough — read every TTM-based cell with that in mind.
- **DCF on course NOPAT / FCF at low growth tiers.** For a peak-cycle TTM, the credible cells are
  the g = 0 and low-growth tiers; high-growth cells extrapolate the peak and should not anchor
  `cells_base`. At a trough, base cash flow may be ≤ 0 and DCF cells come back `na` — say so
  rather than forcing a value.
- **Reverse DCF** (`reverse_dcf.implied_growth_pct`): ask whether the implied growth is plausible
  *from the current point in the cycle*. Negative implied growth at a peak is often correct,
  not a bargain signal.
- **EV/EBIT** (grid `ev_ebit` cells, configured band `valuation.multiple_bands.materials`) is
  usually meaningful because leverage varies widely; prefer it over P/E for levered producers.
- **Replacement cost / EV per unit of capacity** and **P/NAV** for miners are industry standards
  but the grid does not compute them. Use them only when a cited source publishes the figure
  (state its price deck); otherwise leave null and list it in `missing_data`.

**What misleads**
- P/E at peak margins (value trap) and P/E at trough (looks expensive just before recovery).
- Course NOPAT in heavy growth-capex years: `(EBITDA − capex) × (1 − tax)` treats growth capex as
  a permanent cost and can understate value; in under-investment years it overstates it.
- EV-based cells when material **equity-method JVs** sit outside EBIT (EV/EBIT looks expensive) or
  when environmental, ARO or pension liabilities are large (EV understates claims).
- Specialty chemicals and gases are the exception: stable margins allow normal DCF and P/E use.

**Reading the grid** — the multiple bands are configured assumptions, not market data. In
`cells_base` combine one low-growth DCF cell with one multiple cell (at least two method families
when both exist); justify the band (low/mid/high) by cycle position, not by the stock's own recent
multiple. Watch the `tv_gt_90` flag — for cyclicals a terminal value dominating EV means the answer
rests on the exit multiple.

## 4. Macro sensitivities

| FRED id | Direction | Mechanism |
|---|---|---|
| INDPRO | positive | Industrial output drives volumes of chemicals, metals, gases and packaging |
| DTWEXBGS | negative | Strong dollar lowers USD commodity prices and hurts US exporters and translated sales |
| DCOILWTICO | mixed | Feedstock/energy cost for converters and oil-based chemistry; reflation signal for miners and pricing |
| CPIAUCSL | mixed | Producers capture inflation in price; converters with lagged pass-through get squeezed |
| DGS10 | mixed | Higher real yields hurt gold (no yield) and housing-linked materials; growth-driven rises coincide with demand |
| T10Y2Y | positive | Re-steepening has tended to accompany early-cycle restocking; deep inversion has preceded industrial slowdowns |
| BAMLH0A0HYM2 | negative | Wider spreads = recession risk and costlier refinancing for levered issuers |
| VIXCLS | negative | Risk-off hits high-beta cyclicals and commodity equities first |

- **Not in the FRED set — China demand.** No configured series captures it. Agents source it via
  WebSearch (official manufacturing PMI, property starts, credit growth, steel and aluminum output,
  export volumes) with URLs and dates; none may be recalled from memory.
- Natural gas (nitrogen fertilizer, US ethane-based chemistry) and metal prices are not configured
  series either — cite exchange or government sources.
- Read direction *and* level change (`chg_3m`, `chg_12m` in `macro.json`); materials respond to the
  second derivative of industrial activity more than to its level.

## 5. Typical failure modes

1. **Peak-earnings value trap.** Record margins, low P/E, management calling a "structural" shift;
   prices normalize and earnings fall faster than the multiple expands.
2. **Capex supercycle → oversupply.** The whole industry approves projects at the top; capacity
   arrives after demand turns, and the bust lasts years.
3. **Debt-funded M&A at the top.** Acquisitions priced on peak cash flow, impairments and dilutive
   equity raises at the trough.
4. **China shock.** Either demand (property or infrastructure slowdown) or supply (export surges of
   steel, aluminum, chemicals) resets global prices.
5. **Jurisdiction and permitting risk.** Royalty hikes, export bans, nationalization, permit
   revocations, community blockades.
6. **Operational and environmental disasters.** Tailings failures, plant explosions, contamination
   and legacy product litigation that outlive the management team that caused them.
7. **Miner cost creep.** Grade decline, labor and energy inflation and sustaining capex absorb the
   commodity price gains; reserves are replaced only through expensive acquisitions.

## 6. Red flags for the Devil's Advocate

- TTM operating income and FCF far above the `fy[]` values while the thesis assumes they persist.
- Capex guidance rising late in the upcycle; growth capex justified by today's prices.
- Leverage (`valuation.net_debt_ebitda`) that looks fine only because TTM is a peak year; name the
  cycle position next to the ratio rather than recomputing it.
- Realized prices diverging from benchmark without explanation; hedge-book losses.
- Inventory building into falling prices; customers or distributors destocking.
- Unit-cost guidance raised, reserve downgrades, impairments or "one-off" operational items recurring.
- Large share of output from one mine, plant or country; open permits or royalty renegotiations.
- New environmental or product-liability suits; growing environmental accruals; pension deficits.
- Bonus metrics rewarding production volume or EBITDA rather than returns on capital (DEF 14A).
- Chinese export volumes of the company's products rising; trade-remedy cases pending or lapsing.

## 7. Lens checklist

- **fundamental**
  - Place TTM in the cycle by setting TTM operating income and FCF beside the `fy[]` values (copied,
    not averaged) before choosing cells; justify the band.
  - Prefer low-growth DCF cells and EV/EBIT; treat P/E at extremes with suspicion.
  - Name debt-like items outside EV (AROs, environmental, pensions, JV stakes) in `balance_sheet_notes`.
  - Cite cost-curve or NAV figures only from a sourced document; otherwise null + `missing_data`.
- **technical**
  - Use only `technicals_<T>.json`: regime, RS vs XLB (`vs_sector`) and SPY, levels, stop candidates.
  - Expect high `atr14_pct` in miners, so stops based on ATR multiples sit wider — say so; a high
    `atr_pctile_252` still means volatility is elevated versus the stock's own past year.
  - Distinguish sector-wide moves (RS vs XLB flat) from stock-specific ones (RS vs XLB diverging).
  - Do not import commodity or macro views; that is other lenses' job.
- **operator_ceo**
  - Capital allocation across the cycle: buybacks at peaks vs troughs, counter-cyclical M&A, dividend policy.
  - DEF 14A metrics: returns-based (ROIC, per-share) vs volume/EBITDA-based incentives.
  - Production, cost and capex guidance vs delivery over several years; safety and environmental record.
  - Form 4 activity around cycle turns.
- **industry_ops**
  - Cost-curve position, asset quality (grade, scale, logistics, energy access), feedstock advantage.
  - Industry capacity additions vs demand; utilization trend; project execution (overruns, delays).
  - Unit economics per tonne/unit; sustaining capex needs; permitting and water/energy constraints.
- **osint**
  - China data releases, trade actions (tariffs, anti-dumping), export-policy news.
  - Incidents, strikes, community opposition, regulatory actions, litigation filings.
  - Plant closures/restarts, hiring freezes or layoffs, customer destocking commentary.
  - Raise `must_address` flags for anything that threatens licenses to operate.

## 8. Data notes

- **Foreign filers.** Many large miners and chemical producers file 20-F under IFRS. Their facts sit
  in the `ifrs-full` namespace (and may be in non-USD units), which `fetch_fundamentals.py`'s us-gaap,
  USD tag map does not read — expect nulls or `coverage: partial`. Read the 20-F directly and cite it.
- **D&A** usually comes through `DepreciationDepletionAndAmortization` (includes depletion — correct
  for miners). Check `tag_map` to see which tag was used and whether history was `stitched`.
- **Capex** may be tagged outside `PaymentsToAcquirePropertyPlantAndEquipment` (mining assets,
  productive assets, capitalized exploration). If FCF looks implausibly high, compare against the
  cash-flow statement in the 10-K before citing FCF cells.
- **LIFO.** US producers using LIFO report margins distorted by LIFO reserve changes in price swings
  (`InventoryLIFOReserve`); LIFO liquidations inflate earnings.
- **Equity-method JVs** (common in chemicals and mining) contribute earnings below operating income
  (`IncomeLossFromEquityMethodInvestments`), so EBIT, EV/EBIT and course NOPAT understate them.
- **Liabilities not in `total_debt`:** AROs, environmental accruals and pension deficits are read from
  the notes; the script's net debt excludes them.
- **Effective tax rate** swings with depletion allowances, foreign mixes and valuation allowances; the
  clamp and the default-rate flag in `fundamentals.json` tell you when `tax_rate_used` is not the
  company's own.
- **Fund data:** `yf_funds("XLB")` returns the top 10 holdings only, so the Phase 3 screen universe is
  XLB's top 10 plus `universe.yaml` additions — mid-caps are invisible unless added there.
- Commodity prices, cost curves and capacity data are not in any script output; agents source them on
  the web with URLs and as-of dates.
