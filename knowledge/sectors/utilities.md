---
sector: utilities
name: Utilities
benchmark_etf: XLU
sector_class: default
key_kpis:
  - "Rate base and rate base growth"
  - "Capex plan (size, funding mix, recovery mechanism)"
  - "Allowed vs earned ROE by jurisdiction"
  - "Authorized equity ratio and capital structure"
  - "Regulatory lag: test year, rate case cadence, riders and formula rates"
  - "Rate case outcomes (granted vs requested)"
  - "Customer bill affordability and rate increases vs inflation"
  - "Weather-normalized load growth (incl. large-load / data-center contracts)"
  - "Credit metrics: FFO to debt, holdco debt share, ratings outlook"
  - "Dividend payout ratio and equity issuance needs"
valuation_methods:
  - "pe vs EPS growth (rate base growth less dilution) as the primary frame"
  - "ev_ebit (D&A is recovered in rates, so EBIT is a fair earnings base)"
  - "dcf_nopat / dcf_fcf usually low-information (growth capex makes base CF small or negative)"
  - "sum-of-the-parts by jurisdiction and unregulated segment (qualitative, from filings)"
  - "dividend yield vs bond yields (qualitative, cited)"
macro_sensitivities:
  - {series: DFF, direction: negative, mechanism: "Short-term debt and commercial paper fund capex; higher short rates raise interest expense ahead of rate recovery"}
  - {series: DGS2, direction: negative, mechanism: "Front-end yields reprice floating debt and near-term refinancings"}
  - {series: DGS10, direction: negative, mechanism: "Bond-proxy valuation; raises cost of debt and equity for capex while allowed ROEs reset slowly"}
  - {series: T10Y2Y, direction: mixed, mechanism: "Growth-led steepening rotates away from defensives; steepening from rate cuts supports utilities"}
  - {series: CPIAUCSL, direction: negative, mechanism: "Costs rise before rates reset (regulatory lag); bill affordability limits rate case outcomes"}
  - {series: CPILFESL, direction: negative, mechanism: "Labor, materials and services inflation in O&M plus higher-for-longer rates"}
  - {series: UNRATE, direction: mixed, mechanism: "Residential demand is stable, but bad debt, affordability politics and C&I load weaken"}
  - {series: PAYEMS, direction: positive, mechanism: "Customer and commercial load growth in service territories"}
  - {series: INDPRO, direction: positive, mechanism: "Industrial electric load and gas throughput track production"}
  - {series: DTWEXBGS, direction: mixed, mechanism: "Revenue is domestic; dollar and tariffs move costs of imported transformers, modules and turbines"}
  - {series: DCOILWTICO, direction: mixed, mechanism: "Weak direct link; fuel costs mostly pass through clauses, but high energy bills strain affordability"}
  - {series: BAMLH0A0HYM2, direction: negative, mechanism: "Credit conditions set holdco, hybrid and IPP refinancing costs and equity-issuance windows"}
  - {series: VIXCLS, direction: positive, mechanism: "Relative: defensive rotation in volatility spikes; absolute returns fall in liquidity-driven sell-offs"}
failure_modes:
  - "Regulatory disallowances and political backlash after affordability or storm crises"
  - "Wildfire and catastrophic-event liabilities overwhelming equity"
  - "Large construction projects (new nuclear, major plants) with cost overruns and abandonments"
  - "Merchant power and yieldco blow-ups when power prices or capital access collapse"
  - "Holdco over-leverage leading to dividend cuts and dilutive equity raises"
  - "Rate shocks de-rating bond proxies regardless of fundamentals"
  - "Stranded assets: coal plants, gas distribution facing electrification policy"
  - "Utility M&A blocked or conditioned by regulators"
