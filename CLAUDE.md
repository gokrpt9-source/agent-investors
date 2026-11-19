# CLAUDE.md — The Agent Investors

A multi-agent investment committee that runs entirely inside Claude Code. Slash commands
orchestrate; subagents (`.claude/agents/`) write JSON reports; Python scripts (`scripts/`) do all
data fetching and arithmetic. Output is decision support for one investor, never advice.

- Requirements: `docs/SPEC.md` · Design contract (authoritative): `docs/DESIGN.md`
- As-built API of `scripts/common.py` + conventions: `CONTRACTS.md` · Judgment calls: `BUILD_NOTES.md`

## Hard constraints (never violate)

1. **No API keys, no paid data.** Only: yfinance (prices; Stooq CSV fallback), SEC EDGAR
   (User-Agent carries `sec_contact_email` from `config/investor_profile.yaml`; ≤10 req/s), FRED
   keyless CSV, yfinance fund data (fallback `config/etf_overrides.yaml`), agents' WebSearch/WebFetch.
2. **Models:** `sonnet` for every reasoning agent; `haiku` only for purely mechanical agents.
   No other model tier is referenced anywhere in this repo.
3. **Subagents never spawn subagents.** The main session (the slash command) runs every phase and
   launches agents in parallel waves. Agents communicate only through files in `runs/<run_id>/` and
   read only the paths their work order lists.
4. **Every number has a source URL and an as-of date.** Unsourced → `null` + `missing_data`.
   Never estimate or recall financial figures from memory.
5. **Deterministic math lives in Python**, never in LLM prose: indicators, ratios, valuation,
   risk/reward, evidence scores, aggregation, look-through, weights, trade sizing.
6. **Cross-platform (user is on Windows):** Python 3.11+, `pathlib`, no bash-only scripts; every
   command works in PowerShell.
7. **Decision support, not advice:** every memo starts with `common.DISCLAIMER`.
8. **Git:** commit locally at milestones. **Never `git push`.**
9. **Copyright:** summarize and paraphrase; never paste article text; quotes ≤ 25 words.

## Repo layout

```
CLAUDE.md  CONTRACTS.md  README.md  BUILD_NOTES.md  requirements.txt  pytest.ini
config/        investor_profile · portfolio · universe · committee (ALL thresholds) · etf_overrides (.yaml)
schemas/       agent-output schemas (*.json), data contracts (data/*.schema.json),
               config schemas (config_*.schema.json); $id via common.schema_id(name)
knowledge/sectors/   11 GICS sector playbooks
scripts/       deterministic layer; common.py is the shared foundation + the ONLY network seam
templates/     Jinja2 memo templates (md + html)
.claude/agents/      one file per role (sector/ticker injected by the work order)
.claude/commands/    committee-setup · committee-run · committee-holding · committee-scorecard
data/cache/    fetched data with timestamps (git-ignored)
runs/<run_id>/ one folder per committee run (git-ignored)
ledger/recommendations.csv   every recommendation ever made
tests/         pytest suite (+ tests/fixtures/<area>/)
```

## Slash commands

- `/committee-setup` — asks the investor questions, fills `investor_profile.yaml`,
  `portfolio.yaml`, `universe.yaml`, then validates (`python scripts/committee.py validate-config`).
- `/committee-run [--dry-run] [--quick] [--sectors a,b]` — full 4-phase committee run
  (`--dry-run` = fixtures, no web, no agents).
- `/committee-holding <TICKER>` — single-holding "trial" (Phase 2 for one ticker).
- `/committee-scorecard` — `ledger.py score`: how past calls performed, by role and action.

## How a run flows (`scripts/committee.py`)

1. `python scripts/committee.py init [--quick] [--sectors a,b]` → validates configs (null holdings →
   "run /committee-setup"), creates `runs/<run_id>/`, snapshots configs (sha256 of each in the manifest),
   prints `{run_id, run_dir, mode}`. Agents read only the `inputs/` snapshot; a config edited mid-run is
   detected by `p4.finalize` and `status` (manifest warning + run_log line + memo "Data gaps" entry; no abort).
