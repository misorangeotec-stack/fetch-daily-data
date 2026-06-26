# Output Schema — Outstanding Sales Debit Note Sheet

This file is the **single source of truth** for the columns written to the destination Google Sheet by the `tally-salesdebitnote-sync-outstanding` skill. Both `tools/fetch_tally_debit_notes.py` and `tools/push_debit_notes_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/debit_notes_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key             | column                       | tally_source                                                  |
|-----------------|------------------------------|---------------------------------------------------------------|
| company         | Company                      | Display name from `reference/companies.md`                    |
| location        | Location                     | Location from `reference/companies.md`; blank if unmapped     |
| month           | Month                        | Derived from voucher DATE (YYYY-MM)                           |
| type            | Type                         | Constant `debit note`                                         |
| date            | Date                         | DATE (formatted DD/MM/YYYY)                                   |
| particulars     | Particulars                  | PARTYLEDGERNAME                                               |
| voucher_type    | Voucher Type                 | VOUCHERTYPENAME                                               |
| voucher_no      | Voucher No.                  | VOUCHERNUMBER                                                 |
| against_invoice | Against Sales Invoice no.    | Voucher-level REFERENCE field (original sales invoice if any) |
| narration       | Narration                    | Voucher-level NARRATION (free text)                           |
| quantity        | Quantity                     | ALLINVENTORYENTRIES.LIST → ACTUALQTY (numeric, summed)        |
| rate            | Rate                         | Weighted avg = sum(value)/sum(quantity)                       |
| value           | Value                        | ALLINVENTORYENTRIES.LIST → AMOUNT (line-level, summed)        |
| gross_total     | Gross Total                  | abs(party LEDGERENTRIES amount); voucher-level total          |
| ledger_id       | ledger_id                    | Tally ledger GUID of PARTYLEDGERNAME (= Ledger Master `ledger_id`); identity FK |

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no)`** to dedupe rows already present in the sheet. If those `key` values are renamed, update `tools/push_debit_notes_to_sheet.py` as well. Location is part of the key because two raw Tally companies can share a display name (Surat + Noida branches), so voucher numbers can otherwise collide.
