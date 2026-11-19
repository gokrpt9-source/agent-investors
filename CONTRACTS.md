# CONTRACTS — as-built reference for `scripts/common.py` and shared conventions

This file documents what **exists** (unit U0). The design intent is `docs/DESIGN.md`; per-script
payload contracts (prices, technicals, fundamentals, valuation, macro, look-through, screen,
evidence, aggregate, portfolio, ledger, validate, render) are **DESIGN.md §4.1–§4.14** and are not
repeated here. Import with `from scripts import common`.

---

## Hard rule: no agent trades

No agent trades: never place, modify or cancel an order, never move money, never record a yes; at most prepare information or a ticket for the investor to place himself. This holds for every agent, every slash command and every script in this repo. The committee proposes; the investor decides and places any order himself, in his own broker. `.claude/settings.json` denies the order and money tools of the connected trading MCP servers, and tests fail if an agent gets an order tool or a way to record a yes.

## 1. Environment variables

| Variable | Effect | Default |
|---|---|---|
| `AGINV_CONFIG_DIR` | config directory (`*.yaml`) | `<repo>/config` |
| `AGINV_CACHE_DIR` | cache root | `<repo>/data/cache` |
| `AGINV_RUNS_DIR` | runs root | `<repo>/runs` |
| `AGINV_LEDGER` | ledger CSV path | `<repo>/ledger/recommendations.csv` |
| `AGINV_AS_OF` | "today" for `common.today()` (YYYY-MM-DD) | real date |
| `AGINV_OFFLINE` | `1/true/yes/on` → every network call raises `OfflineError` | off |
| `AGINV_FIXTURES_DIR` | serve ALL network from fixture files (§8); wins over offline | off |
| `AGINV_NOW` | verbatim override of `now_iso()` (deterministic `generated_at`) | real time |
| `AGINV_SCHEMAS_DIR` / `AGINV_KNOWLEDGE_DIR` / `AGINV_TEMPLATES_DIR` | optional dir overrides | `<repo>/schemas`, `/knowledge`, `/templates` |

Relative env paths resolve against the current working directory. `common.AGINV_ENV_VARS` lists
all of them; `ENV_*` constants hold the names.

## 2. Paths and common CLI flags

```python
REPO_ROOT: Path                       # repo root (parent of scripts/)
config_dir() -> Path; cache_dir() -> Path; runs_dir() -> Path; ledger_path() -> Path
schemas_dir() -> Path; knowledge_dir() -> Path; templates_dir() -> Path
schema_path(name: str) -> Path        # schemas_dir() / SCHEMA_FILES[name]; KeyError if unknown
schema_id(name: str) -> str           # $id of that schema (urn:agent-investors:[data/]<name>)
role_schema(role: str, mode: str | None = None) -> str   # cio: allocation→cio_allocation, reconciliation→cio_final
is_offline() -> bool
active_fixtures_dir() -> Path | None
add_common_args(parser, *, exclude: Iterable[str] = ()) -> parser
apply_common_args(args) -> args
```
`add_common_args` adds `--config-dir --cache-dir --runs-dir --ledger --as-of` (dests `config_dir,
cache_dir, runs_dir, ledger, as_of`, default None). `exclude` takes dests or flags (use it if the
script defines its own `--as-of`). `apply_common_args` exports every non-None flag to its env var
(dirs resolved absolute, `--as-of` validated → `ConfigError` if not a date) and calls
`clear_caches()`. Scripts call both before doing work, then use the path functions above.

## 3. Constants

| Name | Value / meaning |
|---|---|
| `DISCLAIMER` | "Research output for decision support only — not personalized financial advice. Figures come from free public sources that may be delayed or incomplete. The investor makes all decisions." |
| `ROLE_KEYS` | `fundamental, technical, operator_ceo, industry_ops, osint, devils_advocate, etf, macro, cio, sector_head` |
| `SPECIALIST_ROLES` | `fundamental, technical, operator_ceo, industry_ops, osint` (write `specialist_report`) |
| `SCORED_ROLES` | specialists + `devils_advocate, etf` (keys of `evidence.required_fields`) |
| `AGENT_TYPES` | role → agent type: `fundamental-analyst, technical-analyst, operator-ceo, industry-ops, osint-sentiment, devils-advocate, etf-analyst, macro-geopolitics, cio, sector-head` |
| `ROLE_SCHEMAS` | role → schema name (`cio` → `cio_allocation`; use `role_schema(role, mode)`) |
| `AGENT_SCHEMA_NAMES` | `common_defs, specialist_report, devils_advocate_report, etf_review, sector_verdict, macro_view, cio_allocation, cio_final, failed_report` → `schemas/<name>.json` |
| `DATA_SCHEMA_NAMES` | `prices, technicals, fundamentals, valuation, macro, lookthrough, screen, evidence, aggregate, portfolio_plan, manifest, work_order` → `schemas/data/<name>.schema.json` |
| `CONFIG_NAMES` | `investor_profile, portfolio, universe, committee, etf_overrides` → `schemas/config_<name>.schema.json` |
| `SCHEMA_FILES` | schema name → path relative to `schemas_dir()`; config schemas are keyed `config_<name>`. `$id` = `schema_id(name)`: `urn:agent-investors:<name>` for agent and config schemas, `urn:agent-investors:data/<name>` for data contracts (as built by the schema unit) |
| `SECTOR_CLASSES` | `default, financials, fund` |
| `SEVERITIES` | `low, medium, high` |
| `OHLCV_COLUMNS` | `["Open","High","Low","Close","Volume"]` |
| `FactKind` | `str` Enum `PRICE/FUNDAMENTAL/MACRO/OTHER` (`str(FactKind.PRICE) == "price"`); `FACT_KINDS` = the values |
| `NA_REASONS` | `missing, not_meaningful, negative_denominator, insufficient_history, not_applicable`; `NA_UNPENALIZED` = `not_meaningful, not_applicable` |
| URL templates | `SEC_TICKERS_URL`, `SEC_COMPANYFACTS_URL`, `SEC_SUBMISSIONS_URL` (`{cik:010d}`), `FRED_CSV_URL` (`{series}`), `STOOQ_CSV_URL` (`{symbol}`), `YF_HISTORY_URL`, `YF_QUOTE_URL`, `YF_HOLDINGS_URL` (`{ticker}`) |
| HTTP | `HTTP_ATTEMPTS=4`, `HTTP_BACKOFF_BASE_S=1.0`, `HTTP_MAX_SLEEP_S=30.0`, `RETRY_STATUSES={429,500,502,503,504}`, `SEC_MIN_INTERVAL_S=0.11`, `DEFAULT_USER_AGENT` |

