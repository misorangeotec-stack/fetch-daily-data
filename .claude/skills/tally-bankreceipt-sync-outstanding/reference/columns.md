# Output Schema — Outstanding Bank Receipt Sheet

This file is the **single source of truth** for the columns written to the destination Google Sheet by the `tally-bankreceipt-sync-outstanding` skill. Both `tools/fetch_tally_bankreceipt.py` and `tools/push_bankreceipt_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/bankreceipt_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key             | column          | tally_source                                                                 |
|-----------------|-----------------|------------------------------------------------------------------------------|
| company         | Company         | from `reference/companies.md` mapping (raw Tally name → display)             |
| location        | Location        | from `reference/companies.md` mapping                                        |
| month           | Month           | Derived from voucher DATE (YYYY-MM)                                          |
| voucher_type    | Voucher Type    | VOUCHERTYPENAME                                                              |
| voucher_no      | Voucher No      | VOUCHERNUMBER, falling back to GUID / REMOTEID. Bank Receipt vouchers in this Tally setup are stored without sequence numbers, so most rows show a GUID. Used for dedupe; not in original reference Excel. |
| receipt_date    | Receipt Date    | DATE (DD/MM/YYYY)                                                            |
| customer_name   | Customer Name   | PARTYLEDGERNAME (or PARTYNAME fallback)                                      |
| ref_inv_no      | Ref Inv No      | BILLALLOCATIONS.LIST → NAME (the original sales voucher being settled); blank for "On Account" |
| receipt_amt     | Receipt Amt     | BILLALLOCATIONS.LIST → AMOUNT (positive); for "On Account" rows the voucher-level party amount |
| trans_type      | Trans Type      | Derived from the party LEDGERENTRY's ISDEEMEDPOSITIVE flag: `Yes` → "Debit", `No` → "Credit". Tally convention: a "deemed positive" entry is one that increases on the debit side. |
| allocation_type | Allocation Type | BILLALLOCATIONS.LIST → BILLTYPE ("Agst Ref" / "Advance" / "On Account" / "New Ref") |
| ledger_id       | ledger_id       | Tally GUID of the party ledger, resolved from the per-company name→GUID map at fetch time (`fetch_ledger_guid_map`). Same column name + value as `ledger_id` in the Ledger Master sheet (the canonical dimension) so the sheets join cleanly. Stable identity that survives ledger renames — this is the migration key; `Customer Name` stays as the display-only column. Blank = party name did not resolve to a master GUID (D5 data-quality flag). See `scripts/LEDGER_ID_MIGRATION.md`. |

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no, ref_inv_no, allocation_type)`** to dedupe rows already present in the sheet. A single bank-receipt voucher in Tally can split into multiple bill allocations (one row per allocation), so `voucher_no` alone is not unique — the bill ref + allocation type complete the key.

If those `key` values are renamed, update `DEDUPE_KEYS` in `tools/push_bankreceipt_to_sheet.py` as well.
