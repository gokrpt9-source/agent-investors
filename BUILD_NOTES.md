# BUILD_NOTES — judgment calls, deviations, known gaps

The original requirements are in `docs/SPEC.md`. The design contract that interprets them is
`docs/DESIGN.md`, and `CONTRACTS.md` describes the `scripts/common.py` API as actually built.
This file records every deliberate choice where the spec was ambiguous, every departure from it,
and the limits that are known today.

## 0. Build context
- **Location.** The repo was built at `C:\Users\mrkrp\agent-investors`, a new sibling of
  `smart_portfolio`, because that folder was not empty. The user chose this location. It is a
  local git repository with milestone commits and no remote; nothing is ever pushed.
- **Python.** The machine runs Python 3.13.9 with pandas 3.0.1, numpy 2.x, yfinance 1.2.0 and
  jsonschema 4.26. The code is kept 3.11-compatible: every script parses with
  `ast feature_version=(3,11)` and uses no 3.12+ syntax.
- **SEC email.** `sec_contact_email` is `researcher@gmail.com`, chosen by the user. It is sent only
  to sec.gov, inside the EDGAR User-Agent header.
- **WebFetch.** `.claude/settings.json` allows WebFetch for every domain, at the user's choice, so
  that parallel agent waves don't stall on permission prompts. Agents never get Bash, can write
  only their work-order output, and treat web text as data rather than instructions.
- **Who built it.** The build itself ran as parallel Claude Code subagents with strict file
  ownership, each followed by an adversarial verifier and a fix pass. The system's own committee
  agents all use `model: sonnet`, and a lint test enforces this.

## 1. Data sources (hard constraint 1)
- **Stooq is behind a JavaScript proof-of-work bot wall.** Since 2026 `stooq.com/q/d/l/` has
  served one instead of CSV. `fetch_prices` detects the page (HTML body, script or challenge
  markers), records the flag `provider_blocked`, and moves on to the stale cache. It never tries
  to solve or get around the challenge. The Stooq path is still implemented and tested in case
  plain CSV returns. In practice yfinance is the only live price source, with a
  stale-but-flagged cache behind it.
- **Prices are auto-adjusted.** yfinance history is fetched with `auto_adjust=True`, which adjusts
  for splits and dividends. The last close equals the actual last close.
- **SEC companyfacts can lag the latest filing.** When it was verified, KO's companyfacts ended at
  the Q1-2026 10-Q even though later filings existed. `fetch_fundamentals` compares companyfacts
  with the submissions endpoint and sets `lag_detected`. That produces the high flag
  `companyfacts_lag` and a `missing_data` line pointing agents at the filing itself. The evidence
  scorer then marks those fundamentals stale if they were filed more than 120 days ago.
- **XBRL tag drift.** For example, KO stopped using `LongTermDebt` in 2024. Debt is therefore
  built from an ordered list of formulas with a 400-day recency rule (DESIGN §4.3 step 4), and
  every concept carries the tag that was used plus a `stitched`/`tag_mix` flag.
- **Fund composition and expense ratios.** yfinance `funds_data` returns only the **top 10**
  holdings. Fund overlap is therefore a lower bound (`basis: top10`), and the Phase-3 screen
  universe per sector is the benchmark ETF's top 10 plus any `universe.yaml` additions. A small
  universe yields fewer than 6 candidates, and the shortlist is never padded. yfinance
  `netExpenseRatio` is already a percentage; `fund_operations` gives a fraction. Both are
  normalized to percent, and the field used is recorded.
- **Sector classification.** Yahoo's sector labels follow a Morningstar-like scheme, not GICS.
  They are mapped to the 11 GICS keys (`common.YF_SECTOR_MAP`). The fallback is an approximate
  SEC SIC-range map (`common.sic_to_sector`), and the memo caveats the classification.