## 4. Exceptions

```
AginvError
├── ConfigError            config missing/invalid, bad --as-of / AGINV_AS_OF, SEC email missing
└── DataUnavailable        .status_code (int|None), .url (str|None)
    ├── ProviderBlocked    HTTP 403 / bot wall — never retried, never bypassed
    ├── OfflineError       AGINV_OFFLINE=1 and a network call was attempted
    └── FixtureMissing     fixture mode on but no fixture for the resource
```
Fetchers can `except DataUnavailable` to fall back (stale cache, alternative provider) and still
work offline / in fixture mode. `retry()` never retries `ConfigError, OfflineError,
FixtureMissing, ProviderBlocked`.

## 5. Pure helpers and IO

```python
safe_float(x) -> float | None          # None for None/bool/NaN/inf/pd.NA/non-numeric
to_jsonable(obj) -> JSON-safe builtins # see below
retry(fn, attempts=3, base_delay=1.0, *, exceptions=(Exception,), no_retry=None, max_delay=30.0)
norm_ticker(t) -> str                  # strip/upper; "/" and share-class "." → "-" (brk.b→BRK-B);
                                       # exchange suffixes kept (TEVA.TA); ValueError on empty
safe_name(key) -> str                  # Windows-safe file stem (see §7)
data_filename(kind, ticker, ext="json") -> str   # "<kind>_<NORM TICKER>.<ext>", e.g. technicals_BRK-B.json
get_dotted(doc, "a.b.0.c", default=None)         # dotted path read (used for required_fields)
json_pointer_get(doc, "/ttm/revenue")            # RFC 6901 ("#/..." accepted); KeyError if absent
is_fact(obj) -> bool; fact_value(obj) -> float | None   # Fact → .value, number → float
setup_stdio()                          # stdout/stderr → utf-8, errors="replace"
read_text(path) -> str                 # utf-8, BOM stripped
read_json(path) -> Any                 # utf-8 JSON, BOM tolerated
write_text(path, text) -> Path         # atomic (temp in same dir + os.replace, 3 retries on
                                       # PermissionError), utf-8, NO newline translation, mkdir -p
dumps_json(data, *, indent=2) -> str   # to_jsonable + ensure_ascii=False + allow_nan=False
write_json(path, data, *, indent=2) -> Path   # atomic dumps_json + trailing "\n"
```
`to_jsonable`: NaN/inf/pd.NA/NaT → `None`; numpy scalars → int/float/bool; `Enum` → value;
`Path` → posix string; `Decimal` → float; `date` → `YYYY-MM-DD`; `datetime` → ISO-8601;
`pd.Timestamp` at midnight without tz → `YYYY-MM-DD`, else ISO; `DataFrame` →
`{"index","columns","data"}`; `Series` → dict; dataclass → dict; `set` → sorted list; bytes →
utf-8 text; mapping keys → strings (dates ISO); unknown → `str(obj)`.

## 6. Time

```python
today() -> date                 # AGINV_AS_OF if set (ConfigError if unparsable), else date.today()
now_iso() -> str                # UTC "YYYY-MM-DDTHH:MM:SS+00:00" (AGINV_NOW overrides verbatim)
parse_date(x) -> date | None    # date/datetime/Timestamp/np.datetime64/"YYYY-MM-DD..."/ISO ts;
                                # None/""/NaN/NaT → None; anything else → ValueError
date_str(x) -> str | None       # parse_date(x).isoformat()
market_holidays(config_dir=None) -> list[date]   # committee.yaml market_holidays ([] if no config)
trading_days_between(d1, d2, holidays=None) -> int
is_trading_day(d, holidays=None) -> bool
```
`trading_days_between` = number of NYSE trading days **strictly after `d1` up to and including
`d2`** = `np.busday_count(d1+1, d2+1, holidays)`: same day → 0, Fri→Mon → 1, Wed→Fri over
Thanksgiving → 1, `d2 < d1` → negative. `holidays=None` loads `committee.yaml: market_holidays`;
pass `()` for none. Price freshness: a price Fact is stale when
`trading_days_between(fact.as_of, run_date) > evidence.price_max_trading_days`.