edgar_tags_of_interest:
  - RegulatedAndUnregulatedOperatingRevenue
  - PublicUtilitiesPropertyPlantAndEquipmentNet
  - PublicUtilitiesPropertyPlantAndEquipmentPlantInService
  - PublicUtilitiesPropertyPlantAndEquipmentConstructionWorkInProgress
  - RegulatoryAssetsNoncurrent
  - RegulatoryLiabilityNoncurrent
  - PublicUtilitiesAllowanceForFundsUsedDuringConstructionCapitalizedCostOfEquity
  - PublicUtilitiesApprovedReturnOnEquityPercentage
  - PublicUtilitiesRequestedReturnOnEquityPercentage
  - PublicUtilitiesApprovedEquityCapitalStructurePercentage
  - PublicUtilitiesRequestedRateIncreaseDecreaseAmount
  - PublicUtilitiesApprovedRateIncreaseDecreaseAmount
  - AssetRetirementObligation
  - DefinedBenefitPlanFundedStatusOfPlan
  - ProceedsFromIssuanceOfCommonStock
  - InterestExpense
---
# Utilities — Sector Playbook

> Knowledge playbook for agents. It contains no market data: every figure in a report must come from
> the run's data files or a cited source. Thresholds marked *heuristic* are practitioner rules of thumb,
> not sourced statistics, and must not be quoted as facts.

## 1. Scope & benchmark

- **Benchmark ETF:** XLU (`sector_etfs.utilities` in `config/committee.yaml`). It is cap-weighted,
  typically led by large regulated electric and multi-utilities, but a few independent power producers
  can drive its momentum, so `vs_sector` may reflect a few names. For top-10 weights: if
  `phase3/screen_utilities.json` is among your inputs, use its `universe[].etf_weight_pct`; otherwise
  cite the issuer's holdings page (W#) or note the limitation — never recall weights from memory.
- **GICS industries covered:** electric utilities, gas utilities, multi-utilities, water utilities,
  independent power producers & renewable electricity producers.
- **How a ticker lands here:** portfolio `sector_override` → `etf_overrides.yaml` → Yahoo `info.sector`
  "Utilities" (`YF_SECTOR_MAP`) → SIC fallback 4900–4999 (electric, gas, water, sanitary services).
- **Label mismatches:** midstream pipelines and gas gathering are Energy, not Utilities, even when they
  look regulated. The SIC 49xx range also covers waste/sanitary services that GICS puts in Industrials.
  Yahoo splits utilities into regulated electric/gas/water, diversified, IPPs and renewables — the
  sub-label matters more than the sector label.
- **Sub-industry differences:**
  - *Regulated electric and multi-utilities:* earnings ≈ allowed return × equity portion of rate base;
    growth comes from capex the regulator lets into rate base. Jurisdiction quality is the key variable.
  - *Gas distribution:* similar regulatory model; long-term terminal-value risk from electrification
    policy; pipe-replacement programs drive rate base.
  - *Water:* slow, steady rate base growth from infrastructure renewal; fragmented, acquisition-driven.
  - *Vertically integrated vs transmission-and-distribution-only:* integrated utilities own generation
    (fuel, plant and decommissioning risk); wires-only utilities have simpler risk.
  - *IPPs and renewable developers:* merchant power prices, contract roll-offs, capacity markets, tax
    credits and project finance — economics closer to commodities or infrastructure developers.

## 2. KPIs that matter

Agents quote KPIs from the run's data files or cited sources; they do not compute new ratios in prose.
A KPI that cannot be sourced is `null` and listed in `missing_data`.

