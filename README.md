# The Agent Investors

A multi-agent **investment committee** that runs entirely inside **Claude Code**. It uses
subagents and slash commands, sits on a deterministic, keyless Python data and math layer, and
needs no paid APIs or API keys.

> **Decision support, not advice.** Every memo opens with a disclaimer: this is research output,
> not personalized financial advice, and you make all decisions.

## Hard rule: no agent trades

No agent trades: never place, modify or cancel an order, never move money, never record a yes; at most prepare information or a ticket for the investor to place himself. This holds for every agent, every slash command and every script in this repo. The committee proposes; the investor decides and places any order himself, in his own broker. `.claude/settings.json` denies the order and money tools of the connected trading MCP servers, and tests fail if an agent gets an order tool or a way to record a yes.

## Structure

```
User
 └─ Orchestrator (main Claude Code session — /committee-run drives scripts/committee.py)
     ├─ macro-geopolitics   (sector caps/floors, scenarios)
     ├─ cio                 (Phase 1 allocation, Phase 4 reconciliation)
     └─ sector-head ×N      (one call per sector, incl. "funds")
         └─ fundamental-analyst · technical-analyst · operator-ceo · industry-ops ·
            osint-sentiment · devils-advocate      └─ etf-analyst (fund holdings)
```

## Setup

```bash
pip install -r requirements.txt
```

You need Python 3.11+. On Windows, everything works from PowerShell or Git Bash. Then **open
Claude Code in this folder**: agents and commands in `.claude/` load when a session starts.

```bash
python -m pytest -q
```

The test suite runs fully offline because sockets are blocked during tests.

## Commands