## 7. Sectors and SIC

`SECTORS` (ordered) key → `{"name","etf"}`:
`information_technology` XLK · `health_care` XLV · `financials` XLF · `energy` XLE ·
`industrials` XLI · `consumer_discretionary` XLY · `consumer_staples` XLP · `utilities` XLU ·
`materials` XLB · `real_estate` XLRE · `communication_services` XLC. `SECTOR_KEYS` = keys in this
order. `PSEUDO_SECTORS` = `funds`, `cash`, `unclassified`.

```python
sector_display(key) -> str          # "Consumer Staples"; pseudo-sectors "Funds"/"Cash"/"Unclassified"
sector_etf(key) -> str | None       # None for pseudo/unknown
sector_class(sector, asset_type="stock") -> "default"|"financials"|"fund"
sic_to_sector(sic) -> str | None    # int or numeric string; first matching range below
```
`YF_SECTOR_MAP` maps Yahoo `info["sector"]` (`Technology, Healthcare, Financial Services, Energy,
Industrials, Consumer Cyclical, Consumer Defensive, Utilities, Basic Materials, Real Estate,
Communication Services` + GICS spellings) → keys. `YF_FUND_SECTOR_MAP` maps
`funds_data.sector_weightings` keys (`technology, healthcare, financial_services, energy,
industrials, consumer_cyclical, consumer_defensive, utilities, basic_materials, realestate,
communication_services`) → keys.

SIC ranges (inclusive, first match wins — exceptions are listed before their parent range):
0100–0999 staples · 1000–1099 materials · 1200–1399 energy · 1400–1499 materials · 1500–1799
industrials · 2000–2199 staples · 2200–2399 discretionary · 2400–2499 materials · 2500–2599
discretionary · 2600–2699 materials · 2700–2799 communication · 2800–2829 materials · 2830–2836
health care · 2840–2844 staples · 2845–2899 materials · 2900–2999 energy · 3000–3099 materials ·
3100–3199 discretionary · 3200–3399 materials · 3400–3569 industrials · 3570–3579 IT · 3580–3599
industrials · 3600–3629 industrials · 3630–3639 discretionary · 3640–3659 industrials · 3660–3699
IT · 3711–3716 discretionary · other 3700–3799 industrials · 3812 industrials · 3840–3851 health
care · other 3800–3899 IT · 3900–3999 discretionary · 4000–4799 industrials · 4800–4899
communication · 4900–4999 utilities · 5122 health care · 5140–5149 staples · other 5000–5199
industrials · 5400–5499 and 5912 staples · other 5200–5999 discretionary · 6798 real estate ·
6000–6499 financials · 6500–6553 real estate · other 6700–6799 financials · 7000–7099 and
7200–7299 discretionary · 7370–7379 IT · other 7300–7399 industrials · 7800–7989 communication ·
7990–7999 discretionary · 8000–8099 health care · 8700–8799 industrials · anything else → `None`.

## 8. Provenance: Fact, Source, Envelope

### Fact (`common.fact`)
```python
fact(value, unit, source_id, as_of, kind, na_reason=None, **extra) -> dict
na_fact(unit, source_id, as_of, kind, na_reason="missing", **extra) -> dict
```
```json
{"value": 12.3, "unit": "pct", "source_id": "D:SEC:KO:CF", "as_of": "2026-04-03",
 "kind": "fundamental", "na_reason": null, "filed": "2026-04-30", "tag": "Revenues",
 "derivation": "FY2025+3M2026-3M2025"}
```
- `value`: int/float/numpy number or None; NaN/inf → None; bool/str → `TypeError`.
- `kind` ∈ `FACT_KINDS` (or a `FactKind`), else `ValueError`.
- `value is None` → `na_reason` defaults to `"missing"` and must be in `NA_REASONS` (else
  `ValueError`); a non-null value always has `na_reason: null`.
- `as_of` and the date extras `filed`, `observation_date`, `period_end` are normalized to
  `YYYY-MM-DD`; other extras (`tag`, `derivation`, `note`, ...) pass through `to_jsonable`; extras
  equal to None are omitted.
- Units: `pct` (0–100), `x` (multiples), `USD`, `USD/share`, `shares`, `index`, ... (DESIGN §0).

### Source (`common.source`, `common.dataset_source`)
```python
source(source_id=None, url=None, title=None, accessed=None, *, kind="dataset", path=None, id=None) -> dict
dataset_source(source_id, *, title=None, accessed=None, path=None, cik=None, provider="yfinance", url=None) -> dict
dedupe_sources(sources) -> list[dict]          # first per id, order kept
```
`{"id","url","title","accessed","kind":"dataset|web"[,"path"]}` — `url` must be http(s)
(`ValueError` otherwise), `title` defaults to the url, `accessed` defaults to `today()`, `path`
(local file the data came from) is a posix string. The id may be given positionally, as
`source_id=` or `id=`.

Dataset source ids (`dataset_source` builds the URL; an explicit `url=` always wins):

