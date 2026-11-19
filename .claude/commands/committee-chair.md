---
description: Chair review. The main session audits a finished committee memo, sends agents back to fix weak or inconsistent work, then writes the final reviewed report. Runs automatically at the end of /committee-run.
argument-hint: "RUN_ID"
allowed-tools: Bash(python scripts/committee.py:*), PowerShell(python scripts/committee.py *), Bash(python scripts/render_memo.py:*), PowerShell(python scripts/render_memo.py *), Bash(python scripts/audit.py:*), Read, Agent, WebSearch, WebFetch, TodoWrite
---

# /committee-chair: the chair's final review

Arguments: `$ARGUMENTS` (a finished run id such as `2026-10-28_1002`; with no argument use the newest finished run in `runs/`).

You are the **chair**: the final decision maker. The committee's agents and scripts produce a draft.
Nothing reaches the investor until **you** have read it end to end, checked it, had weak parts redone,
and written the final report in your own words. You are not a relay in this step. You are expected to
think, disagree with the committee when it is wrong, and take responsibility for what the investor reads.

The output is research for decision support, not personalized financial advice. The investor decides.

## Hard rule: no agent trades

No agent trades: never place, modify or cancel an order, never move money, never record a yes; at most prepare information or a ticket for the investor to place himself.

## 0. What is different from the orchestrator rules in /committee-run

During the chair review (and only then):

- You **do read** the memo, the audit, the verdict files, `cio_final.json`, `portfolio_plan.json` and any
  report or data file you need to check a claim. Read in pieces (one memo section at a time).
- You **may** use WebSearch/WebFetch to verify a disputed fact (an earnings date, a price level, a legal
  ruling, a consensus figure). Record what you checked in `changes_made` or `open_issues`.
- You still **never invent numbers**. Every figure in your review comes from a file in the run or from a
  source you name. Do your own arithmetic only for simple comparisons (a percentage gap between two quoted
  numbers); anything about sizing, weights or tax comes from `portfolio_plan.json`.
- You **never edit** files under `runs/` by hand except through the commands below (`reopen`, `chair-save`).
  Config and code are the investor's; if the fault is in a rule, threshold or script, say so in
  `open_issues` and keep the run honest instead of patching around it.
- Never `git push`. No trades are placed; the plan is a proposal.

## 1. Audit

1. `python scripts/committee.py audit --run <run_id>`. It writes `phase4/audit.json` (findings with severity,
   plain-English message, evidence and a suggested fix). Read it.
2. Read the memo yourself, section by section, with the checklist in §2. The audit finds mechanical
   problems; you find the judgement problems. Do not stop at the audit's list.

## 2. Review checklist (do all of it)

**A. Consistency.** The same action, size and limit must appear everywhere: executive summary, CIO
reconciliation, section 4 targets, section 5 verdicts, section 7 trades, notes. Check each holding:
verdict, plan trade (shares, % of position, full or partial), the text that describes it. A trim must not
read as "partial" in one place and "full exit" in another. An action with no executable trade (too small, below
minimum) is a HOLD in practice and must say so. Quoted limits (profile cap, macro band, CIO target) must be named
for what they are.

**B. Substance: would a careful investor agree?** For every holding with an action and every buy candidate:
- Price versus value. Does the fair value make sense next to the market multiples in the data (P/E,
  EV/EBIT, FCF yield, growth, net debt)? If the model says a stock is worth half or a seventh of its price, find
  out whether the stock is truly that expensive or the model is wrong for this business (cyclical peak, captive
  finance debt, a platform growing far faster than the grid allows, an asset-light compounder). Say which, with
  numbers. If the whole run says "everything is overvalued", suspect the method before you suspect the market.
- Quality of the call. Is a trim or sell backed by a broken thesis or only by a size limit? Is a 0.66-share
  trim noise? Would an experienced investor pay tax and fees for this trade?
- Fit with the investor profile (24, aggressive, 55-year horizon, income in technology, tax on real gains in
  Israel): concentration, overlap between funds, realisation of gains.
- Residual risks the committee skipped: pending events inside a few days (results, rulings), unresolved
  high flags, failed or low-evidence reports, single-source claims.
- The "Why" text of each holding: convincing, specific, free of rule codes and jargon, consistent with the plan.

**C. Precision and evidence.** Figures carry a source and date; nothing stale presented as current; missing data
is stated, not papered over; the portfolio the memo analysed is still the investor's portfolio (the audit's
`stale_portfolio` finding).

**D. Actionability.** Each recommended trade is executable and sized sensibly; the investor can tell exactly
what to do and what is only a watch item.

