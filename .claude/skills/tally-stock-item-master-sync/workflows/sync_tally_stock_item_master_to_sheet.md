# Workflow: sync_tally_stock_item_master_to_sheet

## Objective

Sync the full stock-item master from local Tally Prime — every stock item
across every loaded company — into the Tally Stock Item Master Google
Sheet. Idempotent: re-running upserts each item row by `(Company,
Location, item_id)`, where `item_id` is Tally's GUID. Existing rows have
their schema columns refreshed (and `updated_at` bumped) only if their
values actually changed; new items are appended.

## Inputs

- **Date range** (optional) — `--from DD-MM-YYYY` and `--to DD-MM-YYYY`.
  Drives Tally's `SVFROMDATE` / `SVTODATE`. `OPENINGBALANCE` /
  `OPENINGVALUE` are computed at the FY-start of the FY containing
  `--from`. Default (when omitted): FY 25-26 (1-Apr-2025 → 31-Mar-2026).
- **Companies** (optional, comma-separated) — restrict to a subset of
  currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `STOCK_ITEM_MASTER_SHEET_URL` — full URL of the destination Google Sheet
  - `STOCK_ITEM_MASTER_SHEET_TAB` — tab name within the sheet (default: `Sheet1`)

Master fields (item name, GUID, unit, GST rate, reorder level, etc.) are
date-independent. Only `opening_qty` and `opening_value` depend on the
date range — they reflect the FY-start of the FY containing `--from`.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` =
`.claude/skills/tally-stock-item-master-sync/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope
   requesting `List of Companies`. Prints a JSON array of company names to
   stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be
     included.

2. **`tools/fetch_tally_stock_item_master.py [--from DD-MM-YYYY] [--to DD-MM-YYYY] [--companies "Co1,Co2"] --output <path>`**
   - `--from` / `--to` (DD-MM-YYYY) drive Tally's `SVFROMDATE` /
     `SVTODATE`. `OPENINGBALANCE` / `OPENINGVALUE` are computed at the
     FY-start of the FY containing `--from`. Defaults to FY 25-26
     (1-Apr-2025 → 31-Mar-2026) if both are omitted.
   - For each loaded (or filtered) company:
     - Pulls the Unit master (NAME, BASEUNITS, ADDITIONALUNITS,
       CONVERSION, ISSIMPLEUNIT) and indexes by name. Used to resolve
       `conversion_factor` for items with a compound base unit.
     - Pulls the full StockItem collection (NAME, GUID, PARENT, CATEGORY,
       BASEUNITS, ADDITIONALUNITS, PARTNO, OPENINGBALANCE, OPENINGVALUE,
       STANDARDCOST, STANDARDPRICE, HSNCODE, GSTAPPLICABLE, REORDERLEVEL,
       MINIMUMORDERQTY, REORDERQTY, GSTDETAILS.LIST, LANGUAGENAME.LIST).
     - Maps the raw Tally company name → display Company / Location via
       `reference/companies.md`.
   - Sign convention:
     - `opening_value` is sign-flipped (Tally exports asset values as
       negative, the sheet shows positive).
     - `opening_qty`, `standard_cost`, `standard_selling_price`,
       `reorder_level`, `reorder_quantity` are written as plain leading
       numerics (unit suffix stripped).
   - HSN / GST extraction walks `GSTDETAILS.LIST → STATEWISEDETAILS.LIST →
     RATEDETAILS.LIST` and returns the first IGST rate. HSN falls back to
     a top-level `HSNCODE` element if the GST sub-collection is empty.
   - Sets `is_active = "TRUE"` for every fetched item; leaves `created_at`
     / `updated_at` blank (the push tool fills them).
   - Writes a JSON list-of-dicts to `<path>` (typically
     `.tmp/stock_item_master_<timestamp>.json`).
   - Prints a one-line summary like
     `{"rows": 1234, "per_company": {...}, "no_guid": 0, "output": "..."}`.