| id | url | needs |
|---|---|---|
| `D:PX:<T>` | `https://finance.yahoo.com/quote/<T>/history/` (yfinance) | `provider="stooq"` → pass the Stooq CSV `url` actually used |
| `D:YF:<T>:INFO` | `https://finance.yahoo.com/quote/<T>/` | |
| `D:YF:<T>:FUND` | `https://finance.yahoo.com/quote/<T>/holdings/` | |
| `D:SEC:<T>:CF` | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | `cik` |
| `D:SEC:<T>:SUB` | `https://data.sec.gov/submissions/CIK##########.json` | `cik` |
| `D:SEC:MAP` | `https://www.sec.gov/files/company_tickers.json` | |
| `D:FRED:<SERIES>` | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>` | |
| `D:VAL:<T>` | companyfacts URL of `<T>` (derived valuation grid; `path` = local valuation file) | `cik` |
| `D:CFG:<name>` | the `source_url` declared in the config entry | `url` |

URL helpers: `sec_companyfacts_url(cik)`, `sec_submissions_url(cik)`, `fred_csv_url(series)`,
`stooq_csv_url(symbol)` (already-mapped Stooq symbol, e.g. `ko.us`, `usdils`), `yf_history_url(t)`,
`yf_quote_url(t)`, `yf_holdings_url(t)`. Agents copy dataset sources verbatim (work-order
`seed_sources`) and add their own web sources `W1..Wn`.

### Envelope (`common.envelope`) — every script output file
```python
envelope(schema_name, as_of, **payload) -> dict
flag(code, severity, detail="") -> {"code","severity","detail"}     # severity low|medium|high
```
```json
{"schema": "<name>/v1", "generated_at": "<now_iso()>", "as_of": "YYYY-MM-DD",
 "sources": [Source...], "missing_data": ["..."], "flags": [{"code","severity","detail"}], ...payload}
