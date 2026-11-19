---
sector: communication_services
name: Communication Services
benchmark_etf: XLC
sector_class: default
key_kpis:
  - "ARPU / ARPA trend and its drivers (price vs mix vs promotions)"
  - "Subscriber net adds and churn (postpaid phone, broadband, paid streaming)"
  - "Advertising revenue growth split into volume (impressions) and price"
  - "Engagement metrics (DAU/MAU, time spent, hours viewed) and their definitions"
  - "Content spend: cash content spend vs P&L amortization"
  - "Capex intensity (capex / revenue) and the network or data-center build phase"
  - "Spectrum holdings, auction payments and license obligations"
  - "Net debt / EBITDA and FCF dividend coverage (telecom, cable, media)"
  - "Stock-based compensation relative to revenue and net share-count change"
  - "Regulatory exposure (antitrust, privacy, FCC, content rules)"
valuation_methods:
  - "DCF on FCF and course NOPAT, adjusted in judgment for SBC, spectrum and content cash"
  - "EV/EBIT and P/E for mature platforms and telecoms"
  - "FCF yield and dividend coverage for telecoms and cable"
  - "Sum-of-the-parts for conglomerates (core business vs loss-making bets and owned stakes)"
  - "Reverse DCF for high-multiple platforms"
macro_sensitivities:
  - series: PAYEMS
    direction: positive
    mechanism: "Advertising budgets follow employment and consumer spending; ads are among the first costs cut"
  - series: UNRATE
    direction: negative
    mechanism: "Rising unemployment drives ad cuts, subscription churn and downgrades to cheaper plans"
  - series: DGS10
    direction: negative
    mechanism: "Long-duration platform valuations and high-dividend, levered telecoms both de-rate as long yields rise"
  - series: BAMLH0A0HYM2
    direction: negative
    mechanism: "Levered telecom, cable and media issuers refinance at wider spreads; M&A financing tightens"
  - series: DTWEXBGS
    direction: negative
    mechanism: "Internet platforms earn heavily abroad; a stronger dollar reduces translated revenue and profit"
  - series: CPIAUCSL
    direction: mixed
    mechanism: "Inflation enables price increases on subscriptions but raises content, labor and network costs"
  - series: VIXCLS
    direction: negative
    mechanism: "High-beta interactive media and entertainment names sell off in risk-off episodes"
failure_modes:
  - "Secular decline (linear TV, print, fixed voice) bought as 'cheap'"
  - "Ad recession hitting the most cyclical revenue line"
  - "Content and sports-rights cost inflation outrunning monetization"
  - "Telecom price wars and capex/spectrum arms races without ARPU uplift"
  - "Leveraged media mergers followed by write-downs"
  - "Platform risk: privacy or OS changes, antitrust remedies, engagement migrating to new apps"
  - "Capex surges (data centers, fiber, 5G) with unproven returns"
  - "High-payout telecom dividend traps"
edgar_tags_of_interest:
  - RevenueFromContractWithCustomerExcludingAssessedTax
  - AdvertisingRevenue
  - ContractWithCustomerLiabilityCurrent
  - CapitalizedContractCostNet
  - IndefiniteLivedLicenseAgreements
  - IndefiniteLivedIntangibleAssetsExcludingGoodwill
  - PaymentsToAcquireIntangibleAssets
  - PaymentsToAcquirePropertyPlantAndEquipment
  - DepreciationDepletionAndAmortization
  - ResearchAndDevelopmentExpense
  - ShareBasedCompensation
  - PaymentsForRepurchaseOfCommonStock
  - OperatingLeaseLiability
---

# Communication Services — Sector Playbook

> Shared knowledge for every lens covering a communication-services name. Rules of thumb below are
> labeled **heuristic** — judgment aids, not thresholds the scripts apply. No market figures appear
> here: every number in a report comes from the run's data files or a cited source.

## 1. Scope & benchmark

- **GICS industries covered:** Telecommunication Services (diversified telecom, wireless telecom) and
  Media & Entertainment — Media (advertising, broadcasting, cable & satellite, publishing),
  Entertainment (movies & entertainment, interactive home entertainment / video games) and
  Interactive Media & Services (search, social, online content platforms). The sector was created by
  GICS in 2018 from telecoms plus media and internet names moved out of Consumer Discretionary and IT.
