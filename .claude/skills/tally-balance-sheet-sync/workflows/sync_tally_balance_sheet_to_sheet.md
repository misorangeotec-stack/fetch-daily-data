# Workflow: sync_tally_balance_sheet_to_sheet

## Objective

Fetch a single, user-named company's **Balance Sheet** (group-level, full
tree) from local Tally Prime and sync it into the Balance Sheet Google Sheet.
Idempotent: re-running upserts each group row by `(Company, Location,
group_id)` where `group_id` is Tally's GUID. Existing rows have their schema
columns refreshed (and `updated_at` bumped) only if their values actually
changed; new groups are appended. A re-run is a **latest-snapshot** refresh.

## Inputs

- **Company** (required) — the exact name of one loaded Tally company. The
  agent must **ask the user** which company after listing the loaded ones.
- **As-of date** (optional) — `--as-of DD-MM-YYYY`. The statement is
  computed as of this date (SVTODATE). SVFROMDATE is set to the 1-Apr
  FY-start of the FY containing it. Default: today.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `BALANCE_SHEET_SHEET_URL` — full URL of the destination Google Sheet
  - `BALANCE_SHEET_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` =
`.claude/skills/tally-balance-sheet-sync/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope
   requesting `List of Companies`. Prints a JSON array of company names.
   - CLI: no args.
   - **Show the user the list and ask which single company** to run for.

2. **`tools/fetch_tally_balance_sheet.py --company "<name>" [--as-of DD-MM-YYYY] --output <path>`**
   - `--company` (required) must match a loaded company exactly; the script
     errors with the loaded list if not.
   - `--as-of` (DD-MM-YYYY, default today) drives `SVTODATE`; `SVFROMDATE` is
     the 1-Apr FY-start of the FY containing it.
   - Pulls the group structure (NAME, GUID, PARENT, ISREVENUE) plus Tally's
     **Trial Balance** report (`EXPLODEFLAG=Yes` → top-level groups + their
     sub-groups, with Tally-computed net closings). The Group/Ledger
     *collections* are NOT used for balances — they return unreliable figures
     on the XML gateway; the Trial Balance is computed by Tally and balances
     exactly.
   - Walks each group's chain to its top-level ancestor (skipping "Primary")
     and classifies into `statement` / `side` via `STATEMENT_RULES`. Custom
     top-level groups fall back to the ISREVENUE flag (Yes → P&L) +
     closing-balance sign for the side.
   - **Keeps only `statement == "Balance Sheet"` rows**; counts the rest in
     `excluded_other_statement`. Adds a synthesized `Profit & Loss A/c` line
     (its accumulated opening + the current-year P&L) so the statement ties
     out (Assets total = Liabilities total).
   - Sign convention: `closing_balance` = sign-flipped (positive = Dr, so
     Assets positive and Liabilities negative).
   - Leaves `created_at` / `updated_at` blank (the push tool fills them).
   - Writes a JSON list-of-dicts to `<path>` (typically
     `.tmp/balance_sheet_<timestamp>.json`).
   - Prints a one-line summary like
     `{"rows": 42, "statement": "Balance Sheet", "company": "Enterprise / Noida", "as_of": "15-06-2026", "excluded_other_statement": 31, "no_guid": 0, "output": "..."}`.

3. **`tools/push_balance_sheet_to_sheet.py --input <path>`**
   - Loads `BALANCE_SHEET_SHEET_URL`, `BALANCE_SHEET_SHEET_TAB`, and Google
     creds from `.env`.
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

- Upserted rows in the destination Google Sheet
  (`BALANCE_SHEET_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/balance_sheet_<timestamp>.json` file (disposable).

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
- **Assets ≠ Liabilities total** → a custom top-level group was mis-sided by
  the fallback. Add it explicitly to `STATEMENT_RULES`.
- **Re-run with a later as-of date** → existing group rows are `updated`
  (balances + `as_of_date` change, `created_at` preserved); nothing
  duplicated.

## Lessons learned

- **Groups already aggregate.** Tally computes each group's
  `CLOSINGBALANCE` from its children, so a balance sheet needs only the Group
  collection — no per-ledger roll-up. This is one fast request (no per-month
  chunking, unlike voucher fetches).
- **Classify on the top-level ancestor, skipping "Primary".** Tally's root
  pseudo-group `Primary` sits above every user-visible top-level group and is
  meaningless for classification; the fetch walks to the topmost non-Primary
  ancestor (same approach as `tally-ledger-master-sync`'s `classify()`).
- **`Profit & Loss A/c` belongs on the Balance Sheet.** It has
  `ISREVENUE = No`, so the fallback keeps it on the BS with its side derived
  from sign — mirroring how Tally carries the period result onto the balance
  sheet.
- **Sign convention matches the ledger-master skill.** Tally exports debit
  balances as negative; `closing_balance` is flipped so Assets (debit) show
  positive and Liabilities (credit) show negative.
- **Push with `valueInputOption="RAW"`.** USER_ENTERED rounds large balances
  on round-trip; RAW round-trips byte-for-byte (numeric columns display
  text-aligned until the user applies Format → Number).
