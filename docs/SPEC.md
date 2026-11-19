# BUILD: "The Agent Investors" — Multi-Agent Investment Committee

> Original build specification, preserved verbatim. Design decisions and interpretations
> live in `docs/DESIGN.md`, `CONTRACTS.md` and `BUILD_NOTES.md`.

## 0. Your mission

Build, in this (empty) repository, a complete multi-agent investment research system that runs **entirely inside Claude Code** using subagents and slash commands. No paid APIs, no API keys. Build everything in this single session, test it, and finish with a working, documented system.

Do not stop to ask me questions during the build. Where something is ambiguous, make the most reasonable choice and record it in `BUILD_NOTES.md`.

The system simulates an institutional investment committee:

```
User
 └─ Orchestrator (main Claude Code session — runs all phases, passes files)
     ├─ Macro/Geopolitics agent  (sits beside the CIO; sets sector caps/floors)
     ├─ CIO agent                (allocation, reconciliation, final verdict)
     └─ Sector Heads (one per GICS sector, parameterized)
         └─ Specialists: Fundamental · Technical · Operator (ex-CEO) ·
            Industry-Ops (ex-PM/operations) · OSINT/Sentiment · Devil's Advocate
     └─ ETF Analyst (for fund holdings)
```

---

## 1. Hard constraints (never violate)

1. **No API keys, no paid data.** Only free, keyless sources:
   - Prices: `yfinance` (pip), with Stooq CSV fallback (`https://stooq.com/q/d/l/?s={ticker}.us&i=d`).
   - Fundamentals: SEC EDGAR (`https://www.sec.gov/files/company_tickers.json`, `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`). EDGAR requires a descriptive `User-Agent` with a contact email → read it from `config/investor_profile.yaml` (`sec_contact_email`). Respect ≤10 req/s.
   - Macro: FRED keyless CSV (`https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}`).
   - ETF composition: `yfinance` fund data (sector weightings / top holdings), fallback to `config/etf_overrides.yaml`.
   - Qualitative: agents' WebSearch / WebFetch.
2. **Models:** `sonnet` for all reasoning agents; `haiku` only for purely mechanical agents if you create any. **Never use opus.**
3. **Subagents cannot spawn subagents.** The orchestrator command (main session) runs every stage and launches agents in parallel waves. Agents communicate **only through files** in `runs/<run_id>/`. No agent reads another agent's output unless the orchestrator explicitly passes the path.
4. **Every number has a source URL and an as-of date.** If a figure can't be sourced, it is `null` and listed in `missing_data`. Agents must **never** estimate or recall financial figures from memory.
5. **Deterministic math lives in Python, not in LLM prose:** technical indicators, ratios, valuation arithmetic, risk/reward, evidence scores, aggregation, look-through, portfolio weights, trade sizing. Agents interpret script outputs; they don't compute them.
6. **Cross-platform:** the user is on Windows. Use Python 3.11 + `pathlib`; no bash-only scripts. Commands must work in PowerShell.
7. **Decision support, not advice.** Every memo carries a header disclaimer: research output, not personalized financial advice; the user makes all decisions.
8. **Git:** `git init` and commit locally at milestones. **Never `git push`.**
9. **Copyright:** OSINT and other agents summarize and paraphrase sources; never paste article text or long quotes.

---

## 2. Repository layout

```
agent-investors/
├─ CLAUDE.md                     # project rules for future sessions (constraints above, layout, commands)
├─ README.md
├─ BUILD_NOTES.md
├─ requirements.txt              # pandas, numpy, yfinance, requests, pyyaml, jsonschema, pytest, jinja2
├─ config/
│  ├─ investor_profile.yaml
│  ├─ portfolio.yaml
│  ├─ universe.yaml
│  ├─ committee.yaml             # all thresholds, concurrency, cache TTLs, enabled sectors
│  └─ etf_overrides.yaml
├─ schemas/                      # JSON Schema (draft 2020-12) for every agent output
├─ knowledge/sectors/            # 11 GICS sector playbooks
├─ scripts/                      # deterministic data + math layer
├─ .claude/
│  ├─ agents/                    # one file per role (sector injected at call time)
│  └─ commands/                  # slash commands (orchestration)
├─ data/cache/                   # fetched data with timestamps
├─ runs/<YYYY-MM-DD_HHMM>/       # one folder per committee run
│  ├─ inputs/  phase1/  phase2/  phase3/  phase4/  run_log.md
├─ ledger/recommendations.csv    # every recommendation ever made
└─ tests/  (+ tests/fixtures/)
```