- **Benchmark ETF:** XLC (`committee.yaml: sector_etfs.communication_services`). Very concentrated in
  a couple of interactive-media mega-caps (each with share classes); check the run's fund data for
  top-10 concentration before treating XLC-relative strength as a broad-sector signal.
- **Yahoo mapping:** `info["sector"] == "Communication Services"` → `communication_services`; fund
  sector key `communication_services`. Yahoo's Morningstar-style taxonomy mostly matches GICS here,
  but e-commerce, software-like and some gaming names can land in other sectors. Towers are REITs
  (Real Estate), not telecoms. SIC fallback: 2700–2799 (publishing), 4800–4899 (communications),
  7800–7989 (motion pictures, amusement). Check `classification_source` when a name looks misfiled.
- **Sub-industry differences that change the analysis:**
  - *Wireless / wireline telecom:* utility-like, levered, capex- and spectrum-heavy; value comes from
    FCF after capex and spectrum, and from the dividend's coverage.
  - *Cable & satellite:* broadband vs video mix; video is in secular decline; leverage targets matter.
  - *Interactive media (search, social):* ad-funded, high margins, net cash, large SBC and rising
    capex; engagement and regulation are the swing factors.
  - *Streaming and studios:* content is the main capital outlay, mostly sitting in operating cash flow.
  - *Video games:* release-cycle lumpiness; live-service engagement and deferred revenue matter.
  - *Broadcasting, publishing, advertising agencies:* cyclical ad exposure plus secular pressure.

## 2. KPIs that matter

| KPI | Why it matters | Where to find it | Healthy vs warning signs (heuristics) |
|---|---|---|---|
| ARPU / ARPA | Pricing power and mix; the main organic growth lever in subscriptions | Earnings release trending schedules; 10-K Item 7 (MD&A) | Healthy: ARPU up with stable churn. Warning: ARPU up only via price hikes while churn rises |
| Subscriber net adds & churn | Demand and competitive position | Earnings release; MD&A; industry trackers | Warning: negative net adds, rising postpaid or broadband churn, metric disclosure discontinued |
| Ad revenue: impressions vs price | Separates demand (price) from inventory growth (volume) | 10-K MD&A (some platforms disclose impressions and price per ad); earnings release | Healthy: both rising. Warning: growth only from added ad load while price per ad falls |
| Engagement (DAU/MAU, time spent, hours viewed) | Leading indicator of ad and subscription revenue | Earnings release; investor letters; third-party app data (OSINT) | Warning: definitions changed, metric dropped, time spent shifting to competitors |
| Cash content spend vs amortization | Streaming profits can lag cash; long amortization lives flatter earnings | Cash-flow statement (Item 8); content-asset note | Heuristic: cash spend persistently above amortization means FCF trails reported earnings |
| Capex intensity | Network (5G, fiber) and data-center build phases consume FCF | Cash-flow statement; MD&A capital plan; earnings call guidance | Warning: capex guide raised without revenue line of sight; depreciation surge ahead |
| Spectrum holdings & auction payments | Capacity for growth; large lumpy outflows outside capex | Item 1 Business; intangibles note; FCC auction results | Warning: debt-funded auction wins; spectrum excluded from the "FCF" management highlights |
| Net debt / EBITDA; FCF dividend coverage | Telecom and cable are levered; dividends compete with capex | `fundamentals.json` `valuation.net_debt_ebitda`; debt note; dividend history | Heuristic: dividend covered by script FCF with cited spectrum payments named alongside (not netted); leverage stable or falling |
| SBC and net share count | SBC is a real cost; buybacks may just offset dilution | Cash-flow statement; equity note; share count trend in `fundamentals.json` | Warning: SBC rising faster than revenue; buybacks large but share count flat |
| Regulatory exposure | Antitrust remedies, privacy rules, FCC and content regulation can reset economics | Item 1A Risk Factors; Item 3 Legal Proceedings; regulator releases | Warning: pending remedies touching core revenue; adverse rulings in key markets |

## 3. Valuation methods

