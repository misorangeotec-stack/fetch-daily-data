# Workflow: sync_tally_sales_to_sheet

## Objective

Sync sales vouchers from local Tally Prime into **two** Google Sheets in a single run:

1. **Sales sheet** — one row per voucher (inventory aggregated, gross total).
2. **Sales Outstanding Register** — one row per `BILLALLOCATIONS.LIST` entry (bill ref, bill type, bill amount, credit period, computed due date). Joins back to the Sales sheet on `(Company, Location, Voucher No.)`.

Idempotent: re-running for an overlapping date range adds new rows only (no duplicates).

## Inputs

- **From date** (DD-MM-YYYY) — start of date range, user-supplied at runtime.
- **To date** (DD-MM-YYYY) — end of date range, user-supplied at runtime. Defaults to today if not given.
- **Companies** (optional, comma-separated) — restrict to a subset of currently loaded companies. Default: all loaded companies.
- **Project `.env`** (in the cwd) must define:
  - `TALLY_HOST` — e.g. `http://localhost:9000`
  - `GOOGLE_CREDENTIALS_FILE` — path to OAuth client secrets JSON
  - `GOOGLE_TOKEN_FILE` — path to cached OAuth token JSON
  - `SALES_SHEET_URL` — full URL of the per-voucher Sales sheet
  - `SALES_SHEET_TAB` — tab name within that sheet (e.g. `Sales`)
  - `SALES_DETAILS_SHEET_URL` — full URL of the bill-wise Sales Outstanding Register sheet
  - `SALES_DETAILS_SHEET_TAB` — tab name within that sheet (default `Sales Outstanding Register`)

## Chunking rule

**Always fetch one calendar month at a time, even if the user requests a multi-month range.** Tally's HTTP endpoint has a 180s read timeout in the fetch tool, and ranges longer than ~1 month (especially across multiple loaded companies) routinely exceed it; the failure also leaves Tally unresponsive for ~30s+, breaking subsequent calls.

For a multi-month request like `01-04-2025 → 26-04-2026`, run the fetch+push pair sequentially per month: `01-04 → 30-04`, `01-05 → 31-05`, …, `01-04-2026 → 26-04-2026` (clamp the final chunk's `to` to the user's actual end date). The push step is idempotent on `(Company, Location, Voucher No.)`, so per-month chunks never produce duplicates.

## Tools (in order)

All paths below are relative to the skill folder (`<SKILL_DIR>` = `~/.claude/skills/tally-sales-sync-outstanding/`).

1. **`tools/list_tally_companies.py`** — POSTs a Tally XML envelope to `$TALLY_HOST` requesting `List of Companies`. Prints a JSON array of company names to stdout.
   - CLI: no args.
   - Use the output to confirm with the user which companies will be included.

2. **`tools/fetch_tally_sales.py --from <DD-MM-YYYY> --to <DD-MM-YYYY> [--companies "Co1,Co2"] --output <path>`**
   - Loops over each loaded (or filtered) company, sets `SVCURRENTCOMPANY`, and pulls all `Sales` vouchers between `SVFROMDATE` and `SVTODATE`.
   - Reads two column schemas: `reference/columns.md` (per-voucher) and `reference/columns_details.md` (bill-wise) via `tools/_schema.py`.
   - For each voucher, builds (a) one aggregated voucher row, (b) one row per `BILLALLOCATIONS.LIST` entry under any ledger leg of that voucher (with `Due Date` = voucher date + parsed `BILLCREDITPERIOD` for `New Ref` rows). Vouchers with no real allocation emit a single synthetic `On Account` detail row carrying the gross total.
   - Writes a JSON dict to `<path>` (typically `.tmp/sales_<timestamp>.json`): `{"vouchers": [...], "details": [...]}`.
   - Prints a one-line summary like `{"vouchers": 312, "rows": 487, "detail_rows": 1840, "companies": ["A","B"]}`.

