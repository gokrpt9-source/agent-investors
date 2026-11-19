---
sector: funds
name: Funds (ETFs)
benchmark_etf: SPY
sector_class: fund
key_kpis:
  - "Role in portfolio (core / satellite / factor_tilt / hedge / redundant)"
  - "Expense ratio and all-in cost (tracking difference, spread, turnover, tax drag)"
  - "Top-10 concentration"
  - "Overlap with other funds and with direct holdings (top-10 lower bound)"
  - "Look-through sector contribution vs max_sector_pct (CIO targets and macro bounds: sector head / CIO)"
  - "Relative strength vs SPY (3/6/12m) from technicals"
  - "Index methodology: selection, weighting, caps, rebalance cadence"
  - "Position size vs max_fund_position_pct"
valuation_methods:
  - "No DCF or fair value: a fund is judged on role, cost, concentration, overlap and fit"
  - "Look-through sector exposure vs max_sector_pct (the sector head and CIO add macro bounds and CIO targets)"
  - "Relative strength and drawdown context from technicals (no rr test for funds)"
macro_sensitivities:
  - series: DGS10
    direction: negative
    mechanism: "Growth-heavy indexes such as the Nasdaq-100 are long-duration and de-rate when long yields rise"
  - series: VIXCLS
    direction: negative
    mechanism: "Volatility spikes coincide with drawdowns and with sharp leadership reversals that hurt momentum funds"
  - series: BAMLH0A0HYM2
    direction: negative
    mechanism: "Widening credit spreads mark risk-off regimes that hit equity funds broadly"
  - series: T10Y2Y
    direction: mixed
    mechanism: "Curve regime shifts often accompany leadership rotation, which momentum funds catch only at the next rebalance"
  - series: DTWEXBGS
    direction: mixed
    mechanism: "USD funds held by an ILS-based investor; broad dollar moves also change mega-caps' translated foreign revenue"
failure_modes:
  - "Several funds that are the same mega-cap bet (hidden concentration)"
  - "Momentum crash in a sharp rebound after a drawdown"
  - "Rebalance lag: a momentum fund holding stale winners between rebalances"
  - "Chasing a factor after its best run (crowding)"
  - "Cost and tax drag ignored (domicile, withholding, turnover)"
  - "Index methodology rules (caps, special rebalances) forcing concentrated selling"
  - "Tracking error, fund closure or merger, and illiquid share classes"
edgar_tags_of_interest: []
---

# Funds (ETFs) — ETF Playbook

> For the `etf-analyst` (role `etf`, schema `etf_review`) and the `funds` sector head. Rules of thumb
> are labeled **heuristic**. No market figures appear here — expense ratios, weights and returns come
> from the run's data files (`lookthrough.json`, `technicals_<T>.json`) or cited issuer pages.

## 1. Role in portfolio

`role_in_portfolio` is one of five values; pick the one that describes the job the fund does *in this
portfolio*, not in general.

- **core** — broad, low-cost exposure the investor would hold regardless of views (total-market or
  large-cap blend trackers). A Nasdaq-100 tracker is core only if the CIO intends a growth-tilted core;
  say so explicitly.
- **satellite** — sector, thematic or regional fund held for a specific view; sized smaller than core.
- **factor_tilt** — a systematic tilt (momentum, quality, value, low volatility, size). Momentum funds
  such as SPMO are the textbook case: the tilt, not the stock list, is the thesis.
- **hedge** — reduces a specific risk (rates, equity drawdowns, currency). Judge it on behavior in
  stress, not on standalone return.
- **redundant** — overlaps so heavily with another holding that it adds little diversification. A
  redundant fund is a consolidation candidate, but consolidation realizes gains (see §7) — weigh it.

Questions to answer in the thesis: what job does the fund do; what else in the portfolio already
does that job; does its look-through sector exposure stay within `max_sector_pct`?
The etf analyst's work order carries `lookthrough.json`, technicals, `prices.json`,
`investor_profile.yaml` and this playbook — not the CIO allocation or the macro view. The analyst
therefore never states CIO target weights or macro sector bounds (never from memory); fit against
them is tested in the portfolio plan's constraint checks and judged by the CIO, whose packet carries
the preview sector table and the macro bounds.

