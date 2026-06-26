---
name: tally-sales-sync-outstanding
description: Sync sales vouchers from local Tally Prime into TWO Google Sheets — the per-voucher Sales sheet and the bill-wise Sales Outstanding Register (with credit period + due date). Use whenever the user asks to fetch/pull/sync/export Tally sales for the outstanding report, update the outstanding sales sheet, run the daily outstanding sync, or push Tally data to the outstanding sheet. Handles multiple loaded companies, user-supplied date range, and dedupes by Voucher No. on the main sheet and by (Voucher No., Bill Ref Name) on the detail sheet.
---

# tally-sales-sync-outstanding

Pulls sales vouchers from local Tally Prime via XML over HTTP, then appends them (deduped) into **two** Google Sheets:

1. **Sales sheet** — one row per voucher (inventory aggregated, gross total). Schema: [`reference/columns.md`](reference/columns.md). Dedupe key: `(Company, Location, Voucher No., Date)`.
2. **Sales Outstanding Register** — one row per `BILLALLOCATIONS.LIST` entry (bill ref, bill type, bill amount, credit period, computed due date). Schema: [`reference/columns_details.md`](reference/columns_details.md). Dedupe key: `(Company, Location, Voucher No., Bill Ref Name, Date)`. Joins back to the Sales sheet via `(Company, Location, Voucher No., Date)`.

Both sheets are written in a single push run — there is no separate "details sync" to invoke.

This skill is **self-contained**: tools and workflow live inside this folder. It expects to be run from the `FETCH DAILY DATA` project so it can pick up the project's `.env` (Tally host, Google credentials, sheet URLs).

## Steps

Follow the SOP in [`workflows/sync_tally_sales_to_sheet.md`](workflows/sync_tally_sales_to_sheet.md). At a high level:

1. **Verify env.** Confirm the project `.env` exists and has these keys: `TALLY_HOST`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `SALES_SHEET_URL`, `SALES_SHEET_TAB`, `SALES_DETAILS_SHEET_URL`, `SALES_DETAILS_SHEET_TAB`. If any are missing, prompt the user before running anything. (`SALES_DETAILS_SHEET_TAB` defaults to `Sales Outstanding Register` if unset; `SALES_DETAILS_SHEET_URL` is required for the bill-wise push and the script will warn-and-skip if it's blank.)

2. **List loaded companies.** Run:
   ```
   python "<SKILL_DIR>/tools/list_tally_companies.py"
   ```
   Show the user the returned companies. If none, tell the user to load companies in Tally and stop.

3. **Ask for date range.** Prompt the user for `from` and `to` dates in `DD-MM-YYYY` format. Default `to` to today if the user only gives `from`.

4. **Fetch sales.** Run:
   ```
   python "<SKILL_DIR>/tools/fetch_tally_sales.py" --from <DD-MM-YYYY> --to <DD-MM-YYYY> --output .tmp/sales_<timestamp>.json
   ```
   (Add `--companies "Co1,Co2"` if the user wants a subset.) The script reads the column schemas from this skill's [`reference/columns.md`](reference/columns.md) and [`reference/columns_details.md`](reference/columns_details.md), so you do not need to know the schemas. Output JSON shape: `{"vouchers": [...], "details": [...]}`.

5. **Push to both sheets.** Run:
   ```
   python "<SKILL_DIR>/tools/push_sales_to_sheet.py" --input .tmp/sales_<timestamp>.json
   ```
   The script:
   * Pushes voucher rows to `SALES_SHEET_URL` / `SALES_SHEET_TAB`, deduped on `(Company, Location, Voucher No.)`.
   * Pushes detail rows to `SALES_DETAILS_SHEET_URL` / `SALES_DETAILS_SHEET_TAB`, deduped on `(Company, Location, Voucher No., Bill Ref Name)`.
   * Validates each sheet's row 1 against its schema first; writes the header on a blank tab; errors on mismatch.

6. **Report.** Read the JSON summary printed by the push script and tell the user: voucher rows fetched/appended/skipped, detail rows fetched/appended/skipped, and both sheet URLs.

## Schema changes

* Per-voucher sheet schema: [`reference/columns.md`](reference/columns.md).
* Bill-wise detail schema: [`reference/columns_details.md`](reference/columns_details.md).

To add/remove/reorder a column on either sheet, edit the corresponding markdown table — no Python changes needed. The shared loader [`tools/_schema.py`](tools/_schema.py) parses both.

## Troubleshooting

- **`Connection refused` to Tally** — Tally Prime isn't running, or it's not exposing port 9000. In Tally: `F1 (Help) → Settings → Connectivity → Client/Server configuration → TallyPrime acting as: Both`.
- **Google auth error** — delete `token.json` (path from `GOOGLE_TOKEN_FILE`) and re-run; the push script will re-open the OAuth browser flow.
- **`Sheet header mismatch`** — the destination tab has a non-empty row 1 that doesn't match the schema. The error message tells you which sheet (`SALES_SHEET_URL` vs `SALES_DETAILS_SHEET_URL`). Either fix the sheet's row 1 to match, or update the corresponding `reference/columns*.md` file.
- **`SALES_DETAILS_SHEET_URL is not set` warning** — add the URL to `.env`. Until you do, the per-voucher push still runs but the detail sheet is skipped.
- **Legacy `.tmp/sales_*.json` warning** — input file is from an older fetch (flat list shape). Re-run `fetch_tally_sales.py` to produce a current-shape JSON with both `vouchers` and `details` keys, then re-push.
- **Duplicate rows after a run** — confirm the dedupe keys are still `(Company, Location, Voucher No.)` (vouchers) and `(Company, Location, Voucher No., Bill Ref Name)` (details) in `push_sales_to_sheet.py`. If `reference/columns*.md` keys were renamed, the script needs the new key names.
