# Output Schema — Tally Bill-wise Outstanding Sheet

Single source of truth for the columns written to the destination Google Sheet
by the `tally-billwise-outstanding-sync` skill. Both
`tools/fetch_tally_billwise.py` and `tools/push_billwise_to_sheet.py` parse this
file at runtime via `tools/_schema.py`.

One row per **open bill** per ledger (Sundry Debtors only), as-on the snapshot
date. This is Tally's actual receivable bill set — the dashboard's
`process_data.py` reads this sheet (`TALLY_BILLWISE_SHEET_URL`/`_TAB`) and, for
any party whose calculated outstanding disagrees with Tally, sources that
party's outstanding + overdue + aging straight from these bills.

The first table (`## Columns`) is the one parsed. Header names = exact text in
row 1 of the sheet. Several headers (`$Name`, `Company`, `Location`, `Due Date`,
`Dr/Cr`, `Closing Balance`) are matched verbatim by `process_data.py`, so do
not rename them without updating the consumer.

## Columns

| key             | column          | tally_source                                                        |
|-----------------|-----------------|---------------------------------------------------------------------|
| company         | Company         | Mapped from raw Tally name (see companies.md)                       |
| location        | Location        | Mapped from raw Tally name (see companies.md)                       |
| ledger_id       | ledger_id       | LEDGER → GUID (Tally's stable internal ID)                          |
| ledger_name     | $Name           | LEDGER NAME (the Sundry-Debtor party)                              |
| bill_ref_name   | Bill Ref Name   | Ledger Outstandings → BILLFIXED/BILLREF                            |
| bill_date       | Bill Date       | Ledger Outstandings → BILLFIXED/BILLDATE (ISO)                     |
| due_date        | Due Date        | Ledger Outstandings → BILLDUE (ISO; may be future-dated)          |
| closing_balance | Closing Balance | Ledger Outstandings → BILLCL as-on the snapshot date, absolute     |
| dr_cr           | Dr/Cr           | Sign of BILLCL: Dr (Tally <0, a receivable) / Cr (>0, advance)     |
| as_of_date      | as_of_date      | Snapshot date the bills were read as-on (ISO)                      |
| created_at      | created_at      | Set by push tool on first insert (ISO 8601 UTC)                    |
| updated_at      | updated_at      | Set by push tool whenever a row changes (ISO 8601 UTC)             |

## Dedupe / replace key

Bill-wise is a **snapshot**: each sync the open-bill set changes (paid bills
disappear, new bills appear). A plain upsert would leave stale paid bills in
the sheet and over-state outstanding. So the push tool uses **replace-by-scope**
semantics: for every `(company, location)` present in the input, it deletes all
existing rows for that scope and writes the fresh snapshot. Within a scope, a
row is identified by **`(company, location, ledger_id, bill_ref_name)`** for the
created_at-preservation check.

## Sign convention

Tally exports debit balances as **negative**. A Sundry-Debtor open bill (a
receivable) is therefore `Dr` (raw <0); an unallocated advance is `Cr` (raw >0).
`closing_balance` is the **absolute** value; the sign lives in `dr_cr`. This
mirrors `process_data.py`, which reads `closing_balance` + `Dr/Cr` and signs Cr
bills negative when summing a party's closing.
