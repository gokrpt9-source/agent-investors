# DESIGN — The Agent Investors (authoritative build contract)

`docs/SPEC.md` is the user's original spec (requirements). This file is the **design contract** every
builder follows. When SPEC and DESIGN differ, DESIGN is the deliberate interpretation (each difference
is also logged in `BUILD_NOTES.md`). `CONTRACTS.md` (repo root) is the *as-built* API reference for
`scripts/common.py` and conventions, written in build step U0.

Repo root: `C:\Users\mrkrp\agent-investors` (Windows 11, PowerShell + Git Bash; Python 3.13 installed,
code must be **Python 3.11-compatible**; pandas 3.0.1, numpy 2.x, yfinance 1.2.0, jsonschema 4.26,
jinja2 3.1.6, pyyaml, requests, pytest installed globally). Today = 2026-10-03.

---

## 0. Global conventions (every file obeys)

- **pathlib only**; no bash-only scripts; every CLI works in PowerShell (`python scripts/x.py ...`).
- **UTF-8 everywhere**: every `open()` / `read_text()` / `write_text()` passes `encoding="utf-8"`;
  CSV opened with `newline=""`; JSON written with `ensure_ascii=False, allow_nan=False, indent=2`
  via atomic write (temp file in same dir + `os.replace`, 3 retries on `PermissionError`).
  CLIs call `common.setup_stdio()` (reconfigure stdout/stderr to utf-8, errors="replace").
- **pandas 2.2–3.x compatible**: no `applymap`, no `fillna(method=...)`, no chained assignment,
  `pd.read_json(io.StringIO(s))`, positional access via `.iloc`, freq aliases `ME`/`B`.
- **No NaN/inf in JSON** — convert with `common.to_jsonable` (NaN/inf → None).
- **Dates** `YYYY-MM-DD`; timestamps ISO-8601 with UTC offset. "Today" comes from
  `common.today()` which honours env `AGINV_AS_OF` (deterministic tests/dry-run).
- **Determinism**: no randomness except seeded; all thresholds read from `config/committee.yaml`.
- **Script bootstrap**: every script in `scripts/` begins with
  ```python
  import sys
  from pathlib import Path
  if __package__ in (None, ""):
      sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
  from scripts import common  # noqa: E402
  ```
  so both `python scripts/x.py ...` and `import scripts.x` work. `scripts/__init__.py` exists (empty).
- Every script exposes a pure-ish `run(...)`/function API **and** `main(argv=None) -> int` with argparse;
  `if __name__ == "__main__": sys.exit(main())`.
- Common CLI flags (all scripts that touch them): `--config-dir`, `--cache-dir`, `--runs-dir`,
  `--ledger`, `--as-of`. Env equivalents: `AGINV_CONFIG_DIR`, `AGINV_CACHE_DIR`, `AGINV_RUNS_DIR`,
  `AGINV_LEDGER`, `AGINV_AS_OF`. Plus `AGINV_OFFLINE=1` (no network; cache only) and
  `AGINV_FIXTURES_DIR=<dir>` (all network replaced by fixture files — see §3.3).
- **File naming**: data/report files are always `<kind>_<TICKER>.json` (never bare `<TICKER>.json`;
  Windows reserves PRN/CON/AUX/NUL). Tickers normalized by `common.norm_ticker` (`brk.b`→`BRK-B`, upper).
- **Units**: weights/percentages are percent numbers 0–100 (`unit: "pct"`); multiples `unit: "x"`;
  money `USD`; per-share `USD/share`; counts `shares`.
- **Sector keys** (11 GICS, snake_case) + pseudo-sector `funds`:
  `information_technology, health_care, financials, energy, industrials, consumer_discretionary,
  consumer_staples, utilities, materials, real_estate, communication_services`.
  Display names + benchmark ETFs: XLK, XLV, XLF, XLE, XLI, XLY, XLP, XLU, XLB, XLRE, XLC.
  `common.SECTORS` (ordered dict key→{name, etf}). Look-through also uses `cash` and `unclassified`.
- **Disclaimer** constant `common.DISCLAIMER` = "Research output for decision support only — not
  personalized financial advice. Figures come from free public sources that may be delayed or
  incomplete. The investor makes all decisions." Every memo starts with it.
- **Never** `git push`. Never reference the model "opus" anywhere in the repo.

---

## 1. Provenance model

### 1.1 Fact
Every numeric value in every script output is a **Fact**:
```json
{"value": 12.3, "unit": "pct", "source_id": "D:SEC:KO:CF", "as_of": "2026-04-03",
 "kind": "fundamental", "na_reason": null,
 "filed": "2026-04-30", "tag": "Revenues", "derivation": "FY2025+3M2026-3M2025"}
```
- required keys: `value, unit, source_id, as_of, kind, na_reason`; optional: `filed, tag, derivation,
  observation_date, period_end, note`.
- `kind` ∈ `price | fundamental | macro | other` (drives freshness).
- `value: null` ⇒ `na_reason` ∈ `missing | not_meaningful | negative_denominator |
  insufficient_history | not_applicable`. `not_meaningful`/`not_applicable` are **not** penalized.
- Helper `common.fact(value, unit, source_id, as_of, kind, na_reason=None, **extra)`; if value is None
  and na_reason None → `"missing"`. Non-finite → None.

### 1.2 Source
`{"id","url","title","accessed","kind":"dataset|web","path"?}`. `url` is always an http(s) URL
(upstream). Dataset ids are deterministic:

| id | url |
|---|---|
| `D:PX:<T>` | `https://finance.yahoo.com/quote/<T>/history/` (yfinance) or the Stooq CSV URL if provider=stooq |
| `D:YF:<T>:INFO` | `https://finance.yahoo.com/quote/<T>/` |
| `D:YF:<T>:FUND` | `https://finance.yahoo.com/quote/<T>/holdings/` |
| `D:SEC:<T>:CF` | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` |
| `D:SEC:<T>:SUB` | `https://data.sec.gov/submissions/CIK##########.json` |
| `D:SEC:MAP` | `https://www.sec.gov/files/company_tickers.json` |
| `D:FRED:<SERIES>` | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` |
| `D:VAL:<T>` | companyfacts URL of T (derived grid; `path` = local valuation file) |
| `D:CFG:<name>` | `source_url` declared inside the config entry (configs carrying numbers must declare one) |

Agents copy dataset sources verbatim (the work order lists them as `seed_sources`) and add their own web
sources with ids `W1..Wn`.

### 1.3 Envelope (every script output file)
```json
{"schema": "<name>/v1", "generated_at": "<iso ts>", "as_of": "YYYY-MM-DD",
 "sources": [Source...], "missing_data": ["..."], "flags": [{"code","severity","detail"}], ...payload}
```
`common.envelope(schema_name, as_of, **payload)`.

### 1.4 data_ref (anti-hallucination)
Agent `key_figures[]` entries that cite a `D:*` source **must** carry
`data_ref: "<abs-or-repo-relative path>#<JSON pointer to the Fact or number>"`, e.g.
`".../phase2/data/fundamentals_KO.json#/ttm/revenue"`. `validate.py` resolves it (pointer may point at a
Fact — compare `.value` — or a bare number) and requires |agent − data| ≤ 0.5% (or ≤ 1e-6 abs when data is 0).
Mismatch → validation error → retry. Unit conversions are not allowed: agents copy numbers as given.

---

## 2. Run directory & work orders

```
runs/<run_id>/                     run_id = YYYY-MM-DD_HHMM (suffix _2,_3 if taken; dry-run adds _dryrun)
  manifest.json  run_log.md
  inputs/        *.yaml snapshot of config + config_sha256 in manifest
  orders/        index.json + <order_id>.md
  phase1/        prices.json macro.json lookthrough.json macro_view.json cio_allocation.json
  phase2/        data/{technicals,fundamentals,valuation}_<T>.json
                 reports/<role>_<T>.json  (retry → <role>_<T>.r2.json; failure stub → <role>_<T>.failed.json)
                 evidence.json aggregate.json packets/sector_<s>.json <sector>_verdict.json
  phase3/        screen_<sector>.json data/... reports/... evidence_light.json aggregate_light.json
                 evidence.json aggregate.json packets/... <sector>_verdict.json
  phase4/        portfolio_preview.json cio_packet.json cio_final.json portfolio_plan.json
                 memo.md memo.html summary.json  (dry-run: ledger_preview.csv)
