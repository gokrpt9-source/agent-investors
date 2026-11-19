---
description: Score past committee recommendations against SPY and the sector ETFs. Reports hit rate by agent role and by action, with sample sizes and caveats.
argument-hint: "[--ledger path/to/recommendations.csv]"
allowed-tools: Bash(python scripts/committee.py:*), PowerShell(python scripts/committee.py *), Read
---

# /committee-scorecard: how did past calls perform?

Arguments: `$ARGUMENTS`

## Hard rule: no agent trades

No agent trades: never place, modify or cancel an order, never move money, never record a yes; at most prepare information or a ticket for the investor to place himself.

## 1. Parse arguments

- With no argument, the command uses the default ledger, `ledger/recommendations.csv`.
- `--ledger <path>` is the only option. Check the path before using it:
  - It must match `^[A-Za-z0-9_.:/\\-]+\.csv$`: letters, digits and `_ . : / \ -` only, ending
    in `.csv`. A drive letter such as `C:` is fine. Spaces, quotes, `$`, `;`, `&`, `|`, `<`,
    `>`, backticks and every other character are rejected.
  - **Convert every backslash to a forward slash** before the call, so
    `C:\Users\me\ledger\recs.csv` becomes `C:/Users/me/ledger/recs.csv`. Python accepts forward
    slashes on Windows. The Bash tool is Git Bash, which strips unquoted backslashes and would
    pass a path that does not exist (`C:Usersmeledgerrecs.csv`). Use the converted path with
    the PowerShell tool too.
- For anything else, show the usage line `/committee-scorecard [--ledger path]` and stop.
- Never paste unvalidated text into a shell command.

## 2. Run the scorer

1. Run `python scripts/committee.py scorecard` (adding `--ledger <path>` with the
   forward-slash path if one was given). Run it from the repository root with the Bash or
   PowerShell tool and a timeout of 600000 ms.
   - It wraps `ledger.py score` and may fetch or reuse cached prices.
   - It writes a `scorecard_<date>.md` file next to the ledger it scored, and prints that
     file's path in the `scorecard_md` field. With `--ledger <path>` that is the given ledger's
     folder, not `ledger/`, so never guess the path.
2. Read what it prints. If the printed output does not include the per-role and per-action
   tables, Read the file at the printed `scorecard_md` path; it is small.
3. If it fails, show the error and stop.

## 3. Summarize

Quote figures **exactly as printed**. Do not recompute, average, extrapolate or invent any number.

Present the following:

1. **Coverage**: as-of date, ledger rows, rows scored, and rows not scored. Rows go unscored
   when they are too young, are HOLD/PASS/neutral calls, or have missing prices.
2. **By role**: a table of role · n · hit rate · mean excess vs SPY (pp) · mean excess vs sector
   ETF (pp). The roles are committee, sector_head, each specialist role and devils_advocate.
3. **By action**: the same columns for ADD / BUY / TRIM / SELL.
4. A one-line takeaway, only if the samples support one; otherwise say there are too few
   scored calls to draw a conclusion.

Always add these caveats:

- **Small samples.** Mark every cell with fewer than about 10 scored calls as anecdotal. A hit
  rate from a handful of calls is mostly noise. The same tickers recur across runs and roles,
  so rows are not independent.
- **Minimum age.** Recommendations younger than `ledger.min_age_days` (`config/committee.yaml`)
  are not scored. Short windows are dominated by market noise and say little about a long-horizon
  thesis.
- **Survivorship and selection.** The ledger contains only names the committee reviewed.
  Tickers whose prices can no longer be fetched (delisted, merged, renamed) drop out, which
  flatters results. HOLD, PASS and neutral calls are excluded from hit rates.
- **What a "hit" means.** A hit is only the sign of the excess return vs SPY. It is not
  risk-adjusted, and it ignores position size, costs, taxes and USD/ILS currency effects.
- Past calls do not predict future performance. This is decision support, not personalized
  financial advice.

If the ledger is empty or nothing is old enough to score, say so. Suggest running
`/committee-run`, then scoring again once recommendations are older than `ledger.min_age_days`.