```
`/v1` is appended when `schema_name` has no `/`; `as_of=None` → `today()`; `sources` de-duplicated
by id; `missing_data` de-duplicated (order kept); the remaining payload keys follow in order.
Write it with `write_json`.

### data_ref
Agent `key_figures[]` citing a `D:*` source carry `data_ref: "<path>#<JSON pointer>"`;
`json_pointer_get` + `fact_value` resolve it (DESIGN §1.4, validated by `validate.py`).

## 9. Config loading and validation

```python
load_yaml(path) -> Any                  # ConfigError if missing / parse error; empty file → {}
load_config(name, config_dir=None) -> dict   # name ∈ CONFIG_NAMES; not validated
committee_cfg(config_dir=None) -> dict  # cached per (path, mtime); returns a deep copy
load_schema(name) -> dict               # JSON schema by SCHEMA_FILES name (cached by mtime)
validate_config(name, data, *, require_portfolio_values=True) -> list[str]
validate_configs(config_dir=None, *, require_portfolio_values=True) -> dict[str, list[str]]
config_errors(result) -> list[str]      # flatten validate_configs() in CONFIG_NAMES order
email_problem(email) -> str | None      # None = usable contact email
format_error_path(parts) -> str         # ["holdings",0,"shares"] → "holdings[0].shares"; [] → "(root)"
clear_caches()                          # committee cfg, schemas, fixture index, SEC limiter clock
```
**YAML loader** (SafeLoader subclass, YAML-1.2-like scalars): only `true/false` are booleans
(`ON`, `NO`, `Y` stay strings — tickers are safe), dates stay `"YYYY-MM-DD"` strings, `1e9` /
`10e9` are floats. The repo configs quote tickers and dates so plain `yaml.safe_load` returns the
same data.

**Errors** are `"<name>.yaml: <path>: <message>"`, e.g.
`portfolio.yaml: holdings[0].shares: 'ten' is not of type 'number', 'null'`. JSON-Schema errors
(Draft 2020-12 + FormatChecker, schema `config_<name>`) come first (sorted by path), then semantic
checks:
- investor_profile: `sec_contact_email` is a real email without "example"; `max_position_pct ≤
  max_sector_pct`; `target_cash_pct < 100`; `allow_short` must be false.
- portfolio: no duplicate tickers (after `norm_ticker`); not both `cash_usd` and `cash_pct`;
  Σ`weight_pct` + `cash_pct` ≤ 100.5; weight-only holdings need `total_value_usd`; with
  `require_portfolio_values=True` every holding needs `shares` or `weight_pct` and the list must
  be non-empty — these errors say **"run /committee-setup"** (the prefilled template fails this on
  purpose). If both `shares` and `weight_pct` are given, shares win (documented in the YAML).
- universe: tickers must already be normalized.
- committee: `sell_max < trim_below < add_conviction` within `conviction_bounds`; add/buy evidence
  ≥ `min_evidence` ≤ `evidence.start`; `floor < start`; `broken ≤ damaged ≤ survives ≤ 0`;
  `finalists ≤ shortlist_size`, `max_buys_per_sector ≤ finalists`; screen weights sum to 1;
  ascending `growth_tiers` and `exit_multiple_by_growth`; `multiple_bands` for all 11 sectors with
  `pe` + (`p_b` for financials else `ev_ebit`) and `0 < low ≤ mid ≤ high`;
  `valuation.default_tax_rate == fundamentals.default_tax_rate`; `adx_range_max ≤ adx_trend_min`;
  unique macro series ids; holidays unique and not weekends; `sector_etfs` match `SECTORS`;
  `evidence.required_fields` covers every `SCORED_ROLES` role with a `default` list.
- etf_overrides: normalized tickers; funds need `source_url` + `as_of` (schema) and
  `sector_weights` summing to 100 ± 1; a security with `share_class.class_factor` needs `source_url`.

### committee.yaml sections (key names; values documented inline in the file)
`version, enabled_sectors, concurrency, benchmark, fx_ticker, fx_stooq_symbol, sector_etfs,
phase1_reuse_days, cache_ttl_hours{prices,fundamentals,macro,etf,info,sec_map},
orchestration{step_time_budget_seconds,max_attempts,command_timeout_ms},
roles{phase2_stock,phase2_fund,quick_stock,quick_fund,phase3_light,phase3_full,da_for_funds},
prices{period,stooq_fallback,adv_window_days,return_windows_days{1m,3m,6m,12m}},
technicals{sma_periods,rsi_period,macd_fast,macd_slow,macd_signal,atr_period,adx_period,
atr_pctile_window,high_low_window,high_low_min_bars,sma200_slope_days,max_drawdown_window,
rs_windows{3m,6m,12m},volume_short,volume_long,volume_rising_ratio,volume_falling_ratio,adv_window,
pivot_left,pivot_right,level_lookback,level_cluster_atr,max_levels_per_side,stop_support_buffer_atr,
stop_atr_multiple,weekly_closes_weeks,regime{adx_trend_min,adx_range_max,choppy_window,
choppy_min_sma50_crosses,choppy_atr_pctile_min,choppy_adx_max,range_window,range_max_width_pct,
sma200_slope_min_pct}},
fundamentals{forms,fy_extra_forms,duration_days{Q,H,9M,FY},ytd_prior_tolerance_days,
quarter_gap_days,debt_formula_max_gap_days,share_count_divergence_pct,tax_rate_bounds,
default_tax_rate,cagr_years,financials_sic,reit_sic},
valuation{discount_rates,years,growth_tiers,exit_multiple_by_growth,reverse_dcf_exit_multiple,
reverse_dcf_discount_rate,reverse_dcf_growth_bounds,default_tax_rate,tv_share_flag_threshold,
multiple_bands{<sector>{pe|ev_ebit|p_b:[low,mid,high]}}},
screen{weights{quality,valuation,momentum},etf_top_n,impute_percentile,momentum_windows},
phase3{shortlist_size,finalists,max_buys_per_sector,max_expand_sectors,rotation{enabled,lookback_runs}},
(rotation: `screen.run(..., recent=...)` skips, reason `recent`, tickers that `screen.recent_shortlisted` finds in the
`phase3/screen_<sector>.json` shortlists of the last `lookback_runs` earlier runs, so each run proposes new names;
a shortfall is never padded and raises flag `rotation_exhausted`),
lookthrough{sum_tolerance_pct,top_n,overlap_basis},
evidence{start,floor,null_required_penalty,stale_penalty,few_sources_threshold,few_sources_penalty,
missing_data_penalty,missing_data_max,price_max_trading_days,fundamental_max_days_after_filing,
fundamental_filing_lag_days,macro_max_days,failed_score,required_fields{<role>{default,financials,fund}}},
aggregation{neutral_value,da_adjust{survives,damaged,broken},unresolved_flag_roles,
mean_excluded_roles,conviction_bounds,round_dp,failed_evidence},
risk_reward{round_dp},
actions{min_evidence,holding{add_conviction,add_evidence,add_rr,sell_max,trim_below},
candidate{buy_conviction,buy_evidence,buy_rr,block_on_unresolved_high_flag},sector_breach_tolerance_pp},
sizing{trim_fraction,breach_buffer_pp,add_step_pp,initial_position_pct},
macro{chg_3m_days,chg_12m_days,history_tail,series[{id,name,frequency,units,change,yoy}]},
validation{data_ref_tolerance_pct,data_ref_abs_tolerance,level_match_tolerance_pct,
cell_mean_tolerance_pct,min_base_method_families,probability_sum_tolerance,
sector_weight_sum_tolerance_pct,min_justification_words,max_quote_words,
max_executive_summary_items,max_top_risks,word_limits{thesis,sector_rationale,sector_summary,
regime_summary,scenario_description,technical_setup,extension_assessment,flag_response_note,
cio_rationale,executive_summary_item,dissent_summary}},
ledger{min_age_days}, market_holidays[...]`.
`exit_multiple_by_growth` is a list of `[g_max, multiple]`: the first pair with `g ≤ g_max` wins.
Macro `change: diff` = latest − prior in percentage points; `pct` = percent change of a level.

## 10. Disk cache

```python
cache = DiskCache(root=None)            # root defaults to cache_dir() at construction
cache.path_for(kind, key, ext="json") -> Path
cache.put(kind, key, payload, source_url) -> Path | None
cache.get(kind, key, ttl_hours) -> payload | None          # ttl_hours=None → no TTL check
cache.get_stale(kind, key) -> payload | None               # ignores TTL
cache.get_entry(kind, key, ttl_hours=None) -> dict | None  # raw entry + age_hours, stale
cache.put_frame(kind, key, df, source_url, meta=None) -> Path | None
cache.get_frame(kind, key, ttl_hours=None) -> DataFrame | None
cache.get_frame_stale(kind, key) -> DataFrame | None
cache.delete(kind, key)
```
Layout: `<root>/<safe_name(kind)>/<safe_name(key)>.json` =
`{"fetched_at": "<UTC ISO>", "source_url": ..., "payload": ...}`. Frames add
`<safe_name(key)>.csv` (index written as `Date`, midnight timestamps as `YYYY-MM-DD`, tz dropped)
and the JSON entry carries `payload` = `meta` plus `"frame"`, `"rows"`, `"columns"`. E.g.
`put_frame("prices", "prices_KO", df, url)` → `data/cache/prices/prices_KO.csv` + `.json`.
- **Errors are never cached:** `put` ignores `None`, exceptions and dicts with a truthy
  `"error"` key; `put_frame` ignores `None`/empty frames (both return `None`, nothing written).
- TTL is wall-clock age of `fetched_at` (not `AGINV_AS_OF`); `age ≤ ttl_hours` is fresh.
- `get`/`get_stale` return the payload itself; dict/list payloads come back as `CachedDict` /
  `CachedList` with attributes `.fetched_at .source_url .age_hours .stale` (`stale` is True from
  `get_stale`). For compatibility, `obj["payload"]`, `obj["fetched_at"]`, `obj["source_url"]`
  (and `.get(...)`) also work on a `CachedDict` when the payload has no such key.
- Frames come back with a `DatetimeIndex` named `Date` and metadata in `df.attrs`
  (`fetched_at, source_url, meta, age_hours, stale`).
- Corrupt/partial entries read as absent.

`safe_name(key)`: characters invalid on Windows (`<>:"/\|?*`, control chars, whitespace) → `_`;
trailing dots/spaces stripped; reserved device names (`CON PRN AUX NUL COM1–9 LPT1–9`, any
case, with or without extension) get a trailing underscore on the stem (`PRN` → `PRN_`,
`con.txt` → `con_.txt`); empty → `_`; > 120 chars → truncated + `_<sha1[:12]>`.