```

**Role keys** (schemas `agent_role` enum): `fundamental, technical, operator_ceo, industry_ops, osint,
devils_advocate, etf, macro, cio, sector_head`.
**Agent types** (`.claude/agents/<name>.md`): `fundamental-analyst, technical-analyst, operator-ceo,
industry-ops, osint-sentiment, devils-advocate, etf-analyst, macro-geopolitics, cio, sector-head`.
Report filename prefix = role key (`fundamental_KO.json`, `devils_advocate_KO.json`, `etf_QQQ.json`).

### 2.1 Work order file (`orders/<order_id>.md`)
order_id examples: `p1-macro`, `p1-cio`, `p2-a-fundamental-KO`, `p2-b-devils_advocate-KO`,
`p2-sh-consumer_staples`, `p3-light-technical-MRK`, `p3-full-osint-MRK`, `p3-da-MRK`,
`p3-sh-health_care`, `p4-cio`.
```markdown
---
order_id: p2-a-fundamental-KO
run_id: 2026-10-03_1015
step: p2.wave_a
agent_type: fundamental-analyst
agent_role: fundamental
mode: null                # cio: allocation|reconciliation ; sector_head: phase2|phase3
ticker: KO                # null for macro/cio/sector_head
sector: consumer_staples
sector_class: default     # default|financials|fund
attempt: 1
as_of: 2026-10-03
inputs:                   # the ONLY files the agent may read (absolute posix paths)
  - {path: C:/Users/.../phase2/data/fundamentals_KO.json, purpose: "SEC fundamentals + ratios"}
  - {path: C:/Users/.../knowledge/sectors/consumer_staples.md, purpose: "sector playbook"}
output: C:/Users/.../phase2/reports/fundamental_KO.json
schema: C:/Users/.../schemas/specialist_report.json
seed_sources: [ {id: "D:SEC:KO:CF", url: ..., title: ..., accessed: ..., kind: dataset, path: ...}, ... ]
required_fields: [extensions.valuation.fair_value_base, invalidation_price, conviction]
prior_errors: []          # filled on retry with validate.py messages
word_limits: {thesis: 60}
---
# Work order — fundamental-analyst on KO (consumer_staples)
(body: rendered rules — read only listed inputs; cite every number; data_ref for D:* figures;
 null + missing_data when unsourced; paraphrase, never quote >25 words; "up to 3" never padded;
 write exactly one JSON file to `output` with the Write tool; final reply exactly `DONE <output>` or
 `FAILED <reason>`; this is research, not advice.)
```
The Task prompt the orchestrator sends is ONE line: `Execute work order <order_path>`.

### 2.2 `committee.py` CLI (thin orchestration)
- `init [--quick] [--sectors a,b] [--holding T] [--dry-run] [--run-id ID] [--as-of D] [dirs]`
  → validates configs (fails with actionable message, e.g. null holdings → "run /committee-setup"),
  creates run dir, snapshots configs, writes manifest + run_log; prints JSON `{run_id, run_dir, mode}`.
- `next --run ID` → idempotent state machine. Executes pending **python** steps inline (stops early
  and returns `{"type":"continue"}` if >90 s elapsed so the caller's shell timeout is never hit).
  At an **agents** step: if its orders were already dispatched, it first checks outputs: valid →
  done; invalid/missing on attempt 1 → new retry order (attempt 2, output `.r2.json`, `prior_errors`);
  invalid/missing on attempt 2 → writes `<role>_<T>.failed.json` stub, marks failed (evidence 1).
  Returns pending orders:
  ```json
  {"run_id":"...","step":"p2.wave_a","type":"agents","concurrency":8,
   "orders":[{"order_id":"p2-a-fundamental-KO","agent_type":"fundamental-analyst","order_path":"C:/.../orders/p2-a-fundamental-KO.md"}],
   "summary":"Wave A: 14 orders (KO, PG, JPM, BRK-B x4; QQQ, SPMO x2)"}
  ```
  `{"type":"done","memo_md":...,"memo_html":...,"summary":...}` at the end.
  Contract: the orchestrator calls `next` again only after every emitted order has finished.
- `redispatch --run ID` → re-emit dispatched orders without counting a failed attempt (crash recovery).
- `status --run ID` → compact progress JSON. `validate-config` → human-readable config errors.
- `holding T` → init in holding mode (Phase 1 reused from the latest run ≤7 days old else re-run;
  Phase 2 for one ticker; held → holding rules, not held → candidate rules; trial memo
  `phase4/trial_<T>.md/.html`; ledger append).
- `dry-run [--runs-dir] [--fixtures-dir tests/fixtures/dryrun] [--as-of 2026-10-02]` → whole pipeline
  offline: `AGINV_FIXTURES_DIR` data providers + fixture agent outputs copied into each order's output
  path; ledger rows go to `phase4/ledger_preview.csv`; prints memo paths.
- `scorecard [--ledger]` → `ledger.py score` wrapper; writes `ledger/scorecard_<date>.md`.

### 2.3 Step machine (full run)
1 `p1.data` (py): fetch_macro→phase1/macro.json; fetch_prices(holdings+proxies+SPY+11 sector ETFs+`ILS=X`)
  →phase1/prices.json; etf_lookthrough→phase1/lookthrough.json.
2 `p1.macro` (agents): macro-geopolitics → phase1/macro_view.json.
3 `p1.cio` (agents): cio mode=allocation → phase1/cio_allocation.json.
4 `p2.data` (py): classify; stocks: compute_technicals + fetch_fundamentals + valuation; funds: compute_technicals.
5 `p2.wave_a` (agents): stocks × `roles.phase2_stock` (quick: `roles.quick_stock`); funds × `roles.phase2_fund`.
6 `p2.wave_b` (agents): devils-advocate per stock (funds only if `roles.da_for_funds`).
7 `p2.aggregate` (py): evidence_score + aggregate → evidence.json, aggregate.json, sector packets.
8 `p2.sector_heads` (agents): one per sector present (incl. `funds`) → phase2/<sector>_verdict.json.
9 `p3.screen` (py; skipped in quick/holding): expand_sectors ∩ --sectors, capped at
  `phase3.max_expand_sectors` (log what was dropped) → screen + data for shortlist.
10 `p3.light` (agents) → 11 `p3.finalists` (py: evidence+aggregate light, top `phase3.finalists` by
  agg_conviction then agg_evidence) → 12 `p3.full` (agents) → 13 `p3.da` (agents) →
  14 `p3.aggregate` (py) → 15 `p3.sector_heads` (agents; ≤3 BUY/sector).
16 `p4.preview` (py): portfolio_preview.json from verdicts + cio_packet.json.
17 `p4.cio` (agents): cio mode=reconciliation → phase4/cio_final.json.
18 `p4.finalize` (py): portfolio_plan.json, memo.md/html, ledger append, summary.json → done.

---

## 3. Shared module `scripts/common.py` (U0 owns)

Must provide (names fixed; document exact signatures in CONTRACTS.md):
- Paths: `REPO_ROOT`, `config_dir()`, `cache_dir()`, `runs_dir()`, `ledger_path()`, `schemas_dir()`,
  `knowledge_dir()`, `templates_dir()` (env/flag aware); `add_common_args(parser)` +
  `apply_common_args(args)` (sets env from flags).
- Config: `load_yaml(path)`, `load_config(name)` (`investor_profile|portfolio|universe|committee|etf_overrides`),
  `committee_cfg()` cached per config dir.
- IO: `read_json`, `write_json` (atomic), `write_text` (atomic utf-8), `setup_stdio()`, `to_jsonable`,
  `safe_float`, `norm_ticker`, `safe_name`.
- Provenance: `fact`, `source`, `envelope`, `FactKind`, `NA_REASONS`.
- Time: `today()`, `now_iso()`, `parse_date`, `trading_days_between(d1, d2, holidays)`
  (numpy busday_count with `committee.yaml: market_holidays`).
- Sectors: `SECTORS`, `SECTOR_KEYS`, `sector_display(key)`, `sector_etf(key)`,
  `YF_SECTOR_MAP` (Yahoo info names → keys), `YF_FUND_SECTOR_MAP` (funds_data keys → keys),
  `sic_to_sector(sic:int) -> str|None` (SIC range map, rough fallback).
- Cache: `DiskCache(root=None)` with `get(kind, key, ttl_hours) -> dict|None` and
  `put(kind, key, payload, source_url)`; layout `data/cache/<kind>/<safe key>.json` =
  `{"fetched_at","source_url","payload"}` (DataFrames cached as CSV next to it via helper
  `put_frame/get_frame`). **Never cache errors.** `get_stale(kind,key)` returns expired entries
  (for flagged stale fallback).
- Network seam (ALL network goes through these):
  `http_get(url, *, headers=None, timeout=30, sec=False) -> requests.Response-like`,
  `http_get_text`, `http_get_json`; `sec=True` adds UA `agent-investors research <sec_contact_email>`
  (refuse if email missing/placeholder) and a process-wide rate limiter (≥0.11 s between SEC calls);
  retry with exponential backoff on 429/5xx/connection errors (honour Retry-After).
  `yf_history(ticker, period="3y") -> DataFrame[Open,High,Low,Close,Volume]` (auto_adjust=True,
  drop non-finite Close rows, tz-naive index via `tz_localize(None)`), `yf_info(ticker) -> dict`,
  `yf_funds(ticker) -> {"sector_weightings":{yf_key: frac}, "top_holdings":[{"symbol","name","holding_pct"}],
  "expense_ratio_pct": float|None, "expense_ratio_field": str}`.
  - `AGINV_OFFLINE=1` → raise `OfflineError` instead of any network call.
  - `AGINV_FIXTURES_DIR=<dir>` → served from fixtures, never network:
    `<dir>/http/index.json` maps URL→relative file (`.json`/`.csv`/`.txt`);
    `<dir>/yf/history/<T>.csv` (Date,Open,High,Low,Close,Volume), `<dir>/yf/info/<T>.json`,
    `<dir>/yf/funds/<T>.json` (already in the normalized `yf_funds` shape). Missing fixture → `FixtureMissing`.
- Exceptions: `DataUnavailable`, `ProviderBlocked` (e.g. Stooq bot wall), `OfflineError`, `FixtureMissing`, `ConfigError`.
- `retry(fn, attempts=3, base_delay=1.0)`.
- Constants: `DISCLAIMER`, `ROLE_KEYS`, `AGENT_TYPES` (role→agent type), `SCHEMA_FILES`.

### 3.1 tests/conftest.py (U0 owns)
- autouse fixture blocking real network (monkeypatch `socket.socket.connect` to raise) + sets
  `AGINV_OFFLINE=1` unless a test opts into fixtures; `AGINV_AS_OF=2026-10-02` default.
- fixtures: `tmp_env` (isolated config/cache/runs/ledger dirs under tmp_path, copying repo config),
  `make_ohlcv(n=800, seed=0, drift=0.0003, vol=0.015, start=100.0, end="2026-10-02")` (seeded,
  business-day index), `fixtures_dir` helper, `repo_root`.

---

## 4. Script contracts (payload shapes; all wrapped in the Envelope)

### 4.1 fetch_prices.py (U1) → `phase1/prices.json` (schema `prices/v1`)
API: `run(tickers, out=None, *, as_of=None, cache=None, period="3y") -> dict`;
CLI `--tickers KO,SPY` or `--ticker KO`, `--out`.
Order: cache (TTL `cache_ttl_hours.prices`) → yfinance → Stooq (`https://stooq.com/q/d/l/?s=<t>.us&i=d`;
detect HTML/JS bot-check or "No data" → `ProviderBlocked`; NEVER try to solve/bypass it) →
stale cache (flag `stale_cache`, high) → failed. FX `ILS=X` (Stooq `usdils`).
Series cached as `data/cache/prices/prices_<T>.csv` (Date,Open,High,Low,Close,Volume).
```json
{"tickers": {"KO": {"last_close": Fact, "last_date": "...", "first_date": "...", "rows": 753,
   "provider": "yfinance|stooq|cache_stale", "stale": false, "adv_usd_63d": Fact,
   "returns_pct": {"1m": Fact, "3m": Fact, "6m": Fact, "12m": Fact}, "series_path": "..."}},
 "failed": {"XYZ": "reason"}, "fx": {"usdils": Fact|null}}
```