- **Non-USD quotes (Tel Aviv).** Yahoo quotes TASE stocks in agorot (`ILA`, 1/100 ILS). The first
  live run priced Bank Leumi's `7522` agorot as `$7,522`, so its dollar volume and proposed trade
  size were wrong. `fetch_prices` now reads the quote currency (Yahoo `info['currency']`, then a
  suffix table: `.TA` → agorot) and converts ILS/ILA series to USD at the spot USD/ILS rate applied
  to the whole history. The cache keeps the native quote plus its currency. Returns, RSI and trend
  are therefore those of the local price, while USD levels exclude historical FX moves (flag
  `converted_to_usd`). A `native` block in each prices.json entry carries the ILS price. Any other
  non-USD or unknown currency (pence, euros, ...) is **refused** (`failed{}`), never priced as USD,
  and `load_series` returns None rather than a wrong-unit price when the FX rate is not cached.
  Leumi is now entered by shares (45) with an ILS cost basis (ILS 67.1533 per share), and the tax
  estimate for such holdings is computed in ILS, the currency Israeli tax is calculated in. The two
  TASE funds are still priced through their US proxies (QQQ, SPY), because Yahoo has no data for
  them.
- **Point-in-time inputs.** Data dated after `as_of` (price bars, SEC facts, FRED observations)
  is ignored so that nothing looks ahead. Yahoo `marketCap` in the screen is not point-in-time;
  this is a known gap.

## 2. Provenance (hard constraint 4)
- **Every number is a Fact.** Each numeric value in a script output is a
  `Fact {value, unit, source_id, as_of, kind, na_reason}`, and each file's envelope lists
  `sources[]` with deterministic ids (`D:PX:KO`, `D:SEC:KO:CF`, `D:FRED:DGS10`, ...) and the
  upstream http(s) URL. This satisfies "source URL + as-of for every value" through the id
  mapping, without repeating the URL on every number.
- **Agent figures are checked against the data.** An agent key figure that cites a dataset needs
  a `data_ref` JSON pointer, and `validate.py` compares it with the data file within 0.5%. A
  mismatch triggers one retry with the errors listed; a second failure marks the agent failed
  (evidence 1). This is the main defense against figures recalled from memory.
- **Valuation multiple bands are assumptions.** They live in `committee.yaml` and are labelled as
  configurable assumptions, not market data. Each multiple cell carries `assumption: true`.
  Because no http source exists for them, no `D:CFG` source entry is emitted.

