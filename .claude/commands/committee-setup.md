---
description: Interactive setup. Ask the investor questions, fill config/investor_profile.yaml, config/portfolio.yaml and config/universe.yaml, then validate them.
argument-hint: "[profile|portfolio|universe]"
allowed-tools: Read, Edit(config/investor_profile.yaml), Edit(config/portfolio.yaml), Edit(config/universe.yaml), Write(config/investor_profile.yaml), Write(config/portfolio.yaml), Write(config/universe.yaml), AskUserQuestion, TodoWrite, Bash(python scripts/committee.py:*), PowerShell(python scripts/committee.py *)
---

# /committee-setup: configure the committee for this investor

Arguments: `$ARGUMENTS`

- With no argument, set up all three files in this order: **profile**, then **portfolio**, then
  **universe**.
- With `profile`, `portfolio` or `universe`, redo only that file.
- For any other argument, show the usage line `/committee-setup [profile|portfolio|universe]`
  and stop.

These files are configuration, not advice. The committee's output is decision support only.

## Hard rule: no agent trades

No agent trades: never place, modify or cancel an order, never move money, never record a yes; at most prepare information or a ticket for the investor to place himself.

## 0. Rules

- **Privacy.** Never ask for or store passwords, API keys, broker account numbers, ID numbers
  or other credentials. None are needed, because every data source is keyless. If the user
  offers one, decline and do not write it anywhere. The only personal datum stored is
  `sec_contact_email`.
- **No arithmetic.** Do not derive numbers yourself (no ILS→USD conversion, no value→shares, no
  weight sums). Ask the user for each figure in the unit of the field it goes in. If the user
  only knows market values, use `weight_pct` plus `total_value_usd`, using numbers they supply.
- **Edit in place.** Use the **Edit** tool. Use Write only if a file is missing or unreadable;
  in that case rebuild it with the same keys and comments as the template described below.
  - Preserve every comment, key and the key order. Change only values.
  - **Quote tickers and dates**: `ticker: "KO"`, `purchase_date: "2024-02-01"`, `as_of: "2026-10-03"`.
  - Numbers are bare (`shares: 12`, `min_market_cap_usd: 10000000000`). Unknown values are `null`.
    Booleans are `true`/`false`.
  - Tickers use the Yahoo style, upper case: `BRK-B`, not `BRK.B`.
- **Scope.** Do not touch `config/committee.yaml` (committee thresholds) or
  `config/etf_overrides.yaml` unless the user explicitly asks. Only the three files above are
  pre-approved for Edit and Write. Any other path asks the user for permission, by design.

## 1. Read the current state

1. Read `config/investor_profile.yaml`, `config/portfolio.yaml` and `config/universe.yaml`.
2. Run `python scripts/committee.py validate-config` from the repository root with a timeout of
   600000 ms. It shows what is still missing; the prefilled portfolio fails on purpose.
3. Show the user a compact summary of the current values. Optionally create a short task list
   with one item per file.

## 2. How to ask

- Use **AskUserQuestion** in small groups: at most 4 questions per call, with 2-4 options each.
  The user can always type a custom answer through "Other".
- Put the current value first, for example "Keep 10% (current)".
- Ask about one topic group at a time. Never ask everything at once.
- For values that have no sensible presets (shares, cost basis, dates), ask in a short
  plain-text message that names exactly the fields and the format. For example:
  `KO: shares, average cost per share (USD), purchase date YYYY-MM-DD, or "skip"`.
  Cover 2-3 holdings per message.

## 3. Profile (`config/investor_profile.yaml`)

Ask in these groups:

1. Who the committee works for. Ask about the `investor` block first, because it determines the
   next two answers: age, occupation or study field, any period of low fixed costs
   (`lives_with_parents_until`), expected income (`expected_income.monthly_ils`, when it starts, and
   whether the figure is gross or net), whether the career is tied to one sector
   (`income_sector_correlation`), whether the horizon is lifelong or has a withdrawal date
   (`horizon_note`), and any regular monthly contribution (`contribution_plan_ils_per_month`).
   Record what the user states and use `null` for anything they don't know. Never invent a value.
   Then set `horizon_years` (for example 5 / 10 / 20, or about 55 for a 24-year-old investing for
   life) and `risk_tolerance` (`conservative`, `moderate` or `aggressive`), proposing values that
   follow from the answers and letting the user change them.
2. Limits:
   - `max_position_pct`: max weight of one stock
   - `max_fund_position_pct`: max weight of one fund/ETF
   - `max_sector_pct`: max look-through weight of one GICS sector

   These must satisfy `max_position_pct <= max_sector_pct`.
3. Trading:
   - `min_trade_usd`: smaller trades are dropped from the plan
   - `target_cash_pct`: cash kept after buys; must be below 100