### 4.2 compute_technicals.py (U1) → `technicals_<T>.json` (`technicals/v1`)
API: `compute(ohlcv: DataFrame, bench: DataFrame, sector_etf_df: DataFrame|None, *, ticker,
sector_etf, sources, cfg) -> dict`; `run(ticker, out, *, sector_etf=None, as_of=None)` loads cached
series (fetching if absent). Port + fix stock_dashboard indicators (see §8). Sources list **three**
price sources (ticker, SPY, sector ETF) so the technical report can reach 3 distinct sources.
```json
{"ticker":"KO","benchmark":"SPY","sector_etf":"XLP","close": Fact,
 "trend": {"sma20":Fact,"sma50":Fact,"sma200":Fact,"sma200_slope_20d_pct":Fact,"close_vs_sma200_pct":Fact,
           "regime":"trend_up|trend_down|range|choppy|transition","regime_tests":[{"test","value","pass"}],
           "structure":"HH_HL|LH_LL|transition|unknown"},
 "momentum": {"rsi14":Fact,"macd":{"line":Fact,"signal":Fact,"hist":Fact},"adx14":Fact,"pdi14":Fact,"ndi14":Fact},
 "volatility": {"atr14":Fact,"atr14_pct":Fact,"atr_pctile_252":Fact},
 "range": {"high_52w":Fact,"low_52w":Fact,"pct_from_high_52w":Fact,"drawdown_from_3y_peak_pct":Fact,"max_drawdown_1y_pct":Fact},
 "relative_strength": {"vs_spy":{"3m":Fact,"6m":Fact,"12m":Fact},"vs_sector":{"3m":Fact,"6m":Fact,"12m":Fact}},
 "volume": {"avg20":Fact,"avg50":Fact,"ratio_20_50":Fact,"trend":"rising|falling|flat","adv_usd_63d":Fact},
 "levels": {"supports":[{"id":"S1","price":Fact,"touches":2,"last_date":"..."}],"resistances":[{"id":"R1",...}]},
 "stop_candidates": [{"id":"STOP1","price":Fact,"basis":"below_support_0.5atr|close_minus_2atr|below_sma200"}],
 "weekly_closes_52w": [["YYYY-MM-DD", 61.2], ...]}
```
RS = excess return in percentage points on **date-aligned** series: `(1+r_t)/(1+r_b) − 1` × 100 over 63/126/252 trading days.
Regime rules (first match, thresholds in committee.yaml `technicals`): choppy (≥4 SMA50 crosses in
20 bars AND atr_pctile>80 AND ADX<20) → trend_up (ADX≥25, +DI>−DI, close>sma50>sma200, sma200 slope>0)
→ trend_down (ADX≥25, −DI>+DI, close<sma50<sma200, slope<0) → range (ADX<20 AND 20-bar range ≤10% of
midpoint) → transition. Fewer than 200 bars → sma200 null (insufficient_history), regime from the
available tests, flag.

### 4.3 fetch_fundamentals.py (U2) → `fundamentals_<T>.json` (`fundamentals/v1`)
API: `run(ticker, out, *, price=None, as_of=None, sector_class=None) -> dict`. EDGAR via
`common.http_get(..., sec=True)`; map cached 7 d; companyfacts/submissions cached per TTL.
```json
{"ticker":"KO","cik":21344,"coverage":"sec|partial|non_sec",
 "entity":{"name","sic","sic_description","fiscal_year_end","sector_class":"default|financials"},
 "companyfacts_latest":{"form","filed","period_end","accn"},"submissions_latest":{"form","filed","report_date","accn"},
 "lag_detected":false,
 "price":Fact,"shares":Fact,"market_cap":Fact,
 "ttm":{"revenue","gross_profit","operating_income","net_income","ocf","capex","fcf","da","ebitda","pretax_income","income_tax"}: Fact each,
 "fy":[{"fy":2025,"period_end":"...","revenue":Fact,"operating_income":Fact,"net_income":Fact,"ocf":Fact,"capex":Fact,"fcf":Fact}],
 "balance":{"cash","st_investments","total_debt","net_debt","equity","goodwill","intangibles"}: Fact each,
 "margins_pct":{"gross","operating","net","fcf"}: Fact each,
 "growth_pct":{"revenue_cagr_3y","ni_cagr_3y","fcf_cagr_3y","revenue_yoy_ttm"}: Fact each,
 "valuation":{"ev","pe","ev_ebit","ev_sales","fcf_yield_pct","net_debt_ebitda","p_b","p_tbv","roe_pct","roic_pct","tax_rate_used"}: Fact each,
 "tag_map":{"revenue":{"tag":"Revenues","stitched":false}, ...}}
```
**EDGAR algorithm** (must implement exactly; tests with trimmed real-shape fixtures):
1. CIK from company_tickers.json (`.`→`-`). None → `coverage:"non_sec"`, every value null
   (`na_reason:"missing"`), `missing_data` entry "not an SEC filer".
2. Fetch companyfacts + submissions (UA from investor_profile `sec_contact_email`; ≤10 req/s).
3. Concept → candidate tags (us-gaap, USD unless noted), priority-ordered:
   revenue: Revenues, RevenueFromContractWithCustomerExcludingAssessedTax,
   RevenueFromContractWithCustomerIncludingAssessedTax, SalesRevenueNet, RevenuesNetOfInterestExpense ·
   cogs: CostOfRevenue, CostOfGoodsAndServicesSold · gross_profit: GrossProfit (else revenue−cogs) ·
   operating_income: OperatingIncomeLoss · net_income: NetIncomeLoss, ProfitLoss ·
   ocf: NetCashProvidedByUsedInOperatingActivities, NetCashProvidedByUsedInOperatingActivitiesContinuingOperations ·
   capex: PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets ·
   da: DepreciationDepletionAndAmortization, DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet ·
   pretax: IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest,
   IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments ·
   income_tax: IncomeTaxExpenseBenefit · cash: CashAndCashEquivalentsAtCarryingValue,
   CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents · st_investments: ShortTermInvestments,
   MarketableSecuritiesCurrent · equity: StockholdersEquity,
   StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest · goodwill: Goodwill ·
   intangibles: IntangibleAssetsNetExcludingGoodwill · shares: dei:EntityCommonStockSharesOutstanding,
   us-gaap:CommonStockSharesOutstanding, WeightedAverageNumberOfDilutedSharesOutstanding (unit shares).