| Command | What it does |
|---|---|
| `/committee-setup` | Asks you questions and fills `config/investor_profile.yaml`, `config/portfolio.yaml` and `config/universe.yaml`, then validates them. `config/portfolio.yaml` already holds the investor's real holdings; use this command to change holdings, limits or the investor profile. The empty template (null shares and weights, which `validate-config` rejects) lives in `tests/fixtures/config_template/`. |
| `/committee-run` | Full 4-phase committee run. Flags: `--quick` (Phase 2 with fundamental + technical + devil's advocate only, no Phase 3), `--sectors a,b` (limit the scope), `--dry-run` (fixtures only: no web, no agents). |
| `/committee-holding <TICKER>` | Single-ticker "trial": a Phase-2 review of one name. It reuses Phase 1 from a run no more than 7 days old and writes `phase4/trial_<T>.md/.html`. |
| `/committee-scorecard` | Scores past recommendations in `ledger/recommendations.csv` against SPY and the sector ETF, with hit rates by agent role and by action. |

You can also run the plumbing directly:

| Command | Purpose |
|---|---|
| `python scripts/committee.py validate-config` | Check the config files |
| `python scripts/committee.py dry-run` | Run the whole pipeline on fixtures and produce a memo (full pipeline only: `--quick`, `--sectors` and `--holding` are rejected with exit 2; exit 1 and `ok: false` when any order failed) |
| `python scripts/committee.py init --quick` | Start a run |
| `python scripts/committee.py next --run <id>` | Advance the state machine |
| `python scripts/committee.py status --run <id>` | Show progress (also reports a config edited after the run started and a tripped circuit breaker) |
| `python scripts/committee.py redispatch --run <id>` | Resume after a crash or stop: re-emits only the orders that still need an agent (valid outputs are skipped, a bad output is set aside) |

Each data script is also callable on its own, for example
`python scripts/fetch_prices.py --tickers KO,SPY --out prices.json`, `compute_technicals.py`,
`fetch_fundamentals.py`, `fetch_macro.py`, `etf_lookthrough.py`, `screen.py`, `valuation.py`,
`risk_reward.py`, `evidence_score.py`, `aggregate.py`, `portfolio.py`, `validate.py`,
`ledger.py` and `render_memo.py`.

## How a run flows

1. **Step 0, setup.** `committee.py init` validates the configs, creates `runs/<YYYY-MM-DD_HHMM>/`,
   snapshots the configs into `inputs/` (with a sha256 of each in `manifest.json`), and starts
   `run_log.md` and `manifest.json`. Agents and scripts only ever read the `inputs/` snapshot, so a config
   edited mid-run does not change the run's decisions; `p4.finalize` and `status` compare the live files with
   the snapshot hashes and report any difference (manifest warning, `run_log.md` line and a memo "Data gaps"
   entry). The run is not aborted.
2. **The loop.** The slash command repeatedly calls `committee.py next`:
   - Python steps run inline.
   - When agents are needed, `next` writes **work-order files** (`orders/<id>.md`) that list the
     only files the agent may read, its output path, schema, seed sources and required fields.
   - The main session launches each order as a subagent with the one-line prompt
     `Execute work order <path>`. Batches run in parallel, up to `concurrency` (8) per message.
   - On the next call, `next` validates every output (JSON Schema plus semantic checks, including
     re-checking every cited number against the data files). It retries an invalid output once,
     with the errors attached. If the retry also fails, the agent is marked failed (evidence 1).
     Missing reports are never fabricated.
   - **Circuit breaker.** If, on the first check of a step, at least half of its orders (and at least 3)
     produced no output file at all, `next` stops with an error instead of burning attempts: that is
     almost always an agent usage limit or outage. Run `redispatch`, launch what it lists, then `next`
     (`next --ignore-circuit-breaker` accepts the failures). Thresholds: `orchestration.circuit_breaker_fraction`
     and `orchestration.circuit_breaker_min_orders` in `config/committee.yaml` (optional; defaults 0.5 and 3).
3. **Phase 1, macro and allocation.**
   - Python fetches FRED macro series, prices for holdings, SPY, the 11 sector ETFs and USD/ILS,
     and computes the ETF **look-through** sector exposure. A quote in ILS or agorot (Bank Leumi-style
     Tel Aviv stocks such as `LUMI.TA`, which Yahoo quotes in agorot) is converted to USD at the spot
     USD/ILS rate; the native ILS price stays in `prices.json` (`native.last_close`).
   - `macro-geopolitics` writes `macro_view.json`, then the `cio` writes `cio_allocation.json`
     (current vs target sector weights, plus expand and reduce sectors). The CIO and the ETF analyst also
     read the `investor` block of `config/investor_profile.yaml` (age, occupation, expected income, career
     concentration, contribution plan; unverified statements by the investor, any field may be null).
4. **Phase 2, holding trials.**
   - Python computes technicals, fundamentals from SEC EDGAR, and a valuation grid.
   - **Wave A** runs fundamental, technical, OSINT and operator agents for each stock, and the
     etf-analyst plus technical agent for each fund.
   - **Wave B** runs the devil's advocate.
   - Python then computes evidence scores and applies the rule-based aggregation and action
     rules, and each sector head issues its verdict.
5. **Phase 3, stock picking** (expand sectors only, at most 2).
   - A screen produces a shortlist, and a light wave (fundamental and technical) narrows it to
     the top 4.
   - A full wave (operator, industry-ops, OSINT) plus the devil's advocate reviews the finalists.
   - The sector head proposes at most 3 BUYs. Zero is a valid outcome.
6. **Phase 4, the memo.**
   - A portfolio preview is built first; the CIO then reconciles everything into
     `cio_final.json`.
   - Python builds the rebalancing plan (constraint checks, 25% Israeli capital-gains estimate,
     USD/ILS), renders `phase4/memo.md` and `phase4/memo.html` (self-contained), and appends every
     recommendation to the ledger.

**What the memo contains:** the disclaimer, an executive summary, macro regime, scenarios and
constraints, current vs target portfolio (cash floor from the profile and the CIO's cash target are
shown separately), holding verdicts (R/R next to the base fair value and the stop; a negative R/R is
shown as "n/m (FV below price)" and a null one with its reason), new proposals, the rebalancing plan
(with a currency breakdown by listing currency), devil's-advocate highlights and dissents, overrides
and rule deviations (with their justifications), data gaps, and a sources appendix.

## Live dashboard

Open a second terminal in this folder and start it (during a run or any time):

```bash
python scripts/dashboard.py          # opens http://127.0.0.1:8765 (--run <id>, --port N)
```

Four tabs, refreshing on their own:
- **Room**: a picture of the committee. The Macro analyst and the CIO sit at their desks, every sector
  has a glass room with its head, and every agent is a little person at a desk who types while working.
  A paper flies to the sector head when a report lands, and the printer produces the memo at the end.
  The whole roster is always shown: agents and sectors the run does not use are faded as "off duty".
  Hover anyone for their result.
- **Board**: the same run as a list of cards.
- **Portfolio**: your holdings from `config/portfolio.yaml` with live Yahoo prices (every 10 s): shares,
  average cost, last price, day change, value, and profit/loss per holding and in total, in each holding's
  own currency with totals in USD and ILS. **Edit holdings** changes shares, average cost and cash, removes
  a holding or adds one after a trade: it validates first, keeps your comments, saves a backup in
  `data/backups/`, and is protected by a per-session token.

- **Notifications**: end-of-day warning signs for the live portfolio, each with a short **Do** and **Why**.
  Critical: a stock above your single-stock cap, a fund above the fund cap, a sector above the sector cap
  (looking through funds), a price below the committee's stop, a very large one-day drop or a very deep loss.
  Warning: cash below your buffer, a smaller drop or loss, committee advice you have not acted on, a holding
  with no live quote. Info: stale holdings list, a big USD/ILS move. After the 16:00 ET US close the check
  uses the closing prices ("final"), and a critical alert pops up once (in the page and, if you enable it, as
  a desktop notification) with the badge on the tab; during market hours alerts are shown as "live" and
  only pop up if you tick the intraday box. Limits come from `investor_profile.yaml`; alert levels from the
  `alerts` section of `committee.yaml`. First-time alerts are kept in `data/alerts/log.json`. The page must be
  open in a browser to pop up: the dashboard does not run in the background.

The dashboard binds to localhost only. The edit form is the only thing it ever writes; everything else is
read-only. It cannot show an agent's internal tool calls, because Claude Code does not expose them to the
project, so "working" means dispatched with no output yet. Quotes can be about 15 minutes delayed, and P/L is
nominal (before fees and Israeli capital-gains tax).

## Expected usage per run (agent calls)

Counts below are for the default 4 stocks + 2 funds; they scale with the number of holdings
(≈ 3 agent calls per stock + 1–2 per fund in `--quick`).

| Mode | Agent calls (approx.) |
|---|---|
| `--quick` | ≈ 20: macro 1 + CIO 2, plus 4 stocks × (fundamental, technical, DA), plus 2 funds × ETF, plus about 3 sector heads |
| Full, no expand sectors | ≈ 35 |
| Full, 2 expand sectors | ≈ 85–100: adds per sector 6 × 2 light, 4 × 3 full, 4 DA and 1 sector head |
| Retries | at most +1 per invalid output |

Each agent is `model: sonnet`. A full run fetches many web pages, so expect it to take a while.
Start with `--quick`.

## Data sources (free, keyless)

- **Prices:** `yfinance`, with a Stooq CSV fallback. Stooq currently returns a JavaScript bot
  wall; the code detects it, never bypasses it, and falls back to stale cache with a flag.
- **Fundamentals:** SEC EDGAR `company_tickers.json`, `companyfacts` and `submissions`. EDGAR
  requires a User-Agent with a contact email, `sec_contact_email`, and limits you to 10 requests
  per second or fewer.
- **Macro:** FRED `fredgraph.csv` for DFF, DGS2, DGS10, T10Y2Y, CPIAUCSL, CPILFESL, UNRATE,
  PAYEMS, INDPRO, DTWEXBGS, DCOILWTICO, BAMLH0A0HYM2 and VIXCLS.
- **ETF composition:** yfinance `funds_data` (top 10 holdings and sector weights), falling back
  to `config/etf_overrides.yaml`.
- **Qualitative research:** the agents' WebSearch and WebFetch. Sources are paraphrased; quotes
  are capped at 25 words.

Every number in a script output is a **Fact** with a `source_id` (which maps to a URL) and an
`as_of` date, or it is `null` with a reason and listed in `missing_data`. Cache TTLs are prices
1 day, fundamentals 7 days, macro 1 day and ETF data 7 days, all configurable in
`config/committee.yaml`.

## Repository layout

```
CLAUDE.md  README.md  BUILD_NOTES.md  CONTRACTS.md  requirements.txt  pytest.ini
config/        investor_profile, portfolio, universe, committee (all thresholds), etf_overrides
schemas/       JSON Schema 2020-12 for every agent output (+ data/ contracts, examples/)
knowledge/     sectors/<11 GICS>.md playbooks + etf_playbook.md
scripts/       deterministic data + math layer; committee.py (orchestrator) + _steps.py
templates/     memo.md.j2, memo.html.j2, work_order.md.j2
.claude/       agents/ (10 roles) · commands/ (4) · settings.json
data/cache/    fetched data with timestamps (git-ignored)
runs/<id>/     inputs/ orders/ phase1/ phase2/ phase3/ phase4/ run_log.md manifest.json (git-ignored)
ledger/        recommendations.csv
docs/          SPEC.md (original spec), DESIGN.md (design contract)
tests/         pytest suite + fixtures (incl. tests/fixtures/dryrun for --dry-run)
```

## Limitations

- **Data freshness, no live feeds.** Prices are daily closes. SEC companyfacts can lag the latest
  10-Q (this is detected and flagged). Monthly FRED series lag by about 4–6 weeks.
- **Fund holdings.** yfinance exposes only each fund's top 10 holdings, so fund overlap is a lower
  bound and the Phase-3 universe is small. Add names in `universe.yaml`.
- **Sector classification.** Sectors are mapped from Yahoo labels or SEC SIC codes, which is
  approximate GICS.
- **Bank and REIT KPIs.** NIM, CET1 and FFO are not standard XBRL tags; agents read them from
  filings.
- **Tax figures.** The tax number is a nominal-gain estimate. Israeli tax is levied on real gains,
  so consult a tax advisor.
- **Starting live runs.** Agents load only when a session starts, so after cloning or editing
  `.claude/` you need to restart Claude Code in this folder.
- **Judgment calls.** See `BUILD_NOTES.md` for every judgment call and known gap.

## Day-trader skills (optional reference methods)

The `trader-*` skills from https://github.com/Jakeschincariol/day-trader-skill (commit 3d0f4ef) are installed at user level in `~/.claude/skills/`. Three committee agents preload the ones that fit a long-term, no-shell committee through the `skills:` frontmatter: `fundamental-analyst` and `operator-ceo` (trader-analyst: reading earnings from SEC filings) and `osint-sentiment` (trader-news, trader-pelosi). They are reference methods only: the agents cannot run the scripts and never place or size trades. The intraday skills (scanner, entry, exit, time machine, budget, risk, alerts, broker) are for you to use directly, for example `/trader-scanner daily --symbols PLTR,QCOM,CAT`. On a machine without the skills installed the agents still work; the preload is skipped.

## Day-trading desk

`/trader-desk` is a short-term, long-side-only variant of the committee that uses all eleven day-trader skills. It is education and decision support: **most people who day trade lose money**, and the desk ends every run with "Nothing was ordered. You place the order yourself." It is paper by default (`"paper": true` in `trader/config.json`).

```
/trader-desk                         market-hours check, scan, news, you choose a ticker
/trader-desk scan --holdings         also check your holdings (TASE units skipped)
/trader-desk pick CAT                you chose CAT: timing, replay, size, risk, tickets
/trader-desk status                  what exists for today
```

The main session is the chair. Four Sonnet subagents do the work and pass results through files only (`runs/desk/<date>/`, `trader/`); each has its own result schema (`schemas/desk_*.schema.json`) and the chair validates it (`python scripts/desk.py validate --file F --schema scout|timer|quant|risk`) before moving on:

| Agent | Skills | Does |
|---|---|---|
| `desk-scout` | scanner, news, pelosi, analyst | scan, news tags, congress-trade flags, earnings this week -> `scout.json` |
| `desk-timer` | entry, exit, alerts | one ticker: entry state, stop and target (`trader/plan.json`), three alerts |
| `desk-quant` | timemachine, budget | 10,000-session replay (a coin flip stays a coin flip), whole-share sizing -> proposed order JSON |
| `desk-risk` | risk, broker | risk check; on BLOCK the line stops; on PASS a plain ticket + exact JSON for you to place yourself, and a flat-by-time close ticket |

**Before first use:** `python scripts/desk.py init-day` (the chair runs it) creates `trader/config.json` from the skills' example, unchanged, and the desk then stops before any sizing until you set your own numbers there: `budget`, `max_loss_per_trade_usd`, `max_position_pct`, `cash_buffer_pct`, `max_daily_loss_usd`, `max_trades_per_day`, `no_entries_before`, `no_entries_after` (or add `"desk_limits_confirmed": true` if the example numbers really are yours). `trader/` is git-ignored. Restart Claude Code in this folder so the `desk-*` agents load.

**Safety, enforced in `.claude/settings.json` and by tests:** no agent or command places, modifies or cancels an order or moves money; the trading MCP tools (`execute_order`, `close_position`, `cancel_order`, deposits, withdrawals, trading-mode switches, Alpaca `place_*`/`cancel_*`/`close_*`, ...) are denied; only the exact skill script invocations the desk needs are allowed (one rule per script, never a bare Bash); `ticket.py approve|decline|result|fill` and `entry.py watch` are denied. From the broker skill the desk uses only `show`, `log` and `flatten` (into `trader/flatten.json`). Fills are never recorded by the desk. `scripts/desk.py` (`init-day`, `holdings-symbols`, `validate`, `status`) has no network, subprocess or order code.