## 3. Deterministic math (hard constraint 5)
- **Added script `scripts/valuation.py`.** The spec requires valuation arithmetic in Python but
  lists no valuation script. This one builds:
  - a reverse DCF;
  - a DCF grid on "course NOPAT" `(EBITDA − capex)·(1−t)` and on FCF, with r ∈ {9%, 10%} ×
    5 growth tiers × an exit multiple chosen by growth (the user's course method);
  - P/E, EV/EBIT and (for financials) P/B multiple cells.

  The fundamental analyst cites cell ids for low/base/high. Its fair values must equal the mean
  of the cited cells within 0.5%, which the validator recomputes. The agent averages two to four
  script numbers, and Python verifies the result.
- **Technical invalidation.** `compute_technicals` emits ids for levels (`S#`, `R#`) and stop
  candidates (`STOP#`). The technical analyst's invalidation must equal one of them, and its
  `trend_regime` must equal the computed regime.
- **Devil's Advocate bear case.** `bear_case_price` must equal a valuation cell or a technical
  level, referenced through `bear_case_ref`.
- **Indicators are hand-written.** They avoid pandas_ta and ta-lib, which break on numpy 2 and
  pandas 3. They are ported from `smart_portfolio/stock_dashboard` with fixes:
  - RSI is Wilder RSI seeded with an SMA, and a flat series gives 50;
  - `sma200` needs 200 bars;
  - the ATR percentile looks back over 252 bars only (no look-ahead);
  - relative strength is computed on date-aligned series.

## 4. Decision-rule interpretations (SPEC §5)
- **Rounding.** `agg_conviction` and `agg_evidence` are rounded half-up to 1 dp (Decimal) before
  any comparison, and rr to 2 dp. A raw 6.95 therefore rounds to 7.0 and passes ADD; the boundary
  tests document this.
- **Neutral stance counts as 5.** The spec only defines the bearish inversion (`10 − c`). Without
  this rule, a strongly held neutral view would read as bullish. Configurable via
  `aggregation.neutral_value`.
- **OSINT** is left out of the conviction mean; its role is to raise flags (spec: "not a score
  that gets averaged"). The Devil's Advocate applies its adjustment and is also left out of the
  mean.
- **Failed agents** are excluded from conviction but count as evidence 1. If the DA was never
  run, it adds 0 and counts no evidence. If the DA was expected but failed, it adds 0 and counts
  as evidence 1.
- **TRIM band.** "TRIM: agg_conviction 4" is implemented as the band `3.0 < c < 4.5`
  (`actions.holding.trim_below`). "SELL ≤ 3" is inclusive.
- **Holding precedence:**
  1. no conviction or evidence < 5 → HOLD ("insufficient evidence"), or TRIM if a limit is breached;
  2. SELL (conviction ≤ 3 or DA "broken");
  3. limit-breach TRIM;
  4. ADD;
  5. conviction TRIM;
  6. HOLD.

  "DA broken not rebutted by the Sector Head" is handled as follows: the rule says SELL, and the
  sector head may rebut through a justified deviation with `da_rebuttal`.
- **What is "addressed".** OSINT red flags carry ids. The DA (and any specialist) answers them in
  `flag_responses[]`. A flag is resolved when an answer is marked mitigated or refuted.
  Confirmed, high-severity, must-address OSINT flags remain unresolved and block ADD.
- **Downside stop for rr.** It is the technical analyst's invalidation if that lies below the
  price; otherwise the highest computed stop candidate; otherwise the median of the other
  specialists' invalidations. A bearish analyst's invalidation above the price is normal and
  ignored. A bullish or neutral analyst whose invalidation is at or above the price raises
  `auto:invalidation_breached` (high, unresolved).
- **The fundamental analyst may leave `invalidation_price` null.** Its kill condition is usually a
  business metric, stated in `invalidation_rationale`, so it is not a required field.
- **Funds** have their own rules (DESIGN §4.10):
  - ADD needs no rr test, because funds have no fair value;
  - a separate `max_fund_position_pct` (40%) applies;
  - a look-through sector breach caused by funds is a warning and never triggers an automatic
    fund TRIM;
  - the DA does not review funds by default (`roles.da_for_funds: false`);
  - a fund that is over its cap but has low conviction sells (SELL precedes breach TRIM).
- **Candidates.** BUY follows the spec. Unresolved high flags do not block a candidate, because
  the spec doesn't say they should (`candidate_block_on_unresolved_high_flag: false`, which can be
  switched on).
- **Sector caps.** "position/sector exceeds profile limits or CIO target" uses look-through sector
  weight > min(max_sector_pct, CIO target) + 1 pp tolerance, and applies only to directly held
  stocks. Sizing trims the lowest-conviction stocks first.
- **"Not assessed" counts as null.** An operator or industry-ops assessment written as
  "Not assessed: <reason>" (the only schema-valid placeholder) counts as a null required field
  (−2).

## 5. Portfolio and tax
- **Sizing (configurable):**
  - SELL → 0;
  - conviction TRIM → `min(cap, cur·(1−0.33))`;
  - breach TRIM → `cap − 0.5pp`;
  - ADD → +2pp, limited by cap and sector headroom;
  - BUY → 3%;
  - buys are scaled pro-rata so that post-trade cash stays at or above the target;
  - trades below `min_trade_usd` are dropped and listed;
  - whole shares only;
  - funds are never sold automatically to pay for stock buys.
- **Israeli tax.** The estimate is 25% of the **nominal USD gain**, and is shown only when a cost
  basis exists. Israeli capital-gains tax is levied on the real gain (adjusted for CPI/FX), so the
  memo labels this an estimate and suggests consulting a tax advisor. This is not advice.
- **USD/ILS** comes from Yahoo `ILS=X`, and trade values are shown in ILS. The user's real
  holdings include TASE UCITS funds; `portfolio.yaml` supports `proxy_ticker` (for example, a
  NASDAQ-100 tracker → QQQ) for pricing and look-through.

## 6. Orchestration (hard constraint 3)
- **`committee.py next` drives the run.** It is an idempotent, resumable state machine: it runs
  Python steps itself and hands agent work to the main session as **work-order files**. The
  agent prompt is one line, `Execute work order <path>`, and the agent replies
  `DONE <path>` or `FAILED <reason>`. The main session's context stays small over the ~100 agent
  calls of a full run.
- **Retries** write `<role>_<T>.r2.json`, because the Write tool cannot overwrite a file it has
  not read. After the last attempt, a schema-valid `.failed.json` stub is written. Reports are
  never fabricated.
- **Orchestrator contract.** The orchestrator calls `next` only after every emitted order has
  finished, and a missing output counts as a failed attempt. Use `redispatch` for crash
  recovery; it does not use up an attempt.
- **Funds** form a pseudo-sector `funds`. They are reviewed by the etf-analyst (and the technical
  analyst), and the sector-head is called once for funds using `knowledge/etf_playbook.md`.
- **Inputs differ by role, which keeps the lenses distinct:**

  | Role | Reads |
  |---|---|
  | technical | only its technicals file |
  | fundamental | fundamentals + valuation + playbook (+ filings) |
  | operator / industry-ops / OSINT | fundamentals + playbook + their own web research |
  | DA | every wave-A report + the data files |

- **Phase-3 cap.** `phase3.max_expand_sectors` (2) limits the number of expand sectors, and any
  sector dropped is logged in the manifest.
- **`/committee-holding`.** It reuses Phase 1 from the most recent run that is at most 7 days old.
  A ticker not held is judged with the candidate (BUY/PASS) rules. It writes a trial memo,
  `phase4/trial_<T>.md/.html`.
- **Dry runs never touch real data.** A dry run uses fixture config, fixture data and a scratch
  cache, and writes its ledger rows to `phase4/ledger_preview.csv`, never to the real ledger.
- **Agents load at session start.** `.claude/agents` is read when Claude Code starts, so a live
  run needs a session opened in this folder after the build. The `/committee-run` preflight
  checks for this.

## 7. Ledger
- **One row per (run, ticker, role).** Roles are: committee (the CIO's final action), sector_head,
  each specialist, and the DA (stance derived from its verdict). This makes hit rate by agent
  role possible.
- **Scoring.** `score` compares excess return against SPY and the sector ETF:
  - ADD/BUY/bullish is a hit when excess > 0;
  - SELL/TRIM/bearish is a hit when excess < 0;
  - HOLD, PASS and neutral are excluded;
  - recommendations younger than `min_age_days` (20) are excluded.
- **CSV injection guard.** Text cells that start with `= + - @` are stored with a leading quote,
  and the ledger loader strips it again.

## 7b. Dry run (`--dry-run`)
- **Fixtures.** `tests/fixtures/dryrun/` holds a sample filled config, offline data fixtures and
  59 fixture agent outputs:
  - prices are 26 seeded synthetic series;
  - FRED series are synthetic;
  - SEC data is trimmed real filings for KO, JPM and BRK-B, plus synthetic filings for the rest.

  The fixtures are regenerated with `python tests/fixtures/dryrun/build_fixtures.py all`.
- **Fixture agent text is placeholder prose.** It is written programmatically from each run's own
  data files, so every cited number passes the validator. It is not real research.
- **What the dry run covers.** It exercises the full path with no network and no agents: 59
  orders, Phase 3 (health care: 1 BUY, 3 PASS), one sector-head deviation (PG: rule SELL → HOLD,
  rebutting a "broken" DA verdict), and one CIO macro-bound override (technology above its cap).
  Ledger rows go to `phase4/ledger_preview.csv`.
- **Integration fix it exposed.** A conviction TRIM was being sized as a sector-breach TRIM.
  `portfolio.py` now reads the aggregate `rule_label` to tell the two apart.
- **Reading negative rr.** A negative rr (for example KO at −43) means the base fair value is
  *below* the price; the downside to the stop is small, so the ratio is large and negative. This
  follows the spec's formula exactly. Any rr below 2.0 fails the ADD test.

## 7c. Investor profile (added 2026-10-04, after the first build)
The first build shipped placeholder profile values (`horizon_years: 10`, `risk_tolerance: moderate`).
They were replaced with the investor's own situation:
- **Facts recorded** in a new optional `investor` block of `config/investor_profile.yaml`
  (schema-checked): age, occupation, living costs, expected income and that the investor invests
  for life with no withdrawal date. (The investor's own values were removed from this published copy.)
- **Derived values:** `horizon_years: 55` (the investor accumulates for life) and
  `risk_tolerance: aggressive` (young, very long horizon, low fixed costs, rising income, and the
  investor's earlier statement of a relatively high tolerance in the `smart_portfolio` project).
  These two are inferences, not direct answers. Change them if they are wrong.
- **Not invented:** the monthly contribution amount (`contribution_plan_ils_per_month: null`).
- **Human-capital concentration.** A software career is likely tied to the technology sector, so a
  large technology weight stacks on top of the investor's income risk. This is recorded in
  `income_sector_correlation`, and the CIO and the ETF analyst are told to treat it as extra
  exposure and to name it as a risk. It is a diversification flag, not a sell instruction.
- **What the profile does and does not do.** `risk_tolerance` and the `investor` block inform
  agent judgment only. The deterministic rules (action thresholds, position/sector caps, sizing)
  read the explicit numeric limits, which were not changed: `max_sector_pct` 35, `max_position_pct`
  10, `max_fund_position_pct` 40. Retune those yourself if you want an aggressive profile to allow
  more concentration.
- **Who sees it:** the CIO (allocation and, new, reconciliation), the ETF analyst, and the memo
  (section 4 shows age, risk tolerance and horizon). Specialist agents (fundamental, technical,
  operator, industry-ops, OSINT, DA, sector heads) deliberately do not, so each lens judges the
  company on its own merits.
- **Test isolation.** Tests now run against a pristine template copy in
  `tests/fixtures/config_template/` (null holdings, placeholder profile) instead of the live
  config files, so editing your real portfolio or profile can no longer break the suite. (Filling
  in `portfolio.yaml` the first time did break 7 template-dependent tests; this fixes that.)

## 8. Known gaps / limitations
- **No live feeds.** Prices are daily closes (cached for up to 24h); fundamentals depend on the
  timeliness of EDGAR companyfacts; FRED monthly series lag by about 4–6 weeks.
- **`tv_gt_90` is effectively dormant.** With the current grid (5 years, exit multiples 10–22×,
  r 9–10%), the terminal-value share tops out near 0.83, so the >90% flag (the course's red flag)
  essentially never fires. Lower `valuation.tv_share_flag_threshold` if you want it active.
- **Reverse-DCF bias.** The reverse DCF uses a fixed exit multiple of 15×, while the grid ties the
  exit multiple to growth. Compare `implied_growth_pct` with history; don't compare it with
  individual cells.
- **EV for cash-rich companies.** EV ignores long-term investments (`MarketableSecuritiesNoncurrent`),
  so it is overstated for cash-rich tech names. E&P companies that tag capex as oil-and-gas
  property spend may get null capex, which leaves their DCF cells `na`.
- **Near-zero tax rates are not flagged.** A positive effective tax rate close to zero is not
  flagged, which can leave NOPAT almost untaxed.
- **Fundamental freshness is per figure.** It is measured from each figure's `filed` date, so
  history derived from a 10-K looks stale for part of the year. This is conservative.
- **Share counts for multi-class companies.** Counts such as BRK's are cross-checked against
  Yahoo `marketCap`. A divergence above 10% raises `share_count_ambiguous` and uses Yahoo's
  market cap; `etf_overrides.yaml` can set a class factor.
- **The screen** sees only the top 10 holdings of the benchmark ETF plus manual additions.
  Market cap is current, not point-in-time.
- **Banks and REITs.** Bank KPIs (NIM, CET1, credit costs) and REIT FFO are not standard XBRL
  tags, so agents must read them from filings or the web, citing them as web sources.
- **Decision support, not advice.** Every memo carries the disclaimer, and the investor makes
  every decision.

## 9. Live smoke test (build step 9, run 2026-10-03 on this machine)
Run with the real config, cache in `data/cache/`, and outputs in a scratch folder. No full committee
was run (per spec).

| Step | Command | Result |
|---|---|---|
| Prices | `python scripts/fetch_prices.py --tickers KO,SPY,XLP --out <tmp>/prices.json` | PASS: yfinance for all 3 tickers (753 daily bars each, last bar 2026-10-02, KO close 85.65). USD/ILS from `ILS=X` = 3.0532. No fallback needed. |
| Technicals | `python scripts/compute_technicals.py --ticker KO --sector consumer_staples --out ...` | PASS: 753 bars, regime `range`, RSI14 38.4, SMA200 79.13. Supports S1–S3 at 85.01/83.01/81.45; stops 84.40/83.22/78.52. RS vs SPY over 12m: +13.1 pp. |
| Fundamentals | `python scripts/fetch_fundamentals.py --ticker KO --out ...` | PASS: `coverage: sec`, CIK 21344. TTM revenue $49.28B (`FY2025+3M2026−3M2025`), net income $13.70B, FCF $12.56B. Total debt $43.81B via formula (c) `LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities + CommercialPaper` (the tag-drift handling worked). P/E 26.9, EV/EBIT 27.8, FCF yield 3.4%, net debt/EBITDA 2.14. **`lag_detected: true`**: companyfacts ends at the Q1-2026 10-Q, but submissions list the Q2 10-Q filed 2026-07-29. This is flagged high and added to `missing_data` with the filing URL. |
| Macro | `python scripts/fetch_macro.py --series DGS10 --out ...` | PASS: DGS10 = 5.24 (observation 2026-10-01), +0.75 pp over 3m, +1.12 pp over 12m. |

Stooq was not needed. A separate probe during planning showed it serves a JavaScript bot wall,
which the code detects and does not bypass (see §1).

## 10. Final review pass (2026-10-04) and what it changed
Five independent read-only reviewers audited the build in parallel: spec completeness, decision
math, live-data robustness, agent/command consistency, and a postmortem of the first live run
(`runs/2026-10-04_1015`). The spec audit found no critical or major gaps (six minor). The other
four found real defects, all fixed with regression tests (suite: 2036 passed, 4 live-network tests
skipped). Full detail is in `docs/DESIGN.md` §10; the main points:
- **Wrong numbers fixed:**
  - Debt picked a stale annual tag over newer quarterly data (ABT understated 2.6x, GOOGL 2x); the
    formula with the latest balance sheet now wins, with more tag coverage (`DebtCurrent`, notes/loans).
  - 52/53-week retailers (COST, PEP) got null TTM; fiscal-period windows widened in `committee.yaml`.
  - Tel Aviv quotes in agorot were priced as dollars; they are now converted to USD.
  - Breach trims rounded down to zero shares, leaving BRK-B over its 10% cap; they now round up.
  - Debt-free companies (PLTR) lost every EV/DCF cell and showed rr -44.77; they now get debt 0
    with a flag, and a stop closer than 1% gives rr null (`stop_too_tight`).
  - Stale cached prices were scored in the ledger; scoring now needs fresh, same-dated series.
- **Honesty of agent output:** a key figure must now carry the same `as_of`/`kind`/`unit`/`filed` as
  the data it cites (an agent could previously change them to alter its evidence score). Devil's
  Advocate verdicts were all `damaged` in the live run; the prompt has a calibration rubric.
  A candidate BUY now requires a completed Devil's Advocate review and no breached stop.
- **Orchestration safety:** a usage-limit or outage mid-run no longer burns retry attempts and
  fabricates failed stubs (circuit breaker, then `redispatch` + `next`); `redispatch` only
  re-emits missing or invalid outputs; a config file edited mid-run raises a warning (the first
  live run used the investor profile as it was when the run started).
- **Memo clarity:** fair value and stop shown beside rr (negative rr shown as "n/m (FV below
  price)"), a separate cash floor vs the CIO's cash target, currency exposure by listing currency,
  full-exit and conglomerate labels.
- **Deliberately deferred (rare, noted for later):** ADR market-cap cross-check when Yahoo is
  down, successor-registrant CIK mapping (XOM), quarterly data filed on 6-K, back-dated runs mixing
  today's market cap, ROIC when invested capital is tiny but positive (PLTR shows 712%), the
  copyright guard ignoring single-quoted spans, and Yahoo-only TASE funds (priced via US proxies).
- **First-live-run caveat.** That run's decisions used the pre-edit profile (moderate, 10 years)
  and the old Leumi price. Its ledger rows are real data and were left untouched; re-run
  `/committee-run --quick` to get recommendations on the current profile and fixes.
- **Captive finance net debt (2026-10-29).** `valuation.py` no longer deducts consolidated net debt for
  tickers in `valuation.captive_finance_tickers` (CAT, DE, F, GM, PCAR, HOG, CNH; AGCO and Polaris use
  unconsolidated JVs and are left out): `net_debt_basis` = `captive_excluded` | `override` | `consolidated`,
  medium flag `captive_finance_debt_excluded`. `fetch_fundamentals.py` adds the `captive_finance` /
  `possible_captive_finance` flags, `balance_notes` and a null `net_debt_ebitda_ex_captive`. Details in
  DESIGN §10. Follow-up for the memo renderer: add `captive_finance_debt_excluded` to
  `render_memo.VALUATION_CAVEAT_CODES` if it should always appear in the "Valuation caveats" table (it is
  medium, so it already shows under "Data-quality flags").
