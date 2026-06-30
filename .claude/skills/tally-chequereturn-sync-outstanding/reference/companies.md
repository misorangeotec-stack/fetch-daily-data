# Company Mapping — Raw Tally Name → Display Name + Location

Both `tools/fetch_tally_chqreturn.py` and `tools/push_chqreturn_to_sheet.py` use this
mapping to (a) write clean company / location values to the sheet and
(b) compute the dedupe key.

To onboard a new Tally company, append a row. To rename a display value,
edit the row in place — no Python changes needed.

If a Tally company isn't listed here, the fetch tool prints a single
warning to stderr and falls back to the raw Tally name with a blank
location. The script does not crash, so new companies still flow through.

**Refresh every April.** Tally embeds the financial year in the company
name (e.g. `(from 1-Apr-25)` becomes `(from 1-Apr-26)` after the FY
rollover). After the rollover, update the `tally_name` column to match.

The first table (`## Companies`) is the one parsed. Header row must be
`tally_name | company | location` (case-insensitive).

## Companies

Display values must match the sibling Tally sync skills (`tally-sales-sync-outstanding`,
`tally-bankreceipt-sync-outstanding`, `tally-salescreditnote-sync-outstanding`,
`tally-credit-limit-and-opening-sync-outstanding`) exactly so that all sheets
fed by Tally share the same `(Company, Location)` keys. Don't drift without
updating the other skills together — joining or VLOOKUP-ing across sheets
depends on this consistency.

| tally_name                                                                         | company    | location | tally_no |
|------------------------------------------------------------------------------------|------------|----------|----------|
| ORANGE O TEC ENTERPRISES PVT LTD(F.Y.2026-27)                                      | Enterprise | Surat    | 100026   |
| ORANGE O TEC ENTERPRISES PVT LTD(F.Y.2024-26)                                      | Enterprise | Surat    | 102426   |
| ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA -FY 26-27                           | Enterprise | Noida    | 100008   |
| ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA - FY25-26                           | Enterprise | Noida    | 100022   |
| ORANGE O TEC PRIVATE LIMITED (01-04-25TO31-03-27)                                  | O-tec      | Surat    | 100011   |
| ORANGE O TEC PRIVATE LIMITED-NOIDA-(from 1-Apr-25)                                 | O-tec      | Noida    | 100000   |
| COLORIX DIGITAL PRINTING SOLUTIONS LLP - (from 1-Apr-20)                           | Colorix    | Surat    | 010021   |