**What fits**
- **DCF on FCF and course NOPAT** (grid `dcf_fcf` / `dcf_nopat` cells) for platforms and telecoms with
  stable cash flows. For telecoms, the FCF cells are the anchor; name cited spectrum payments next to them (they
  sit outside capex). For platforms, FCF is flattered by SBC (added back in operating cash flow) — say
  so in `method` and prefer lower-growth cells when SBC is material.
- **EV/EBIT and P/E** (grid `ev_ebit` / `pe` cells, band `valuation.multiple_bands.communication_services`)
  work for mature platforms and telecoms. EV/EBIT is the better comparator when net cash or net debt is
  large relative to market cap.
- **FCF yield and dividend coverage** for telecoms and cable: `fundamentals.json` provides
  `valuation.fcf_yield_pct`; dividend coverage is judged qualitatively from cited dividends and FCF.
- **Sum-of-the-parts** for conglomerates: core profits subsidize loss-making bets and listed stakes.
  The grid values the consolidated whole — note in `method` when a loss-making segment or an equity
  stake makes it misleading; segment values need cited sources.
- **Reverse DCF** (`reverse_dcf.implied_growth_pct`) frames the expectations embedded in high-multiple
  platforms; compare it with sourced engagement and ad-market growth, not with last year's revenue.

**What misleads**
- EBITDA-based thinking where **content amortization** sits inside D&A (EBITDA balloons, course NOPAT
  overstated) — check whether the D&A tag includes content or program amortization (§8).
- P/E for streaming and studios in transition years (amortization timing, impairments, restructuring).
- EV/EBIT for cable and telecom ignores **lease liabilities** (towers, fiber) and spectrum obligations.
- Telecom "FCF" headlines that exclude spectrum, device financing or securitized receivables.

**Reading the grid** — `cells_base` should combine a DCF cell and a multiple cell; pick the growth tier
consistent with subscriber and ARPU trends (telecoms rarely justify the high tiers). A `tv_gt_90` flag
on a platform means the value sits in the exit multiple — state the assumption explicitly.

## 4. Macro sensitivities

| FRED id | Direction | Mechanism |
|---|---|---|
| PAYEMS | positive | Ad budgets follow employment and consumer spending |
| UNRATE | negative | Ad cuts, subscription churn, downgrades to cheaper plans |
| DGS10 | negative | Long-duration platforms and bond-proxy telecoms both de-rate as long yields rise |
| BAMLH0A0HYM2 | negative | Levered telecom, cable and media refinance at wider spreads |
| DTWEXBGS | negative | Large international revenue translates lower in a strong dollar |
| CPIAUCSL | mixed | Enables subscription price increases; raises content, labor and network costs |
| VIXCLS | negative | High-beta media and entertainment sell off in risk-off episodes |

- Advertising is the most cyclical revenue line in the sector — it tends to fall earlier and recover
  faster than GDP. Use `chg_3m` / `chg_12m` of PAYEMS and UNRATE in `macro.json` as direction checks.
- Telecoms are defensive in demand but sensitive to rates through leverage and dividend-yield
  comparisons; platforms are growth-sensitive through valuation duration.
- Not in the FRED set: ad-market forecasts, smartphone cycles, spectrum auction calendars — cite
  web sources with dates.

## 5. Typical failure modes

1. **Secular decline bought as value.** Linear TV, print and fixed voice trade at low multiples for
   years while subscribers and pricing erode — cash flows shrink faster than the multiple re-rates.
2. **Ad recession.** Ad-funded companies see revenue fall abruptly while costs stay fixed.
3. **Content cost inflation.** Sports rights and premium content bid up; subscription pricing lags.
4. **Telecom arms races.** Price wars, handset subsidies, network and spectrum spending with no
   ARPU uplift, financed with debt.
5. **Leveraged media M&A.** Mega-mergers priced on synergies, then impairments, asset sales and
   dividend cuts.
6. **Platform risk.** Mobile OS privacy changes that weakened ad targeting, antitrust remedies, and
   user attention migrating to newer apps have each reset platform economics in the past.
7. **Capex surges with unproven returns.** Data-center, fiber or 5G builds raise depreciation for
   years; returns arrive late or not at all.