| KPI | Why it matters | Where to find it | Healthy vs warning signs |
|---|---|---|---|
| Rate base & growth | The earnings engine: allowed return is earned on rate base | Investor presentations; 10-K Item 1 / Item 7; state commission orders | Healthy: growth supported by an approved or rider-recovered capex plan. Warning: rate base growth assumed but not yet approved |
| Capex plan | Drives rate base, financing needs and bill impact | MD&A capital-resources section; investor-day plans; `ttm.capex`, `fy[].capex` | Warning: capex raised repeatedly without matching recovery mechanisms or funding plan |
| Allowed vs earned ROE | Earned below allowed = regulatory lag or cost overruns | Rate orders; company disclosures by jurisdiction; commission annual reports | Warning (heuristic): earned ROE persistently well below allowed in major jurisdictions |
| Authorized equity ratio | Sets how much of rate base earns the equity return | Rate case orders; `PublicUtilitiesApprovedEquityCapitalStructurePercentage` where tagged | Warning: regulator cutting equity ratio or imputing a lower one |
| Regulatory lag & mechanisms | Speed of cost recovery | 10-K Item 1 regulatory section; rate case filings (test year, riders, formula rates, decoupling, fuel clauses) | Healthy: forward test years, riders, decoupling. Warning: historical test years with long gaps between cases in an inflationary period |
| Rate case outcomes | Regulatory relationship quality | Commission orders and dockets; earnings releases | Warning (heuristic): repeated awards far below requests, or settlements with disallowances |
| Bill affordability | The political ceiling on rate increases | Commission filings; company bill comparisons; public energy-price data (EIA) | Warning: bills rising much faster than local incomes or inflation; intervenor and political pushback |
| Load growth | Spreads fixed costs; large-load contracts add rate base | MD&A sales statistics (weather-normalized); investor presentations | Healthy: contracted large loads with protective tariffs. Warning: speculative large-load pipeline driving capex with customers not bearing the risk |
| Credit metrics | Ratings determine financing cost and equity needs | Rating-agency reports (public summaries); MD&A liquidity; `valuation.net_debt_ebitda` | Warning: negative outlook, metrics near the company's downgrade threshold, rising holdco debt share |
| Payout & equity needs | Dividend safety and dilution | Cash-flow statement; financing plans (ATM programs, block equity, hybrids) | Warning (heuristic): payout near or above the top of the company's own target range while equity issuance is rising |
| AFUDC share of earnings | Non-cash earnings during construction | Income statement / regulatory note (`...CapitalizedCostOfEquity`) | Warning: AFUDC equity a growing share of net income — earnings not yet cash |
| Reliability & safety | Regulators penalize poor service | Commission reliability reports (SAIDI/SAIFI); incident investigations | Warning: worsening outage metrics, fines, wildfire or explosion investigations |

## 3. Valuation methods

- **What fits:** P/E relative to EPS growth (rate base growth less dilution from equity issuance) and
  EV/EBIT. D&A is recovered in customer rates, so EBIT and EBITDA are fair earnings bases. A
  sum-of-the-parts view by jurisdiction or segment is useful but must stay qualitative.
- **Reading the `valuation_<T>.json` grid:**
  - `dcf_nopat` uses course NOPAT = (EBITDA − capex) × (1 − tax). In a capex build-out, capex can match
    or exceed EBITDA, so the base is small or negative and cells come back `na`; `dcf_fcf` is usually
    negative for the same reason. This is expected, not a data error: growth capex earns an allowed
    return, so subtracting all of it understates value.
  - The reverse DCF uses the same NOPAT base, so `implied_growth_pct` is often null. Do not attempt a
    DCF by hand; say the DCF family is not meaningful and why in `extensions.valuation.notes`
    (≤ 60 words).
  - `pe` and `ev_ebit` cells use `valuation.multiple_bands.utilities` (source
    `D:CFG:valuation.multiple_bands`) — configured assumptions, labeled as such. Low band for weak
    jurisdictions or heavy dilution; high band only for premium jurisdictions with visible rate base growth.
  - When DCF cells are all `na`, the base case can rest on the multiple family alone (the two-family
    rule applies only when both are available). If some DCF cells exist, include one in `cells_base` as
    required and state its downward bias in `extensions.valuation.notes`.
  - Low effective tax rates (tax credits, excess deferred-tax amortization) raise NOPAT. Check
    `tax_rate_used`: a negative rate is clamped to the lower bound of `fundamentals.tax_rate_bounds`
    (flag `tax_rate_clamped`) and a near-zero rate passes unflagged; either way NOPAT is nearly
    untaxed, so state the upward bias of `dcf_nopat` cells and the reverse DCF in
    `extensions.valuation.notes`.
