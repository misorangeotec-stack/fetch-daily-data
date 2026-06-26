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

The first table (`## Companies`) is the one parsed. Header row must START with
`tally_name | company | location` (case-insensitive). An OPTIONAL 4th column
`apr25_opening` may follow: value `zero` forces every ledger's **Opening Apr-25**
to 0 for that company. Use it ONLY for FY-rollover *continuation* books (opened
1-Apr-2026), whose Tally OPENINGBALANCE is the carried-forward FY26 opening, not
a true 1-Apr-2025 opening — without it the fetcher mislabels that carry-forward
into the Apr-25 column (Apr-25 == Apr-26), phantom-inflating opening balance.
Leave blank to keep Tally's OPENINGBALANCE. (Verified with the accountant
2026-06-24: the Noida Enterprises continuation book has zero true Apr-25 debtor
openings — all its balances are post-1-Apr-2025 sales.)

## Companies

| tally_name                                                       | company    | location | apr25_opening |
|------------------------------------------------------------------|------------|----------|---------------|
| ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA -FY 26-27 | Enterprise | Noida    | zero          |
| ORANGE O TEC ENTERPRISES PVT LTD - FY24-26               | Enterprise | Surat    | zero          |
| ORANGE O TEC PRIVATE LIMITED (01-04-25TO31-03-27)                | O-tec      | Surat    |               |
| ORANGE O TEC PRIVATE LIMITED-NOIDA-(from 1-Apr-25)               | O-tec      | Noida    |               |
| COLORIX DIGITAL PRINTING SOLUTIONS LLP - (from 1-Apr-20) | Colorix    | Surat    |               |
| ORANGE O TEC ENTERPRISES PVT LTD(F.Y.2024-26)                                | Enterprise | Surat    | zero          |
| ORANGE O TEC ENTERPRISES PVT LTD(F.Y.2026-27)                                | Enterprise | Surat    | zero          |
| ORANGE O TEC ENTERPRISES PRIVATE LIMITED-NOIDA - (from 1-Apr-25) - (from 1-Apr-26) | Enterprise | Noida    | zero          |

<!--
SURAT apr25_opening=zero (2026-06-24): user confirmed Enterprise/Surat openings
are the same phantom class as Noida. Only ONE party carried a nonzero Apr-25
(DR ARUN MAHAJAN FOODS PVT LTD, ₹2,00,000 Cr, Apr-26 already 0) — zeroed in the
sheet + flagged here so a re-sync keeps it 0. CAVEAT: unlike the Noida
continuation book, the FY24-26 Surat book DID exist on 1-Apr-2025, so this
blanket-zeroes ALL future Surat Apr-25 openings. Fine today (only the one phantom
exists); if a GENUINE Surat opening ever appears post-rollover, revisit this flag.
O-tec (Surat/Noida) and Colorix are left as-is — their Apr-25 openings are real.
-->


<!--
DRIFT NOTE (2026-06-24): the three rows just above are ALIASES added after the
last sync (10-Jun-2026) wrote them as raw/blank-location duplicates into the
sheet (648 rows, since deleted; backup in FETCH DAILY DATA/backups/). They are
the FY-rollover-renamed forms of the Enterprise Surat (FY24-26 + FY26-27 split)
and Enterprise Noida books. Tally renames these every April, so the live names
may have drifted AGAIN. NEXT TIME TALLY IS CONNECTED: list the loaded company
names (tools/list_tally_companies.py) and reconcile them against this table —
add/rename rows so every loaded company resolves to a (company, location) and
none falls back to the raw name. Then it is safe to re-run the credit-limit sync.
-->

