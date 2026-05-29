# Output Schema — Outstanding Sales Sheet

This file is the **single source of truth** for the columns written to the destination Google Sheet by the `tally-sales-sync-outstanding` skill. Both `tools/fetch_tally_sales.py` and `tools/push_sales_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/sales_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key           | column        | tally_source                                           |
|---------------|---------------|--------------------------------------------------------|
| company       | Company       | SVCURRENTCOMPANY (loop variable)                       |
| location      | Location      | Company master state; blank if not set                 |
| month         | Month         | Derived from voucher DATE (YYYY-MM)                    |
| type          | Type          | High-level category from VOUCHERTYPENAME (before "-")  |
| date          | Date          | DATE                                                   |
| particulars   | Particulars   | PARTYLEDGERNAME                                        |
| voucher_type  | Voucher Type  | VOUCHERTYPENAME                                        |
| voucher_no    | Voucher No.   | VOUCHERNUMBER                                          |
| gstin         | GSTIN/UIN     | PARTYGSTIN (voucher or party ledger)                   |
| quantity      | Quantity      | ALLINVENTORYENTRIES.LIST → ACTUALQTY (numeric part)    |
| rate          | Rate          | ALLINVENTORYENTRIES.LIST → RATE (numeric part)         |
| unit          | Unit          | ALLINVENTORYENTRIES.LIST → ACTUALQTY (unit token) or stock-item BASEUNITS |
| value         | Value         | ALLINVENTORYENTRIES.LIST → AMOUNT (line-level)         |
| gross_total   | Gross Total   | Voucher-level AMOUNT (repeats across lines)            |
| ledger_id     | ledger_id     | Tally ledger GUID of PARTYLEDGERNAME (= Ledger Master `ledger_id`); identity FK |

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no, date)`** to dedupe rows already present in the sheet. ``date`` is in the key because some Tally voucher types reset their auto-numbering counter at FY rollover but keep the FY prefix hard-coded — e.g. `HD/N/25-26/1` exists in both Apr 2025 and Apr 2026 with different parties/amounts, genuinely different vouchers reusing the same number. If those four `key` values are renamed, update `tools/push_sales_to_sheet.py` as well.