3. **`tools/push_sales_to_sheet.py --input <path>`**
   - Loads `SALES_SHEET_URL`/`SALES_SHEET_TAB`, `SALES_DETAILS_SHEET_URL`/`SALES_DETAILS_SHEET_TAB`, and Google creds from `.env`.
   - Validates each sheet's row 1 against its schema; writes headers if blank, errors out on mismatch.
   - **Push 1 — Sales sheet:** reads existing `(Company, Location, Voucher No.)` keys; appends only voucher rows whose key is new.
   - **Push 2 — Sales Outstanding Register:** reads existing `(Company, Location, Voucher No., Bill Ref Name)` keys; appends only detail rows whose composite key is new. Skipped (with a warning) if `SALES_DETAILS_SHEET_URL` is unset or if the input JSON is in the legacy flat-list shape.
   - Prints summary JSON: `{"fetched": N, "appended": N, "skipped": N, "sheet_url": "...", "details": {"fetched": N, "appended": N, "skipped": N, ...}}`.

## Outputs

- Appended rows in the per-voucher Sales sheet (URL from `SALES_SHEET_URL`).
- Appended rows in the bill-wise Sales Outstanding Register (URL from `SALES_DETAILS_SHEET_URL`).
- A combined summary printed to the terminal.
- An intermediate `.tmp/sales_<timestamp>.json` file (disposable; safe to delete).

## Edge cases

- **Tally not running / port closed** → `list_tally_companies.py` exits non-zero with a connection error. Tell the user to start Tally Prime and confirm Help → Settings → Connectivity is set to "Both" on port 9000.
- **No companies loaded** → `list_tally_companies.py` returns `[]`. Tell the user to load at least one company in Tally and stop.
- **Empty date range** → `fetch_tally_sales.py` may return 0 vouchers. Push step will still run (writes 0 rows) and the summary will say `appended: 0`.
- **Google OAuth token expired or missing** → `push_sales_to_sheet.py` opens the browser for re-auth on first run (uses the credentials file). If that fails non-interactively, delete `token.json` and re-run from a terminal that can open a browser.
- **Sheet header row mismatch** → `push_sales_to_sheet.py` exits non-zero. Either fix the sheet's row 1 or update `reference/columns.md`.
- **Voucher with missing inventory entries** (cash sale w/o stock items) → emit one row with blank `quantity`, `rate`, `unit`, `value` and the voucher-level `gross_total`. Sales.xlsx already shows nulls in these columns, so this matches the reference format.
- **Stock item with compound unit (e.g. `BOX of 12 PCS`)** → `ACTUALQTY` may not split cleanly; fetch tool falls back to the stock item master's `BASEUNITS`.
- **Voucher numbers reused across companies** → dedupe key is the composite `(Company, Voucher No.)`, not just `Voucher No.`, so this is safe.
- **Re-run with overlapping date range** → expected and supported. Both pushes skip already-present rows independently (vouchers on `(Company, Location, Voucher No.)`, details on `(Company, Location, Voucher No., Bill Ref Name)`); summary will show `skipped > 0` per sheet.
- **Voucher with multiple bill splits (e.g. invoice paid in 14 instalments)** → the per-voucher Sales sheet still gets one aggregated row; the Sales Outstanding Register gets 14 rows, one per `BILLALLOCATIONS.LIST` entry. Each detail row carries the bill ref name, bill type, bill amount, credit period, and computed due date (= voucher date + credit period for `New Ref`; blank otherwise).
- **Voucher with no bill allocations** (e.g. cash sales without bill-wise marking, or `<BILLALLOCATIONS.LIST/>` empty placeholder) → the detail sheet receives one synthetic `On Account` row carrying the voucher's gross total. This guarantees `SUM(Bill Amount)` on the detail sheet equals `SUM(Gross Total)` on the per-voucher sheet, voucher-for-voucher.
- **`New Ref` allocation with blank `BILLCREDITPERIOD`** (typical for advance-applied portions of an instalment plan) → emit `Credit Period = "0 Days"` and `Due Date = voucher date`. Per business rule: blank means due immediately. Non-`New Ref` types still leave both columns blank (they settle existing bills, not create new ones).
- **`BILLCREDITPERIOD` in date form** (e.g. `"17-Jul-25"`) → Tally stores whatever the user typed; if they entered a due date instead of a day count, Tally exports the date string here. The fetch script parses both forms and normalizes: `Credit Period` always shows days, `Due Date` always shows DD/MM/YYYY.
- **`SALES_DETAILS_SHEET_URL` not set in `.env`** → push script warns and skips the detail push; per-voucher push still succeeds. Add the URL to `.env` and re-run with the same `.tmp/sales_*.json` to backfill the detail sheet.
- **Legacy `.tmp/sales_*.json` from older fetch script** (a flat list, not `{"vouchers": [...], "details": [...]}`) → push warns and skips the detail push. Re-fetch with the current fetch script to produce a current-shape JSON.