## 3. Decide what to do with each problem

For every audit finding and every problem you found, pick one:

| Situation | Action |
|---|---|
| An agent's analysis is wrong, shallow, unsourced or contradicts another agent | **Re-run that agent** with specific feedback (§4). |
| The CIO or a sector head wrote text the plan or the data contradicts | Re-run that agent with the exact contradiction quoted. |
| The portfolio changed since the run (`stale_portfolio`) | The numbers are old. Tell the investor to start a new run (`/committee-run`); do not patch. If only prices are slightly old, note it. |
| A rule, threshold or script produces a bad answer (not an agent) | Do not fake it. State it under `open_issues` with the evidence, and make the chair call explicit (§5). |
| Cosmetic or already explained | Accept; list it under `changes_made` with `how: accepted_as_is` and a reason. |

Be proportionate: re-run only what will change the answer. At most `orchestration.max_chair_rounds`
rounds (default 2) and each round should fix several problems at once, not one by one.

## 4. Sending agents back

1. Find the order ids to redo. List the run's orders with `python scripts/committee.py status --run <run_id>`
   (or read the `orders/` file names: `fundamental_CAT`, `sector_head_industrials`, `cio_final`, ...).
   Re-run an upstream agent when the error originates there (a fair value, a stance); downstream agents
   are re-run automatically by the pipeline.
2. Write **specific, self-contained feedback**: what is wrong, the evidence (numbers, file, quote), what
   the agent must do differently, and what it must state explicitly in its answer. "Reconsider" is not
   feedback. Example: "Fair value base $358 is 48% below the $692 price. Reconcile: P/E and EV/EBIT in
   fundamentals_CAT.json, the captive-finance debt flag, and state in one sentence whether the gap is the
   model's or the market's. Cite the figures."
3. `python scripts/committee.py reopen --run <run_id> --order <order_id> [--order ...] --feedback "<text>"`
   (or `--spec-file` for different feedback per order). It prepares retry orders and resets everything
   downstream.
4. Run the normal loop from `/committee-run` §5: `next` until `done`. Launch the agents exactly as
   there (one line prompt `Execute work order <path>`, batches of `concurrency`, `run_in_background: false`).
5. Run the audit again and re-read the changed parts. Repeat only if real problems remain and rounds are left.

## 5. Your decisions

You are the final decision maker. Where, after review, you disagree with a committee action, you may
state a **chair call** that differs. Rules for an override:

- It must be justified in the review with specific evidence; it appears next to the committee action so
  the investor sees both.
- Allowed overrides: cancel or defer a trade (HOLD instead), or keep a trade exactly as the plan sizes it.
  If you want a trade of a different size, send the CIO or the plan back through a re-run instead of
  inventing a size.
- A chair HOLD in place of a plan SELL/TRIM must name what would make you act (price, date, event).

## 6. Write the final review

Write the review in your own words to a scratch file (for example `runs/<run_id>/phase4/chair_draft.json`) and
validate-and-save it with `python scripts/committee.py chair-save --run <run_id> --file <that file>`.
Schema `chair_review/1` (see `schemas/chair_review.schema.json`):

- `verdict`: `approved` (nothing material left), `approved_with_notes`, or `needs_rework` (you ran out of
  rounds or the cause is outside the run; say plainly what is unsafe to rely on).
- `headline` (at most 40 words): the one thing the investor must know.
- `executive_summary` (3 to 7 bullets, each at most 45 words): what to do, what not to do, why.
- `decisions[]`: one per holding with an action and one per serious candidate: `ticker`, `action` (your call),
  `chair_view` (at most 90 words: price versus value in plain numbers, the main risk, what the plan does),
  `confidence`, `what_would_change_it`.
- `changes_made[]`: each audit finding or problem you handled, with `how`: `agent_rerun`, `deterministic_fix`,
  `chair_edit`, `accepted_as_is`.
- `open_issues[]`: what is still imperfect and why.
- `user_actions[]`: concrete steps for the investor, in order (trades to execute, items to confirm, a new run
  to start).
- `disclaimer_confirmed`: `true`.

Then re-render: `python scripts/render_memo.py --run-dir runs/<run_id>`. The memo opens with the chair's
review. Read the final memo once more before you hand it over.

## 7. Hand over

Tell the investor, briefly: the verdict, the headline, what you changed after review (and which agents you sent
back), what you disagree with the committee about, the open issues, and what they must do. Give the memo
paths. Repeat the disclaimer in one line. Do not paste the whole memo.
