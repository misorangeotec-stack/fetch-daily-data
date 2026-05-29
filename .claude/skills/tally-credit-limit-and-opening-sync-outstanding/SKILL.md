---
name: tally-credit-limit-and-opening-sync-outstanding
description: Sync Sundry Debtor master data (Credit Period, Credit Limit, Opening Apr-25, Opening Apr-26) from local Tally Prime into the credit-limit Google Sheet. Use whenever the user asks to fetch/pull/sync/export Tally credit limits, opening balances, or debtor master data into the credit-limit/opening sheet, refresh the opening balances, or run the daily/periodic credit-limit sync. Handles multiple loaded companies, computes Apr-26 opening as the FY 25-26 closing balance, and upserts by (Company, Location, $Name).
---

# tally-credit-limit-and-opening-sync-outstanding

Pulls Sundry Debtor ledger master data from local Tally Prime via XML over
HTTP, then **upserts** rows in a single Google Sheet that holds credit limits
and FY opening balances per customer per company / location.

This skill is **self-contained**: tools and workflow live inside this folder.
It expects to be run from the `FETCH DAILY DATA` project so it can pick up
the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_credit_limits_to_sheet.md`](workflows/sync_tally_credit_limits_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys:
   `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`,
   `CREDIT_LIMIT_SHEET_URL`, `CREDIT_LIMIT_SHEET_TAB`. If any are missing,
   prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load
   companies in Tally and stop.

3. **Fetch credit limits & openings.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_credit_limits.py" --output .tmp/credit_limits_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script
   reads the column schema from this skill's
   [`reference/columns.md`](reference/columns.md), the company mapping from
   [`reference/companies.md`](reference/companies.md), and the sales-person
   mapping from [`reference/sales_persons.md`](reference/sales_persons.md),
   so you do not need to know any of that schema.

   The fetch is **fixed to FY 25-26** (1-Apr-2025 → 31-Mar-2026) — Apr-25
   opening comes from `OPENINGBALANCE` in that book and Apr-26 opening is
   the `CLOSINGBALANCE` (Tally computes it from the date variables). When
   FY 26-27 books exist as a separate company in Tally, switch to using
   that company's `OPENINGBALANCE` for Apr-26 instead — see workflow's
   "Lessons learned".

4. **Push to sheet (upsert).** Run:
   ```
   python "<SKILL_DIR>/tools/push_credit_limits_to_sheet.py" --input .tmp/credit_limits_<timestamp>.json
   ```
   The script:
   - Validates row 1 against `reference/columns.md` (writes the header if
     the tab is empty).
   - Reads existing rows keyed by `(Company, Location, $Name)`.
   - For each fetched row: if the key exists, **updates that row in place**
     (preserves any extra columns the user has added to the right). If new,
     **appends**.

5. **Report.** Read the JSON summary printed by the push script and tell
   the user: rows fetched, rows appended, rows updated, rows unchanged, and
   the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in
[`reference/columns.md`](reference/columns.md). To add/remove/reorder a
column, edit that table — no Python changes needed. The shared loader
[`tools/_schema.py`](tools/_schema.py) parses it.

To add a new sales-person mapping or fix one, edit
[`reference/sales_persons.md`](reference/sales_persons.md).

To onboard a new Tally company or refresh names after the April FY
rollover, edit [`reference/companies.md`](reference/companies.md).

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's
  not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity →
  Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from
  `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth
  browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1
  that doesn't match `reference/columns.md`. Either fix the sheet's header
  row to match, or update the schema file.
- **All values are negative** — sign-flip is broken. Tally exports debtor
  balances as negative; the fetch tool flips the sign on credit limit and
  openings. If you see negatives in the sheet, a customer genuinely has an
  advance/credit balance with the company — investigate.
- **Sales Person blank for some rows** — that ledger name isn't in
  `reference/sales_persons.md`. Look at stderr for the warning lines and
  add the missing rows to the mapping.
- **Wrong company name in sheet** — the raw Tally name is missing from
  `reference/companies.md`. Add it; FY rollovers in April change the
  `(from 1-Apr-XX)` suffix.