3. **`tools/push_stock_item_master_to_sheet.py --input <path>`**
   - Loads `STOCK_ITEM_MASTER_SHEET_URL`, `STOCK_ITEM_MASTER_SHEET_TAB`,
     and Google creds from `.env`.
   - Validates the sheet's row 1 against the schema; writes headers if
     blank, errors out on mismatch.
   - Reads existing rows; builds a map from `(company, location, item_id)`
     to 1-based sheet-row number.
   - For each fetched row:
     - **Match** + non-timestamp columns equal → counted as `unchanged`.
       The two timestamp columns are excluded from the equality check, so a
       no-op re-run reports `unchanged`, not `updated`.
     - **Match** + columns differ → updates the row in place via
       `values.batchUpdate`. Preserves the existing `created_at`; sets
       `updated_at = now`.
     - **No match** → appends at the bottom with both `created_at` and
       `updated_at` set to the run timestamp.
   - Single timestamp value is used for the whole run (UTC ISO 8601, second
     precision).
   - Skips rows with any blank component of the upsert key (defensive).
   - Prints summary JSON:
     `{"fetched": N, "appended": N, "updated": N, "unchanged": N, "sheet_url": "..."}`.

## Outputs

- Upserted rows in the destination Google Sheet (URL from
  `STOCK_ITEM_MASTER_SHEET_URL`).
- A summary printed to the terminal.
- An intermediate `.tmp/stock_item_master_<timestamp>.json` file
  (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits
  non-zero with a connection error. Tell the user to start Tally Prime and
  confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → fetch step exits with "no companies are
  loaded". Tell the user to load at least one company in Tally and stop.
- **Unmapped Tally company** → fetch tool prints a stderr warning, uses
  the raw Tally name, and leaves Location blank. The script does not
  crash. Tell the user to add a row to `reference/companies.md`.
- **Item with no GUID** → fetch tool counts these in `no_guid`. Push tool
  skips rows missing any part of the upsert key; surface the count and
  investigate. (Has not been observed in practice — modern Tally always
  emits a GUID.)
- **Sheet header row mismatch** → push tool exits non-zero. Either fix
  the sheet's row 1 or update `reference/columns.md`.
- **Re-run with no changes** → expected and supported. All rows report as
  `unchanged`; timestamps are preserved bit-for-bit.
- **Extra columns to the right of the schema** → preserved. The upsert
  range is bounded to the schema width; user-added columns from the
  column after the last schema column onward are untouched.
- **Compound unit with no conversion** — `conversion_factor` will be
  blank. This usually means the unit is configured as simple in Tally even
  though the item has an `alternate_unit` value (Tally allows this). Edit
  the unit master in Tally, or accept the blank value.
- **Item uses CGST/SGST instead of IGST** — `gst_rate` will be blank
  because the fetch tool only matches `GSTRATEDUTYHEAD = IGST`. If your
  chart relies on intrastate-only items, extend `_extract_gst_rate()` to
  fall back to CGST*2.

## Lessons learned

- **Use the Tally GUID as the per-company stable ID.** The GUID survives
  rename and is unique within a company; using `item_name` as the PK
  fails the moment an item is corrected for spelling or repackaging.
  The composite `(company, location, item_id)` survives both renames and
  cross-company collisions (same SKU loaded under two companies).

- **Tally's stock-asset sign convention mirrors ledger debits.** A stock
  item with 100 units in stock at ₹500/unit shows OPENINGVALUE as
  `-50000` in Tally's XML. We flip the sign so the sheet displays
  `50000.00` — consistent with the ledger-master skill's `closing_balance`.

- **Quantity strings have unit suffixes.** OPENINGBALANCE comes back as
  `"10 PCS"` (or `"10 PCS = 120 NOS"` for compound units). The fetch tool
  extracts the leading numeric (the base-unit quantity) — drop the unit
  suffix before writing or downstream math will break.

- **GST/HSN data lives in a nested subcollection.** `<GSTDETAILS.LIST>`
  is per-applicable-from-date and contains `<HSNCODE>` plus one or more
  `<STATEWISEDETAILS.LIST>` blocks; each STATEWISEDETAILS contains
  `<RATEDETAILS.LIST>` blocks per duty head (CGST/SGST/IGST/CESS). The
  fetch tool only extracts the first IGST rate found. If your chart uses
  multiple GST rates per item over time, you'd want to keep the
  most-recent applicable-from entry — extend `_extract_gst_rate()` to
  sort by APPLICABLEFROM.

- **Push with `valueInputOption="RAW"`, not `"USER_ENTERED"`.**
  USER_ENTERED corrupts long numeric strings (HSN codes that start with
  zero, SKU codes with leading zeros, large opening_value numbers above
  ~1e8 lose precision on round-trip). RAW stores everything as text and
  round-trips byte-for-byte. See the project memory for the original
  incident on the ledger-master skill.

- **Timestamp columns must be excluded from the equality check.** If
  `updated_at` is part of the comparison, every push will mark every row
  as updated. The push tool excludes both `created_at` and `updated_at`
  indices when comparing.
