# Dry-run fixtures

Default fixtures for `python scripts/committee.py dry-run` (SPEC §10, DESIGN §2): the whole
committee pipeline — real `_steps`, `validate`, `aggregate`, `portfolio`, `render_memo`,
`ledger` — runs offline at as-of **2026-10-02** and writes `phase4/memo.md` / `memo.html`.

**Everything here is synthetic test data.** Prices, fund compositions, FRED series and most SEC
files are generated; the agent reports are programmatic placeholders. Nothing is real market data,
real research or investment advice.

## Layout

| Path | Content |
| --- | --- |
| `config/` | `committee.yaml`, `etf_overrides.yaml`, `universe.yaml` (copies of the repo config), `investor_profile.yaml` (test contact email), `portfolio.yaml` (sample portfolio: QQQ, SPMO, BRK-B, KO, PG, JPM + cash) |
| `data/yf/history/<T>.csv` | seeded synthetic OHLCV, 780 NYSE business days ending 2026-10-02 (holdings, SPY, the 11 SPDR sector ETFs, `ILS=X`, the health care universe) |
| `data/yf/info/<T>.json` | Yahoo-shaped quote/profile info (sector, shares, market cap = shares × last close) |
| `data/yf/funds/<T>.json` | normalized fund composition for QQQ, SPMO and XLV (the Phase 3 universe) |
| `data/http/index.json` | URL → file map for the network seam (CONTRACTS §12) |
| `data/http/fred/*.csv` | the 13 FRED series of `committee.yaml macro.series` |
| `data/http/sec/*` | SEC `company_tickers.json`, companyfacts and submissions. KO, JPM and BRK-B are the trimmed real-shaped files from `tests/fixtures/edgar`; PG and the health care names (LLY, JNJ, ABBV, MRK, TMO, ISRG, ABT) are compact synthetic filings |
| `agent_outputs/<order_id>.json` | one report per work order (59), with `${RUN_DIR}`, `${REPO_ROOT}`, `${RUN_ID}`, `${AS_OF}` tokens that `committee.simulate_agents` substitutes |
| `build_fixtures.py` | generator for `data/` (`data` stage) and `agent_outputs/` (`agents` stage); `all` runs both |
| `_authors.py` | the role authors used by the `agents` stage |

## What the dry run exercises

* Phase 1: macro view (constraints incl. an information-technology cap below the current
  look-through weight) and CIO allocation with a documented macro-bound **constraint override**;
  `health_care` is the one expand sector.
* Phase 2 (6 holdings): KO HOLD (an OSINT high / must-address flag answered by the Devil's
  Advocate), PG rule SELL from a **broken** DA verdict → sector-head **rule deviation** to HOLD
  with justification and DA rebuttal, JPM TRIM after a **damaged** DA verdict, BRK-B HOLD,
  QQQ HOLD, SPMO ADD.
* Phase 3 (`health_care`, XLV top holdings): screen → 6 candidates → light wave → 4 finalists →
  full wave + DA → **MRK BUY** (unresolved confirmed OSINT flag), the other finalists **PASS**.
* Phase 4: CIO reconciliation, final plan (SPMO buy, JPM sell, MRK buy), ledger preview
  (`phase4/ledger_preview.csv`; the real ledger is never touched), memo with all 11 sections.

## Regenerating

```
python tests/fixtures/dryrun/build_fixtures.py          # data + agents
python tests/fixtures/dryrun/build_fixtures.py agents   # agent outputs only
```

The `agents` stage runs the real dry run in a temporary runs dir and, at every agents step,
authors each order's output from that run's own input files (data files, aggregate, packets),
validates it with `validate.validate_order_output_report` and stores it here with the run dir
tokenized. Both stages are deterministic; rerun `agents` after any change to the data, the
configs or the deterministic scripts that alters a number an agent cites, then run
`pytest tests/test_e2e_dry_run.py`.
