# Per-ledger Opening Apr-25 overrides

`tools/fetch_tally_credit_limits.py` applies these AFTER the company-level
`apr25_opening` flag (see `companies.md`). An override here **wins** over a
company's blanket `zero`, forcing a specific ledger's **Opening Apr-25** to a
known-correct value.

Use this for ledgers whose true 1-Apr-2025 opening is NOT recoverable from the
synced Tally book. The classic case: a company is on `apr25_opening=zero`
because it's an FY26-27 *continuation* book (see companies.md), but ONE ledger
genuinely had a real opening at 1-Apr-2025 that was **fully settled by
31-Mar-2026** — so the continuation book reads 0 for it, and the blanket zero is
correct for everyone EXCEPT that ledger. The override restores its real opening
(taken from the bill-wise opening export / Tally FY25-26 book).

Match key = `(company, location, ledger)` exactly as written to the sheet
(ledger compared case-insensitively). `apr25_amount` is a positive number;
`apr25_drcr` is `Dr` or `Cr`. Leave the `note` for provenance.

The first table (`## Overrides`) is the one parsed. Header row must START with
`company | location | ledger | apr25_amount | apr25_drcr` (case-insensitive).

## Overrides

| company    | location | ledger                     | apr25_amount | apr25_drcr | note                                                                                          |
|------------|----------|----------------------------|--------------|------------|-----------------------------------------------------------------------------------------------|
| Enterprise | Noida    | KIMORA FASHIONS PVT. LTD.  | 6432700      | Dr         | FY25-26 machine deal MC/EN/24-25/1-1..6 (bills 16-Oct-2024), fully paid by FY26 so continuation book reads 0; real opening confirmed by Tally + bill-wise. Receipts are AGST REF to these bills. |
