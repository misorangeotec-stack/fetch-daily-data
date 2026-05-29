# Workflow: sync_tally_ledger_master_to_sheet

## Objective

Sync the full ledger master from local Tally Prime — every ledger across
every loaded company — into the Tally Ledger Master Google Sheet.
Idempotent: re-running upserts each ledger row by `(Company, Location,
ledger_id)`, where `ledger_id` is Tally's GUID. Existing rows have their
schema columns refreshed (and `updated_at` bumped) only if their values
actually changed; new ledgers are appended.

## Inputs

- **Date range** (optional) — `--from DD-MM-YYYY` and `--to DD-MM-YYYY`.
  Drives Tally's `SVFROMDATE` / `SVTODATE`. Tally then computes
  `OPENINGBALANCE` as the FY-start balance of the FY containing `--from`,
  and `CLOSINGBALANCE` as of `--to`. Default (when omitted): FY 25-26
  (1-Apr-2025 → 31-Mar-2026).
- **Companies** (optional, comma-separated) — restrict to a subset of
  currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `LEDGER_MASTER_SHEET_URL` — full URL of the destination Google Sheet
  - `LEDGER_MASTER_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` =
`.claude/skills/tally-ledger-master-sync/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope
   requesting `List of Companies`. Prints a JSON array of company names to
   stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be
     included.

2. **`tools/fetch_tally_ledger_master.py [--from DD-MM-YYYY] [--to DD-MM-YYYY] [--companies "Co1,Co2"] --output <path>`**
   - `--from` / `--to` (DD-MM-YYYY) drive Tally's `SVFROMDATE` /
     `SVTODATE`. `OPENINGBALANCE` is the FY-start balance of the FY
     containing `--from`; `CLOSINGBALANCE` is as of `--to`. Defaults to
     FY 25-26 (1-Apr-2025 → 31-Mar-2026) if both are omitted. Legacy
     `--from-date` / `--to-date` (YYYYMMDD) are still accepted.
   - For each loaded (or filtered) company:
     - Pulls the Group collection (NAME, PARENT) and builds a parent map.
     - Pulls the full Ledger collection with the resolved date variables.
       All ledgers are kept; no group filtering is applied (this is
       "master" data, not Sundry-Debtor-only like the credit-limit skill).
     - Walks each ledger's parent chain to its top-level group and
       classifies into `primary_group` / `ledger_type` per
       `CLASSIFICATION_RULES`:
       Sundry Debtors → Debtors/Customer; Sundry Creditors →
       Creditors/Supplier; Bank Accounts or Bank OD A/c → Bank/Bank;
       Direct/Indirect Expenses or Purchase Accounts → Expense/Expense;
       Direct/Indirect Incomes or Sales Accounts → Income/Income;
       fallback = top-level group name, blank type.
     - Maps the raw Tally company name → display Company / Location via
       `reference/companies.md`.
   - Sign convention:
     - `opening_balance` = abs(OPENINGBALANCE); `opening_balance_type` =
       `Dr` if Tally raw < 0 else `Cr` if > 0 else "".
     - `closing_balance` = sign-flipped (Dr positive, Cr negative).
     - `credit_limit` = sign-flipped (positive).
   - Sets `is_active = "TRUE"` for every fetched ledger; leaves
     `created_at` / `updated_at` blank (the push tool fills them).
   - Writes a JSON list-of-dicts to `<path>` (typically
     `.tmp/ledger_master_<timestamp>.json`).
   - Prints a one-line summary like
     `{"rows": 4321, "per_company": {...}, "no_guid": 0, "no_classification": 12, "output": "..."}`.

3. **`tools/push_ledger_master_to_sheet.py --input <path>`**
   - Loads `LEDGER_MASTER_SHEET_URL`, `LEDGER_MASTER_SHEET_TAB`, and
     Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if
     blank, errors out on mismatch.
   - Reads existing rows; builds a map from `(company, location,
     ledger_id)` to 1-based sheet-row number.
   - For each fetched row:
     - **Match** + non-timestamp columns equal → counted as `unchanged`.
       The two timestamp columns are excluded from the equality check, so a
       no-op re-run reports `unchanged`, not `updated`.
     - **Match** + columns differ → updates the row in place via
       `values.batchUpdate`. Preserves the existing `created_at`; sets
       `updated_at = now`.
     - **No match** → appends at the bottom with both `created_at` and
       `updated_at` set to the run timestamp.
   - Single timestamp value is used for the whole run (UTC ISO 8601, second
     precision). All inserts and updates in a single push share that
     timestamp — easier to filter downstream.
   - Skips rows with any blank component of the upsert key (defensive).
   - Prints summary JSON:
     `{"fetched": N, "appended": N, "updated": N, "unchanged": N, "sheet_url": "..."}`.

## Outputs

- Upserted rows in the destination Google Sheet (URL from
  `LEDGER_MASTER_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/ledger_master_<timestamp>.json` file (disposable;
  safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits
  non-zero with a connection error. Tell the user to start Tally Prime and
  confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → fetch step exits with "no companies are loaded".
  Tell the user to load at least one company in Tally and stop.
- **Unmapped Tally company** → fetch tool prints a stderr warning, uses the
  raw Tally name, and leaves Location blank. The script does not crash.
  Tell the user to add a row to `reference/companies.md`.
- **Ledger with no GUID** → fetch tool counts these in `no_guid`. Push tool
  skips rows missing any part of the upsert key; surface the count and
  investigate. (Has not been observed in practice — modern Tally always
  emits a GUID.)
- **Ledger with no classifiable parent chain** → `primary_group` and
  `ledger_type` left blank. Counted in `no_classification`. Usually means
  the ledger sits directly under a non-standard top-level group; check
  whether the rule list needs extending.
- **Sheet header row mismatch** → push tool exits non-zero. Either fix the
  sheet's row 1 or update `reference/columns.md`.
- **Re-run with overlapping data** → expected and supported. Upsert
  refreshes the schema columns on existing rows; `unchanged` in the summary
  counts rows whose non-timestamp values matched bit-for-bit.
- **Extra columns to the right of the schema** → preserved. The upsert
  range is bounded to the schema width; user-added columns from the column
  after the last schema column onward are untouched.
- **Negative number in `closing_balance`** → that ledger has a credit (Cr)
  closing balance. Expected for liability/income ledgers; unusual for asset
  ledgers like Sundry Debtors.

## Lessons learned

- **Use the Tally GUID as the per-company stable ID.** The GUID survives
  rename and is unique within a company; using `ledger_name` as the PK
  fails the moment a customer name is corrected. The composite
  `(company, location, ledger_id)` survives both renames and
  cross-company collisions (same legal entity loaded twice).

- **Debit / credit sign convention is column-specific.** This skill mixes
  two conventions deliberately:
  - `opening_balance` is split into magnitude + Dr/Cr type because that
    matches how Tally itself displays openings on the ledger screen, and
    the user spec listed `opening_balance_type` explicitly.
  - `closing_balance` is a single signed number (positive = Dr) because
    the user spec didn't include a type column and a single signed cache
    is the most compact representation. Downstream consumers can
    `> 0` filter for receivables.
  Both columns *flip* Tally's raw sign — Tally exports debits as negative,
  the sheet shows debits as positive.

- **Timestamp columns must be excluded from the equality check.** If
  `updated_at` is part of the comparison, every push will mark every row
  as updated (because the new value is "now" and the old value isn't),
  defeating the whole point of upsert idempotency. The push tool excludes
  both `created_at` and `updated_at` indices when comparing.

- **`BILLCREDITPERIOD` is a string with a unit suffix.** Tally returns
  `"45 Days"`, `"60 Days"`, or blank. The schema column is
  `credit_period_days` (an integer), so the fetch tool extracts the
  leading digits via regex. If you want the original string verbatim,
  rename the column and remove the parse.

- **`LEDGERPHONE` and `LEDGERMOBILE` are separate fields.** Some ledgers
  set one but not the other. The fetch tool pulls `LEDGERPHONE` first and
  falls back to `LEDGERMOBILE` if it's blank. If you need both, split the
  column.

- **Aliases are nested inside `LANGUAGENAME.LIST`.** Tally TDL doesn't
  expose `ALIAS` as a flat ledger field. The structure is:
  `<LANGUAGENAME.LIST><NAME.LIST><NAME>Primary</NAME><NAME>Alias</NAME>...</NAME.LIST></LANGUAGENAME.LIST>`.
  The first `<NAME>` is the primary ledger name; subsequent `<NAME>`
  entries are aliases. The fetch tool returns the first non-name alias
  found across all language entries (in practice there's one English entry).

- **`is_active` is a constant.** Tally's ledger collection only returns
  non-deleted ledgers; deleted ones are filtered out before reaching XML.
  Setting `is_active = TRUE` for every fetched row is the honest
  reflection of that — a `FALSE` value would never appear with the current
  fetch design. If you need to track deletions, you'd need a soft-delete
  pass that compares the previous run's keys against the current run.

- **Skip "Primary" in the classification fallback.** Tally's chart of
  accounts has a root pseudo-group named literally `"Primary"` whose
  children are the user-visible top-level groups (Capital Account, Current
  Liabilities, Sundry Debtors, etc.). Walking a ledger's parent chain
  upward terminates at "Primary", but that label is structurally
  meaningless for classification — without skipping it, ~20% of ledgers
  ended up with `primary_group = "Primary"` instead of useful labels like
  "Current Liabilities" or "Fixed Assets". The `classify()` function
  iterates from the top of the chain downward and returns the first
  ancestor that isn't "Primary". The only ledgers that legitimately have
  parent="Primary" directly are Tally's system `Profit & Loss A/c` records
  (one per company) — those genuinely have no classifiable group and
  surface in the `no_classification` warning.

- **Push with `valueInputOption="RAW"`, not `"USER_ENTERED"`.** USER_ENTERED
  silently corrupted two columns on the first dry-run:
  (1) phone strings like `"8882133534,8882677675,9319936266"` were parsed
  as thousand-separated single numbers and reformatted to garbage like
  `888,213,353,488,826,000,000,000,000,000`;
  (2) closing/opening balances above ~1e8 lost precision on round-trip
  (`-126283286.46` → `-126283286.5`) because Sheets' default display
  formatting truncates to ~11 significant digits and that's what
  `values.get` returns. RAW stores everything as literal text, sidesteps
  both bugs, and round-trips byte-for-byte — at the cost of numeric
  columns being text-aligned until the user manually applies Format →
  Number on the column. See the project-level memory on this for context;
  sibling skills should be audited if they ever add phone or
  large-balance columns.