## 2. Expense ratio & costs

- **Expense ratio** — `lookthrough.json#/funds/<T>/expense_ratio_pct` (percent). It is a required
  field for the etf role; cite it with `data_ref` and copy it exactly.
- **Tracking difference** (fund return minus index return over years) is the true all-in cost; read it
  from the issuer's site (cite) — it can differ from the expense ratio because of securities lending,
  cash drag and withholding.
- **Trading costs** — bid-ask spread and premium/discount to NAV (issuer page). Large US ETFs are
  liquid; small or foreign-listed share classes may not be.
- **Turnover** — momentum and other factor funds trade more at each rebalance; those costs sit inside
  returns, not in the expense ratio.
- **Tax drag** — dividend withholding depends on fund domicile and the investor's residence (§7).
- **Cheaper twins** — several popular indexes are offered by the same issuer in a lower-fee share
  class or fund (a Nasdaq-100 twin exists). A switch realizes gains and triggers tax.
- **Heuristic:** a cost gap justifies switching only if, compounded over `horizon_years`, it exceeds the
  tax and trading cost of the switch. For a factor tilt, judge the fee against a plain index
  alternative: the tilt has to earn its extra cost.
- **What counts as "high" depends on the kind of fund (heuristic):** broad cap-weighted trackers are
  commodity products — anything clearly above the cheapest comparable tracker of the same index is
  high. Factor and sector funds legitimately cost more, but the premium must be explained by the
  strategy. Thematic, leveraged or active funds carry the highest costs and the weakest case for a
  long-horizon holding. Compare against peers via cited issuer pages, never from memory.
- **Fund-level risks beyond cost:** tracking error versus the index, fund closure or merger (small
  funds are closed, forcing a taxable sale), index-provider methodology changes, and thin trading in
  small or foreign-listed share classes (wide spreads, premiums/discounts in stress).

## 3. Concentration (top-10)

- **Metric:** `lookthrough.json#/funds/<T>/top10_concentration_pct` (sum of the top-10 holding weights,
  percent; required for the etf role).
- **Reading it:** when the top 10 are a large share of the fund, the fund behaves like a basket of a
  few companies — its risk is those companies' earnings, valuation and regulation risk.
- **Heuristic:** once the top-10 weight approaches half of the fund, analyze it as a concentrated
  portfolio: name the top holdings and their GICS sectors in the thesis.