---

## 3. Data & math layer (`scripts/`)

Each fetch script caches to `data/cache/` with a timestamp and reuses cache within TTL (prices 1 day, fundamentals 7 days, macro 1 day, ETF data 7 days — configurable). Each writes JSON that includes `source_url` and `as_of` for every value. All scripts are CLI-callable (`python scripts/x.py --ticker KO --out path`) and importable.

| Script | Responsibility |
|---|---|
| `fetch_prices.py` | ~3y daily OHLCV for tickers + SPY + relevant sector ETF. yfinance → Stooq fallback. |
| `compute_technicals.py` | SMA 20/50/200, RSI(14), MACD, ATR(14), 52w high/low, drawdown from high, relative strength vs SPY and sector ETF (3/6/12m), volume trend, swing-pivot support/resistance levels, trend regime label (rule-based). |
| `fetch_fundamentals.py` | EDGAR companyfacts → revenue, gross/operating margin, net income, operating cash flow, capex, FCF, total debt, cash, shares outstanding; TTM and 3y growth; plus price-derived P/E, EV/EBIT, EV/Sales, FCF yield, net debt/EBITDA. Ticker→CIK map cached. Non-SEC filers → `null` + `missing_data`. |
| `fetch_macro.py` | FRED: DFF, DGS2, DGS10, T10Y2Y, CPIAUCSL (YoY computed), CPILFESL, UNRATE, PAYEMS, INDPRO, DTWEXBGS, DCOILWTICO, BAMLH0A0HYM2, VIXCLS. Latest value, 3/12m change. |
| `etf_lookthrough.py` | Converts portfolio into true **sector exposure** by decomposing ETFs (and applying overrides, e.g. BRK-B treated as Financials but flagged "conglomerate"). Weights must sum to 100% ± rounding. |
| `screen.py` | Phase 3 candidate generation: universe = constituents of the sector's benchmark ETF (top holdings via yfinance) + any `universe.yaml` additions; filter by market cap, avg dollar volume, exclusions, already-held; rank by a transparent composite (quality + valuation + momentum from the scripts above). Output shortlist with the raw metrics. |
| `risk_reward.py` | `upside = (fair_value_base − price)/price`, `downside = (price − invalidation_price)/price`, `rr = upside/downside`. Any missing input → `rr = null`. |
| `evidence_score.py` | Rule-based evidence quality per report (see §5). |
| `aggregate.py` | Per-ticker aggregation and action rules (see §5). |
| `portfolio.py` | Current vs target weights (sector and position), trade list honoring `investor_profile.yaml` constraints (max position, max sector, min trade size, target cash). Flags sells of positions with gains (if cost basis given) for Israeli capital-gains tax impact, and notes USD/ILS exposure. |
| `validate.py` | Validates any agent output against its schema; returns human-readable errors for a retry. |
| `ledger.py` | Appends every recommendation (date, ticker, action, price, conviction, evidence, thesis, invalidation). `score` subcommand: return since recommendation vs SPY and the sector ETF, hit rate by agent role and by action. |
| `render_memo.py` | Builds the final memo as Markdown **and** a self-contained HTML file (inline CSS, no external build tools) from Phase 1–4 JSON via Jinja2 templates. |

---

## 4. Schemas (`schemas/`)

Write full JSON Schemas. Minimum contents:

**`specialist_report.json`** (all specialists share this base, with role-specific `extensions`):
- `run_id`, `agent_role`, `sector`, `ticker`, `as_of`
- `thesis` (≤ 60 words), `stance` ∈ {bullish, neutral, bearish}
- `strengths`: up to 3 × `{point, evidence_refs[]}` — **"up to 3", never padded**
- `risks`: up to 3 × `{point, evidence_refs[], severity ∈ low|medium|high}`
- `key_figures[]`: `{name, value, unit, source_id, as_of}`
- `invalidation_price` + `invalidation_rationale` (what would prove the thesis wrong)
- `conviction` (1–10) + `conviction_reason` — how strongly the view is held
- `self_evidence` (1–10) + reason — agent's own view of data completeness (recorded, but decisions use the rule-based score)
- `missing_data[]`, `red_flags[]`: `{flag, severity, must_address: bool}`
- `sources[]`: `{id, url, title, accessed}`
- `extensions` per role:
  - fundamental → `valuation {fair_value_low, fair_value_base, fair_value_high, method, input_refs}`, quality and balance-sheet notes
  - technical → `trend_regime`, `key_levels[]`, `setup`, references to computed indicators only
  - operator_ceo → management quality, capital allocation record, incentives (proxy DEF 14A), insider activity (Form 4), guidance-vs-delivery history
  - industry_ops → supply chain, capacity/backlog, project execution, unit economics, operational moat
  - osint → `signals[]: {signal, direction, source_ids, trend}`; output is **flags for others to address**, not a score that gets averaged

