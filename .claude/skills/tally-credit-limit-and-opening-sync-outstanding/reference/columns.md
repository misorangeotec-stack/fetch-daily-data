# Output Schema — Credit Limit & Opening Sheet

This file is the **single source of truth** for the columns written to the
destination Google Sheet by the `tally-credit-limit-and-opening-sync-outstanding`
skill. Both `tools/fetch_tally_credit_limits.py` and
`tools/push_credit_limits_to_sheet.py` parse this file at runtime via
`tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of
columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate
`.tmp/credit_limits_*.json` file (lowercase, snake_case, no special chars). It
must be unique.

## Columns

| key            | column          | tally_source                                         |
|----------------|-----------------|------------------------------------------------------|
| company        | Company         | Mapped from raw Tally name (see companies.md)        |
| location       | Location        | Mapped from raw Tally name (see companies.md)        |
| name           | $Name           | LEDGER NAME (Sundry Debtor ledgers only)             |
| sales_person   | Sales Person    | Mapped from ledger name (see sales_persons.md)       |
| credit_period  | Credit Period   | LEDGER → BILLCREDITPERIOD (e.g. "45 Days")           |
| credit_limit   | Credit Limit    | LEDGER → CREDITLIMIT (sign flipped to positive)      |
| opening_apr_25      | Opening Apr-25       | LEDGER → OPENINGBALANCE for FY 25-26 (absolute value) |
| opening_apr_25_type | Opening Apr-25 Dr/Cr | "Dr" if OPENINGBALANCE < 0, "Cr" if > 0, "" if zero  |
| opening_apr_26      | Opening Apr-26       | LEDGER → CLOSINGBALANCE for FY 25-26 (absolute value) |
| opening_apr_26_type | Opening Apr-26 Dr/Cr | "Dr" if CLOSINGBALANCE < 0, "Cr" if > 0, "" if zero  |
| ledger_id           | ledger_id            | LEDGER → GUID (Tally's stable globally-unique id); identity key (= Ledger Master `ledger_id`) |

## Dedupe / upsert key

The push tool upserts on the stable Tally GUID **`ledger_id`** (`ID_KEY` in
`tools/push_credit_limits_to_sheet.py`). This survives ledger renames — a name
edit updates the existing row instead of appending a duplicate. The composite
**`(company, location, name)`** (`UPSERT_KEYS`) is the **fallback** key, used
only for the rare row that has no `ledger_id` (legacy/manual entries). Changed
2026-06-24 after 65 duplicate rows accumulated under name-keying (the old key).

## Sign convention

Tally exports debit balances as **negative** in XML (a Sundry Debtor with a
debit closing balance of ₹86,376 appears as `-86376.00`). The fetch tool flips
the sign on `credit_limit`, `opening_apr_25`, and `opening_apr_26` so the
sheet shows positive receivables, matching the original
`references/Credit Limit & Opening.xlsx` convention. A negative number in the
sheet therefore means the party has an advance/credit balance with us
(unusual for a Sundry Debtor — worth investigating).
