# Excluded ledgers — never written to the Credit Limit & Opening sheet

`tools/fetch_tally_credit_limits.py` skips any ledger listed here, even if Tally
classifies it under Sundry Debtors. Use this for ledgers that are **not real
external debtors** — GL accruals, suspense/control accounts, etc. — that leak in
because they sit under (or roll up to) the Sundry Debtors group in Tally.

Match key = `(company, location, ledger)` exactly as it would be written to the
sheet (ledger compared case-insensitively). Keep the `reason` for provenance.

Do NOT list inter-company / related-party ledgers here unless explicitly decided
— those are handled separately.

The first table (`## Excluded`) is the one parsed. Header row must START with
`company | location | ledger` (case-insensitive).

## Excluded

| company | location | ledger          | reason                                                           |
|---------|----------|-----------------|------------------------------------------------------------------|
| O-tec   | Surat    | ACCRUED REVENUE | GL accrual, not a Sundry Debtor — leaked into the debtor sheet; removed 2026-06-24. |