## Lessons learned

- **Don't use the `Sales Vouchers` report — it filters to ONE master voucher type per company and silently drops everything else.** Instead, list every voucher type whose PARENT class is in `SALES_PARENT_CLASSES` (`Sales` or `Sales Accounts`) for that company, then issue one `Voucher Register` query per type with explicit `<VOUCHERTYPENAME>`. See `list_sales_voucher_types()` and `fetch_company_vouchers()`. This is more chatty (N HTTP calls per company) but actually returns all sales vouchers — the original approach was missing entire categories of vouchers in companies that had >1 sales voucher type.
- **Different companies use different sales parent classes.** Most use `Sales` but some configure their voucher types under `Sales Accounts`. Both are legitimate sales transactions — accept both via `SALES_PARENT_CLASSES`. Do NOT match `Sales Order` (those are commitments, not booked sales). If a new parent class appears in the wild, add it to that set.
- **Use the older-style envelope** with `<TALLYREQUEST>Export Data</TALLYREQUEST>` and the `<EXPORTDATA><REQUESTDESC><REPORTNAME>...` wrapper. The newer `<TYPE>Data</TYPE><ID>...` form is unreliable for Voucher Register / Sales Vouchers.
- **Voucher type names may contain typos** (e.g. `GST SALE- SPARE PARTS` singular instead of `SALES`). Don't filter by substring on the name — the parent-class filter (`PARENT in SALES_PARENT_CLASSES`) is the source of truth.
- **Tally XML is dirty.** The response routinely contains (a) raw control bytes (`0x00–0x1F`), (b) stray Windows-1252 bytes that aren't valid UTF-8, and (c) numeric character references like `&#0;` that XML 1.0 forbids. The fetch tool's `_sanitize_tally_xml` handles all three before parsing — don't pass raw bytes to ElementTree.
- **Voucher-level gross total isn't a single XML tag.** No `<AMOUNT>` exists at the voucher level. Compute it as `abs(amount of the party's LEDGERENTRIES.LIST entry)` — the party's row is negative because the party owes the company. Helper: `_voucher_gross_total()` in fetch tool.
- **Output is voucher-aggregated, not item-wise.** One row per voucher; quantity and value are numeric sums; rate is the weighted average (`sum(value)/sum(quantity)`); unit is the shared unit if every line item uses the same one (e.g. all `KGS`), else blank. Original `references/Sales.xlsx` had one row per inventory line, but the user explicitly prefers voucher-level for the outstanding report. See `_aggregate_inventory()` in the fetch tool.
- **Dedupe key is the triple `(Company, Location, Voucher No.)`** — not just `(Company, Voucher No.)`. Two raw Tally companies can share a display name (e.g. Surat + Noida branches both map to "Orange O-Tech Enterprise"), so Location is needed to keep their voucher numbers from colliding. See `DEDUPE_KEYS` in the push tool.
- **Company / location come from `reference/companies.md`, not Tally.** Tally's company names embed the financial year (e.g. `(from 1-Apr-25)`) and are uglier than what the user wants in the sheet. The mapping file translates raw Tally name → display Company + Location. Unmapped companies emit a one-time stderr warning and fall back to the raw name with blank location (no crash). **Refresh the mapping every April** — the FY suffix changes after the rollover (`(from 1-Apr-25)` → `(from 1-Apr-26)`).
- **Cancelled vouchers are empty placeholders.** Tally returns them with `ACTION="Cancel"` or `ISCANCELLED=Yes` and no real fields. Filter them out (`_is_cancelled` in fetch tool) so they don't pollute the sheet with blank rows.
- **The 14th column is `Unit`** (added during initial build between `Rate` and `Value`). Fetch extracts it from `ACTUALQTY` (e.g. `"20.0000 KGS"` → quantity `"20.0000"`, unit `"KGS"`), with a fallback to the stock item's `BASEUNITS`.
- **Date column format is `DD/MM/YYYY`** (e.g. `01/04/2026`). Indian convention; Google Sheets recognizes it as a real date so sort/filter work. The internal `Month` column stays `YYYY-MM` because that sorts naturally as text. See `format_date()` in the fetch tool.
