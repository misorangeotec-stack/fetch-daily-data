---
name: tally-ledger-master-sync
description: Sync the full ledger master (every ledger across every loaded company — customers, suppliers, banks, expense, income, etc.) from local Tally Prime into the Tally Ledger Master Google Sheet. Use whenever the user asks to fetch/pull/sync/export the Tally ledger master, refresh the ledger master sheet, build a unified chart-of-accounts / customer-supplier-bank master, or run the daily/periodic ledger-master sync. Handles multiple loaded companies, classifies each ledger into Debtors/Creditors/Bank/Expense/Income via parent-chain walk, splits opening balance into amount + Dr/Cr type, caches a sign-flipped closing balance, manages created_at / updated_at timestamps automatically, and upserts by (Company, Location, ledger_id = Tally GUID).
---

# tally-ledger-master-sync

Pulls the full ledger master from local Tally Prime via XML over HTTP, then
**upserts** rows in a single Google Sheet that holds one row per ledger per
company / location. Every ledger across every loaded company is included
(not just Sundry Debtors); each is classified into a `primary_group` /
`ledger_type` based on its top-level group ancestor.

This skill is **self-contained**: tools and workflow live inside this
folder. It expects to be run from the `FETCH DAILY DATA` project so it can
pick up the project's `.env` (Tally host, Google credentials, sheet URL).

## Steps

Follow the SOP in [`workflows/sync_tally_ledger_master_to_sheet.md`](workflows/sync_tally_ledger_master_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys:
   `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`,
   `LEDGER_MASTER_SHEET_URL`, `LEDGER_MASTER_SHEET_TAB`. If any are missing,
   prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load
   companies in Tally and stop.

3. **Fetch the ledger master.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_ledger_master.py" \
       --from 28-04-2026 --to 30-04-2026 \
       --output .tmp/ledger_master_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script
   reads the column schema from this skill's
   [`reference/columns.md`](reference/columns.md) and the company mapping
   from [`reference/companies.md`](reference/companies.md), so you do not
   need to know any of that schema.

   **Date range.** `--from` and `--to` (DD-MM-YYYY) drive Tally's
   `SVFROMDATE` / `SVTODATE` static variables, which determine how Tally
   computes the balances for this run:
   - `OPENINGBALANCE` = balance at the **FY-start** of the FY containing
     `--from` (e.g. `--from 28-04-2026` → opening as of 1-Apr-2026, the
     start of FY 26-27).
   - `CLOSINGBALANCE` = balance **as of** `--to` (e.g. `--to 30-04-2026`
     → closing as of end of day 30-Apr-2026).

   If `--from` / `--to` are omitted, the script falls back to FY 25-26
   (1-Apr-2025 → 31-Mar-2026). When FY 26-27 books exist as a separate
   company in Tally, run the skill against that company too — the upsert
   key includes Company so they won't collide.

4. **Push to sheet (upsert).** Run:
   ```
   python "<SKILL_DIR>/tools/push_ledger_master_to_sheet.py" --input .tmp/ledger_master_<timestamp>.json
   ```
   The script:
   - Validates row 1 against `reference/columns.md` (writes the header if
     the tab is empty).
   - Reads existing rows keyed by `(Company, Location, ledger_id)`.
   - For each fetched row: if the key exists and only timestamps would
     differ, **leaves it alone** (counted as `unchanged`). If the key
     exists and other columns differ, **updates that row in place**,
     preserves `created_at`, and bumps `updated_at`. If the key is new,
     **appends** with both timestamps set to the run time.

5. **Report.** Read the JSON summary printed by the push script and tell
   the user: rows fetched, rows appended, rows updated, rows unchanged, and
   the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in
[`reference/columns.md`](reference/columns.md). To add/remove/reorder a
column, edit that table — no Python changes needed. The shared loader
[`tools/_schema.py`](tools/_schema.py) parses it.

To onboard a new Tally company or refresh names after the April FY
rollover, edit [`reference/companies.md`](reference/companies.md).

To adjust how ledgers are classified into `primary_group` / `ledger_type`,
edit `CLASSIFICATION_RULES` near the top of
[`tools/fetch_tally_ledger_master.py`](tools/fetch_tally_ledger_master.py).

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
- **Every row reported as `updated` on a no-op re-run** — means the
  timestamp columns leaked into the equality check. Verify
  `TIMESTAMP_KEYS` in `push_ledger_master_to_sheet.py` matches the
  `created_at` / `updated_at` keys in `reference/columns.md`.
- **`primary_group` blank for many rows** — the ledger's top-level group
  isn't in `CLASSIFICATION_RULES`. Either it's a non-standard chart of
  accounts (extend the rules), or you've encountered the fallback
  intentionally (e.g. `Capital Account`, `Duties & Taxes` — these get the
  raw top-level name as primary_group and a blank ledger_type).
- **`ledger_type = "Bank"` missing some bank ledgers** — they're parked
  under a custom group, not under `Bank Accounts` / `Bank OD A/c`. Add the
  group's top-level name to the Bank rule's set in
  `CLASSIFICATION_RULES`.
- **Wrong company name in sheet** — the raw Tally name is missing from
  `reference/companies.md`. Add it; FY rollovers in April change the
  `(from 1-Apr-XX)` suffix.
