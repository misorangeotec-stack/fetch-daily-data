# Output Schema — Outstanding Sales Journal Sheet

This file is the **single source of truth** for the columns written to the destination Google Sheet by the `tally-salesjournal-sync-outstanding` skill. Both `tools/fetch_tally_journals.py` and `tools/push_journals_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/journals_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key              | column                       | tally_source                                                                                                          |
|------------------|------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| company          | Company                      | Display name from `reference/companies.md`                                                                            |
| location         | Location                     | Location from `reference/companies.md`; blank if unmapped                                                             |
| month            | Month                        | Derived from voucher DATE (YYYY-MM)                                                                                   |
| type             | Type                         | Constant `journal`                                                                                                    |
| date             | Date                         | DATE (formatted DD/MM/YYYY)                                                                                           |
| particulars      | Particulars                  | The Sundry-Debtor leg's LEDGERNAME                                                                                    |
| voucher_type     | Voucher Type                 | VOUCHERTYPENAME                                                                                                       |
| voucher_no       | Voucher No.                  | VOUCHERNUMBER, falling back to GUID / REMOTEID (manual journals often have no number)                                 |
| ref_inv_no       | Reference Invoice Number     | BILLALLOCATIONS.LIST → NAME (the original invoice this allocation settles against); blank for `On Account` allocations |
| narration        | Narration                    | Voucher-level NARRATION (free text)                                                                                   |
| transaction_type | Transaction Type             | `Dr` if the debtor leg is debited (receivable up), `Cr` if credited (receivable down)                                 |
| amount           | Amount                       | abs() of BILLALLOCATIONS.LIST → AMOUNT for that allocation; falls back to abs(leg AMOUNT) for `On Account` rows       |
| ledger_id        | ledger_id                    | Tally ledger GUID of the kept leg's LEDGERNAME (= Ledger Master `ledger_id`); identity FK |

## Row granularity

This skill emits **one row per `BILLALLOCATIONS.LIST` entry** under each Sundry-Debtor leg. A journal that posts ₹10,00,000 against six original invoices for the same debtor produces six rows, each carrying that allocation's `Reference Invoice Number` and partial `Amount`. If a debtor leg has no bill allocations (an `On Account` posting), one row is emitted with blank `ref_inv_no` and the full leg amount.

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no, particulars, ref_inv_no)`** to dedupe rows already present in the sheet. `ref_inv_no` is part of the key because a single voucher can produce multiple rows (one per allocation), and `particulars` is included for the rare case of multiple debtor legs in one journal.

If those `key` values are renamed, update `DEDUPE_KEYS` in `tools/push_journals_to_sheet.py` as well.