## 11. Network seam (the ONLY place `requests` / `yfinance` are used)

```python
http_get(url, *, headers=None, timeout=30, sec=False, params=None, attempts=4, raise_for_status=True)
    -> requests.Response | FixtureResponse
http_get_text(url, **kw) -> str        # body decoded as UTF-8 (BOM stripped), else resp.text
http_get_json(url, **kw) -> Any        # DataUnavailable on invalid JSON
response_text(resp) -> str
sec_user_agent(config_dir=None) -> str # "agent-investors research <sec_contact_email>"
yf_history(ticker, period="3y", *, interval="1d") -> DataFrame[Open,High,Low,Close,Volume]
yf_info(ticker) -> dict
yf_funds(ticker) -> {"sector_weightings","top_holdings","expense_ratio_pct","expense_ratio_field"}
clean_ohlcv(df, *, ticker="") -> DataFrame
```
**Order of precedence for every call:** fixture mode (`AGINV_FIXTURES_DIR`) → offline
(`AGINV_OFFLINE=1` → `OfflineError`, nothing is attempted) → network. Callers should not gate on
`is_offline()` themselves: call the seam and catch `DataUnavailable` (covers `OfflineError`,
`FixtureMissing`, `ProviderBlocked`) to fall back to the (stale) cache — that way fixture mode
works even when `AGINV_OFFLINE=1` is also set.

`http_get` (network): default UA `DEFAULT_USER_AGENT`, `Accept-Encoding: gzip, deflate`, caller
`headers` override. `params` are url-encoded onto the URL (also in fixture mode — the index key is
the full URL). Retries up to `attempts` total on 429/500/502/503/504, `ConnectionError`,
`Timeout`, `ChunkedEncodingError`; backoff 1, 2, 4 s, or `Retry-After` (seconds or HTTP-date)
when present; every sleep capped at 30 s; sleeps use `time.sleep` (tests patch it). Final status
≥ 400 → `DataUnavailable` (`ProviderBlocked` for 403) with `.status_code`/`.url`, unless
`raise_for_status=False`. Exhausted connection errors → `DataUnavailable("network error ...")`.
Bot-wall detection for 200-with-HTML responses (Stooq) is the caller's job → raise
`ProviderBlocked`; never try to bypass it.

`sec=True`: User-Agent = `sec_user_agent()` read from `investor_profile.yaml` in the **active
config dir** — `ConfigError` (message mentions `/committee-setup`) if the email is missing,
malformed or contains "example"; plus a process-wide lock enforcing ≥ `SEC_MIN_INTERVAL_S`
(0.11 s) between SEC requests (incl. retries). In fixture mode no email is needed.

`yf_history`: `yf.Ticker(t).history(period, interval, auto_adjust=True)` wrapped in `retry`
(3 attempts) → `clean_ohlcv`: DatetimeIndex (tz-aware → wall time via `tz_localize(None)`) or a
`Date` column (first 10 chars parsed), normalized to midnight, rows with non-finite Close dropped,
missing Open/High/Low filled with Close and missing Volume with 0, float64, sorted (stable),
duplicate dates keep the last, index named `Date`. Empty → `DataUnavailable`.

`yf_info`: `dict(Ticker.info)`; empty / all-None → `DataUnavailable`.

`yf_funds`: `sector_weightings` = `Ticker.funds_data.sector_weightings` (Yahoo keys such as
`technology`, `realestate`; values are **fractions**; map with `YF_FUND_SECTOR_MAP`);
`top_holdings` = `funds_data.top_holdings` (index Symbol, columns Name / Holding Percent fraction)
→ `[{"symbol","name","holding_pct"}]` with `holding_pct` in **percent**; `expense_ratio_pct` (percent)
from `info["netExpenseRatio"]` (already percent, QQQ ≈ 0.18) → `expense_ratio_field =
"info.netExpenseRatio"`, else `funds_data.fund_operations` row "Annual Report Expense Ratio"
(fraction × 100) → `"funds_data.fund_operations.Annual Report Expense Ratio"`, else `None` /
`"unavailable"`. No weights and no holdings → `DataUnavailable`.