- **What misleads:**
  - Consolidated ROE vs allowed ROE: holdco debt, goodwill and unregulated segments distort it;
    jurisdictional earned ROE comes from regulatory filings.
  - Dividend yield as the valuation anchor when the dividend is funded by issuance.
  - EV/EBIT for IPPs with heavy project debt and tax-equity structures; noncontrolling and tax-equity
    interests are not in `net_debt`.
  - P/E on earnings inflated by AFUDC equity or one-time regulatory true-ups.

## 4. Macro sensitivities

Direction = effect on sector earnings or relative valuation when the series **rises**. Agents cite
current readings from `phase1/macro.json` (source `D:FRED:<ID>`); the table lists transmission channels.
Natural gas prices matter more than oil but are not in the configured series list — source them
separately (cited) or record them in `missing_data`.

| Series | Direction | Mechanism |
|---|---|---|
| DFF | negative | Short-term debt and commercial paper fund capex; higher short rates raise interest expense before rates reset |
| DGS2 | negative | Reprices floating debt and near-term refinancings |
| DGS10 | negative | Bond-proxy valuation; raises cost of debt and equity for capex while allowed ROEs reset only slowly |
| T10Y2Y | mixed | Growth-led steepening rotates money away from defensives; steepening driven by rate cuts supports utilities |
| CPIAUCSL | negative | O&M and materials costs rise before rates reset (regulatory lag); affordability limits rate case outcomes |
| CPILFESL | negative | Labor and services inflation in O&M; sticky core keeps long yields high |
| UNRATE | mixed | Residential demand is stable, but bad debt, affordability politics and commercial/industrial load weaken |
| PAYEMS | positive | Customer additions and commercial load growth in service territories |
| INDPRO | positive | Industrial electric load and gas throughput track production |
| DTWEXBGS | mixed | Revenue is domestic; dollar and tariffs move costs of imported transformers, solar modules and turbines |
| DCOILWTICO | mixed | Weak direct link; fuel costs mostly pass through adjustment clauses, but high energy bills strain affordability |
| BAMLH0A0HYM2 | negative | Credit conditions set holdco, hybrid and IPP refinancing costs and equity-issuance windows |
| VIXCLS | positive | Relative: defensive rotation during volatility spikes; absolute returns fall in liquidity-driven sell-offs |

## 5. Typical failure modes

- **Regulatory backlash:** after storms, outages or affordability crises, commissions cut allowed
  returns, disallow costs or freeze rates; "constructive" jurisdictions can turn quickly.
- **Catastrophic liabilities:** wildfire liability regimes (including strict-liability doctrines) have
  pushed a large Western utility into bankruptcy; equity is the shock absorber.
- **Big-project overruns:** new nuclear and other large plants have run years late and far over budget,
  leading to abandonments, write-offs and management turnover.
- **Merchant and yieldco busts:** IPPs and renewable vehicles financed on cheap capital collapsed when
  power prices fell or capital access closed.
- **Holdco leverage:** debt at the parent funding equity into subsidiaries; downgrades lead to dividend
  cuts and dilutive equity raises.
- **Rate shocks:** rising long yields de-rate the whole sector even when fundamentals are unchanged.
- **Stranded assets:** coal retirements and gas distribution facing electrification or new-connection
  bans threaten unrecovered rate base.
- **Blocked M&A:** regulators reject or heavily condition utility mergers, leaving deal costs and
  strategic drift.

## 6. Red flags for the Devil's Advocate

- Earned ROE persistently below allowed; rate case awards far below requests; settlements with disallowances.
- Capex plan raised again with no new recovery mechanism; equity needs understated or deferred to "hybrids".
- Payout at or above the top of target range while issuing equity — the dividend is financed, not earned.
- AFUDC equity a growing share of earnings; regulatory assets ballooning (deferred costs not yet approved).
- Rating outlook negative, FFO/debt near the company's stated threshold, holdco debt share rising.
- Wildfire, storm or safety exposure without securitization or liability protection.
- Large-load (data-center) growth assumptions with capex committed before contracts protect customers.
- Valuation base case on `g06`+ DCF cells or a high multiple band without approved rate base growth.
- Political or legal controversy (lobbying scandals, commission turnover, ballot initiatives on rates).

