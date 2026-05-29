# Workflow: sync_tally_credit_limits_to_sheet

## Objective

Sync Sundry Debtor master data (Credit Period, Credit Limit, Opening Apr-25,
Opening Apr-26) from local Tally Prime into the credit-limit Google Sheet.
Idempotent: re-running upserts each ledger row by `(Company, Location, $Name)`
— existing rows have their schema columns refreshed, new ledgers are
appended.

## Inputs

- **Companies** (optional, comma-separated) — restrict to a subset of
  currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `CREDIT_LIMIT_SHEET_URL` — full URL of the destination Google Sheet
  - `CREDIT_LIMIT_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` =
`.claude/skills/tally-credit-limit-and-opening-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope
   requesting `List of Companies`. Prints a JSON array of company names to
   stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be
     included.

2. **`tools/fetch_tally_credit_limits.py [--companies "Co1,Co2"] --output <path>`**
   - For each loaded (or filtered) company:
     - Pulls the Group collection and walks the parent chain to find every
       descendant of `Sundry Debtors` (depth-unbounded).
     - Pulls the Ledger collection with FY 25-26 date variables, then keeps
       only ledgers whose `PARENT` is in the SD-set.
     - Maps the raw Tally company name → display Company / Location via
       `reference/companies.md`.
     - Maps each ledger name → Sales Person via
       `reference/sales_persons.md`.
   - Sign-flips `OPENINGBALANCE`, `CLOSINGBALANCE`, `CREDITLIMIT` so debit
     balances appear as positive numbers in the sheet.
   - Writes a JSON list-of-dicts to `<path>` (typically
     `.tmp/credit_limits_<timestamp>.json`).
   - Prints a one-line summary like
     `{"rows": 1601, "per_company": {...}, "unmapped_sales_persons": 12, "output": "..."}`.

3. **`tools/push_credit_limits_to_sheet.py --input <path>`**
   - Loads `CREDIT_LIMIT_SHEET_URL`, `CREDIT_LIMIT_SHEET_TAB`, and Google
     creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if
     blank, errors out on mismatch.
   - Reads existing rows; builds a map from `(Company, Location, $Name)` to
     1-based sheet-row number.
   - For each fetched row:
     - **Match** → updates that row's schema columns in place via
       `values.batchUpdate` (preserves any extra columns beyond the schema).
     - **No match** → appends at the bottom via `values.append`.
   - Prints summary JSON:
     `{"fetched": N, "appended": N, "updated": N, "unchanged": N, "sheet_url": "..."}`.

## Outputs

- Upserted rows in the destination Google Sheet (URL from
  `CREDIT_LIMIT_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/credit_limits_<timestamp>.json` file (disposable;
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
- **Unmapped ledger name (Sales Person)** → fetch tool batches these into a
  single stderr warning at the end with a 5-name sample and a count. Sales
  Person column for those rows is left blank. Tell the user to add rows to
  `reference/sales_persons.md`.
- **Sheet header row mismatch** → push tool exits non-zero. Either fix the
  sheet's row 1 or update `reference/columns.md`.
- **Negative number in the sheet** → the customer has a credit/advance
  balance with the company (unusual for a Sundry Debtor). Worth flagging,
  not necessarily a bug — the sign-flip works the same for these.
- **Re-run with overlapping data** → expected and supported. Upsert
  refreshes the schema columns on existing rows; `unchanged` in the summary
  counts rows whose values matched bit-for-bit.
- **Extra columns to the right of the schema** → preserved. The upsert
  range is bounded to the schema width (`A:H` for 8 columns); user-added
  columns from `I` onward are untouched.

## Lessons learned

- **Tally XML returns debtor balances as negative.** `OPENINGBALANCE` for a
  Sundry Debtor with a ₹86,376 receivable shows as `-86376.00` in the XML.
  The reference Excel — and the user — wants positive numbers for
  receivables. The fetch tool's `_flip_sign()` handles this for
  `credit_limit`, `opening_apr_25`, and `opening_apr_26`. Don't apply the
  flip to `credit_period` (it's a string like "45 Days").

- **`OPENINGBALANCE` and `CLOSINGBALANCE` need the FY date window.** Tally
  doesn't store a fixed "FY 25-26 closing" — it computes it on demand from
  `SVFROMDATE` + `SVTODATE`. The fetch tool sets `20250401` → `20260331` so
  Apr-25 = OB and Apr-26 = CB. When the user loads a separate FY 26-27
  company in Tally (typically post-rollover), switch to reading
  `OPENINGBALANCE` from that book — closer to source-of-truth than
  computing from the previous year's CB.

- **Sundry Debtors is a tree, not a single group.** Every Tally company we
  saw nests receivable groups several levels deep
  (`Sundry Debtors → Punjab → Amritsar`, `… → SalesMan` for internal staff,
  etc.). Filter by `PARENT in SD_DESCENDANTS`, not `PARENT == 'Sundry
  Debtors'` — the latter misses ~30% of debtor ledgers in practice. The
  walk in `descendants_of()` is depth-unbounded.

- **`SalesMan` group inside Sundry Debtors is a real Tally pattern.** Some
  companies park internal sales staff under Sundry Debtors so commissions
  and reimbursements flow through the receivables side. These ledgers will
  appear in the sheet and have no Sales Person mapping. That's fine — the
  user can add them to `sales_persons.md` with a placeholder if they don't
  want them in the report (or filter them post-hoc in the sheet).

- **`BILLCREDITPERIOD` field is the user-visible "Credit Period" string,
  not a number.** Tally returns it formatted (e.g. `"45 Days"`,
  `"60 Days"`, blank). Pass through verbatim — formatting it as a number
  will lose the unit suffix.

- **Upsert key is the triple `(Company, Location, $Name)`.** Two raw Tally
  companies can share a display name (e.g. Surat + Noida branches both map
  to the same legal entity), so Location is needed to keep their ledger
  rows from colliding. The same customer ledger name can also exist
  legitimately in multiple companies — Company is part of the key for that
  reason.

- **Sales Person mapping was seeded from `references/Credit Limit & Opening.xlsx`.**
  The reference sheet has 1217 distinct names with zero conflicts across
  Company/Location, so a flat name-only mapping is sound. Future-proof: if
  a name ever needs different sales persons in different branches, the
  mapping file can be widened to `(name, company, location, sales_person)`
  — the `_schema.load_sales_persons()` parser would need an update.

- **Don't filter ledgers via TDL `<FILTER>`.** It's possible to add a
  `$$IsLedOfGrp` filter to the Ledger collection, but in our testing it
  silently dropped some ledgers in companies where the group hierarchy
  used non-standard nesting. Doing the filter client-side, against the
  Group collection we already need to fetch, is more robust and only
  marginally slower.