- **Index rules shape concentration.** Modified cap-weighting, single-name caps and special rebalances
  can force selling of the largest names at awkward times. Read the index methodology (cite the index
  provider's document) before asserting caps or cadences.
- Concentration differs from overlap: a concentrated fund may still be unique in the portfolio, and two
  diversified funds can be redundant with each other.

## 4. Overlap math & look-through sector caps

**Pairwise overlap** (`lookthrough.json#/overlap/pairs`, `basis: top10`):
`overlap_pct(A, B) = Σ over names in both top-10 lists of min(weight in A, weight in B)`.
Only the top 10 of each fund are visible, so this is a **lower bound** — common names ranked 11+ can
only add to it. A high top-10 overlap is already decisive; a low one does not prove independence.

**Direct overlap** (`lookthrough.json#/overlap/direct_overlap`): a stock held directly that is also in
a fund's top 10 (`fund_weight_pct` = its weight inside that fund). The investor's effective exposure is
the direct weight plus the indirect weight through each fund. The script does not total it; agents do
not compute it in prose — flag it (with the cited inputs) when it could matter against
`max_position_pct`.

**Look-through sector caps**
- `lookthrough.json#/sector_weights_pct` decomposes funds into sectors; `max_sector_pct`
  (`investor_profile.yaml`), the macro view's sector bounds and the CIO target weights all apply to
  these look-through weights. **Who checks what:** the etf analyst tests only `max_sector_pct` (the
  one bound in its inputs); the CIO targets and macro bounds are tested in the portfolio plan's
  constraint checks and reach the CIO through the CIO packet (a fund's own sector packet has no CIO
  target, since `funds` is not a GICS sector).
- Funds are often what pushes a sector over its cap. Report each fund's top sectors
  (`lookthrough_summary.top_sectors` with `data_ref` to the holding's `sector_split`).
- "Tech" is split across three GICS sectors — Information Technology, Communication Services and
  Consumer Discretionary hold the mega-cap platforms — so a cap on IT alone understates the bet.
- Fund weights that do not sum to 100 leave a residual in `unclassified`; say so if it is material.

## 5. Factor-ETF specifics

**Momentum funds (e.g. SPMO)**
- *Methodology (verify against the current index document before citing):* the S&P momentum index
  family ranks parent-index stocks by risk-adjusted price momentum over roughly the past year,
  excluding the most recent month, and weights the selection by size and momentum score with caps.
- *Rebalance cadence:* the index rebalances on a fixed periodic schedule (semi-annual at the time of
  writing — verify). Between rebalances the fund keeps yesterday's winners; after an abrupt reversal
  it can hold the wrong leaders for months.
- *Crowding:* momentum leadership is often a handful of mega-caps or one theme, so the fund can end up
  with the same names as QQQ or SPY's top holdings — check `overlap.pairs` before calling it a
  diversifier.
- *Momentum crashes:* historically the factor's worst losses came in sharp rebounds after deep
  drawdowns (past losers rally hardest) and in sudden leadership rotations. VIX spikes and a technical
  regime turning from `trend_up` to `transition` or `choppy` are the observable warnings here.
- *Timing:* there is no reliable way for the committee to time the factor; treat it as a strategic
  tilt with a size limit, and judge it on whether the tilt still fits the portfolio.

**Nasdaq-100 trackers (e.g. QQQ)**
- *Index:* the largest non-financial companies listed on Nasdaq, modified market-cap weighted,
  periodically rebalanced and reconstituted annually. Membership is decided by listing venue, not by
  sector — the fund holds essentially no financials.
- *Tech concentration:* mega-cap platforms dominate across IT, Communication Services and Consumer
  Discretionary; the top-10 weight is the key risk number.
- *Rate duration:* long-duration growth earnings make the fund sensitive to DGS10 and real yields.
- *QQQ + SPMO together:* when momentum leadership sits in mega-cap tech, the two funds are one bet —
  the overlap pair and the look-through IT weight will show it.

## 6. How the deterministic fund rules work

- **Who reviews a fund:** `roles.phase2_fund` (etf + technical; `roles.quick_fund` in `--quick`).
  Devil's Advocate runs on funds only if `roles.da_for_funds` is true. Funds never enter Phase 3.
- **Evidence:** etf reports are scored on `evidence.required_fields.etf` (expense ratio, top-10
  concentration, action) plus the generic freshness, source-count and missing-data rules.
- **Conviction:** the evidence-weighted mean over the etf and technical reports (bearish stances
  inverted); DA adjustment applies only if DA ran.
- **No rr test:** funds have no fair value, so risk/reward is not applicable. ADD needs agg_conviction
  ≥ `actions.holding.add_conviction`, agg_evidence ≥ `add_evidence`, no unresolved high flag, and a
  position below the cap.
- **Cap:** `max_fund_position_pct` (investor profile) replaces `max_position_pct`. A fund's
  `limit_breach` is position > that cap; it triggers TRIM.
- **Look-through sector breaches become warnings:** for stocks, a sector above
  `min(max_sector_pct, CIO target) + sector_breach_tolerance_pp` triggers TRIM; for funds it does not.
  The breach is surfaced in the portfolio plan's constraint checks and left to the funds sector head
  and the CIO. The etf analyst names any look-through sector above `max_sector_pct` and cites the
  fund's `sector_split` and position weight behind it (no prose arithmetic); the CIO-target and
  macro-bound comparisons are made in the portfolio plan's constraint checks and by the CIO, whose
  inputs carry them. Trimming for a sector breach is a rule deviation that needs a written
  justification.
- **Other precedence** (same as stocks): insufficient evidence caps at HOLD (TRIM if breached); SELL if
  agg_conviction ≤ `sell_max`, or if the Devil's Advocate verdict is `broken` (only when
  `roles.da_for_funds` is on — the sector head can rebut it only through a justified rule deviation);
  TRIM if between `sell_max` and `trim_below`; else HOLD. A broken DA verdict therefore forces SELL even
  when agg_conviction would otherwise give HOLD.
- **Sizing** (`portfolio.py`): ADD raises the weight by up to `sizing.add_step_pp` within the cap and
  sector headroom; conviction TRIM sells `sizing.trim_fraction`; funds are never sold to fund stock buys.
- The report's `action` is the analyst's view; the rule output governs unless the sector head records
  a deviation with justification.

## 7. Israeli-investor considerations

Qualitative notes only — the committee does not give tax advice; the investor should consult a
licensed Israeli tax advisor or investment professional before acting on any of this.

- **Currency:** the investor reports in ILS; USD funds carry USD/ILS exposure (`prices.json` `fx.usdils`).
  Returns in ILS can diverge from USD returns.
- **US-domiciled ETFs (e.g. QQQ, SPMO):** dividends paid to non-US persons are subject to US withholding
  tax (a treaty rate may apply with the right forms), and US-situs assets held by non-US persons can be
  exposed to US estate tax with only a small exemption — material for larger holdings.
- **UCITS accumulating funds** (often Ireland-domiciled): dividends are reinvested inside the fund and
  withholding is handled at fund level under the domicile's treaties; they are generally not US-situs
  for estate-tax purposes. Trade-offs can include higher fees, lower liquidity and tracking differences.
- **TASE-listed funds** tracking the same index can be entered with `proxy_ticker` in `portfolio.yaml`;
  the look-through then uses the proxy, so fee, hedging and tracking differences are not captured.
- **Proxy pricing distorts weights entered as shares.** A holding given as `shares` is valued at
  shares × the proxy's latest USD close, but TASE fund units trade at prices unrelated to the proxy
  (QQQ, SPMO). The position weight is only right if the holding was entered as `weight_pct` (or as
  proxy-equivalent shares). If `lookthrough.json` shows `proxy_priced` in the holding's `flags` and the
  `proxy_ticker` flag says shares are valued at the proxy's price, treat the position weight — and so
  the `max_fund_position_pct` cap test and the fund's look-through sector contribution — as unreliable,
  and say so in `missing_data`.
- **Israeli tax** applies to real (inflation/FX-adjusted) gains; consolidating or switching funds
  realizes gains. `portfolio.py`'s tax estimate is indicative only.

## 8. Data notes

- **Top 10 only:** `yf_funds` (Yahoo `funds_data`) exposes the top 10 holdings. Concentration is exact
  for the top 10; overlap is a lower bound; holdings 11+ are unknown — say so in `missing_data` if the
  conclusion depends on them.
- **Expense ratio units:** `info.netExpenseRatio` is already in percent (0.20 means 0.20 %, not 20 %).
  The fallback `fund_operations` "Annual Report Expense Ratio" is a fraction that `common.yf_funds`
  converts to percent. `expense_ratio_field` records which one was used — never convert again.
- **Sector weightings** are fractions under Yahoo keys (`technology`, `realestate`, ...) mapped by
  `common.YF_FUND_SECTOR_MAP` and renormalized to 100; Yahoo's taxonomy is Morningstar-style, not
  GICS, so look-through sectors are approximate at the edges.
- **Fallbacks:** `etf_overrides.yaml: funds.<T>` (sector weights with `source_url` and `as_of`); if
  nothing is available the fund is 100 % `unclassified` with a `missing_data` entry.
- **Momentum vs benchmark:** copy `rs_3m/6m/12m` from `technicals_<T>.json#/relative_strength/vs_spy`
  with `data_ref`; do not recompute.
- **Staleness:** Yahoo's holdings snapshot can lag the issuer's latest disclosure; cite the issuer's
  holdings page when the date matters.
