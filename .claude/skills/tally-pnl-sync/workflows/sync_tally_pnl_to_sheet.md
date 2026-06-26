# Workflow: sync_tally_pnl_to_sheet

## Objective

Fetch a single, user-named company's **Profit & Loss** statement
(group-level, full tree) from local Tally Prime and sync it into the P&L
Google Sheet. Idempotent: re-running upserts each group row by `(Company,
Location, group_id)` where `group_id` is Tally's GUID. Existing rows have
their schema columns refreshed (and `updated_at` bumped) only if their values
actually changed; new groups are appended. A re-run is a **latest-snapshot**
refresh.

## Inputs

- **Company** (required) — the exact name of one loaded Tally company. The
  agent must **ask the user** which company after listing the loaded ones.
- **As-of date** (optional) — `--as-of DD-MM-YYYY`. The P&L period runs from
  the FY-start (SVFROMDATE = 1-Apr of the FY containing it) up to this date
  (SVTODATE). Default: today.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `PNL_SHEET_URL` — full URL of the destination Google Sheet
  - `PNL_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` =
`.claude/skills/tally-pnl-sync/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope
   requesting `List of Companies`. Prints a JSON array of company names.
   - CLI: no args.
   - **Show the user the list and ask which single company** to run for.

2. **`tools/fetch_tally_pnl.py --company "<name>" [--as-of DD-MM-YYYY] --output <path>`**
   - `--company` (required) must match a loaded company exactly; the script
     errors with the loaded list if not.
   - `--as-of` (DD-MM-YYYY, default today) drives `SVTODATE`; `SVFROMDATE` is
     the 1-Apr FY-start of the FY containing it, so `closing_balance` is the
     **period** income/expense.
   - Pulls the group structure (NAME, GUID, PARENT, ISREVENUE) plus Tally's
     **Trial Balance** report (`EXPLODEFLAG=Yes` → top-level groups + their
     sub-groups, with Tally-computed net closings). The Group/Ledger
     *collections* are NOT used for balances — they return unreliable figures
     on the XML gateway; the Trial Balance is computed by Tally.
   - Walks each group's chain to its top-level ancestor (skipping "Primary")
     and classifies into `statement` / `side` via `STATEMENT_RULES`. Custom
     top-level groups fall back to the ISREVENUE flag (Yes → P&L) +
     closing-balance sign for the side.
   - **Keeps only `statement == "Profit & Loss"` rows**; counts the rest in
     `excluded_other_statement`.
   - Sign convention: `closing_balance` = sign-flipped (positive = Dr/Expense,
     negative = Cr/Income). Nett Profit = `-(Income total) - (Expense total)`.
   - Leaves `created_at` / `updated_at` blank (the push tool fills them).
   - Writes a JSON list-of-dicts to `<path>` (typically
     `.tmp/pnl_<timestamp>.json`).
   - Prints a one-line summary like
     `{"rows": 31, "statement": "Profit & Loss", "company": "Enterprise / Noida", "as_of": "15-06-2026", "excluded_other_statement": 42, "no_guid": 0, "output": "..."}`.

3. **`tools/push_pnl_to_sheet.py --input <path>`**
   - Loads `PNL_SHEET_URL`, `PNL_SHEET_TAB`, and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if blank,
     errors on mismatch.
   - Reads existing rows; builds a map from `(company, location, group_id)`
     to 1-based sheet-row number.
   - For each fetched row:
     - **Match** + non-timestamp columns equal → `unchanged` (timestamps
       excluded from the equality check).
     - **Match** + columns differ → updates the row in place via
       `values.batchUpdate`. Preserves `created_at`; sets `updated_at = now`.
     - **No match** → appends at the bottom with both timestamps set to the
       run timestamp.
   - Skips rows with any blank component of the upsert key (defensive).
   - Prints summary JSON:
     `{"fetched": N, "appended": N, "updated": N, "unchanged": N, "sheet_url": "..."}`.

## Outputs

- Upserted rows in the destination Google Sheet (`PNL_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/pnl_<timestamp>.json` file (disposable).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits
  non-zero with a connection error. Tell the user to start Tally Prime and
  confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → fetch step exits with "no companies are loaded".
  Tell the user to load the company in Tally and stop.
- **Wrong / misspelled `--company`** → fetch tool exits and prints the loaded
  company list. Re-ask the user with the exact name.
- **Unmapped Tally company** → fetch tool prints a stderr warning, uses the
  raw Tally name, and leaves Location blank. Add a row to
  `reference/companies.md`.
- **Group with no GUID** → counted in `no_guid`; push tool skips rows missing
  any upsert-key part. (Not observed in practice — modern Tally emits GUIDs.)
- **Nett Profit doesn't match Tally** → a custom top-level group was
  mis-classified by the fallback. Add it explicitly to `STATEMENT_RULES`.
- **Re-run with a later as-of date** → existing group rows are `updated`
  (balances + `as_of_date` change, `created_at` preserved); nothing
  duplicated.

## Lessons learned

- **Groups already aggregate.** Tally computes each group's
  `CLOSINGBALANCE` from its children, so a P&L needs only the Group
  collection — no per-ledger roll-up. This is one fast request (no per-month
  chunking, unlike voucher fetches).
- **The P&L figure is the period closing.** Because SVFROMDATE is the
  FY-start, each revenue/expense group's `CLOSINGBALANCE` over the window is
  the period income/expense — exactly what the P&L shows.
- **Classify on the top-level ancestor, skipping "Primary".** Tally's root
  pseudo-group `Primary` is meaningless for classification; the fetch walks
  to the topmost non-Primary ancestor (same approach as
  `tally-ledger-master-sync`'s `classify()`).
- **Sign convention matches the ledger-master skill.** Tally exports debit
  balances as negative; `closing_balance` is flipped so Expense (debit) shows
  positive and Income (credit) shows negative.
- **Push with `valueInputOption="RAW"`.** USER_ENTERED rounds large balances
  on round-trip; RAW round-trips byte-for-byte (numeric columns display
  text-aligned until the user applies Format → Number).
