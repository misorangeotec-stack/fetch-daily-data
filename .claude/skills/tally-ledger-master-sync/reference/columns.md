# Output Schema — Tally Ledger Master Sheet

This file is the **single source of truth** for the columns written to the
destination Google Sheet by the `tally-ledger-master-sync` skill. Both
`tools/fetch_tally_ledger_master.py` and `tools/push_ledger_master_to_sheet.py`
parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of
columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate
`.tmp/ledger_master_*.json` file (lowercase, snake_case, no special chars).
It must be unique.

## Columns

| key                   | column                | tally_source                                                            |
|-----------------------|-----------------------|-------------------------------------------------------------------------|
| company               | Company               | Mapped from raw Tally name (see companies.md)                           |
| location              | Location              | Mapped from raw Tally name (see companies.md)                           |
| ledger_id             | ledger_id             | LEDGER → GUID (Tally's internal globally-unique ID, stable on rename)   |
| ledger_name           | ledger_name           | LEDGER NAME                                                             |
| alias_name            | alias_name            | LEDGER → second LANGUAGENAME.LIST/NAME.LIST/NAME entry (first is name)  |
| parent_group          | parent_group          | LEDGER → PARENT (immediate parent group)                                |
| primary_group         | primary_group         | Derived: top-level ancestor classified into Debtors/Creditors/Bank/Expense/Income (else top group name) |
| ledger_type           | ledger_type           | Derived from primary_group: Customer / Supplier / Bank / Expense / Income / blank |
| opening_balance       | opening_balance       | LEDGER → OPENINGBALANCE for FY 25-26, absolute value                    |
| opening_balance_type  | opening_balance_type  | Derived from sign of OPENINGBALANCE: Dr (Tally <0) / Cr (Tally >0) / blank if zero |
| closing_balance       | closing_balance       | LEDGER → CLOSINGBALANCE for FY 25-26, sign-flipped (positive = Dr, negative = Cr) |
| credit_limit          | credit_limit          | LEDGER → CREDITLIMIT (sign flipped to positive)                         |
| credit_period_days    | credit_period_days    | LEDGER → BILLCREDITPERIOD parsed to integer days (e.g. "45 Days" → 45)  |
| gstin                 | gstin                 | LEDGER → PARTYGSTIN                                                     |
| pan_number            | pan_number            | LEDGER → INCOMETAXNUMBER                                                |
| state                 | state                 | LEDGER → LEDSTATENAME                                                   |
| country               | country               | LEDGER → COUNTRYOFRESIDENCE                                             |
| pincode               | pincode               | LEDGER → PINCODE                                                        |
| contact_person        | contact_person        | LEDGER → LEDGERCONTACT                                                  |
| phone                 | phone                 | LEDGER → LEDGERPHONE (fallback LEDGERMOBILE if blank)                   |
| email                 | email                 | LEDGER → EMAIL                                                          |
| is_active             | is_active             | Always TRUE (Tally collection only returns non-deleted ledgers)         |
| created_at            | created_at            | Set by push tool on first insert (ISO 8601 UTC). Preserved on update.   |
| updated_at            | updated_at            | Set by push tool whenever a row's data changes (ISO 8601 UTC).          |

## Dedupe / upsert key

The push tool uses the composite **`(company, location, ledger_id)`** to
upsert rows. Tally's ledger GUID is unique within a company, and Company /
Location disambiguate the same legal entity loaded as separate Tally
companies. If those three `key` values are renamed in the table above,
update `UPSERT_KEYS` in `tools/push_ledger_master_to_sheet.py` as well.

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

Tally exports debit balances as **negative** in XML (a Sundry Debtor with a
debit closing balance of ₹86,376 appears as `-86376.00`). Two columns
handle this differently:

- **`opening_balance`** is the **absolute value**; the sign is captured
  separately in **`opening_balance_type`** (`Dr` if Tally returned <0, `Cr`
  if >0, blank if zero). This matches the way Tally reports openings in the
  UI.
- **`closing_balance`** is **signed and flipped** — positive = Dr,
  negative = Cr, zero = zero. This keeps the cache compact (no second type
  column) and lets a downstream user filter `> 0` for receivables.
- **`credit_limit`** is sign-flipped (Tally stores it negative for debtor
  ledgers); displayed as a positive number.

## Classification mapping (primary_group / ledger_type)

The fetch tool walks each ledger's parent chain to its top-level ancestor
and classifies as follows:

| ancestor in parent chain                                | primary_group | ledger_type |
|---------------------------------------------------------|---------------|-------------|
| Sundry Debtors                                          | Debtors       | Customer    |
| Sundry Creditors                                        | Creditors     | Supplier    |
| Bank Accounts OR Bank OD A/c                            | Bank          | Bank        |
| Direct Expenses OR Indirect Expenses OR Purchase Accounts | Expense     | Expense     |
| Direct Incomes OR Indirect Incomes OR Sales Accounts    | Income        | Income      |
| anything else                                           | top-level group name | blank |

Tested precedence: Debtors → Creditors → Bank → Expense → Income →
fallback. Edit `CLASSIFICATION_RULES` in `tools/fetch_tally_ledger_master.py`
to adjust.