8. **Dividend traps.** High-payout telecoms with rising leverage cut the dividend once capex resumes.

## 6. Red flags for the Devil's Advocate

- Subscriber or engagement metrics redefined or no longer disclosed.
- ARPU growth driven only by price increases while churn or downgrades rise.
- Ad growth from more ad load with falling price per ad; reliance on one ad format or region.
- Capex guidance raised repeatedly without matching revenue guidance; useful-life extensions that
  lower depreciation.
- Content amortization lives lengthened; cash content spend well above amortization.
- "Adjusted FCF" excluding spectrum, device financing, securitization or lease payments.
- SBC growing faster than revenue; buybacks that do not reduce the share count.
- Pending antitrust, privacy or FCC proceedings touching core revenue; large fines provisioned.
- Leverage above the company's own target with a dividend payout funded by debt.
- Dual-class control insulating management from shareholders on capital allocation.

## 7. Lens checklist

- **fundamental**
  - Choose DCF cells consistent with subscriber/ARPU or engagement trends; justify the tier.
  - Adjust the reading for SBC, spectrum and content cash in `method` and `quality_notes`.
  - Telecom/cable: leverage, maturities, dividend coverage in `balance_sheet_notes`.
  - Check `share_count_ambiguous` (multi-class shares) before citing per-share values.
- **technical**
  - Use only `technicals_<T>.json`: regime, levels, RS vs XLC and SPY, stop candidates.
  - XLC is mega-cap dominated — RS vs XLC for the mega-caps is close to RS vs themselves; also weigh RS vs SPY.
  - Earnings gaps are common in platforms; ATR-based stops may sit inside a typical gap.
- **operator_ceo**
  - Capital allocation: buybacks vs capex vs M&A; discipline in content and spectrum bidding.
  - DEF 14A: dual-class control, founder incentives, metrics (revenue/EBITDA vs per-share FCF, ROIC).
  - Guidance record on subscribers, ARPU, capex; Form 4 selling patterns (10b5-1 plans vs discretionary).
- **industry_ops**
  - Network quality and coverage vs rivals; spectrum depth; fiber or data-center build execution.
  - Content pipeline and hit-rate; rights-renewal calendar; distribution deals and carriage disputes.
  - Unit economics: ARPU minus cost-to-serve, customer acquisition cost, ad load vs engagement.
- **osint**
  - App-store rankings and reviews, engagement trackers, outage reports, customer complaints.
  - Regulator dockets (FCC, antitrust, privacy authorities), court rulings, legislation.
  - Talent and hiring trends, layoffs, creator or advertiser boycotts, content controversies.
  - Raise `must_address` flags on regulatory remedies or metric-disclosure changes.

## 8. Data notes

- **Multi-class shares** (several large names have two or three listed classes): `fetch_fundamentals.py`
  sums `dei` share counts by default and cross-checks against yfinance market cap; a
  `share_count_ambiguous` flag means per-share values may be off. `etf_overrides.yaml:
  securities.<T>.share_class` can fix it — never adjust per-share numbers by hand.
- **Content amortization:** streaming and studio companies report content additions inside operating
  cash flow and amortization separately; if the script's D&A tag includes content amortization, EBITDA
  and course NOPAT are inflated — check `tag_map` and the cash-flow statement.
- **Spectrum** purchases are investing outflows outside the capex tags (`PaymentsToAcquireIntangibleAssets`
  or company-extension tags); script FCF excludes them. Licenses often sit in indefinite-lived intangibles
  or extension tags, so `intangibles` may be partial.
- **Leases:** tower and fiber leases are operating-lease liabilities (`OperatingLeaseLiability`) excluded
  from `total_debt` and EV.
- **SBC** is added back in operating cash flow, so FCF and FCF yield look better than owner economics;
  `ShareBasedCompensation` gives the size.
- **Subscribers, ARPU, churn and engagement** are not in us-gaap XBRL (sometimes company-extension tags);
  read them from earnings releases and trending schedules and cite them as W-sources.
- **Fund data:** `yf_funds("XLC")` returns the top 10 holdings only (several are share classes of the
  same company); the Phase 3 universe is those plus `universe.yaml` additions.