## 12. Fixture seam (`AGINV_FIXTURES_DIR=<dir>`)

```
<dir>/http/index.json        {"<full URL incl. query>": "relative/file.json|.csv|.txt"
                              | {"file": "rel/file" | null, "status": 404, "headers": {...}}}
<dir>/http/<files...>        paths relative to <dir>/http/ (falls back to <dir>/)
<dir>/yf/history/<T>.csv     Date,Open,High,Low,Close,Volume   (returned whole; period ignored)
<dir>/yf/info/<T>.json       raw Yahoo info dict
<dir>/yf/funds/<T>.json      already in the normalized yf_funds shape
```
`<T>` = `safe_name(ticker)` as passed, then `safe_name(norm_ticker(ticker))` (`BRK-B.csv`,
`ILS=X.csv`). A URL missing from the index, a missing file or a missing index → `FixtureMissing`.
Status ≥ 400 entries raise like the network path (403 → `ProviderBlocked`) unless
`raise_for_status=False`. `FixtureResponse` has `.url .status_code .ok .headers .content .text
.json() .raise_for_status()`.

## 13. Run directory and file naming

Per DESIGN §2: `runs/<run_id>/` with `run_id = YYYY-MM-DD_HHMM` (`_2`, `_3` when taken; dry runs
add `_dryrun`):
```
manifest.json  run_log.md
inputs/   *.yaml config snapshot (sha256 in manifest)
orders/   index.json + <order_id>.md            (work orders, DESIGN §2.1)
phase1/   prices.json macro.json lookthrough.json macro_view.json cio_allocation.json
phase2/   data/{technicals,fundamentals,valuation}_<T>.json
          reports/<role>_<T>.json  (retry: <role>_<T>.r2.json; failure stub: <role>_<T>.failed.json)
          evidence.json aggregate.json packets/sector_<sector>.json <sector>_verdict.json
phase3/   screen_<sector>.json data/... reports/... evidence_light.json aggregate_light.json
          evidence.json aggregate.json packets/... <sector>_verdict.json
phase4/   portfolio_preview.json cio_packet.json cio_final.json portfolio_plan.json
          memo.md memo.html summary.json   (dry-run: ledger_preview.csv; trial: trial_<T>.md/.html)
```
Per-ticker files are always `<kind>_<TICKER>.json` (`data_filename`) — never a bare `<T>.json`
(Windows reserves `PRN`, `CON`, `AUX`, `NUL`). Report filename prefix = role key
(`fundamental_KO.json`, `devils_advocate_KO.json`, `etf_QQQ.json`). Cached price series:
`data/cache/prices/prices_<T>.csv`.

## 14. Ledger

`ledger/recommendations.csv` (header committed, UTF-8, `newline=""`), columns in this exact order:
`run_id,rec_date,price_date,ticker,asset_type,sector,kind,role,action,rule_action,stance,conviction,evidence,rr,fair_value_base,invalidation_price,target_weight_pct,price_at_rec,spy_at_rec,sector_etf,sector_etf_at_rec,thesis,run_mode,report_path`
(semantics: DESIGN §4.12).

## 15. Test harness (`tests/conftest.py`)

- Autouse `_hermetic_env`: removes inherited `AGINV_*` vars, sets `AGINV_OFFLINE=1`,
  `AGINV_AS_OF=2026-10-02`, points cache/runs/ledger at a per-test temp dir, blocks
  `socket.socket.connect`/`connect_ex` (raises `NetworkBlocked`, a `RuntimeError`), clears
  `common` caches, and restores every `AGINV_*` var afterwards. The repo `config/` is used
  read-only unless the test uses `tmp_env`.
- `--run-live` enables tests marked `@pytest.mark.live` (otherwise skipped); live tests keep real
  sockets, no offline flag and the real date.
- Fixtures: `tmp_env` (namespace: `root, config_dir, cache_dir, runs_dir, ledger`,
  `read_config(name)`, `write_config(name, data)`, `update_config(name, fn)`,
  `use_sample_portfolio()`), `make_ohlcv` / `make_ranging_ohlcv` (factories), `use_fixtures(dir)`
  (sets `AGINV_FIXTURES_DIR`; relative to `tests/fixtures`), `fixtures_dir`, `repo_root`,
  `sample_portfolio`. `use_fixtures` also clears `AGINV_OFFLINE` (sockets stay blocked).
- Importable: `from conftest import make_ohlcv, make_ranging_ohlcv, SAMPLE_PORTFOLIO, REPO_ROOT,
  FIXTURES_DIR, DEFAULT_AS_OF`.
- `make_ohlcv(n=800, seed=0, drift=0.0003, vol=0.015, start=100.0, end="2026-10-02")`: seeded GBM
  bars on a business-day index ending at `end`; `High ≥ max(Open, Close)`, `Low ≤ min(Open, Close)`.
- To exercise live code paths without network, monkeypatch `common.requests.get` (or
  `sys.modules["yfinance"]`) after `monkeypatch.delenv("AGINV_OFFLINE")`.


