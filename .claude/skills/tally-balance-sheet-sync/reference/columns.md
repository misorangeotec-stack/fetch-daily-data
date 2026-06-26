# Output Schema — Tally Balance Sheet Sheet

This file is the **single source of truth** for the columns written to the
destination Google Sheet by the `tally-balance-sheet-sync` skill. Both
`tools/fetch_tally_balance_sheet.py` and `tools/push_balance_sheet_to_sheet.py`
parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of
columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate
`.tmp/balance_sheet_*.json` file (lowercase, snake_case). It must be unique.

## Columns

| key                   | column                | tally_source                                                            |
|-----------------------|-----------------------|-------------------------------------------------------------------------|
| company               | Company               | Mapped from raw Tally name (see companies.md)                           |
| location              | Location              | Mapped from raw Tally name (see companies.md)                           |
| as_of_date            | as_of_date            | The statement date (SVTODATE), DD-MM-YYYY                              |
| group_id              | group_id              | GROUP → GUID (Tally's internal globally-unique ID, stable on rename)    |
| group_name            | group_name            | GROUP NAME                                                              |
| parent_group          | parent_group          | GROUP → PARENT (immediate parent group)                                 |
| primary_group         | primary_group         | Derived: top-level ancestor of the group (skipping Tally's "Primary")  |
| statement             | statement             | Always `Balance Sheet` for this skill (derived; see classification)     |
| side                  | side                  | Derived: Assets / Liabilities                                           |
| closing_balance       | closing_balance       | Group net closing from Tally's Trial Balance as of as_of_date, sign-flipped (positive = Dr, negative = Cr) |
| created_at            | created_at            | Set by push tool on first insert (ISO 8601 UTC). Preserved on update.   |
| updated_at            | updated_at            | Set by push tool whenever a row's data changes (ISO 8601 UTC).          |

## Dedupe / upsert key

The push tool uses the composite **`(company, location, group_id)`** to
upsert rows — latest-snapshot semantics. A re-run refreshes each group's
`as_of_date` / balances in place; rows are never duplicated. Tally's group
GUID is unique within a company, and Company / Location disambiguate the
same legal entity loaded as separate Tally companies. If those three `key`
values are renamed above, update `UPSERT_KEYS` in
`tools/push_balance_sheet_to_sheet.py` as well.

> If you ever want **historical snapshots** instead of latest-state, add
> `as_of_date` to `UPSERT_KEYS` so each date appends rather than overwrites.

## Push-managed columns

`created_at` and `updated_at` are **not** populated by the fetch tool — the
fetch JSON leaves them blank. The push tool fills them at upsert time:

- **Insert** (no existing row with the key): both timestamps = now.
- **Update** (existing row, schema columns differ): preserve `created_at`,
  set `updated_at` = now.
- **Unchanged** (existing row, all other schema columns match): leave both
  timestamps alone.

The "did the row change?" check **excludes** `created_at` and `updated_at`
from the equality comparison — otherwise every push would report every row
as updated.

## Sign convention

Tally exports debit balances as **negative** in XML. This skill mirrors
`tally-ledger-master-sync`:

- **`opening_balance`** is the **absolute value**; the sign is captured
  separately in **`opening_balance_type`** (`Dr` if Tally returned <0, `Cr`
  if >0, blank if zero).
- **`closing_balance`** is **signed and flipped** — positive = Dr,
  negative = Cr, zero = zero. So Assets (debit) show positive and
  Liabilities (credit) show negative.

## Statement classification (statement / side)

The fetch tool walks each group's parent chain to its **top-level ancestor**
(skipping Tally's "Primary" root pseudo-group) and classifies as follows.
First match wins; edit `STATEMENT_RULES` in
`tools/fetch_tally_balance_sheet.py` to adjust.

| top-level ancestor                                                        | statement     | side        |
|---------------------------------------------------------------------------|---------------|-------------|
| Capital Account, Loans (Liability), Current Liabilities, Suspense A/c, Provisions | Balance Sheet | Liabilities |
| Fixed Assets, Investments, Current Assets, Loans & Advances (Asset), Misc. Expenses (ASSET), Branch / Divisions | Balance Sheet | Assets |
| Sales Accounts, Direct Incomes, Indirect Incomes                          | Profit & Loss | Income      |
| Purchase Accounts, Direct Expenses, Indirect Expenses                     | Profit & Loss | Expense     |
| anything else (custom top-level group)                                    | from ISREVENUE flag | from closing-balance sign |

**This skill keeps only `statement == "Balance Sheet"` rows.** Income /
Expense groups are counted in `excluded_other_statement` and dropped (they
belong to the sibling `tally-pnl-sync` skill).

The special `Profit & Loss A/c` group (Tally's running-result group) has
`ISREVENUE = No`, so it falls through to the fallback and lands on the
Balance Sheet with its side derived from sign (profit = Cr = Liabilities) —
matching how Tally presents the period result on the Balance Sheet.