4. Debt (formula-ordered, first whose components all exist at the latest common instant):
   (a) LongTermDebt + STB; (b) LongTermDebtNoncurrent + LongTermDebtCurrent + STB;
   (c) LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities + STB;
   (d) LongTermDebtAndCapitalLeaseObligations + LongTermDebtAndCapitalLeaseObligationsCurrent + STB.
   STB = ShortTermBorrowings else CommercialPaper else 0 (flag `stb_assumed_zero`). Never add DebtCurrent
   together with explicit current-maturities tags. Record formula in `derivation`.
   A formula only qualifies if its latest instant is within 400 days of the newest balance-sheet instant
   (tag drift: KO's LongTermDebt stops in 2024 — must fall through to (c)/(d)).
5. Normalize: forms 10-K, 10-Q, 10-K/A, 10-Q/A (20-F/40-F accepted for FY). Instant = no `start`;
   duration buckets by days: Q 80–100, H 170–200, 9M 260–290, FY 350–380 (else drop). Dedupe by
   (start,end) keeping latest `filed`.
6. Tag choice: current value = candidate tag with latest `end` (ties → priority). History: per FY first
   tag with a value; `stitched:true` + flag `tag_mix` if tags differ inside the 3-y window.
7. TTM (flows): latest is FY → FY. Else FY_prev + YTD_cur − YTD_prior (same bucket, prior end 1y ± 10 d).
   Fallback: last 4 contiguous discrete quarters (Q4 = FY − 9M; ends 84–98 days apart). Record
   `derivation` (e.g. `FY2025+6M2026-6M2025`). Neither → null `insufficient_history`.
8. Instants: latest `end`; balance components must share it else flag `mixed_instants`.
9. 3y CAGR: FY_latest vs FY_latest−3, both > 0 else null `negative_denominator`; plus TTM YoY.
10. Multi-class shares: several dei values with same accn & end → sum by default; overrides in
   `config/etf_overrides.yaml: securities.<T>.share_class` (`class_factor`, `shares_tags`).
   Cross-check vs yfinance `marketCap` (`D:YF:<T>:INFO`): divergence >10% → flag
   `share_count_ambiguous` (high) and market cap Fact uses yfinance (`derivation:"yfinance_marketCap"`).
11. Financials (`sector_class=financials`: SIC 6000–6799 except 6798 REITs, or sector financials):
   EV, EV/EBIT, EV/Sales, net_debt/EBITDA, gross margin → `not_meaningful`; compute P/B, P/TBV =
   mcap/(equity−goodwill−intangibles), ROE = NI_TTM / equity. Revenue falls back to RevenuesNetOfInterestExpense.
12. Ratios: EV = mcap + debt − cash − st_investments; P/E null (`negative_denominator`) if NI ≤ 0;
   FCF = OCF − capex (capex positive outflow); EBITDA = operating income + D&A; tax rate = tax/pretax
   clamped [0, 35%] default 21% (flag); ROIC = EBIT·(1−t)/(equity + net_debt) (null if denominator ≤ 0).
13. Lag detection: newest 10-Q/10-K in submissions `filings.recent`; if its accession isn't among
   companyfacts accns → `lag_detected:true`, flag `companyfacts_lag` (high), missing_data
   "latest <form> for <report_date> (filed <date>) not yet in companyfacts — read it directly: <url>".
14. Fundamental Facts: `as_of` = period end, `filed` = filing date, `kind:"fundamental"`.

### 4.4 valuation.py (U4) → `valuation_<T>.json` (`valuation/v1`)
API: `build_grid(fundamentals: dict, price: float, sector: str, cfg) -> dict`; `run(ticker, fundamentals_path, out)`.
Implements the user's course method (NOPAT DCF with exit multiple, reverse DCF first, r 9–10%).
- Base cash flows: `nopat = (EBITDA_ttm − capex_ttm) × (1 − tax)` ("course NOPAT") and `fcf = FCF_ttm`.
- DCF cells: base ∈ {nopat, fcf} × r ∈ `valuation.discount_rates` [0.09, 0.10] × g ∈
  `valuation.growth_tiers` [0.00, 0.03, 0.06, 0.10, 0.15]; `years` 5; exit multiple from
  `valuation.exit_multiple_by_growth` (piecewise: g≤0→10, ≤0.04→12, ≤0.07→15, ≤0.11→18, else 22);
  EV = Σ CF·(1+g)^t/(1+r)^t + CF_N·M/(1+r)^N; equity = EV − net_debt; fv/share = equity/shares;
  `tv_share` = PV(TV)/EV; flag `tv_gt_90` when > 0.9. Base CF ≤ 0 → cells `na` with reason.
- Multiple cells from `valuation.multiple_bands[<sector>]` (low/mid/high; configured assumptions,
  labeled as such): default sectors `pe` & `ev_ebit`; financials `pe` & `p_b`. fv from TTM EPS
  (NI/shares), EBIT (EV→equity bridge), book value per share.
- Reverse DCF: bisection on g ∈ [−0.30, 0.60] with base nopat, r 0.10, exit multiple
  `valuation.reverse_dcf_exit_multiple` (15) → `implied_growth_pct` (null if no sign change).
- Cell: `{"id":"DCF-NOPAT-r10-g06","method":"dcf_nopat|dcf_fcf|pe|ev_ebit|p_b","params":{...},
  "fv_per_share": float|null, "tv_share": float|null, "flags":[], "na_reason": null}`.
Output `{"ticker","price":Fact,"shares":Fact,"net_debt":Fact,"base_cf":{"nopat":Fact,"fcf":Fact},
"reverse_dcf":{"implied_growth_pct":Fact,"assumptions":{...}},"cells":[...],"methods_available":[...]}`.
Fundamental analyst cites cells: `extensions.valuation.cells_low/base/high` (ids);
`fair_value_x` must equal mean(fv of cited cells) ±0.5%; `cells_base` must span ≥2 method families
(dcf vs multiple) when both are available.

### 4.5 fetch_macro.py (U3) → `phase1/macro.json` (`macro/v1`)
API: `run(series=None, out=None, *, as_of=None)`. FRED CSV (`observation_date,<ID>`; '.' = missing).
Series + metadata in committee.yaml `macro.series` ({id, name, frequency d|w|m, units, change: diff|pct, yoy: bool}).
Per series: `{"latest":Fact,"observation_date","frequency","chg_3m":Fact,"chg_12m":Fact,"yoy_pct":Fact|null,
"history_tail":[[date,val]×13]}`; change lookups by date (last obs ≤ latest−91 d / −365 d). Monthly series:
Fact `as_of` = **period end** (month end) and `observation_date` kept. CPIAUCSL & CPILFESL yoy computed.

### 4.6 etf_lookthrough.py (U3) → `phase1/lookthrough.json` (`lookthrough/v1`)
API: `run(portfolio_cfg, prices, out, *, as_of=None)`; also `classify_security(ticker, *, info=None,
overrides=None, sic=None) -> {"sector","asset_type","classification_source","flags"}` used by others.
Classification order: portfolio `sector_override` → etf_overrides `securities.<T>` (BRK-B → financials,
flags [conglomerate]) → yfinance info sector (YF_SECTOR_MAP) → SIC map → `unclassified`.
Fund decomposition: `yf_funds` sector_weightings (renormalized to 100; residual → unclassified) →
fallback `etf_overrides.funds.<T>.sector_weights` (must carry source_url/as_of) → 100% unclassified + missing_data.
Weights: shares × price (prices.json, proxy_ticker if set) or `weight_pct` with `total_value_usd`;
cash from `cash_usd`/`cash_pct`.
```json
{"total_value_usd":Fact,"holdings":[{"ticker","asset_type":"stock|fund","weight_pct":Fact,"sector":"...",
  "sector_split":{"sector":pct},"classification_source","flags":[]}],
 "sector_weights_pct":{"<sector>":pct,...,"cash":pct,"unclassified":pct},     // sums to 100 ±0.01
 "funds":{"QQQ":{"expense_ratio_pct":Fact,"top10":[{"symbol","weight_pct"}],"top10_concentration_pct":Fact,"source_id"}},
 "overlap":{"pairs":[{"a","b","overlap_pct","basis":"top10"}],"direct_overlap":[{"fund","holding","fund_weight_pct"}]}}
```

### 4.7 screen.py (U3) → `phase3/screen_<sector>.json` (`screen/v1`)
API: `run(sector, out, *, held, as_of=None, fetch=True) -> dict`. Universe = `yf_funds(<sector ETF>)`
top holdings (top 10 only — documented) + `universe.yaml additions.<sector>`; filters: exclusions,
held, market cap ≥ `min_market_cap_usd` (yf info), 63-day avg dollar volume ≥ `min_avg_dollar_volume_usd`,
non-SEC → excluded. Composite = weighted mean of percentile ranks (within set) of quality
(operating margin, FCF margin, ROIC/ROE), valuation (FCF yield, inverse EV/EBIT or inverse P/E/P/B for
financials), momentum (6m & 12m RS vs SPY); missing input → imputed 0.5 and listed. Weights in
committee.yaml `screen.weights`. Shortlist = top `phase3.shortlist_size` (6).
```json
{"sector","benchmark_etf","universe":[{"ticker","origin":"etf_top10|universe_add","etf_weight_pct"}],
 "excluded":[{"ticker","reason":"held|exclusion|mcap|adv|no_data|non_sec"}],"weights":{...},
 "ranked":[{"ticker","rank","composite","components":{"quality":{"score","inputs"},...},"raw":{"mcap":Fact,"adv_usd":Fact},"imputed":[]}],
 "shortlist":["..."]}
```

### 4.8 evidence_score.py (U4) → `evidence.json`
API: `score_report(report, *, role, sector_class, run_date, cfg) -> {"score","deductions":[...]}`;
`score_run_reports(paths, ...)`. Rules (config `evidence`): start 10; −2 per required field
(`evidence.required_fields[role][sector_class|default]`, dotted paths) null/missing; −1 per stale key
figure (price: >3 trading days; fundamental: run_date − (`filed` or as_of + `fundamental_filing_lag_days`
45) > 120 d; macro: > 45 d; other: not checked; `value:null` figures skipped); −2 if < 3 distinct
source URLs; −1 per missing_data item max −4; clamp [1,10]. Failed stub → score 1, status failed.
```json
{"reports":{"<path>":{"agent_role","ticker","status":"ok|failed","score":7,"start":10,"floor":1,
  "deductions":[{"rule":"null_required|stale|few_sources|missing_data","detail","points":-2}]}}}
```

### 4.9 risk_reward.py (U4)
`risk_reward(price, fair_value_base, invalidation_price) -> {"upside_pct","downside_pct","rr","null_reason"}`.
Any None/≤0 input → rr null `missing_input`; invalidation ≥ price → rr null `price_below_invalidation`;
rr = upside/downside rounded 2 dp (may be negative when fv < price). CLI `--price --fv --inv`.

### 4.10 aggregate.py (U4) → `aggregate.json`
API: `aggregate_ticker(...)`, `run(phase_dir, *, kind, context, cfg) -> dict`.
- Specialists in mean: roles except `osint`, `devils_advocate`; failed → excluded from conviction,
  evidence 1 counted. effective conviction: bullish c, neutral `aggregation.neutral_value` (5),
  bearish 10−c, clamp [1,10]. `conviction_mean` = Σ(eff·ev)/Σ(ev). DA adj survives 0 / damaged −1 /
  broken −3 (DA failed → 0, noted). `agg_conviction` = clamp(mean+adj, 1, 10) rounded 1 dp
  (Decimal ROUND_HALF_UP); none eligible → null. `agg_evidence` = mean of all reports' scores (incl.
  OSINT, DA, failed=1) rounded 1 dp.
