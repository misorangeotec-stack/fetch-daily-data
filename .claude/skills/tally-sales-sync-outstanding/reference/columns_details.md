# Output Schema — Sales Outstanding Register (Bill-wise Detail Sheet)

This file is the **single source of truth** for the columns written to the **bill-wise detail** Google Sheet by the `tally-sales-sync-outstanding` skill (one row per `BILLALLOCATIONS.LIST` entry under a sales voucher). Both `tools/fetch_tally_sales.py` and `tools/push_sales_to_sheet.py` parse this file at runtime via `tools/_schema.py`.

This is the **second** sheet the skill writes to. The first (per-voucher summary) is governed by [`columns.md`](columns.md). Together they form a parent/child pair joined on `(Company, Location, Voucher No.)`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate `.tmp/sales_*.json` file (lowercase, snake_case-ish, no special chars). It must be unique.

## Columns

| key            | column         | tally_source                                                              |
|----------------|----------------|---------------------------------------------------------------------------|
| company        | Company        | SVCURRENTCOMPANY (loop variable) — FK to sales sheet                      |
| location       | Location       | Company master state — FK to sales sheet                                  |
| month          | Month          | Derived from voucher DATE (YYYY-MM)                                       |
| date           | Date           | Voucher DATE                                                              |
| particulars    | Particulars    | PARTYLEDGERNAME                                                           |
| voucher_type   | Voucher Type   | VOUCHERTYPENAME                                                           |
| voucher_no     | Voucher No.    | VOUCHERNUMBER — FK to sales sheet                                         |
| bill_ref_name  | Bill Ref Name  | BILLALLOCATIONS.LIST → NAME. Duplicates within one voucher are suffixed " (#2)", " (#3)", … on the 2nd / 3rd / … occurrence so the dedupe key stays unique. |
| bill_type      | Bill Type      | BILLALLOCATIONS.LIST → BILLTYPE (New Ref / Agst Ref / Advance / On Account) |
| bill_amount    | Bill Amount    | BILLALLOCATIONS.LIST → AMOUNT, sign-stripped. For foreign-currency vouchers Tally embeds an FX expression like "$9000.00 @ ₹90.80/$ = -₹817200.00" — we extract the trailing INR amount. |
| credit_period  | Credit Period  | Normalized day count from BILLCREDITPERIOD (e.g. "37 Days"). Blank New Ref = "0 Days". Blank for Agst Ref / Advance / On Account. |
| due_date       | Due Date       | DD/MM/YYYY = voucher date + credit period. Blank New Ref = voucher date itself. Blank for non-New-Ref types. |
| ledger_id      | ledger_id      | Tally ledger GUID of PARTYLEDGERNAME (= Ledger Master `ledger_id`); inherited from the parent voucher; identity FK |

## Dedupe key

The push tool uses the composite **`(company, location, voucher_no, bill_ref_name, date)`** to dedupe rows already present in the sheet.

- `bill_ref_name` keeps allocations within the same voucher distinct. Tally **does not** enforce unique NAME per allocation — duplicates do appear (e.g. two `Agst Ref` rows both carrying the voucher's own number, or two equal-amount advance splits). The fetch script suffixes the 2nd / 3rd / … occurrence with " (#2)", " (#3)" so each row gets a unique key.
- `date` keeps cross-FY voucher-number collisions distinct. Some voucher types reset their counter at FY rollover but keep the FY prefix hard-coded — e.g. `HD/N/25-26/1` exists in both Apr 2025 and Apr 2026 with different parties / amounts, genuinely different vouchers.

If those five `key` values are renamed, update `tools/push_sales_to_sheet.py` as well.

## Foreign key

`(company, location, voucher_no)` is a foreign key to the per-voucher sales sheet (governed by [`columns.md`](columns.md)). Use `=VLOOKUP` / `QUERY` against that triple to pull voucher-level fields (Quantity, Rate, Value, Gross Total, GSTIN) into ageing reports without duplicating them on every detail row.

## Synthetic "On Account" rows

A sales voucher with **no** `BILLALLOCATIONS.LIST` entries (or only empty `<BILLALLOCATIONS.LIST/>` placeholders) emits a single synthetic row with:

- `bill_ref_name` = blank
- `bill_type` = `On Account`
- `bill_amount` = voucher gross total
- `credit_period` = blank
- `due_date` = blank

This guarantees `SUM(Bill Amount)` on the detail sheet equals `SUM(Gross Total)` on the per-voucher sheet, voucher-for-voucher.
