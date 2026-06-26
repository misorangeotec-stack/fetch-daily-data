---
name: tally-balance-sheet-sync
description: Fetch a company's Balance Sheet (group-level, full tree) from local Tally Prime and sync it into the Balance Sheet Google Sheet. Use whenever the user asks to fetch/pull/export/sync a Tally Balance Sheet, get the balance sheet for a company, or refresh the balance-sheet sheet. Asks the user which single company to run for, takes an optional as-of date (default today), pulls every group with its closing balance, classifies each into Assets/Liabilities via a top-level-ancestor walk, keeps only Balance-Sheet groups, sign-flips balances (positive = Dr), and upserts by (Company, Location, group GUID).
---

# tally-balance-sheet-sync

Pulls a single company's **Balance Sheet** from local Tally Prime via XML
over HTTP — one row per Tally group (full tree, not just top-level), each
classified into `Assets` / `Liabilities` and carrying its closing balance —
then **upserts** the rows into a Google Sheet keyed by `(Company, Location,
group_id)`.

Unlike the master-sync skills, this skill runs for **one company that the
user names** (not every loaded company), because a balance sheet is a
per-company statement.

This skill is **self-contained**: tools and workflow live inside this
folder. It expects to be run from the `FETCH DAILY DATA` project so it can
pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_balance_sheet_to_sheet.md`](workflows/sync_tally_balance_sheet_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys:
   `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`,
   `BALANCE_SHEET_SHEET_URL`, `BALANCE_SHEET_SHEET_TAB`. If any are missing,
   prompt the user before running anything.

2. **List loaded companies and ASK which one.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies and **ask which single company** to
   produce the balance sheet for. If none are loaded, tell the user to load
   the company in Tally and stop.

3. **Ask the as-of date (optional).** Default is today. The balance sheet is
   computed as of this date (DD-MM-YYYY).

4. **Fetch the balance sheet.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_balance_sheet.py" \
       --company "<exact company name>" --as-of 15-06-2026 \
       --output .tmp/balance_sheet_<timestamp>.json
   ```
   The script reads the column schema from
   [`reference/columns.md`](reference/columns.md) and the company mapping
   from [`reference/companies.md`](reference/companies.md). It pulls Tally's
   **Trial Balance** report (group tree, Tally-computed closings), classifies
   each group via `STATEMENT_RULES`, keeps only `Balance Sheet` rows, and adds
   the `Profit & Loss A/c` balancing line so Assets = Liabilities. (Tally's
   Group/Ledger *collections* return unreliable balances on the XML gateway —
   the Trial Balance report is the authoritative source.)

5. **Push to sheet (upsert).** Run:
   ```
   python "<SKILL_DIR>/tools/push_balance_sheet_to_sheet.py" --input .tmp/balance_sheet_<timestamp>.json
   ```
   The script writes the header if the tab is empty, then for each fetched
   row: new key → append; existing key with changed data → update in place
   (preserve `created_at`, bump `updated_at`); existing key, no change →
   leave alone (`unchanged`).

6. **Report.** Read the JSON summary printed by the push script and tell the
   user: rows fetched, appended, updated, unchanged, and the sheet URL.
   Optional sanity check: the sum of `Assets`-side `closing_balance` should
   roughly equal the (negated) sum of `Liabilities`-side — the classic
   balance-sheet balance.

## Schema changes

The output schema lives in [`reference/columns.md`](reference/columns.md).
To add/remove/reorder a column, edit that table — no Python changes needed.
The shared loader [`tools/_schema.py`](tools/_schema.py) parses it.

To onboard a new Tally company or refresh names after the April FY rollover,
edit [`reference/companies.md`](reference/companies.md).

To adjust how groups are classified into `statement` / `side`, edit
`STATEMENT_RULES` near the top of
[`tools/fetch_tally_balance_sheet.py`](tools/fetch_tally_balance_sheet.py).

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not
  exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity →
  Client/Server configuration → TallyPrime acting as: Both`.
- **`company '...' is not loaded in Tally`** — the `--company` name must
  match a loaded company exactly. Re-run `list_tally_companies.py` to copy
  the exact name.
- **Google auth error** — delete `token.json` (path from
  `GOOGLE_TOKEN_FILE`) and re-run; the push script re-opens the OAuth flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1
  that doesn't match `reference/columns.md`. Fix the sheet's header row or
  update the schema file.
- **Assets and Liabilities totals don't match** — usually a custom top-level
  group landed on the wrong side via the ISREVENUE/sign fallback. Add it to
  the correct set in `STATEMENT_RULES`. Also confirm the `Profit & Loss A/c`
  group is present (it carries the period result onto the balance sheet).
- **A group shows on the P&L instead of the Balance Sheet (or vice-versa)**
  — its top-level ancestor isn't in `STATEMENT_RULES` and the ISREVENUE flag
  disagrees with your expectation. Add the top-level group name explicitly.