- rr: price = latest close; fv_base = fundamental report `extensions.valuation.fair_value_base`;
  downside stop (`invalidation`) = technical report's `invalidation_price` if 0 < inv < price, else the
  highest `stop_candidates[].price` below price in `technicals_<T>.json` (deterministic), else median of
  other specialists' invalidations that are < price; record `invalidation_source`
  (`technical_report|technicals_stop_candidate|specialist_median|none`). A **bearish** specialist's
  invalidation above price is normal (bear thesis invalidated by a rally) and is ignored for rr.
  Any **bullish or neutral** specialist whose `invalidation_price` ≥ price → auto flag
  `auto:invalidation_breached` (high, must_address, unresolved). Funds: rr not applicable.
  The fundamental lens may leave `invalidation_price` null (kill condition stated in
  `invalidation_rationale`).
- Flags: all red_flags collected as `{"flag_id":"<role>:<id>", ...}`; unresolved = flags from
  `aggregation.unresolved_flag_roles` ([osint]) with severity high & must_address, not answered by any
  `flag_responses[]` entry (`flag_ref` == flag_id, status mitigated|refuted) + auto flags.
- Context per ticker: position_pct, cap (`max_position_pct` or `max_fund_position_pct` for funds),
  sector look-through pct, cio target pct for the sector, `sector_breach_tolerance_pp`.
  limit_breach = position > cap OR (stock AND sector_pct > min(max_sector_pct, cio_target) + tol).
- **Holding precedence**: (1) agg_conviction null → TRIM if limit_breach else HOLD [insufficient_evidence];
  (2) agg_evidence < `min_evidence` (5) → TRIM if limit_breach else HOLD [insufficient_evidence];
  (3) SELL if conv ≤ `sell_max` (3.0) or DA broken [Sector Head may rebut via deviation];
  (4) TRIM if limit_breach; (5) ADD if conv ≥ 7.0, ev ≥ 6.0, rr ≥ 2.0 (funds: no rr test), no
  unresolved high flag, position < cap; (6) TRIM if sell_max < conv < `trim_below` (4.5); (7) HOLD.
- **Candidates**: BUY if conv ≥ 7.0, ev ≥ 6.0, rr ≥ 2.5, DA ≠ broken (and no unresolved high flag only
  if `candidate_block_on_unresolved_high_flag`, default false) else PASS.
- All comparisons on rounded values; every test recorded in `rule_trace`.
```json
{"tickers":{"KO":{"kind":"holding|candidate","asset_type","sector","sector_class",
  "inputs":{"price":Fact,"position_pct","cap_pct","sector_lookthrough_pct","cio_target_pct","limit_breach":false},
  "specialists":[{"role","path","status","stance","conviction","effective_conviction","evidence","in_mean"}],
  "conviction_mean","da":{"path","verdict","adj"},"agg_conviction","agg_evidence",
  "rr":{"price","fv_base","invalidation","invalidation_source","upside_pct","downside_pct","rr","null_reason"},
  "flags":{"all":[...],"unresolved":[...]},
  "rule_action","rule_label","rule_trace":[{"rule","test","result"}]}}}
```

### 4.11 portfolio.py (U5) → `portfolio_preview.json` / `portfolio_plan.json` (`portfolio_plan/v1`)
API: `build_plan(lookthrough, prices, actions: {T: {"action","conviction","asset_type","sector"}},
target_sector_weights, profile, cfg, *, fx=None) -> dict`.
Sizing (config `sizing`): SELL → 0; conviction TRIM → min(cap, cur·(1−`trim_fraction` 0.33)); breach
TRIM → min(cap − `breach_buffer_pp`, cur) and, for sector breaches, reduce TRIM-flagged stocks of that
sector lowest-conviction-first until sector ≤ target; ADD → min(cur + `add_step_pp` (2), cap, sector
headroom); BUY → min(`initial_position_pct` (3), cap, sector headroom); HOLD/PASS unchanged. Buys scaled
pro-rata so post-trade cash ≥ `target_cash_pct`; funds never sold to fund stock buys. Trades with
|value| < `min_trade_usd` dropped (`dropped_reason`). Shares rounded down to whole shares
(`allow_fractional_shares` false). Tax: SELL/TRIM with cost_basis & gain>0 → `est_tax_usd = 25% × gain`
+ note on Israeli real-gain rules (CPI/FX adjusted, consult advisor). FX USD/ILS from prices.json
`fx.usdils` → `value_ils`. Post-trade look-through sector check vs max_sector_pct and targets →
`constraint_checks` (pass|warn|fail).
```json
{"base_currency":"ILS","fx_usdils":Fact,"total_value_usd":Fact,"cash":{"current_pct","target_pct","post_pct"},
 "positions":[{"ticker","asset_type","sector","action","action_source","price","shares_cur","value_usd_cur",
   "weight_cur_pct","weight_tgt_pct","trade":{"side":"BUY|SELL|NONE","shares","value_usd","value_ils"},
   "dropped_reason","tax":{"applicable","est_gain_usd","est_tax_usd","note"}}],
 "sectors":[{"sector","cur_lookthrough_pct","post_lookthrough_pct","target_pct","max_pct","gap_pp","breach"}],
 "constraint_checks":[{"check","status","detail"}],"notes":[...]}
```

### 4.12 ledger.py (U5) — `ledger/recommendations.csv`
`append(rows, ledger_path)` dedupes on (run_id, ticker, role). Columns (exact order):
`run_id,rec_date,price_date,ticker,asset_type,sector,kind,role,action,rule_action,stance,conviction,evidence,rr,fair_value_base,invalidation_price,target_weight_pct,price_at_rec,spy_at_rec,sector_etf,sector_etf_at_rec,thesis,run_mode,report_path`
role ∈ committee | sector_head | each specialist role | devils_advocate (one row per role per ticker;
committee row carries the CIO final action). `rows_from_run(run_dir) -> list[dict]`.
`score(ledger_path, *, price_fn=None, as_of=None, min_age_days=20) -> dict`: per row `ret_pct,
spy_ret_pct, sector_ret_pct, excess_spy_pp, excess_sector_pp, days, hit`; hit: ADD/BUY or bullish →
excess_spy > 0; SELL/TRIM or bearish → excess_spy < 0; HOLD/PASS/neutral excluded; summary by role ×
action `{n, hit_rate, mean_excess_spy_pp, mean_excess_sector_pp}`. `price_fn(ticker, date)` injectable
(default uses fetch_prices cache). CLI `append --run ID`, `score [--out]`.