**`devils_advocate_report.json`**: `attacks[]: {target_claim, target_agent, counter_evidence, source_ids, severity}`, `bear_case_price`, `kill_criteria[]`, `verdict_on_thesis` ∈ {survives, damaged, broken}, sources.

**`etf_review.json`**: look-through summary, expense ratio, concentration (top-10 weight), overlap with other holdings, momentum vs benchmark, role in portfolio, conviction, action.

**`sector_verdict.json`**: per ticker → `action` (holdings: ADD|HOLD|TRIM|SELL; candidates: BUY|PASS), aggregated conviction, aggregated evidence, rr, rationale (≤ 80 words), `dissents[]`, `unresolved_red_flags[]`, `rule_deviation` + `deviation_justification` (required if action differs from the rule output).

**`macro_view.json`**: regime label, key drivers (each tied to fetched data), scenarios with probabilities (sum to 1), `sector_constraints[]: {sector, min_weight, max_weight, rationale}`, conviction, sources.

**`cio_allocation.json`**: current sector weights (look-through), target weights, `expand_sectors[]`, `reduce_sectors[]`, `constraint_overrides[]: {sector, macro_bound, cio_target, justification}`.

**`cio_final.json`**: reconciliation of Phase 1 targets vs Phase 2/3 findings (explicitly addressing conflicts such as "trim Tech" vs "all Tech holdings score 9"), final target weights, final actions, top risks, committee-level dissent summary.

---

## 5. Scoring & decision rules (all thresholds in `config/committee.yaml`)

**Evidence quality (rule-based, `evidence_score.py`)** — start at 10:
- −2 per required role field that is null
- −1 per key figure older than its freshness threshold (prices > 3 trading days; fundamentals older than latest filing + 120 days; macro > 45 days)
- −2 if fewer than 3 distinct sources
- −1 per `missing_data` item (max −4)
- floor 1

**Aggregation per ticker (`aggregate.py`):**
- `agg_conviction` = evidence-weighted mean of specialists' conviction, where bearish stances count as `10 − conviction`
- Devil's Advocate adjustment: survives 0, damaged −1, broken −3
- `agg_evidence` = mean evidence score
- OSINT high-severity flags with `must_address=true` that no specialist addressed → `unresolved_red_flags`

**Action rules — holdings:**
- ADD: agg_conviction ≥ 7, agg_evidence ≥ 6, rr ≥ 2.0, no unresolved high flag
- SELL: agg_conviction ≤ 3, or DA verdict "broken" not rebutted by the Sector Head
- TRIM: agg_conviction 4, or position/sector exceeds profile limits or CIO target
- HOLD: everything else
- agg_evidence < 5 → capped at HOLD, labeled "insufficient evidence"

**Action rules — candidates:** BUY requires agg_conviction ≥ 7, agg_evidence ≥ 6, rr ≥ 2.5, DA verdict ≠ broken. Otherwise PASS. **Never force picks** — zero proposals for a sector is a valid outcome.

**Overrides:** Sector Heads may deviate from rule output and the CIO may override macro bounds, but only with a written justification that is rendered in the memo's "Overrides & Deviations" section.

---

## 6. Agents (`.claude/agents/*.md`)

