# Company Mapping — Raw Tally Name → Display Name + Location

Both `tools/fetch_tally_credit_limits.py` and
`tools/push_credit_limits_to_sheet.py` use this mapping to (a) write clean
company / location values to the sheet and (b) compute the upsert key.

To onboard a new Tally company, append a row. To rename a display value,
edit the row in place — no Python changes needed.

If a Tally company isn't listed here, the fetch tool prints a single
warning to stderr and falls back to the raw Tally name with a blank
location. The script does not crash, so new companies still flow through.

**Refresh every April.** Tally embeds the financial year in the company
name (e.g. `(from 1-Apr-25)` becomes `(from 1-Apr-26)` after the FY
rollover). After the rollover, update the `tally_name` column to match.

The display values here use the **short** form (`Enterprise`, `O-tec`)
that appears in `references/Credit Limit & Opening.xlsx` — note this
differs from the sibling `tally-sales-sync-outstanding` skill which uses
the longer legal-name form. The two skills target different sheets.

The first table (`## Companies`) is the one parsed. Header row must be
`tally_name | company | location` (case-insensitive).

## Companies

| tally_name                                                       | company    | location |
|------------------------------------------------------------------|------------|----------|
| ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA - (from 1-Apr-25) | Enterprise | Noida    |
| ORANGE O TEC ENTERPRISES PVT LTD - (from 1-Apr-24)               | Enterprise | Surat    |
| ORANGE O TEC PRIVATE LIMITED (01-04-25TO31-03-27)                | O-tec      | Surat    |
| ORANGE O TEC PRIVATE LIMITED-NOIDA-(from 1-Apr-25)               | O-tec      | Noida    |