2. Loop `python scripts/committee.py next --run <run_id>` (shell timeout 10 min):
   - `{"type":"continue"}` → call `next` again (python steps are time-boxed to ~90 s per call);
   - `{"type":"agents", "orders":[...], "concurrency":N}` → launch every order with the Agent/Task
     tool, `subagent_type = agent_type`, prompt exactly `Execute work order <order_path>`, at most N
     per message and all orders of a batch in ONE message; call `next` only after all finished
     (`next` validates outputs, retries once with `prior_errors`, then marks failures);
   - `{"type":"done", "memo_md", "memo_html", "summary"}` → print the summary + memo path.
3. Recovery: `redispatch --run <id>` re-emits only the dispatched orders that still need an agent (valid
   outputs are skipped and marked done; an invalid output is set aside); `status --run <id>` shows progress,
   config drift and a tripped circuit breaker. Circuit breaker: if >= 50% of a step's orders (min 3) have no
   output file on the first check, `next` returns an error and burns no attempt (usage limit / outage) —
   `redispatch`, launch, `next`; `next --ignore-circuit-breaker` accepts the failures.
   `dry-run` supports the full pipeline only (`--quick`/`--sectors`/`--holding` → exit 2) and exits 1 when any order failed.
4. Never read agent reports into the main context; keep a task list for progress.

## Conventions (DESIGN.md §0 — details in CONTRACTS.md)

- UTF-8 everywhere: every `open()/read_text()/write_text()` passes `encoding="utf-8"`; CSV opened with
  `newline=""`; JSON written via `common.write_json` (atomic, `ensure_ascii=False`, NaN/inf → null).
- Every script starts with the bootstrap from DESIGN §0 (so `python scripts/x.py` and
  `import scripts.x` both work), exposes a function API plus `main(argv=None) -> int`, and calls
  `common.setup_stdio()`, `common.add_common_args()` / `apply_common_args()`.
- Flags `--config-dir --cache-dir --runs-dir --ledger --as-of` ↔ env `AGINV_CONFIG_DIR`,
  `AGINV_CACHE_DIR`, `AGINV_RUNS_DIR`, `AGINV_LEDGER`, `AGINV_AS_OF`; plus `AGINV_OFFLINE=1`
  (cache only) and `AGINV_FIXTURES_DIR=<dir>` (all network served from fixtures).
- ALL network goes through `common.http_get*` / `common.yf_history|yf_info|yf_funds`. Never import
  `requests` or `yfinance` anywhere else.
- Every threshold comes from `config/committee.yaml` — never hard-code one.
- "Today" = `common.today()` (honours `AGINV_AS_OF`). Dates `YYYY-MM-DD`.
- Numbers in outputs are Facts (`common.fact`) inside an Envelope (`common.envelope`).
- Per-ticker files are `<kind>_<TICKER>.json` (`common.data_filename`); tickers normalized with
  `common.norm_ticker` (`brk.b` → `BRK-B`); quote tickers in YAML.
- Percentages are percent numbers 0–100. Sector keys are the 11 snake_case GICS keys in
  `common.SECTOR_KEYS` (+ `funds`, `cash`, `unclassified`).
- pandas 2.2–3.x compatible (no `applymap`, no `fillna(method=)`, no chained assignment).

## Testing

- `python -m pytest -q` from the repo root — must stay green. All tests are offline (sockets are
  blocked, `AGINV_OFFLINE=1`, `AGINV_AS_OF=2026-10-02`); use `tests/fixtures/` + `AGINV_FIXTURES_DIR`
  or monkeypatch `common.http_get` / `common.yf_*`.
- Live checks are marked `@pytest.mark.live` and run only with `python -m pytest -q --run-live`.
- Each unit owns `tests/test_<module>.py` and `tests/fixtures/<area>/`.

## Gotchas

- `.claude/agents/*.md` are loaded only at session start — after adding or editing an agent
  definition, restart Claude Code before `/committee-run`.
- `config/portfolio.yaml` holds the investor's real holdings (including TASE funds priced via `proxy_ticker` and
  `LUMI.TA`, a Tel Aviv stock quoted in agorot that the pipeline converts to USD). The empty template with null
  shares/weights lives in `tests/fixtures/config_template/`; `/committee-setup` edits the live files.
- `config/investor_profile.yaml` has an `investor` block (age, occupation, expected income, career-sector
  concentration, contribution plan) read by the CIO and the ETF analyst; fields may be null.
- Editing a config while a run is in progress does not affect that run (it uses its `inputs/` snapshot); the
  difference is reported as a warning.
- `sec_contact_email` must be a real address; SEC requests are refused otherwise.
- Never `git push`.