### 4.13 validate.py (U6)
`validate_file(path, schema_name=None, *, context=None) -> list[str]` (empty = valid) and
`validate_doc(doc, schema_name, *, context=None)`. JSON Schema via `referencing.Registry` built from
`schemas/*.json` (`$id: urn:agent-investors:<name>`), `Draft202012Validator(format_checker=FormatChecker())`.
Human-readable messages: `"<json path>: <message> (got <short value>)"`. Semantic checks:
word limits (thesis ≤60, sector rationale ≤80, etc.); every `evidence_refs`/`source_id`/`source_ids`
resolves to `sources[].id`; `data_ref` value-match (§1.4); fundamental cell-mean match (§4.4);
technical invalidation equals a referenced level/stop price ±0.5% and `trend_regime` equals computed
regime; DA `bear_case_ref` match; macro scenario probabilities sum to 1 ±0.01; sector weights sum to
100 ±0.5; CIO targets outside macro bounds require a `constraint_overrides` entry; `expand_sectors` ≤
`max_expand_sectors`; sector verdict `rule_action`/agg values equal aggregate.json and action ≠
rule_action ⇒ `rule_deviation:true` + justification ≥15 words; ≤3 BUY per sector; cio_final action ≠
sector verdict action ⇒ `override:true` + justification; non-http(s) URLs rejected; any quoted span
(text inside quotation marks) > 25 words rejected (copyright guard). `context` = dict of paths
(`technicals`, `fundamentals`, `valuation`, `aggregate`, `macro`, `macro_view`, `lookthrough`,
`verdicts`, `committee_cfg`) — committee.py builds it from the order's inputs.
CLI: `python scripts/validate.py --file X [--schema NAME] [--order ORDER_MD]` → exit 0/1, prints errors.

### 4.14 render_memo.py (U7)
`build_context(run_dir) -> dict`; `render(run_dir, *, out_md=None, out_html=None, mode="full|trial", ticker=None)`.
Jinja2 templates `templates/memo.md.j2`, `templates/memo.html.j2` (HTML: `autoescape=True`, inline CSS,
no external assets, light+dark via `prefers-color-scheme`), markdown table cells escaped (`|`, newlines).
Exact section headings (both formats): `1. Disclaimer`, `2. Executive Summary`,
`3. Macro Regime, Scenarios & Sector Constraints`, `4. Current vs Target Portfolio`, `5. Holding Verdicts`,
`6. New Proposals`, `7. Proposed Rebalancing Plan`, `8. Devil's Advocate Highlights & Dissents`,
`9. Overrides & Rule Deviations`, `10. Data Gaps & Low-Evidence Warnings`, `11. Appendix: Sources`.
Missing inputs render "Not available in this run" (never crash). Holding verdicts table columns:
ticker · action · conviction · evidence · rr · one-line why. Overrides section lists sector-head rule
deviations, CIO macro-bound overrides, CIO action overrides — each with its justification.
Trial mode (`/committee-holding`) renders sections 1, 5, 8, 9, 10, 11 for one ticker.

---

## 5. Agent-output schemas (U0 writes; JSON Schema 2020-12, `$id: urn:agent-investors:<name>`)
Files: `schemas/specialist_report.json, devils_advocate_report.json, etf_review.json,
sector_verdict.json, macro_view.json, cio_allocation.json, cio_final.json, failed_report.json,
common_defs.json` (shared `$defs`: source, key_figure, red_flag, flag_response, evidence_ref, sector_key,
word-limited strings via maxLength as a coarse bound). Data contracts: `schemas/data/{prices,technicals,
fundamentals,valuation,macro,lookthrough,screen,evidence,aggregate,portfolio_plan,manifest,work_order}.schema.json`
(may be permissive with `additionalProperties: true` but must pin required keys/types). Config schemas:
`schemas/config_{investor_profile,portfolio,universe,committee,etf_overrides}.schema.json`.

Common report fields (specialist / DA / etf): `schema_version` ("1.0"), `run_id`, `order_id`,
`agent_role`, `sector`, `ticker`, `as_of` (date), `sources[]` (minItems 1), `missing_data[]`,
`red_flags[] {id ^F[0-9]+$, flag, severity low|medium|high, must_address bool, source_ids[]}`,
`flag_responses[] {flag_ref "<role>:F<n>", status mitigated|refuted|confirmed, note ≤60 words, source_ids[]}`,
`self_evidence` 1–10 + `self_evidence_reason`.
`key_figure` = `{name, value (number|null), unit, source_id, as_of (date|null), kind
price|fundamental|macro|other, data_ref?, filed?}`; value null ⇒ name must appear in missing_data (semantic).

**specialist_report**: + `thesis` (≤60 words), `stance` bullish|neutral|bearish, `strengths` (maxItems 3,
`{point, evidence_refs[] minItems 1}`), `risks` (maxItems 3, `{point, evidence_refs[], severity}`),
`key_figures[]`, `invalidation_price` (number|null), `invalidation_rationale`, `conviction` 1–10
integer, `conviction_reason`, `extensions` (if/then on agent_role):
- fundamental: `valuation {fair_value_low, fair_value_base, fair_value_high (number|null), method,
  input_refs[], cells_low[], cells_base[], cells_high[]}`, `quality_notes`, `balance_sheet_notes`.
- technical: `trend_regime` (enum), `key_levels[] {ref_id, price, type support|resistance|stop}`,
  `setup` (≤40 words), `indicator_refs[]` (data_ref strings), `invalidation_ref`.
- operator_ceo: `management_quality`, `capital_allocation`, `incentives`, `insider_activity`
  (+ `net_direction` buying|selling|mixed|none|unknown), `guidance_vs_delivery` — each
  `{assessment ≤60 words, source_ids[]}`.
- industry_ops: `supply_chain, capacity_backlog, execution, unit_economics, operational_moat` — each
  `{assessment ≤60 words, source_ids[]}`.
- osint: `signals[] {signal, direction positive|negative|neutral, source_ids[] minItems 1, trend
  improving|stable|deteriorating|unknown}`.
**devils_advocate_report**: common fields + `attacks[]` (minItems 1: `{target_claim, target_agent (role),
counter_evidence, source_ids[], severity}`), `bear_case_price` (number|null), `bear_case_ref`
(valuation cell id or technical level id; required when price non-null), `kill_criteria[]` (minItems 1),
`verdict_on_thesis` survives|damaged|broken, `verdict_reason`, `key_figures[]` (optional).
**etf_review**: common fields + `thesis`, `stance`, `lookthrough_summary {top_sectors[{sector,pct}],
data_ref}`, `expense_ratio_pct` (number|null), `top10_concentration_pct`, `overlap[] {with, overlap_pct,
basis}`, `momentum_vs_benchmark {benchmark, rs_3m, rs_6m, rs_12m}`, `role_in_portfolio`
core|satellite|factor_tilt|hedge|redundant, `conviction`, `conviction_reason`, `action`
ADD|HOLD|TRIM|SELL, `key_figures[]`, `invalidation_price` (nullable).
**sector_verdict**: `run_id, order_id, agent_role: sector_head, phase 2|3, sector, as_of, summary (≤80 words),
verdicts[] {ticker, kind holding|candidate, action ADD|HOLD|TRIM|SELL|BUY|PASS, rule_action,
agg_conviction, agg_evidence, rr (nullable), rationale ≤80 words, dissents[] {role, view},
unresolved_red_flags[], rule_deviation bool, deviation_justification (required non-empty when
rule_deviation), da_rebuttal (nullable), thesis/fair_value_low/base/high/invalidation_price (required
for BUY)}, sources[]`.
**macro_view**: `run_id, order_id, agent_role: macro, as_of, regime, regime_summary ≤80 words,
drivers[] (minItems 2) {driver, direction, series[], data_refs[], source_ids[]}, scenarios[] (2–5)
{name, probability 0–1, description ≤60 words, sector_implications}, sector_constraints[] {sector,
min_weight, max_weight, rationale}, conviction, conviction_reason, key_figures[], sources[], missing_data[]`.
**cio_allocation**: `run_id, order_id, agent_role: cio, mode: allocation, as_of, current_sector_weights
{sector: pct}, target_sector_weights {sector|cash: pct}, expand_sectors[], reduce_sectors[],
constraint_overrides[] {sector, macro_bound min|max, macro_value, cio_target, justification},
rationale ≤150 words, conviction, sources[], missing_data[]`.
**cio_final**: `run_id, order_id, agent_role: cio, mode: reconciliation, as_of, executive_summary[]
(≤5, each ≤40 words), reconciliation[] (minItems 1) {topic, phase1_position, findings, resolution},
final_target_sector_weights, final_actions[] {ticker, action, source_verdict_action, override bool,
override_justification}, constraint_overrides[], top_risks[] (≤5), dissent_summary ≤120 words,
conviction, sources[]`.
**failed_report**: `{order_id, agent_role, ticker, status: "failed", attempts, errors[]}`.

---

## 6. Configs (U0 writes, with config schemas)
- `investor_profile.yaml`: horizon_years 10, risk_tolerance moderate (confirm in setup),
  max_position_pct 10, max_fund_position_pct 40, max_sector_pct 35, min_trade_usd 200,
  target_cash_pct 5, base_currency ILS, tax_jurisdiction IL, capital_gains_tax_pct 25 (note: Israeli
  tax is on real gains — CPI/FX adjusted), allow_short false, sec_contact_email researcher@gmail.com,
  allow_fractional_shares false.