## 7. Lens checklist

- **fundamental**
  - Discuss earnings drivers through company-disclosed rate base, authorized equity ratio and earned ROE:
    cite each figure individually (or mark it missing) and never compute or quote their product.
  - Expect `na` DCF cells; base the valuation on `pe`/`ev_ebit` and explain why in
    `extensions.valuation.notes` (≤ 60 words).
  - Cover holdco debt, hybrids, pension, AROs and regulatory assets in `balance_sheet_notes`.
  - Check equity-issuance plans and their dilution against EPS growth guidance.
- **technical**
  - Utilities are low-beta and rate-sensitive; `range` regimes and modest `atr14_pct` are common.
  - High dividends mean adjusted-price levels differ from raw charts; use computed levels and
    `stop_candidates` only.
- **operator_ceo**
  - Regulatory relationship and rate case track record; settlement vs litigated outcomes.
  - Incentives in the DEF 14A: EPS growth vs safety, reliability and customer metrics; equity-funding discipline.
  - Guidance credibility on EPS growth range and capex; Form 4 activity around equity offerings.
- **industry_ops**
  - Capex execution (on time, on budget), supply of transformers, switchgear, turbines; interconnection queues.
  - Generation mix and fuel exposure; plant retirements and replacement plans; storm hardening.
  - O&M cost per customer, reliability metrics, wildfire mitigation programs.
- **osint**
  - State commission dockets, intervenor filings, consumer-advocate positions, customer complaints.
  - Outage events, wildfire and severe-weather incidents, safety investigations, local political pushback
    on rates or large-load contracts.

## 8. Data notes

- **Revenue tag:** many utilities report total operating revenue under `RegulatedAndUnregulatedOperatingRevenue`,
  which is not in `fetch_fundamentals.py`'s revenue candidates. `RevenueFromContractWithCustomer...`
  can exclude alternative-revenue programs (decoupling) and derivative revenue, understating revenue.
  Check `tag_map.revenue.tag` against the income statement.
- **Capex tag:** some utilities tag capex with company-specific extensions instead of
  `PaymentsToAcquirePropertyPlantAndEquipment`. A null `ttm.capex` leaves FCF, NOPAT, the DCF cells and
  the reverse DCF `na` (`missing`); a partial or stale capex tag (only one subsidiary's or one
  program's spend) makes FCF and NOPAT far too high. Verify against the cash-flow statement before
  using any DCF cell.
- **Regulatory data lives in filings, not companyfacts:** allowed ROE, equity ratio, rate base, test
  year and rate case status are mostly in text, exhibits and state commission dockets; the
  `PublicUtilities...` tags are used inconsistently. `fetch_fundamentals.py` does not read them; they are
  listed for agents navigating filings.
- **Subsidiary registrants:** many utility holdcos file combined 10-Ks with their operating subsidiaries;
  companyfacts for the parent CIK is consolidated. Subsidiary figures need the subsidiary's own CIK or
  the combined filing.
- **`net_debt` excludes** preferred stock, noncontrolling and tax-equity interests, pension deficits
  and AROs; it includes junior subordinated hybrids only when tagged as long-term debt.
- **Tax rate:** effective rates can be near zero or negative from credits. Income tax / pretax income
  is clamped to `fundamentals.tax_rate_bounds`: a negative rate becomes the lower bound (flag
  `tax_rate_clamped`), a near-zero positive rate is used unflagged, and the default rate (flag
  `default_tax_rate`) applies only when pretax income is ≤ 0 or an input is missing. A near-zero
  `tax_rate_used` means NOPAT is effectively untaxed and `dcf_nopat` cells are biased upward.
- **Technicals use dividend-adjusted prices** (`auto_adjust=True`); with high yields, older levels sit
  well below raw-chart levels.
