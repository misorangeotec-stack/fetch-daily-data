# Output Schema — Chq Return Entries Sheet

This file is the **single source of truth** for the columns written to the destination Google Sheet by the `tally-chequereturn-sync-outstanding` skill. Both `tools/fetch_tally_chqreturn.py` and `tools/push_chqreturn_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/chqreturn_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key            | column                    | tally_source                                                                                                                                                |
|----------------|---------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| company        | Company                   | from `reference/companies.md` mapping (raw Tally name → display)                                                                                            |
| location       | Location                  | from `reference/companies.md` mapping                                                                                                                       |
| month          | Month                     | Derived from voucher DATE (YYYY-MM)                                                                                                                         |
| type           | Type                      | Constant `Bank Payment` (matches reference Excel)                                                                                                           |
| date           | Date                      | DATE (DD/MM/YYYY)                                                                                                                                           |
| particulars    | Particulars               | PARTYLEDGERNAME (or PARTYNAME fallback) — the Sundry Debtor whose cheque returned                                                                           |
| voucher_type   | Vch Type                  | VOUCHERTYPENAME (typically `BANK PAYMENT`, uppercase per Tally)                                                                                             |
| voucher_no     | Vch No.                   | VOUCHERNUMBER, falling back to GUID / REMOTEID. Used for dedupe; reference Excel left this blank but we populate it.                                        |
| ref_inv_no     | Reference Invoice Number  | BILLALLOCATIONS.LIST → NAME (the original sales voucher whose cheque bounced); blank if the voucher has no bill allocations (`On Account`)                  |
| debit          | Debit                     | BILLALLOCATIONS.LIST → AMOUNT (positive). For a cheque return the party (debtor) is debited — same side as the original sale.                               |
| credit         | Credit                    | Always blank — bank payments against debtors only post a debit on the party row. Reference Excel mirrors this.                                              |
| ledger_id      | ledger_id                 | Tally ledger GUID of PARTYLEDGERNAME (= Ledger Master `ledger_id`); identity FK                                                                             |

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no, ref_inv_no)`** to dedupe rows already present in the sheet. A single Bank Payment voucher in Tally can split into multiple bill allocations (one row per allocation, e.g. one bounced cheque covering two original invoices), so `voucher_no` alone is not unique — the bill ref completes the key.

If those `key` values are renamed, update `DEDUPE_KEYS` in `tools/push_chqreturn_to_sheet.py` as well.