- `portfolio.yaml`: `as_of`, `total_value_usd`, `cash_usd`, `cash_pct`, holdings QQQ (fund), SPMO (fund),
  BRK-B, KO, PG, JPM (stock) each with `shares: null, weight_pct: null, cost_basis: null,
  cost_basis_currency: USD, purchase_date: null, currency: USD, proxy_ticker: null, sector_override: null`.
  Comment: the user's real TASE UCITS funds can be entered with `proxy_ticker` (e.g. NASDAQ-100 fund → QQQ).
- `universe.yaml`: region US, min_market_cap_usd 10e9, min_avg_dollar_volume_usd 50e6 (63-day),
  exclusions [], additions {sector: []}, max_candidates_per_sector 6.
- `etf_overrides.yaml`: `securities: {BRK-B: {sector: financials, asset_type: stock, flags:
  [conglomerate], note, share_class: {class_factor: null, shares_tags: []}}}`, `funds: {}` with a
  commented example (`sector_weights`, `source_url`, `as_of`, `expense_ratio_pct`).
- `committee.yaml`: enabled_sectors (11), concurrency 8, benchmark SPY, fx_ticker ILS=X,
  sector_etfs map, cache_ttl_hours {prices 24, fundamentals 168, macro 24, etf 168, info 24, sec_map 168},
  roles {phase2_stock [fundamental, technical, osint, operator_ceo], phase2_fund [etf, technical],
  quick_stock [fundamental, technical], quick_fund [etf], phase3_light [fundamental, technical],
  phase3_full [operator_ceo, industry_ops, osint], da_for_funds false},
  evidence {start 10, floor 1, null_required_penalty 2, stale_penalty 1, few_sources_threshold 3,
  few_sources_penalty 2, missing_data_penalty 1, missing_data_max 4, price_max_trading_days 3,
  fundamental_max_days_after_filing 120, fundamental_filing_lag_days 45, macro_max_days 45,
  required_fields {role: {default: [...], financials: [...], fund: [...]}}},
  aggregation {neutral_value 5, da_adjust {survives 0, damaged -1, broken -3},
  unresolved_flag_roles [osint], conviction_bounds [1,10]},
  actions {min_evidence 5, holding {add_conviction 7, add_evidence 6, add_rr 2.0, sell_max 3.0,
  trim_below 4.5}, candidate {buy_conviction 7, buy_evidence 6, buy_rr 2.5,
  block_on_unresolved_high_flag false}, sector_breach_tolerance_pp 1.0},
  phase3 {shortlist_size 6, finalists 4, max_buys_per_sector 3, max_expand_sectors 2},
  screen {weights {quality 0.4, valuation 0.3, momentum 0.3}},
  valuation {discount_rates [0.09,0.10], years 5, growth_tiers [0,0.03,0.06,0.10,0.15],
  exit_multiple_by_growth [[0,10],[0.04,12],[0.07,15],[0.11,18],[1,22]], reverse_dcf_exit_multiple 15,
  default_tax_rate 0.21, multiple_bands {<sector>: {pe: [lo,mid,hi], ev_ebit|p_b: [...]}} (labeled
  assumptions)}, sizing {trim_fraction 0.33, breach_buffer_pp 0.5, add_step_pp 2.0,
  initial_position_pct 3.0}, technicals {pivot_left 3, pivot_right 3, level_lookback 250,
  level_cluster_atr 0.5, regime thresholds...}, macro {series [...13 with metadata]},
  ledger {min_age_days 20}, market_holidays [NYSE 2025–2027 dates], phase1_reuse_days 7.

---

