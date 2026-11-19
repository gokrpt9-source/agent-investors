# knowledge/ — sector and fund playbooks

Static, human-maintained domain knowledge that the committee's agents read alongside the run's data
files. A playbook tells an agent **what matters** in a sector and **how to read the scripts' outputs**
for it; it never supplies numbers.

```
knowledge/
  README.md                         this file
  etf_playbook.md                   funds (ETFs) — etf-analyst + the `funds` sector head
  sectors/
    information_technology.md       XLK      consumer_discretionary.md   XLY
    health_care.md                  XLV      consumer_staples.md         XLP
    financials.md                   XLF      utilities.md                XLU
    energy.md                       XLE      materials.md                XLB
    industrials.md                  XLI      real_estate.md              XLRE
                                             communication_services.md   XLC
```

File stems are the sector keys from `common.SECTOR_KEYS`; the benchmark ETFs are
`config/committee.yaml: sector_etfs`. The directory can be relocated with `AGINV_KNOWLEDGE_DIR`
(`common.knowledge_dir()`).

## Format

Every playbook is UTF-8 Markdown with a YAML front-matter block between `---` lines.

### Front-matter keys (all required)

| Key | Type | Meaning |
|---|---|---|
| `sector` | string | Sector key (`materials`, ..., or `funds` for the ETF playbook); equals the file stem for sector files |
| `name` | string | Display name = `common.sector_display(sector)` (`Funds (ETFs)` for the ETF playbook) |
| `benchmark_etf` | string | `committee.yaml: sector_etfs.<sector>`; `committee.yaml: benchmark` (SPY) for funds |
| `sector_class` | string | `common.sector_class(sector)`: `financials` for financials, `fund` for funds, else `default` |
| `key_kpis` | list[str] | The KPIs the lenses should look for |
| `valuation_methods` | list[str] | Methods that fit the sector (and how to read the valuation grid) |
| `macro_sensitivities` | list[{series, direction, mechanism}] | `series` is a FRED id from `committee.yaml: macro.series`; `direction` is `positive`, `negative` or `mixed` (effect of a *rising* series on the sector); `mechanism` is one short sentence |
| `failure_modes` | list[str] | Generic historical patterns of how investments in the sector go wrong |
| `edgar_tags_of_interest` | list[str] | us-gaap tag names worth checking in companyfacts or the filing (may be `[]`) |

The front-matter is machine-readable (e.g. a macro or CIO work order can be given the
`macro_sensitivities` of every sector); the body is for reading.

### Body headings (sector playbooks — identical in all 11 files, in this order)

```
# <Name> — Sector Playbook
## 1. Scope & benchmark
## 2. KPIs that matter
## 3. Valuation methods
## 4. Macro sensitivities
## 5. Typical failure modes
## 6. Red flags for the Devil's Advocate
## 7. Lens checklist
## 8. Data notes
```

- **§1** GICS industries covered, the benchmark ETF, how Yahoo's sector label maps to the key, and the
  sub-industry differences that change the analysis.
- **§2** a table `KPI | Why it matters | Where to find it | Healthy vs warning signs`.
- **§3** which methods fit and which mislead; how to read `valuation_<T>.json` (DCF on course
  NOPAT/FCF, P/E, EV/EBIT or P/B cells, reverse DCF) for this sector; when EV metrics are not meaningful.
- **§4** the front-matter sensitivities as a table, plus series that are *not* configured and must be
  sourced on the web.
- **§5–§6** failure modes, and the red flags the Devil's Advocate should test.
- **§7** bullets per specialist role: `fundamental`, `technical`, `operator_ceo`, `industry_ops`, `osint`.
- **§8** EDGAR / yfinance quirks: tags that are missing or misleading, and where agents must read the
  filing instead of trusting the script output.

### Body headings (ETF playbook)

```
# Funds (ETFs) — ETF Playbook
## 1. Role in portfolio
## 2. Expense ratio & costs
## 3. Concentration (top-10)
## 4. Overlap math & look-through sector caps
## 5. Factor-ETF specifics
## 6. How the deterministic fund rules work
## 7. Israeli-investor considerations
## 8. Data notes
```

## How agents use the playbooks

- The orchestrator (`scripts/committee.py`) lists the playbook in a work order's `inputs` — sector
  orders get `knowledge/sectors/<sector>.md`; `etf` orders and the `funds` sector head get
  `knowledge/etf_playbook.md`. Agents read only the files their work order lists.
- **Specialists** (fundamental, operator_ceo, industry_ops, osint) use §2 to decide what to look for,
  §3 to pick and justify valuation cells, §7 for their own lens checklist and §8 to know when a script
  value cannot be trusted (then read the filing, cite it, or set the value to `null` and list it in
  `missing_data`).
- The **technical analyst never reads a playbook** (its lens is `technicals_<T>.json` only, by
  design). The `technical` bullets in §7 are a reading guide for sector heads and the Devil's Advocate
  when they weigh a technical report for that sector.
- **Devil's Advocate** uses §5 and §6 to choose attacks; **sector heads** use the whole file to judge
  dissents and deviations; **macro / CIO** can use §4 / `macro_sensitivities` to tie sector
  constraints to fetched FRED series.
- A playbook is guidance, never evidence: it is not a source, cannot be cited for a figure, and does
  not change any rule in `config/committee.yaml`.

## Writing rules

1. **No market figures.** No prices, yields, multiples, margins or dated statistics — agents source live
   numbers with URLs and as-of dates. Rules of thumb are allowed only when labeled **heuristic**.
2. Thresholds the scripts apply live in `config/committee.yaml`; refer to them by key, never restate
   or redefine them.
3. Macro series must be ids from `committee.yaml: macro.series`; EDGAR tags are us-gaap element names.
4. Practitioner tone, concise, English, 150–260 lines per sector playbook.
5. Keep structure identical across files; `tests/test_playbooks.py` checks existence, front-matter
   keys, `benchmark_etf` vs `committee.yaml`, `sector_class`, known FRED ids, and the heading order.