## 12. Quote currency (added after the first live run)
`fetch_prices.fetch_series` / `load_series` return OHLCV in **USD per share**. A series quoted in
`ILS` or `ILA` (agorot) is converted with the spot USD/ILS rate (cache keeps the native quote and
its `currency` in the frame meta); other non-USD or unknown currencies raise `PriceFetchError`
(`load_series` returns None). `prices.json` entries for converted tickers carry `native`
`{quote_currency, currency: "ILS", last_close (ILS/share Fact), usdils, usdils_as_of, fx_usd_per_native}`
and the envelope flag `converted_to_usd`. `portfolio.py` uses `native.last_close` to estimate tax
for holdings whose `cost_basis_currency` is `ILS` (`tax.est_gain_ils`, `tax.est_tax_ils`).

## 13. Review-pass additions (2026-10-04)
- `common.round_price(x, sig=7, dp=6)`, `PRICE_SIG_DIGITS`, `US_SHARE_CLASS_TICKERS` (BRK.B, MKC.V ... map to `-`;
  `.TA`/`.L`/`.DE` keep their exchange suffix), `default_cache_dir()`.
- `http_get`: every `requests` exception is converted to `DataUnavailable` (content-decoding errors are
  retried with `Accept-Encoding: identity`).
- Fixture mode refuses the repo's real `data/cache` (set `AGINV_CACHE_DIR`).
- `fetch_macro` CLI: rc 2 on config/argument errors, rc 1 when every series failed.
- `config/committee.yaml`: `risk_reward.min_downside_pct`; `fundamentals.duration_days.H/9M` and
  `quarter_gap_days` widened. Optional module defaults: `screen.max_imputed_fraction` (0.5),
  `screen.warn_imputed_fraction` (0.25).

## 16. Chair review: `reopen`, `audit`, `chair-save` (committee.py)

The chair (main session) reviews a finished run. CLI (all print one JSON line; refusals are
`{"type":"error","step":<cmd>,"message","details":[...]}`, exit 2):

```
python scripts/committee.py reopen --run ID --order ORDER_ID [--order ORDER_ID ...] --feedback "text"
                                   [--feedback-file path] [--spec-file list.json] [--reason "one line"]
python scripts/committee.py audit --run ID                    # scripts.audit.run(run_dir) -> JSON on stdout
python scripts/committee.py chair-save --run ID --file chair_review.json
```
`--spec-file` = JSON list `[{"order_id","feedback"}, ...]`; `--feedback` / `--feedback-file` apply to every
`--order` (and to spec items without their own feedback). Multiple sources for one order are concatenated.

`reopen` result (type `agents`, same shape as `next` plus): `reopened: true`, `round`, `rounds_left`,
`steps` (steps whose orders are launched now), `invalidated_steps` (reset to pending), `kept_orders`
({step: [unaffected orders]}), `archive_dir`, `feedback_sha`. Launch the listed orders, then call `next` until `done`.

Rules: run `status` must be `done`; every order exists and is `done`/`failed`; feedback is non-empty
(<= 6000 chars, <= 28 lines per order); `orchestration.max_chair_rounds` (default 2) rounds per run;
an order may reach at most `orchestration.max_attempts + 2` attempts. A refusal changes nothing.

Order record additions: `reopened_by: "chair"`, `reopen_round`, `reopen_history[{round,at,from_attempt,
previous_output}]`, `attempt_limit` (validator-retry ceiling of the current round), `chair_prior` (the
non-validator `prior_errors` of the latest dispatch), `rechecked_round` (downstream order re-dispatched
after round n). `prior_errors` entries: `CHAIR FEEDBACK: <line>` and `inputs changed after chair re-run of
<order ids>; re-check your conclusions against the updated reports`; the order markdown renders them as
"Chair feedback - address every point" / "Inputs changed" (validator errors stay under "Retry").

Manifest: `status` `reopened` while a round runs (back to `done`); `chair_rounds[]` =
`{round, at, reason, orders, feedback_sha, feedback_file, invalidated_steps, reopened_steps, kept_orders,
superseded_orders{order_id: {step, attempt, status, output}}, archive_dir, archived_files,
previous_finished_at}`; a reset step carries `reset_round` and (agents steps) `invalidated{round,text,keep,carry}`
until it is re-dispatched. `status` adds `chair_round` and `pending_orders`.

Files: `orders/chair_feedback_r<n>.json` (`{schema:"chair_feedback/1", run_id, round, at, reason, items,
feedback_sha}`); `_superseded/round<n>/<original relative path>` (+ `index.json`) holds the archived outputs, old
order files and the previous memo (nothing is deleted; `ledger_preview.csv` is never moved);
`phase4/chair_review.json` (+ `chair_review.r<round>.json` for earlier rounds), schema
`schemas/chair_review.schema.json` (`chair_review/1`: `schema, run_id, round>=1, reviewed_at, verdict
approved|approved_with_notes|needs_rework, headline<=40 words, executive_summary 3-7 items <=45 words,
decisions[{ticker, action BUY|ADD|HOLD|TRIM|SELL|PASS, chair_view<=90 words, confidence low|medium|high,
what_would_change_it<=40 words}], changes_made[{finding_id|null, what, how agent_rerun|deterministic_fix|
chair_edit|accepted_as_is}], open_issues[], user_actions[], disclaimer_confirmed: true`; unknown keys rejected).

Ledger: `ledger.append(rows, ledger_path=None, *, replace_run_ids=None)` drops the existing rows of the given
run ids before appending; `p4.finalize` passes `[run_id]` once the manifest has `chair_rounds`.