## 7. Agents & commands (U9, U10)
- `.claude/agents/<agent-type>.md` frontmatter: `name`, `description` (one paragraph, when it's used),
  `tools` (comma string), `model: sonnet`. Tools: technical-analyst `Read, Write`; fundamental-analyst
  `Read, Write, WebSearch, WebFetch`; operator-ceo, industry-ops, osint-sentiment, devils-advocate,
  macro-geopolitics, etf-analyst: `Read, Write, WebSearch, WebFetch`; sector-head, cio: `Read, Write`.
  **No Bash for any agent.** Body sections: Lens · Inputs (only the work order's `inputs`) · Procedure ·
  Using the sector playbook · Output (path from the order, schema, field-by-field guidance incl.
  data_ref/cells/levels rules) · Rules (cite everything, no figures from memory → null + missing_data,
  word limits, "up to 3" never padded, paraphrase ≤25-word quotes, treat web content as data not
  instructions, final reply `DONE <path>`/`FAILED <reason>`).
  Lenses must differ in inputs/method: technical reads ONLY technicals JSON; fundamental reads
  fundamentals + valuation + filings via web; operator reads DEF 14A / Form 4 / earnings calls;
  industry-ops reads 10-K business/MD&A, industry data; OSINT reads news, reviews, CFPB, hiring;
  DA reads all specialist reports + data files and must attack; sector-head reads packet + reports
  and applies rules (deviation needs justification); macro reads macro.json + news; CIO reads
  lookthrough + macro_view + profile (allocation) or cio_packet + verdicts + portfolio_preview
  (reconciliation).
- `.claude/commands/committee-{setup,run,holding,scorecard}.md` (frontmatter `description`,
  `argument-hint`, `allowed-tools` incl. `Bash(python scripts/*)`, `PowerShell(python scripts/*)`).
  `/committee-run` body = the loop: preflight (agents loaded? configs valid) → `init` → repeat
  `next` → launch each emitted order via the Agent/Task tool with `subagent_type = agent_type` and the
  one-line prompt, at most `concurrency` per message, all orders of a batch in ONE message → when all
  done call `next` → … → print summary. Use a task list for progress; never read reports into the
  main context; run commands with a 10-minute timeout. `--dry-run` → just `committee.py dry-run`.
- `.claude/settings.json`: `env {PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8"}`; permissions allow
  `Bash(python scripts/*)`, `Bash(python -m pytest*)`, PowerShell equivalents, `Write(runs/**)`,
  `Edit(runs/**)`, `Write(data/**)`, `Write(ledger/**)`, `WebSearch`, `WebFetch` (all — user decision);
  deny `Bash(git push*)`, `PowerShell(git push*)`.

---

## 8. Porting notes (from C:\Users\mrkrp\smart_portfolio\stock_dashboard — copy, never import)
- `indicators/technical.py`: Wilder `_rma = ewm(alpha=1/n, adjust=False)`; FIX: RSI seeded with SMA of
  first n deltas (classic Wilder), flat series → 50; `sma200` min_periods=200; 52w hi/lo need 252 bars
  (else use available ≥200 with flag, <200 → null); `atr_pctile` = trailing 252-bar percentile (no look-ahead).
- `indicators/levels.py`: `swing_pivots(df, left=3, right=3, lookback)` (rejects tied extremes),
  `structure_label`, `support_resistance`. Cluster pivots within `level_cluster_atr`×ATR into levels
  with touch counts; supports below price, resistances above, nearest first, ids S1.. / R1...
- `indicators/regime.py` `detect_regime` — see §4.2 (add slope check to trend_down).
- `utils.py`: retry, safe_float, to_jsonable.
- `tests/conftest.py::make_ohlcv` — add fixed `end`.
- `data/yfinance_provider.py` history quirks (auto_adjust, drop non-finite Close, tz_localize(None)).

## 9. Testing rules
- All tests offline: autouse socket block; yfinance and HTTP replaced via `AGINV_FIXTURES_DIR` or
  monkeypatching `common.http_get` / `common.yf_*`. Live tests marked `@pytest.mark.live`, skipped
  unless `--run-live`.
- Each unit owns its test files (`tests/test_<module>.py`) and fixtures under `tests/fixtures/<area>/`.
- `python -m pytest -q` from repo root must pass. `pytest.ini`/`pyproject` sets `testpaths = tests`,
  registers the `live` marker.


---

## 10. Amendments after the first live run and the review pass (2026-10-04)
These supersede the text above where they differ.

- **§4.1 prices — quote currency.** Series are returned in USD per share. ILS/ILA (agorot) quotes are
  converted at spot USD/ILS (flag `converted_to_usd`; cache keeps the native quote + currency);
  entries carry `native {quote_currency, currency, last_close (ILS/share), usdils, ...}`. Any other
  non-USD or unknown currency is refused, never priced as USD. Price Facts are rounded
  (`common.round_price`) to remove float32 noise.
- **§4.3 fundamentals.**
  - Debt: among formulas whose components share one instant, the formula with the LATEST common
    instant wins (ties: priority a,b,c,d,e,f). Added (e) `LongTermNotesAndLoans`+`NotesPayableCurrent`,
    (f) `NotesPayable`+`LoansPayable`; the current leg may be `LongTermDebtCurrent` or `DebtCurrent`.
    A lone noncurrent leg is used with flag `debt_current_portion_missing`.
  - Debt-free issuers: when no formula qualifies and no debt-like tag has a recent USD instant,
    total debt = 0 with flag `debt_not_reported_assumed_zero` (medium). Otherwise null +
    `debt_unavailable`. (This replaces "debt is never assumed zero".)
  - Duration buckets widened to Q 80–100, H 160–200, 9M 245–290, FY 350–380; quarter gap 84–120 days
    (52/53-week retail calendars). D&A falls back to `Depreciation`+`AmortizationOfIntangibleAssets`;
    capex has extra tags; every null input that silences EV/EBIT, ROIC, EBITDA or FCF adds a
    `missing_data` entry. ROIC is `not_meaningful` when equity ≤ 0 or invested capital ≤ 0.
  - Null Facts of non-SEC tickers carry `source_id: null`.
- **§4.6 look-through.** Totals/cash/weight inputs cite `D:CFG:portfolio`; every cited `D:PX:<T>` is
  defined in `sources[]`.
- **§4.7 screen.** Universe falls back to `etf_overrides.funds.<ETF>.top_holdings` (needs
  `source_url` + `as_of`, flag `universe_from_override`). A candidate with more than half of its
  quality+valuation inputs imputed is excluded (`no_data`); above 25% it gets
  `screen_imputation_heavy`. `ConfigError` propagates.
- **§4.9 risk/reward.** `risk_reward.min_downside_pct` (default 1.0): a stop closer than that gives
  `rr = null`, `null_reason: stop_too_tight` (upside/downside still reported). A negative rr means the
  base fair value is below the price; the memo shows it as "n/m (FV below price)".
- **§4.10 aggregate.** A candidate BUY additionally requires the Devil's Advocate to have run OK
  (otherwise PASS, label `da_unavailable`) and is always blocked by `auto:invalidation_breached`.
  Both are deliberate conservative deviations from SPEC §5 (which says only "DA verdict ≠ broken").
- **§4.11 portfolio.** Breach-driven TRIMs (position above cap, sector over limit) round UP to whole
  shares (fewest shares that reach the target; never more than held); conviction trims and buys
  still round down. Positions carry `full_exit` (+ a note for a TRIM that exits the whole position)
  and `weight_intent_pct` (the weight the sizing rules called for). Tax for an ILS cost basis is
  estimated in ILS from the native price (`tax.est_gain_ils`, `tax.est_tax_ils`).
- **§4.12 ledger.** `score` refuses stale prices: the ticker, benchmark and sector ETF must each have a
  last bar within `evidence.price_max_trading_days` of `as_of` (status `stale_price`, with
  `status_reason`); returns are measured to the earliest common last bar (`price_end_date`). The
  dedupe key uses normalised tickers/roles. `target_weight_pct` = the plan's `weight_intent_pct`.
- **§4.13 validate.** A key figure with a `data_ref` to a Fact must carry that Fact's `as_of`, `kind`,
  `unit` and `filed`. For proxy-priced funds the order's single technicals/fundamentals/valuation
  file is accepted even though its ticker differs.
- **§2 orchestration.** `redispatch` re-emits only orders whose output is missing/invalid;
  `next` stops with an error (without burning attempts) when a first-attempt batch produces no
  output for at least half its orders (usage limit / outage). The manifest warns when a live
  config file changed after the run started (decisions used the `inputs/` snapshot).
- **§2 chair review (reopen).** The *chair* is the main Claude session that reviews a FINISHED run
  (after `p4.finalize`). `committee.py reopen --run ID --order O [--order O ...] --feedback TEXT
  [--feedback-file F] [--spec-file S.json] [--reason R]` sends finished orders back to their agents:
  each named order (done or failed) becomes a retry order exactly like the retry machinery builds one
  (same order id and order file, attempt + 1, output `<stem>.r<attempt>.json`, the earlier output stays
  on disk) whose `prior_errors` are the feedback lines prefixed `CHAIR FEEDBACK:` (the order markdown
  shows them under "Chair feedback - address every point"); the order record gets `reopened_by: "chair"`,
  `reopen_round`, `reopen_history`, `attempt_limit`. Everything downstream is reset: the dependency
  table `STEP_DEPENDS` in `committee.py` (transitive closure, unknown steps depend on everything before
  them) marks the dependent steps `pending`, moves their outputs (and the old memo) to
  `_superseded/round<n>/` (shared `ledger_preview.csv` stays), python steps simply re-run, agent steps
  are re-dispatched as FRESH orders (attempt 1) with `prior_errors` =
  `inputs changed after chair re-run of <order ids>; re-check your conclusions against the updated reports`.
  Per-ticker / per-sector agent steps (`p2.wave_b`, `p3.da` by ticker; `p2/p3.sector_heads` by sector)
  only redo the orders of the touched tickers / sectors and keep the others byte-for-byte, unless a
  python step that decides the order set (`p2.data`, `p3.screen`, `p3.finalists`) is itself re-run.
  A named order that is also downstream of another named order is re-dispatched when its step comes
  up, carrying its chair feedback and continuing its attempt numbering. The manifest status becomes
  `reopened` (back to `done` when `next` finishes), `chair_rounds[]` gets
  `{round, at, reason, orders, feedback_sha, feedback_file, invalidated_steps, reopened_steps,
  kept_orders, superseded_orders, archive_dir, archived_files, previous_finished_at}`, and `run_log.md` a
  `CHAIR REOPEN` entry. Caps: `orchestration.max_chair_rounds` (default 2) rounds per run and
  `max_attempts + 2` attempts per order; a refused reopen changes nothing. After `reopen` the chair
  launches the listed orders, then runs the normal `next` loop (`redispatch` re-lists pending orders).
  `p4.finalize` replaces the ledger rows of the run (`ledger.append(..., replace_run_ids=[run_id])`)
  instead of appending duplicates. `audit --run ID` wraps `scripts/audit.run(run_dir)`.
  `chair-save --run ID --file F` validates the chair's review against `schemas/chair_review.schema.json`
  (`chair_review/1`, closed objects, word limits) and stores it atomically as `phase4/chair_review.json`
  (previous rounds kept as `chair_review.r<round>.json`).


- **§4.3/§4.4 captive finance (2026-10-29).** Consolidated net debt of a company with a captive finance
  subsidiary (CAT, DE, F, GM, PCAR, HOG, CNH) mostly funds finance receivables, an asset that earns its own
  interest (its interest expense is already inside operating income). Deducting it from an industrial
  NOPAT enterprise value understated equity value (CAT: $29.5B = ~$64/share) and made "net debt/EBITDA 1.9x"
  look like industrial leverage. `committee.yaml valuation.captive_finance_tickers` lists the affected
  tickers; for them `valuation.py` deducts no net debt (`net_debt_basis: captive_excluded`, equity value =
  EV, medium flag `captive_finance_debt_excluded`, plus a `missing_data` line so the memo shows it), or
  `captive_finance_net_debt_pct_override[<T>]` x consolidated net debt (`override`; empty by default - fill it
  only from the 10-K segment note). The same net debt feeds the EV/EBIT cells and the reverse DCF. The
  top-level `net_debt` is the amount deducted; `net_debt_consolidated` keeps the reported figure. Industrial
  net debt and cash are ignored (not separable in SEC companyfacts), so fair value may be slightly high.
  `fetch_fundamentals.py` flags the same tickers `captive_finance` (medium), keeps the consolidated
  `net_debt_ebitda` / `ev` / `ev_ebit` / `ev_sales` / `roic_pct` values (schema-stable) but annotates them
  with a `note`, adds `balance_notes` and `valuation.net_debt_ebitda_ex_captive` (null, `not_applicable` +
  note, unless an override fraction gives an estimate). Generic safeguard for unlisted companies: sector
  (from SIC) industrials / consumer_discretionary and consolidated net debt / market cap above
  `valuation.possible_captive_net_debt_to_mcap` (0.25) -> low flag `possible_captive_finance` (the tag map
  resolves no financing-revenue concept, so the test is deliberately cheap and false-positive tolerant).
  The fix is about correctness, not about changing the answer: CAT still trades well above the grid.
- **§4.11 portfolio — sector cure and TRIM semantics (2026-10-29).** The sector-limit cure by stocks is
  capped: a TRIM-verdict stock may lose at most `sizing.sector_trim_max_fraction` (default 0.5, range
  (0,1]) of its CURRENT weight in total, step-1 trim included, and whole-share round-up of that cut never
  exceeds `floor(fraction x shares held)`; a TRIM therefore no longer becomes a full exit through the sector
  rule. SELL still exits fully and a position-cap breach is still cured fully (round up). Exposure the stocks
  cannot cure (it sits in funds, which are never sold) is reported in `residual_breaches`:
  `[{sector, post_pct, limit_pct, max_pct, excess_pp, stock_trim_pp, fund_contributions:[{ticker, weight_pct,
  sector_pct_of_portfolio}] (desc), note}]` - the note names the real lever (fund overlap, new contributions
  directed to under-weight sectors). Every position carries `effective_action` / `effective_action_reason`
  (a TRIM/SELL whose trade was dropped - `rounds_to_zero_shares`, `below_min_trade_usd`, `no_price`... - is
  effectively HOLD; `action` stays the verdict), `trade_pct_of_position` (shares sold / held, 0..1 or null)
  and `exit_kind` (`none` | `partial` | `full`). Schema fields are optional so older plans still validate.
