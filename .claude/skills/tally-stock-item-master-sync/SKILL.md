---
name: tally-stock-item-master-sync
description: Sync the full stock-item master (every stock item across every loaded company — products, raw materials, finished goods) from local Tally Prime into the Tally Stock Item Master Google Sheet. Use whenever the user asks to fetch/pull/sync/export the Tally stock-item master, refresh the stock-item master sheet, build a unified product / SKU / inventory master, or run the daily/periodic stock-item-master sync. Handles multiple loaded companies, resolves conversion factors via the Unit master, parses HSN code and IGST rate from the nested GSTDETAILS subcollection, sign-flips opening_value to match Tally's UI display, manages created_at / updated_at timestamps automatically, and upserts by (Company, Location, item_id = Tally GUID).
---

# tally-stock-item-master-sync

Pulls the full stock-item master from local Tally Prime via XML over
HTTP, then **upserts** rows in a single Google Sheet that holds one row
per item per company / location. Every stock item across every loaded
company is included.

This skill is **self-contained**: tools and workflow live inside this
folder. It expects to be run from the `FETCH DAILY DATA` project so it
can pick up the project's `.env` (Tally host, Google credentials, sheet
URL).

## Steps

Follow the SOP in [`workflows/sync_tally_stock_item_master_to_sheet.md`](workflows/sync_tally_stock_item_master_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys:
   `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`,
   `STOCK_ITEM_MASTER_SHEET_URL`, `STOCK_ITEM_MASTER_SHEET_TAB`. If any
   are missing, prompt the user before running anything.

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load
   companies in Tally and stop.

3. **Fetch the stock-item master.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_stock_item_master.py" \
       --from 01-04-2025 --to 31-03-2026 \
       --output .tmp/stock_item_master_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script
   reads the column schema from this skill's
   [`reference/columns.md`](reference/columns.md) and the company mapping
   from [`reference/companies.md`](reference/companies.md), so you do not
   need to know any of that schema.

   **Date range.** `--from` and `--to` (DD-MM-YYYY) drive Tally's
   `SVFROMDATE` / `SVTODATE`. `OPENINGBALANCE` / `OPENINGVALUE` are
   computed at the FY-start of the FY containing `--from`. Defaults to
   FY 25-26 (1-Apr-2025 → 31-Mar-2026) if both args are omitted. Master
   fields (item name, GUID, unit, GST rate, etc.) are date-independent.

4. **Push to sheet (upsert).** Run:
   ```
   python "<SKILL_DIR>/tools/push_stock_item_master_to_sheet.py" --input .tmp/stock_item_master_<timestamp>.json
   ```
   The script:
   - Validates row 1 against `reference/columns.md` (writes the header if
     the tab is empty).
   - Reads existing rows keyed by `(Company, Location, item_id)`.
   - For each fetched row: if the key exists and only timestamps would
     differ, **leaves it alone** (counted as `unchanged`). If the key
     exists and other columns differ, **updates that row in place**,
     preserves `created_at`, and bumps `updated_at`. If the key is new,
     **appends** with both timestamps set to the run time.

5. **Report.** Read the JSON summary printed by the push script and tell
   the user: rows fetched, rows appended, rows updated, rows unchanged,
   and the sheet URL.

## Schema changes

The output schema (column names, order, Tally source mapping) lives in
[`reference/columns.md`](reference/columns.md). To add/remove/reorder a
column, edit that table — no Python changes needed. The shared loader
[`tools/_schema.py`](tools/_schema.py) parses it.

To onboard a new Tally company or refresh names after the April FY
rollover, edit [`reference/companies.md`](reference/companies.md).

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's
  not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity →
  Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from
  `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth
  browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1
  that doesn't match `reference/columns.md`. Either fix the sheet's
  header row to match, or update the schema file.
- **Every row reported as `updated` on a no-op re-run** — means the
  timestamp columns leaked into the equality check. Verify
  `TIMESTAMP_KEYS` in `push_stock_item_master_to_sheet.py` matches the
  `created_at` / `updated_at` keys in `reference/columns.md`.
- **`gst_rate` blank on items that have GST in Tally** — likely an
  intrastate-only item that uses CGST/SGST instead of IGST. The fetch
  tool only matches `GSTRATEDUTYHEAD = IGST`. Extend
  `_extract_gst_rate()` in
  [`tools/fetch_tally_stock_item_master.py`](tools/fetch_tally_stock_item_master.py)
  to compute `IGST = CGST * 2` if needed.
- **`conversion_factor` blank for compound units** — the unit master in
  Tally has `ISSIMPLEUNIT = Yes` even though the item lists an
  `alternate_unit`. Reconfigure the unit in Tally, or accept the blank.
- **`opening_qty` looks like `0`** — the item has a zero opening (no
  on-hand stock at the FY start). Expected for newly-created items.
- **Wrong company name in sheet** — the raw Tally name is missing from
  `reference/companies.md`. Add it; FY rollovers in April change the
  `(from 1-Apr-XX)` suffix.