4. `sec_contact_email`. **SEC EDGAR requires a real contact email in the User-Agent**
   (`agent-investors research <email>`). It is sent only to sec.gov in that header. It must be a
   real address and must not contain "example". Offer "keep current" plus "Other".

Leave these as they are unless the user asks: `base_currency` (ILS), `tax_jurisdiction` (IL),
`capital_gains_tax_pct` (25), `allow_short` (must stay `false`) and `allow_fractional_shares`.
Mention once that the plan's tax estimate is indicative only. Israeli tax applies to the real
(CPI/FX-adjusted) gain, so the user should consult a tax advisor.

## 4. Portfolio (`config/portfolio.yaml`)

1. **Which holdings.** List the prefilled tickers (as read from the file; initially QQQ, SPMO,
   BRK-B, KO, PG, JPM).
   - Ask which ones to **remove** (multi-select).
   - Ask whether to **add** any, with ticker and `stock` or `fund`.
   - To remove a holding, delete its whole block.
   - To add a holding, copy the full block structure: `ticker, asset_type, shares, weight_pct,
     cost_basis, cost_basis_currency, purchase_date, currency, proxy_ticker, sector_override`.
     Keep the YAML indentation.
2. **Entry method.** Positions are entered as `shares` (preferred) or `weight_pct`, the percent
   of the whole portfolio.
   - If any holding is weight-only, `total_value_usd` is required.
   - If both are given, shares win.
3. **Non-US funds (proxy tickers).** Ask whether any fund is really held through a TASE-listed or
   other non-US UCITS tracker, for example a NASDAQ-100 tracker on TASE.
   - If so, set that holding's `proxy_ticker` to the US-listed ETF that tracks the same index,
     for example `"QQQ"` for NASDAQ-100 or `"SPMO"` for S&P 500 momentum. The committee prices
     and decomposes the fund through the proxy.
   - For those holdings, use `weight_pct`, not `shares`. Shares would be valued at the proxy's
     US price, and one TASE unit is not one proxy share.
   - Set `currency` to the fund's real trading currency (for example `ILS`).
   - **Leave `cost_basis` `null`** for proxy-priced holdings, and leave `cost_basis_currency`
     as it is. A cost per TASE unit cannot be compared with the proxy's USD price, so the plan
     never estimates a capital gain or tax for a holding with a `proxy_ticker`
     (`scripts/portfolio.py` skips it and adds a note). Tell the user this once.
4. **Per-holding values.** For each holding, ask for:
   - shares (or weight %);
   - optional cost basis: the average cost per share **in USD**. It enables the indicative
     capital-gains estimate, but only for a holding priced through its own ticker (no
     `proxy_ticker`) with `cost_basis_currency: USD`. A cost basis in another currency is
     stored but not used for the estimate. Skip this question for proxy-priced holdings;
   - optional purchase date (YYYY-MM-DD).
5. **Portfolio level.**
   - Ask for cash as `cash_usd` **or** `cash_pct`, not both.
   - Ask for `total_value_usd` if needed.
   - Ask for `as_of`, the date the positions were read from the broker. Default to today's date
     and quote it.

The validator also checks:

- `shares > 0`;
- `0 < weight_pct <= 100`;
- the sum of `weight_pct` plus `cash_pct` is at most about 100;
- no duplicate tickers.

## 5. Universe (`config/universe.yaml`)

Ask about:

1. `min_market_cap_usd`. The current value is $10B; alternatives are $2B or $50B.
2. `min_avg_dollar_volume_usd` over the 63-day window. The current value is $50M; alternatives
   are $20M or $100M.
3. `exclusions`: tickers never to propose, as a quoted list, for example `["TSLA", "BRK-A"]`.
4. `additions` per sector: extra candidates on top of the sector ETF's top 10, for example
   `health_care: ["VRTX", "ISRG"]`. Sector keys are `information_technology, health_care,
   financials, energy, industrials, consumer_discretionary, consumer_staples, utilities,
   materials, real_estate, communication_services`.

Leave `region` (US) and `max_candidates_per_sector` as they are unless asked;
`max_candidates_per_sector` mirrors `phase3.shortlist_size` in `committee.yaml`.

## 6. Validate until clean

1. After each file is edited, run `python scripts/committee.py validate-config`.
2. If it reports errors:
   - explain each one in plain words;
   - ask only about the affected fields;
   - fix them with Edit;
   - run it again.
3. Repeat until it reports no errors. Do not stop with a config that fails validation unless the
   user asks you to.
4. Finish with a compact summary of the final values (no secrets). Then give the next steps:
   - `/committee-run --dry-run`: an offline smoke test with no agents and no web.
   - `/committee-run --quick`: the first live run, about 20 agent calls.
   - `/committee-run`: the full run, about 85-100 agent calls with 2 expand sectors.