One file per role; the sector and ticker are injected by the orchestrator at call time. Each file has frontmatter (`name`, `description`, `tools`, `model: sonnet`) and a body covering: the lens it applies, the inputs it reads (exact paths passed by the orchestrator), its procedure, how it uses `knowledge/sectors/<sector>.md`, its output path and schema, and the rules (cite everything, no figures from memory, word limits, "up to 3" items, what to do when data is missing). Restrict tools to what each role needs (Read, Write, WebSearch, WebFetch; Bash only for running the project's scripts).

Personas are **lenses, not costumes**: each role gets different inputs and methods so the committee produces genuinely different views.

| Agent | Lens & inputs |
|---|---|
| `macro-geopolitics` | Reads `fetch_macro` output + news on rates, inflation, geopolitics, supply chains. Sets sector caps/floors with rationale and scenario probabilities. |
| `cio` | Phase 1: allocation from look-through exposure + macro view + investor profile. Phase 4: reconciliation and final verdict. Must justify any macro override. |
| `sector-head` | Synthesizes specialist reports + aggregate script output for its sector; applies rules; records dissents; justifies deviations. One call per sector covering all its tickers. |
| `fundamental-analyst` | Reads `fetch_fundamentals` output + filings; quality, balance sheet, 3-scenario valuation with stated method. |
| `technical-analyst` | Reads **only** `compute_technicals` output. Trend regime, key levels, invalidation. No pattern-spotting beyond the computed data. |
| `operator-ceo` | Former multi-company CEO lens: management, capital allocation, incentives (DEF 14A), insider Form 4 activity, guidance credibility. |
| `industry-ops` | Former PM/operations lens: supply chain, capacity, backlog, execution, cost structure, unit economics. |
| `osint-sentiment` | News tone, product/app reviews, CFPB complaints (where relevant), employee reviews, hiring trends, earnings-call language shifts. Outputs flags. |
| `devils-advocate` | Reads all specialist reports for the ticker; attacks the strongest claims with counter-evidence; bear case price; kill criteria. Forbidden from agreeing without attacking. |
| `etf-analyst` | Reviews fund holdings (look-through, costs, concentration, overlap, role in portfolio). |

---

## 7. Sector playbooks (`knowledge/sectors/`)

Create 11 GICS playbooks (Information Technology, Health Care, Financials, Energy, Industrials, Consumer Discretionary, Consumer Staples, Utilities, Materials, Real Estate, Communication Services). Each contains: benchmark ETF (XLK, XLV, XLF, XLE, XLI, XLY, XLP, XLU, XLB, XLRE, XLC), the KPIs that matter (e.g. banks: NIM, CET1, credit costs; SaaS: NRR, Rule of 40; energy: breakevens, reserve life), appropriate valuation methods, key macro sensitivities, and typical failure modes for that sector.

---

## 8. Slash commands (`.claude/commands/`)

- **`/committee-setup`** — interactively fills `investor_profile.yaml`, `portfolio.yaml` and `universe.yaml` by asking me questions, then validates them.
- **`/committee-run`** — full 4-phase run (below). Flags: `--dry-run` (fixtures, no web, no agents — fixture agent outputs), `--quick` (Phase 2 with Fundamental + Technical + Devil's Advocate only; skip Phase 3), `--sectors X,Y` (limit scope).
- **`/committee-holding <TICKER>`** — single-holding "trial" (Phase 2 for one ticker).
- **`/committee-scorecard`** — runs `ledger.py score` and summarizes how past calls performed, by role and action.

### `/committee-run` sequence

**Step 0 — Setup:** create `runs/<run_id>/`, validate configs, snapshot configs into `inputs/`, start `run_log.md`. Create a task list so progress is visible.

**Phase 1 — Macro roadmap & allocation:**
1. Scripts: `fetch_macro`, `fetch_prices` (holdings + SPY + sector ETFs), `etf_lookthrough`.
2. `macro-geopolitics` agent → `phase1/macro_view.json`.
3. `cio` agent (allocation mode) → `phase1/cio_allocation.json`.
4. Deliverable: current vs target sector weights.

**Phase 2 — Holding review ("trial" per stock):**
1. Scripts per stock: `compute_technicals`, `fetch_fundamentals`.
2. Route each holding to its sector; ETFs → `etf-analyst`.
3. Wave A (parallel across all holdings, concurrency from config, default 8): fundamental, technical, osint, operator-ceo (operator skipped in `--quick`).
4. Validate every output (`validate.py`); on failure retry the agent once with the errors; on second failure mark the agent failed (evidence 1) and continue. Never fabricate a missing report.
5. `evidence_score.py` on every report.
6. Wave B: `devils-advocate` per holding, given all Wave A paths.
7. `aggregate.py` → rule outputs.
8. `sector-head` per sector → `phase2/<sector>_verdict.json`.

**Phase 3 — Stock picking (only for `expand_sectors`):**
1. `screen.py` → shortlist of 6 per sector.
2. Light wave (fundamental + technical) on the shortlist → evidence + aggregate → top 3–4.
3. Full wave (operator, industry-ops, osint) + devil's advocate on the finalists.
4. `sector-head` → up to 3 BUY proposals per sector, each with full thesis, fair value range, invalidation price, rr and dissents.

**Phase 4 — Investment Committee memo:**
1. `cio` agent (reconciliation mode) reads all phase outputs → `phase4/cio_final.json`.
2. `portfolio.py` → current vs target table + proposed rebalancing plan (sizes, constraint checks, tax and currency notes).
3. `render_memo.py` → `phase4/memo.md` + `phase4/memo.html`.
4. `ledger.py` appends all recommendations.
5. Print a short summary in chat with the memo path.

### Memo structure
1. Disclaimer header
2. Executive summary (≤ 5 bullets)
3. Macro regime, scenarios and sector constraints
4. Current vs target portfolio (sector and position tables)
5. Holding verdicts table: ticker · action · conviction · evidence · rr · one-line why
6. New proposals: one thesis card per pick
7. Proposed rebalancing plan (trades, sizes, tax/currency notes)
8. Devil's Advocate highlights and dissents
9. Overrides & rule deviations (with justifications)
10. Data gaps and low-evidence warnings
11. Appendix: sources

---

## 9. Config templates

- **`investor_profile.yaml`**: `horizon_years`, `risk_tolerance` (conservative|moderate|aggressive), `max_position_pct`, `max_sector_pct`, `min_trade_usd`, `target_cash_pct`, `base_currency: ILS`, `tax_jurisdiction: IL` (note 25% capital gains), `allow_short: false`, `sec_contact_email`.
- **`portfolio.yaml`**: holdings with `ticker`, `shares` or `weight_pct`, optional `cost_basis`, `currency`. Prefill tickers QQQ, SPMO, BRK-B, KO, PG, JPM with `null` weights; `/committee-setup` asks me for the values.
- **`universe.yaml`**: region (default US-listed), min market cap (default $10B), min 3-month avg dollar volume, exclusions, max candidates per sector.
- **`committee.yaml`**: enabled sectors, concurrency, cache TTLs, every threshold from §5.

---

## 10. Tests (`tests/`, pytest, all network mocked)

- Schema validation: valid and invalid fixtures for every schema
- `evidence_score`: freshness, missing fields, source-count cases
- `risk_reward`: normal, null inputs, price below invalidation
- `aggregate`: stance inversion, DA adjustments, every action rule boundary, insufficient-evidence cap
- `etf_lookthrough`: weights sum to 100, overrides applied
- `portfolio`: trades respect max position / sector / min trade; tax flag on gains
- `ledger`: append and score
- `render_memo`: renders from fixtures, contains all 11 sections
- End-to-end `--dry-run`: fixtures → complete memo (md + html) without network or agents

---

## 11. Build order

1. Scaffold, `CLAUDE.md`, configs, `requirements.txt`, git init → commit
2. Schemas → commit
3. Scripts + unit tests (green) → commit
4. Sector playbooks → commit
5. Agent definitions → commit
6. Slash commands → commit
7. Dry-run end-to-end test (green) → commit
8. README + BUILD_NOTES → commit
9. Live smoke test of the data layer only: `fetch_prices` + `compute_technicals` + `fetch_fundamentals` for KO, and one FRED series — proves the keyless sources work on this machine. **Do not run a full live committee** (it uses a lot of usage); I'll run it myself.

---

## 12. Definition of done

- All files exist as specified; `pytest` fully green; dry-run produces `memo.md` and `memo.html`; live data smoke test passes (or failures are clearly documented with the fallback that worked).
- `README.md`: setup (`pip install -r requirements.txt`), each command, how a run flows, expected usage per run (rough count of agent calls), and limitations (data freshness, no live feeds, decision support only).
- `BUILD_NOTES.md`: every judgment call, deviation, and known gap.
- Final chat message: repo tree, test results, smoke-test results, and the exact next steps for me (`/committee-setup`, then `/committee-run --quick` for a first run).
